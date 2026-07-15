"""
Normalize: apply a per-corpus mapping config to the raw spine, producing typed,
provenance-carrying normalized tables. GENERIC ENGINE — contains zero knowledge
of any specific corpus; all field semantics live in the mapping JSON, which the
interpreting agent authors per corpus (corpus-clean step 3).

Mapping config shape:
{
  "tables": {
    "<table_name>": {
      "sources": [
        { "group": "<source_group>",            // raw_records.source_group
          "paths": { "<col>": "a[].b.c", ... }, // JSON paths (arrays: first elem)
          "const": { "<col>": "house" },        // optional literal columns
          "explode": "activities[]"             // optional: one row per element;
                                                //  paths then resolve INSIDE the
                                                //  element unless prefixed "^."
        }, ...
      ],
      "types": { "<col>": "text|int|money|date|datetime" }   // default text
    }, ...
  }
}

Every output row automatically carries provenance: source_group, native_id,
content_hash (+ elem_index when exploded). Tables are dropped and rebuilt each
run (deterministic rebuild beats partial-state mystery).

Usage:
  python normalize.py --db spine.db --mapping mapping.json [--only table]
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, date


# ---------- type coercions (generic) ----------
def to_money(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


_DATE_PATTERNS = [
    ("%Y-%m-%d", 10), ("%m/%d/%Y", None), ("%d/%m/%Y", None), ("%Y/%m/%d", None),
]


def to_date(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    iso = s[:10]
    try:
        return date.fromisoformat(iso).isoformat()
    except ValueError:
        pass
    head = s.split()[0] if s else s
    for fmt, cut in _DATE_PATTERNS:
        try:
            return datetime.strptime(head if cut is None else s[:cut], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_datetime(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        d = to_date(s)
        return d


def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


def to_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


COERCE = {"money": to_money, "date": to_date, "datetime": to_datetime,
          "int": to_int, "text": to_text}


# ---------- path resolution on parsed JSON (python-side; flexible for [] ) ----------
def resolve(obj, path):
    """Resolve 'a[].b.c' against a parsed JSON object. '[]' takes the FIRST
    element (for id-ish scalars); use explode for one-row-per-element."""
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        take_first = seg.endswith("[]")
        key = seg[:-2] if take_first else seg
        if key:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        if take_first:
            if isinstance(cur, list):
                cur = cur[0] if cur else None
    if isinstance(cur, (dict, list)):
        return None
    return cur


def resolve_list(obj, path):
    """Resolve an explode path to a LIST of elements. 'activities[]' or
    'a.b[].c[]' (last segment must be a list). A dict where a list is expected
    is treated as a single-element list (XML->dict collapses singletons)."""
    cur = obj
    segs = path.split(".")
    for i, seg in enumerate(segs):
        if cur is None:
            return []
        is_list = seg.endswith("[]")
        key = seg[:-2] if is_list else seg
        if key:
            if not isinstance(cur, dict):
                return []
            cur = cur.get(key)
        if is_list:
            if isinstance(cur, dict):
                cur = [cur]
            if not isinstance(cur, list):
                return []
            if i == len(segs) - 1:
                return cur
            # mid-path list: flatten across elements for the remainder
            rest = ".".join(segs[i + 1:])
            out = []
            for el in cur:
                out.extend(resolve_list(el, rest) if "[]" in rest
                           else ([resolve(el, rest)] if resolve(el, rest) is not None else []))
            return out
    return cur if isinstance(cur, list) else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    with open(args.mapping) as f:
        mapping = json.load(f)

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()

    for tname, tdef in mapping["tables"].items():
        if args.only and args.only != tname:
            continue
        types = tdef.get("types", {})
        # union of columns across sources
        cols = []
        for src in tdef["sources"]:
            for c in list(src.get("paths", {})) + list(src.get("const", {})):
                if c not in cols:
                    cols.append(c)
        prov = ["source_group", "native_id", "content_hash", "elem_index"]
        all_cols = cols + prov

        con.execute(f'DROP TABLE IF EXISTS "norm_{tname}"')
        col_defs = ", ".join(f'"{c}"' for c in all_cols)
        con.execute(f'CREATE TABLE "norm_{tname}" ({col_defs})')

        n = 0
        for src in tdef["sources"]:
            group = src["group"]
            paths = src.get("paths", {})
            consts = src.get("const", {})
            explode = src.get("explode")
            batch = []
            cur = con.execute(
                "SELECT native_id, content_hash, raw_json FROM raw_records WHERE source_group=?",
                (group,),
            )
            for native_id, chash, rj in cur:
                rec = json.loads(rj)
                elems = resolve_list(rec, explode) if explode else [None]
                for idx, elem in enumerate(elems):
                    row = []
                    for c in cols:
                        if c in consts:
                            row.append(consts[c])
                            continue
                        p = paths.get(c)
                        if p is None:
                            row.append(None)
                            continue
                        if explode and not p.startswith("^."):
                            v = resolve(elem, p) if elem is not None else None
                        else:
                            v = resolve(rec, p[2:] if p.startswith("^.") else p)
                        row.append(COERCE.get(types.get(c, "text"), to_text)(v))
                    row += [group, native_id, chash, idx if explode else None]
                    batch.append(row)
                    n += 1
                if len(batch) >= 5000:
                    ph = ",".join("?" * len(all_cols))
                    con.executemany(f'INSERT INTO "norm_{tname}" VALUES ({ph})', batch)
                    batch.clear()
            if batch:
                ph = ",".join("?" * len(all_cols))
                con.executemany(f'INSERT INTO "norm_{tname}" VALUES ({ph})', batch)
        con.commit()
        print(f"norm_{tname}: {n} rows ({time.time()-t0:.0f}s)")

    # pack-authored derived columns / indexes (kept in config so engine stays generic).
    # Failures are reported but non-fatal: re-runs hit duplicate-column ALTERs, and
    # a failed index shouldn't lose an hour of normalization work.
    if not args.only:
        for stmt in mapping.get("post_sql", []):
            try:
                con.execute(stmt)
                con.commit()
                print(f"post_sql ok: {stmt[:70]}...")
            except sqlite3.OperationalError as e:
                print(f"post_sql SKIP ({e}): {stmt[:70]}...")
    print("done")


if __name__ == "__main__":
    main()

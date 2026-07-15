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
  },
  "post_sql": [ "UPDATE ...", "ALTER TABLE ...", "CREATE INDEX ..." ]  // optional
}

post_sql failures are FATAL (exit 1) and print the failing statement + error —
except two recovered cases: duplicate-column ALTER ADD COLUMN (benign re-run)
and 'no such column' where the column is an UPDATE SET target (the column is
auto-created and the UPDATE retried).

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


# ---------- post_sql execution (loud failures; see run_post_sql) ----------
_UPDATE_STMT = re.compile(r'^\s*UPDATE\s+"?([A-Za-z0-9_]+)"?\s+SET\b',
                          re.IGNORECASE | re.DOTALL)
_ALTER_ADD_STMT = re.compile(r'^\s*ALTER\s+TABLE\b.*\bADD\s+COLUMN\b',
                             re.IGNORECASE | re.DOTALL)
_NO_SUCH_COLUMN = re.compile(r"no such column:?\s+([A-Za-z0-9_.\"]+)")


def _is_set_target(stmt, col):
    """True if `col` appears as an assignment TARGET in the statement's SET
    clause (i.e. `SET col = ...` or `, col = ...`), not merely referenced in
    an expression or WHERE clause. Only assignment targets are safe to
    auto-create — auto-adding a referenced column would silently NULL it."""
    m = re.search(r"\bSET\b(.*)", stmt, re.IGNORECASE | re.DOTALL)
    if not m:
        return False
    return re.search(r'(?:^|,)\s*"?%s"?\s*=' % re.escape(col),
                     m.group(1), re.IGNORECASE) is not None


def run_post_sql(con, stmts):
    """Execute pack-authored post_sql statements. LOUD on failure: a silently
    skipped UPDATE leaves normalized columns blank and corrupts everything
    downstream, so any unrecovered failure prints the full statement + error
    and the caller exits non-zero. Two well-understood cases are recovered:
      - 'duplicate column name' on ALTER ... ADD COLUMN: benign re-run, skipped.
      - 'no such column: X' on UPDATE ... SET X=...: the target column is
        created via ALTER TABLE ADD COLUMN and the UPDATE retried, so a pack
        that orders an UPDATE before its ALTER still populates the column.
    Returns the list of failed statements (already reported)."""
    failed = []
    for stmt in stmts:
        for attempt in range(4):  # a few auto-ALTER recoveries per statement
            try:
                con.execute(stmt)
                con.commit()
                print(f"post_sql ok: {stmt[:70]}...")
                break
            except sqlite3.OperationalError as e:
                msg = str(e)
                if "duplicate column name" in msg and _ALTER_ADD_STMT.match(stmt):
                    print(f"post_sql skip (column already exists; re-run): {stmt[:70]}...")
                    break
                um = _UPDATE_STMT.match(stmt)
                cm = _NO_SUCH_COLUMN.search(msg)
                if um and cm and attempt < 3:
                    col = cm.group(1).strip('"').split(".")[-1]
                    if _is_set_target(stmt, col):
                        tbl = um.group(1)
                        print(f'post_sql: column "{col}" missing on "{tbl}" but is an '
                              f'UPDATE SET target — auto-adding it and retrying')
                        try:
                            con.execute(f'ALTER TABLE "{tbl}" ADD COLUMN "{col}"')
                            con.commit()
                            continue
                        except sqlite3.OperationalError as e2:
                            msg = f"{msg}; auto-ALTER also failed: {e2}"
                print("post_sql FAILED:")
                print(f"  statement: {stmt}")
                print(f"  error:     {msg}")
                failed.append(stmt)
                break
    if failed:
        print(f"{len(failed)} post_sql statement(s) FAILED — normalized tables are "
              f"incomplete; fix the mapping pack and re-run")
    return failed


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
    # Failures are LOUD (see run_post_sql): normalization work is already committed
    # table-by-table above, so exiting non-zero here loses nothing and forces the
    # broken statement to be fixed instead of leaving derived columns blank.
    if not args.only:
        if run_post_sql(con, mapping.get("post_sql", [])):
            raise SystemExit(1)
    print("done")


if __name__ == "__main__":
    main()

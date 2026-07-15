"""
Profile: generic discovery-as-SQL over an ingested spine (raw_records table).

Produces a compact machine+human readable profile of the corpus WITHOUT any
corpus-specific knowledge, for the LLM to interpret (corpus-clean step 3):

  1. Per source group: record counts, field paths (dotted, arrays collapsed),
     types, fill rates, cardinality, examples.        [deterministic sample]
  2. Candidate identifier fields (name-hint OR uniqueness based).
  3. Cross-group join candidates: EXACT containment computed over the FULL
     corpus via json_extract -- never sampled (sampling provably produced
     false joins on real data).
  4. Composite-key detection: id-ish values shaped like "A<delim>B" whose
     halves are tested against other groups' id fields, with reliability %.
  5. Name-ish field detection for join fallbacks, tested empirically.

Outputs: profile.json (drives normalization mapping) + PROFILE_REPORT.md.

Deterministic, stdlib only. Heavy lifting in SQLite; Python orchestrates.

Usage:
  python profile.py --db spine.db --out <dir> [--stats-sample 20000]
"""
import argparse
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

# Generic, overridable heuristics (no corpus-specific terms)
ID_NAME_HINT = re.compile(r"(^|[._])(id|uuid|guid|key)s?($|[._])|_id$|id$", re.IGNORECASE)
NAME_NAME_HINT = re.compile(r"name|title|label|organi[sz]ation", re.IGNORECASE)
JUNK_PATH_HINT = re.compile(
    r"middle_name|first_name|last_name|prefix|suffix|phone|address|zip|"
    r"posted_by|contact|printed|signed|_display|country|state",
    re.IGNORECASE,
)
COMPOSITE_DELIMS = ["-", ":", "/", "|"]
MIN_DISTINCT_FOR_JOIN = 25
CONTAINMENT_THRESHOLD = 0.5


def norm_path(fullkey: str) -> str:
    """'$.a[3]."b_c"[0].d' -> 'a[].b_c[].d'  (SQLite quotes keys w/ underscores)"""
    p = re.sub(r"\[\d+\]", "[]", fullkey)
    p = p.replace('"', "")
    return p[2:] if p.startswith("$.") else p


def path_to_jsonpath(path: str) -> str:
    """'a[].b_c' -> '$."a"[0]."b_c"' — quote every segment, first-element arrays."""
    parts = []
    for seg in path.split("."):
        arr = seg.endswith("[]")
        key = seg[:-2] if arr else seg
        parts.append(f'"{key}"' + ("[0]" if arr else ""))
    return "$." + ".".join(parts)


def sample_rowids(con, group, cap):
    """Deterministic spread sample: every Nth rowid within the group."""
    (n,) = con.execute(
        "SELECT COUNT(*) FROM raw_records WHERE source_group=?", (group,)
    ).fetchone()
    if n <= cap:
        step = 1
    else:
        step = n // cap
    rows = con.execute(
        "SELECT rowid FROM raw_records WHERE source_group=? ORDER BY rowid", (group,)
    ).fetchall()
    return [r[0] for r in rows[::step][:cap]], n


def profile_fields(con, group, cap):
    """Field stats via json_tree over a deterministic sample."""
    rowids, total = sample_rowids(con, group, cap)
    stats = defaultdict(lambda: {"types": Counter(), "nonempty": 0, "values": set(), "examples": []})
    CHUNK = 500
    sampled = 0
    for i in range(0, len(rowids), CHUNK):
        chunk = rowids[i : i + CHUNK]
        q = f"""
        SELECT t.fullkey, t.type, t.value
        FROM raw_records r, json_tree(r.raw_json) t
        WHERE r.rowid IN ({','.join('?'*len(chunk))}) AND t.type NOT IN ('object','array')
        """
        for fullkey, typ, value in con.execute(q, chunk):
            path = norm_path(fullkey)
            s = stats[path]
            if value is None or value == "":
                s["types"]["empty"] += 1
                continue
            s["types"][typ] += 1
            s["nonempty"] += 1
            if len(s["values"]) < 20000:
                s["values"].add(str(value))
            if len(s["examples"]) < 4:
                v = str(value)[:80]
                if v not in s["examples"]:
                    s["examples"].append(v)
        sampled += len(chunk)
    return stats, sampled, total


def looks_like_id(path, s, sampled):
    if JUNK_PATH_HINT.search(path):
        return False
    nonempty = s["nonempty"]
    if nonempty < MIN_DISTINCT_FOR_JOIN:
        return False
    distinct = len(s["values"])
    uniq = distinct / nonempty if nonempty else 0
    name_hit = bool(ID_NAME_HINT.search(path))
    # id-ish: named like an id, or near-unique short scalars
    avg_len = sum(len(v) for v in list(s["values"])[:200]) / max(1, min(distinct, 200))
    return (name_hit and uniq > 0.001) or (uniq > 0.95 and avg_len <= 40)


def exact_values(con, group, path, cap=2_000_000):
    """FULL-corpus distinct values of one JSON path for a group (exact, not sampled)."""
    jp = path_to_jsonpath(path)  # only first array element for id paths
    q = """
    SELECT DISTINCT json_extract(raw_json, ?) FROM raw_records
    WHERE source_group=? AND json_extract(raw_json, ?) IS NOT NULL LIMIT ?
    """
    vals = set()
    for (v,) in con.execute(q, (jp, group, jp, cap)):
        if v is not None and v != "":
            vals.add(str(v).strip())
    return vals


def containment(a: set, b: set) -> float:
    if not a:
        return 0.0
    return len(a & b) / len(a)


def detect_composite(values):
    for d in COMPOSITE_DELIMS:
        two = sum(1 for v in list(values)[:5000] if len(v.split(d)) == 2 and all(v.split(d)))
        checked = min(len(values), 5000)
        if checked and two / checked > 0.6:
            left = {v.split(d)[0].strip() for v in values if len(v.split(d)) == 2}
            right = {v.split(d)[1].strip() for v in values if len(v.split(d)) == 2}
            return {"delimiter": d, "left": left, "right": right}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats-sample", type=int, default=20000)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)

    groups = [g for (g,) in con.execute(
        "SELECT DISTINCT source_group FROM raw_records ORDER BY source_group")]
    print(f"{len(groups)} groups")

    profile = {"groups": {}, "joins": []}
    id_fields = {}
    name_fields = {}

    t0 = time.time()
    for g in groups:
        stats, sampled, total = profile_fields(con, g, args.stats_sample)
        fields = {}
        ids, names = [], []
        for path, s in stats.items():
            fill = s["nonempty"] / sampled if sampled else 0
            fields[path] = {
                "types": dict(s["types"]),
                "fill_rate": round(fill, 3),
                "distinct_sampled": len(s["values"]),
                "examples": s["examples"],
            }
            if looks_like_id(path, s, sampled):
                ids.append(path)
            if NAME_NAME_HINT.search(path) and not JUNK_PATH_HINT.search(path) and fill > 0.3:
                names.append(path)
        profile["groups"][g] = {
            "records_total": total, "records_sampled": sampled,
            "n_fields": len(fields), "fields": fields,
            "id_candidates": ids, "name_candidates": names,
        }
        id_fields[g] = ids
        name_fields[g] = names
        print(f"  {g}: {total} recs, {len(fields)} fields, {len(ids)} id-cands ({time.time()-t0:.0f}s)")

    # ---- exact join detection over FULL corpus ----
    print("exact id-value harvest (full corpus)...")
    vals = {}  # (group,path) -> set
    for g in groups:
        for p in id_fields[g]:
            vals[(g, p)] = exact_values(con, g, p)

    print("containment tests...")
    for (ga, fa), va in vals.items():
        if len(va) < MIN_DISTINCT_FOR_JOIN:
            continue
        comp = detect_composite(va)
        for (gb, fb), vb in vals.items():
            if ga == gb or len(vb) < MIN_DISTINCT_FOR_JOIN:
                continue
            ov = containment(va, vb)
            if ov >= CONTAINMENT_THRESHOLD:
                profile["joins"].append({
                    "kind": "direct", "from": {"group": ga, "field": fa},
                    "to": {"group": gb, "field": fb},
                    "containment": round(ov, 4),
                    "from_distinct": len(va), "to_distinct": len(vb),
                })
            if comp:
                for side in ("left", "right"):
                    ovc = containment(comp[side], vb)
                    if ovc >= CONTAINMENT_THRESHOLD:
                        j = {
                            "kind": "composite", "from": {"group": ga, "field": fa,
                                "delimiter": comp["delimiter"], "component": side},
                            "to": {"group": gb, "field": fb},
                            "containment": round(ovc, 4),
                            "from_distinct": len(comp[side]), "to_distinct": len(vb),
                            "reliability": ("HIGH" if ovc >= 0.9 else
                                            "UNRELIABLE — use name-based fallback for the "
                                            "unmatched share; treat this component as a hint only"),
                        }
                        profile["joins"].append(j)

    with open(out / "profile.json", "w") as f:
        json.dump(profile, f, indent=2, default=str)

    write_report(out / "PROFILE_REPORT.md", profile)
    print(f"\nwrote {out/'profile.json'} + PROFILE_REPORT.md, {len(profile['joins'])} joins, {time.time()-t0:.0f}s total")


def write_report(path, profile):
    L = ["# Corpus Profile\n"]
    for g, gp in profile["groups"].items():
        L.append(f"## `{g}`  — {gp['records_total']} records ({gp['records_sampled']} sampled for stats)")
        L.append(f"- id candidates: {', '.join(gp['id_candidates']) or '(none)'}")
        L.append(f"- name candidates: {', '.join(gp['name_candidates']) or '(none)'}")
        top = sorted(gp["fields"].items(), key=lambda kv: -kv[1]["fill_rate"])[:15]
        L.append("- top fields (fill | distinct | example):")
        for p, st in top:
            ex = (st["examples"][0] if st["examples"] else "")[:60]
            L.append(f"    - `{p}` {st['fill_rate']:.0%} | {st['distinct_sampled']} | {ex}")
        L.append("")
    L.append("## Joins (EXACT, full corpus)\n")
    for j in sorted(profile["joins"], key=lambda x: -x["containment"]):
        if j["kind"] == "direct":
            L.append(f"- direct `{j['from']['group']}.{j['from']['field']}` → "
                     f"`{j['to']['group']}.{j['to']['field']}` : {j['containment']:.1%} "
                     f"({j['from_distinct']}→{j['to_distinct']} distinct)")
        else:
            L.append(f"- composite `{j['from']['group']}.{j['from']['field']}` "
                     f"[{j['from']['component']} of '{j['from']['delimiter']}'] → "
                     f"`{j['to']['group']}.{j['to']['field']}` : {j['containment']:.1%} — {j['reliability']}")
    Path(path).write_text("\n".join(L))


if __name__ == "__main__":
    main()

"""
Discovery pass for the corpus-cleanup skill.

Given an arbitrary corpus directory, WITHOUT any hardcoded schema knowledge:
  1. Cluster files into "source groups" by extension + directory-shape.
  2. Sample records from each group and infer a field schema (type, fill rate,
     cardinality, examples) for every dotted field path.
  3. Detect candidate identifier fields (by name heuristic + value uniqueness).
  4. Detect candidate cross-group JOINS by testing value overlap between id-ish
     fields -- including COMPOSITE / ENCODED keys of the form "A-B" whose halves
     reference two other groups' id fields (this is how the House->Senate
     "senateID = registrantID-clientID" bridge is found with no prior knowledge).
  5. Measure each join's reliability by sampling match rate, and when a composite
     component matches poorly, recommend a fallback join key (a shared name-ish
     field) -- which is how the ~43%-unreliable senateID client-id half surfaces.

Outputs:
  <out>/schema_map.json      machine-readable, drives unify.py
  <out>/DISCOVERY_REPORT.md  human-readable summary for the journalist/reviewer

Deterministic, stdlib only, no LLM. Sampling makes it fast even on 400k+ files.

Usage:
  python discover.py --corpus <dir> --out <dir> [--sample-files 8] [--sample-records 400]
"""
import argparse
import json
import random
from collections import defaultdict, Counter
from pathlib import Path

import common

RANDOM_SEED = 12345  # deterministic sampling

ID_NAME_HINT = ("id", "uuid", "guid", "_key", "bioguide", "fec", "cik")
NAME_NAME_HINT = ("name", "client", "registrant", "organization", "org", "title",
                  "payee", "honoree", "contributor", "member", "lobbyist")


def type_of(v):
    if v is None or v == "":
        return "empty"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return "int_str"
        return "str"
    return type(v).__name__


def looks_like_id(field_name, stats):
    lname = field_name.lower()
    name_hit = any(h in lname for h in ID_NAME_HINT)
    # value-based: high uniqueness among non-empty scalar values
    nonempty = stats["nonempty"]
    uniq_ratio = (stats["distinct"] / nonempty) if nonempty else 0
    scalarish = stats["scalar_frac"] > 0.8
    return (name_hit and scalarish) or (scalarish and uniq_ratio > 0.95 and nonempty >= 20)


def looks_like_name(field_name):
    lname = field_name.lower()
    return any(h in lname for h in NAME_NAME_HINT)


def infer_group_schema(group_name, files, sample_records):
    """Sample records across the group's files, infer per-field stats."""
    rng = random.Random(RANDOM_SEED + hash(group_name) % 1000)
    files = list(files)
    rng.shuffle(files)

    field_types = defaultdict(Counter)      # path -> type counter
    field_examples = defaultdict(list)      # path -> example values
    field_values = defaultdict(set)         # path -> distinct scalar values (capped)
    field_present = Counter()               # path -> #records where present & non-empty
    total = 0
    VALUE_CAP = 5000

    for path in files:
        if total >= sample_records:
            break
        for rec, _loc in common.record_iterator(path):
            if total >= sample_records:
                break
            total += 1
            flat = common.flatten(rec)
            for fpath, vals in flat.items():
                for v in vals:
                    if isinstance(v, (dict, list)):
                        continue
                    t = type_of(v)
                    field_types[fpath][t] += 1
                    if t != "empty":
                        field_present[fpath] += 1
                        if len(field_examples[fpath]) < 5 and v not in field_examples[fpath]:
                            field_examples[fpath].append(v)
                        if len(field_values[fpath]) < VALUE_CAP:
                            field_values[fpath].add(v if isinstance(v, (int, float)) else str(v))

    schema = {}
    for fpath, tcounter in field_types.items():
        occ = sum(tcounter.values())
        nonempty = field_present[fpath]
        scalar = occ - tcounter.get("empty", 0)  # occurrences that were scalar & non-empty already excluded empties? no
        scalar_frac = 1.0  # flatten only yields scalars here
        stats = {
            "types": dict(tcounter),
            "occurrences": occ,
            "nonempty": nonempty,
            "fill_rate": round(nonempty / total, 3) if total else 0,
            "distinct": len(field_values[fpath]),
            "scalar_frac": scalar_frac,
            "examples": field_examples[fpath][:5],
        }
        schema[fpath] = stats

    return {
        "group": group_name,
        "n_files": len(files),
        "sampled_records": total,
        "fields": schema,
    }, field_values  # return value sets for join detection


def detect_composite_pattern(values):
    """If most values look like 'A-B' (two tokens split by a single delimiter),
    return the delimiter and per-component value sets. Generic: tries '-', ':', '/', '|'."""
    for delim in ("-", ":", "/", "|"):
        two = 0
        left, right = set(), set()
        n = 0
        for v in values:
            s = str(v)
            parts = s.split(delim)
            n += 1
            if len(parts) == 2 and parts[0] and parts[1]:
                two += 1
                left.add(parts[0])
                right.add(parts[1])
        if n and two / n > 0.6:
            return {"delimiter": delim, "left": left, "right": right}
    return None


def overlap(a, b):
    """Containment of a in b: fraction of a's values present in b (sampled sets)."""
    if not a:
        return 0.0
    a = {str(x) for x in a}
    b = {str(x) for x in b}
    inter = len(a & b)
    return inter / len(a)


JUNK_FIELD_TOKENS = ("middle_name", "first_name", "last_name", "prefix", "suffix",
                     "posted_by", "contact_name", "printed", "signed")


def harvest_field_values(files, fields, max_records, cap=40000):
    """Dense second pass: stream many records collecting ONLY the given field paths'
    scalar values. Cheap (few fields) so we can sample far more records than schema
    inference did -- this is what makes join-containment estimates trustworthy."""
    out = {f: set() for f in fields}
    n = 0
    for path in files:
        if n >= max_records:
            break
        for rec, _loc in common.record_iterator(path):
            if n >= max_records:
                break
            n += 1
            flat = common.flatten(rec)
            for f in fields:
                for v in flat.get(f, []):
                    if isinstance(v, (dict, list)) or v is None or v == "":
                        continue
                    if len(out[f]) < cap:
                        out[f].add(str(v))
    return out


def sibling_name_field(target_group_schema, target_id_field, name_fields):
    """Given an id field like 'registrant.id', find the name-ish field sharing the
    longest path prefix ('registrant.name'). Generic sibling resolution."""
    prefix = target_id_field.rsplit(".", 1)[0] if "." in target_id_field else ""
    best = None
    for nf in name_fields:
        if prefix and nf.startswith(prefix + "."):
            return nf  # exact sibling under same parent object
        if not prefix and "." not in nf:
            best = best or nf
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-files", type=int, default=8)
    ap.add_argument("--sample-records", type=int, default=400)
    ap.add_argument("--max-files-scan", type=int, default=200000)
    args = ap.parse_args()

    corpus = Path(args.corpus).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # 1. cluster files into source groups.
    # Keep only up to KEEP_PER_GROUP sample paths per group (bounds memory and
    # prevents one huge group -- e.g. 400k House XML -- from starving the scan),
    # while still counting the true file total per group.
    KEEP_PER_GROUP = max(args.sample_files * 4, 40)
    groups = defaultdict(list)
    group_counts = Counter()
    exts = {".jsonl", ".json", ".xml", ".csv"}
    scanned = 0
    for p in corpus.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        scanned += 1
        gk = common.group_key(p, corpus)
        group_counts[gk] += 1
        if len(groups[gk]) < KEEP_PER_GROUP:
            groups[gk].append(p)

    print(f"scanned {scanned} candidate files -> {len(groups)} source groups")

    # 2. infer schema + collect id-field value sets per group
    group_schemas = {}
    group_field_values = {}  # group -> {field: set(values sampled)}
    for gkey, files in groups.items():
        sample_files = files[:args.sample_files]
        schema, field_values = infer_group_schema(gkey, sample_files, args.sample_records)
        schema["n_files"] = group_counts[gkey]  # true total, not retained sample
        group_schemas[gkey] = schema
        group_field_values[gkey] = field_values
        print(f"  {gkey}: {len(files)} files, {schema['sampled_records']} recs sampled, {len(schema['fields'])} fields")

    # 3. mark id-ish and name-ish fields
    id_fields = {}    # group -> [field]
    name_fields = {}  # group -> [field]
    for gkey, schema in group_schemas.items():
        ids, names = [], []
        for fpath, stats in schema["fields"].items():
            if looks_like_id(fpath, stats):
                ids.append(fpath)
            if looks_like_name(fpath):
                names.append(fpath)
        id_fields[gkey] = ids
        name_fields[gkey] = names

    # 4. dense id-value harvest (trustworthy overlap %). Also harvest name fields
    #    so we can empirically score name-based fallbacks.
    HARVEST_FILES = 60
    HARVEST_RECORDS = args.sample_records * 20
    id_vals = {}    # group -> {id_field: set}
    name_vals = {}  # group -> {name_field: set(norm_name)}
    for gkey, files in groups.items():
        want = id_fields[gkey] + name_fields[gkey]
        if not want:
            id_vals[gkey], name_vals[gkey] = {}, {}
            continue
        harvested = harvest_field_values(files[:HARVEST_FILES], want, HARVEST_RECORDS)
        id_vals[gkey] = {f: harvested.get(f, set()) for f in id_fields[gkey]}
        name_vals[gkey] = {f: {common.norm_name(v) for v in harvested.get(f, set())} - {""}
                           for f in name_fields[gkey]}

    def is_junk(field):
        lf = field.lower()
        return any(tok in lf for tok in JUNK_FIELD_TOKENS)

    def best_name_overlap(gsrc, gdst, dst_name_field):
        """Pick the source name field whose normalized values best contain dst's."""
        dstvals = name_vals.get(gdst, {}).get(dst_name_field, set())
        best_f, best_ov = None, 0.0
        for sf, svals in name_vals.get(gsrc, {}).items():
            ov = overlap(svals, dstvals) if svals else 0.0
            if ov > best_ov:
                best_f, best_ov = sf, ov
        return best_f, round(best_ov, 3)

    # detect joins (direct id<->id and composite-half<->id)
    joins = []
    gkeys = list(group_schemas.keys())
    for ga in gkeys:
        for fa in id_fields[ga]:
            if is_junk(fa):
                continue
            va = id_vals[ga].get(fa, set())
            if len(va) < 10:
                continue
            comp = detect_composite_pattern(va)
            for gb in gkeys:
                if gb == ga:
                    continue
                for fb in id_fields[gb]:
                    if is_junk(fb):
                        continue
                    vb = id_vals[gb].get(fb, set())
                    if len(vb) < 10:
                        continue
                    ov = overlap(va, vb)
                    if ov >= 0.30:
                        joins.append({
                            "kind": "direct",
                            "from": {"group": ga, "field": fa},
                            "to": {"group": gb, "field": fb},
                            "containment": round(ov, 3),
                            "from_distinct": len(va), "to_distinct": len(vb),
                        })
                    if comp:
                        for side in ("left", "right"):
                            ovc = overlap(comp[side], vb)
                            if ovc >= 0.30:
                                j = {
                                    "kind": "composite_component",
                                    "from": {"group": ga, "field": fa,
                                             "delimiter": comp["delimiter"], "component": side},
                                    "to": {"group": gb, "field": fb},
                                    "containment": round(ovc, 3),
                                    "from_distinct": len(comp[side]), "to_distinct": len(vb),
                                }
                                # reliability + fallback: use the target id's sibling name
                                dst_sib = sibling_name_field(group_schemas[gb], fb, name_fields.get(gb, []))
                                if dst_sib:
                                    src_f, src_ov = best_name_overlap(ga, gb, dst_sib)
                                    if src_f and src_ov >= 0.30:
                                        j["recommended_fallback"] = {
                                            "from_field": src_f, "to_field": dst_sib,
                                            "name_containment": src_ov,
                                        }
                                j["reliability_note"] = (
                                    f"{j['containment']*100:.0f}% id-containment; "
                                    + ("UNRELIABLE (<90%) -> prefer the name-based fallback below."
                                       if j["containment"] < 0.9 else "high; usable as-is.")
                                )
                                joins.append(j)

    # 5. write outputs
    schema_map = {
        "corpus": str(corpus),
        "n_files_scanned": scanned,
        "groups": group_schemas,
        "id_fields": id_fields,
        "name_fields": name_fields,
        "joins": joins,
    }
    with open(out / "schema_map.json", "w") as f:
        json.dump(schema_map, f, indent=2, default=str)

    write_report(out / "DISCOVERY_REPORT.md", schema_map)
    print(f"\nwrote {out/'schema_map.json'} and {out/'DISCOVERY_REPORT.md'}")
    print(f"detected {len(joins)} candidate join(s)")


def write_report(path, sm):
    lines = ["# Corpus Discovery Report\n",
             f"Corpus: `{sm['corpus']}`  \nFiles scanned: {sm['n_files_scanned']}  \n"
             f"Source groups: {len(sm['groups'])}\n",
             "\n## Source groups\n"]
    for gkey, g in sm["groups"].items():
        lines.append(f"### `{gkey}`")
        lines.append(f"- files: {g['n_files']}, records sampled: {g['sampled_records']}")
        lines.append(f"- id-ish fields: {', '.join(sm['id_fields'].get(gkey) or []) or '(none)'}")
        top = sorted(g["fields"].items(), key=lambda kv: -kv[1]["fill_rate"])[:12]
        lines.append("- top fields (fill rate | distinct | example):")
        for fp, st in top:
            ex = st["examples"][0] if st["examples"] else ""
            ex = str(ex)[:50].replace("\n", " ")
            lines.append(f"    - `{fp}`  {st['fill_rate']:.0%} | {st['distinct']} | {ex}")
        lines.append("")
    lines.append("\n## Candidate joins\n")
    if not sm["joins"]:
        lines.append("_none detected_")
    for j in sm["joins"]:
        if j["kind"] == "direct":
            lines.append(f"- **direct**: `{j['from']['group']}.{j['from']['field']}` -> "
                         f"`{j['to']['group']}.{j['to']['field']}`  ({j['containment']:.0%} containment)")
        else:
            lines.append(f"- **composite**: `{j['from']['group']}.{j['from']['field']}` "
                         f"(split on '{j['from']['delimiter']}', {j['from']['component']} half) -> "
                         f"`{j['to']['group']}.{j['to']['field']}`  ({j['containment']:.0%})")
            if j.get("reliability_note"):
                lines.append(f"    - {j['reliability_note']}")
            if j.get("recommended_fallback"):
                fb = j["recommended_fallback"]
                lines.append(f"    - **fallback join**: match on `{fb['from_field']}` ~ `{fb['to_field']}` "
                             f"(normalized) when the id component fails")
    Path(path).write_text("\n".join(lines))


if __name__ == "__main__":
    main()

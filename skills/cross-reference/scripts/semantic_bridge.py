#!/usr/bin/env python3
"""Anchored semantic say-vs-do bridge (engine — OPTIONAL layer).

Finds press-release ↔ lobbying couplings that share MEANING but no exact words
— BUT only between (member, client) pairs that already have a documented
relationship (an "anchor"): a prior press mention, an LD-203 contribution, a
shared home state. Unanchored quarter-wide topic matching is Washington's null
hypothesis (every member speaks on insulin while every pharma client lobbies
it) — the anchor is what turns a topical coincidence into a lead worth a
reporter's time.

Pipeline (all deterministic except the embedding floats):
  1. anchors  — union of config anchor SQLs → candidate (member, client) pairs
  2. filter   — drop match-everything boilerplate descriptions (keep those with
                a bill number or a distinctive token)
  3. null     — empirical similarity floor from random cross-pairs (percentile)
  4. score    — per anchored pair, same quarter: max cosine(press chunk, desc),
                minus the press chunk's similarity to the client's description
                centroid (kills "client lobbies on everything"); tiled matmul
  5. rank     — (margin-adjusted sim) × log(income) × anchor strength, per-client
                cap, top_n → leads table via detect.py emit()

Every lead carries both verbatim texts, the model + score, and the exact
`semantic_query.py` command to reproduce the match. A semantic lead is a
STARTING POINT: promotion requires an independent deterministic confirmation.

Needs numpy always; sentence-transformers only if it must (re)embed. Reads the
index built by embed_index.py and writes only the leads table.

Usage:
  python3 semantic_bridge.py --db spine.db --config packs/<corpus>/semantic.json \\
      --index semantic_index --run-id sembridge1
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required: pip install numpy")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detect import emit, records_show, LEADS_DDL  # noqa: E402

BILL_RE = re.compile(r"\b[HS]\.?\s?[RJ]?\.?\s?\d{1,5}\b")
CAPS_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.\-]{2,}\b")
CAPS_STOP = {"The", "This", "Act", "Bill", "Issues", "House", "Senate",
             "Congress", "Federal", "United", "States", "General", "All"}


def quarter_of(pub_date):
    if not pub_date or len(str(pub_date)) < 7:
        return None, None
    y = str(pub_date)[:4]
    mo = int(str(pub_date)[5:7])
    q = ["first_quarter", "second_quarter", "third_quarter",
         "fourth_quarter"][(mo - 1) // 3]
    return y, q  # year as str — spine stores filing_year as TEXT; compare like-for-like


def load_space(index_dir, space):
    sdir = os.path.join(index_dir, space)
    vecs = np.load(os.path.join(sdir, "vectors.npy"), mmap_mode="r")
    rows = [json.loads(l) for l in open(os.path.join(sdir, "rows.jsonl"))]
    meta = json.load(open(os.path.join(sdir, "meta.json")))
    return vecs, rows, meta


def passes_specificity(text, filt):
    if not filt:
        return True
    if BILL_RE.search(text):
        return True
    for m in CAPS_RE.findall(text):
        if m not in CAPS_STOP:
            return True
    # no distinctive token → boilerplate; drop
    return False


def empirical_floor(pv, dv, pct, n=20000):
    """99.5th percentile similarity of RANDOM press×desc pairs = the noise
    floor. Anything below is indistinguishable from topical coincidence."""
    rng_p = np.linspace(0, pv.shape[0] - 1, min(n, pv.shape[0]), dtype=int)
    rng_d = np.linspace(0, dv.shape[0] - 1, min(n, dv.shape[0]), dtype=int)
    a = np.asarray(pv[rng_p], dtype=np.float32)
    b = np.asarray(dv[rng_d], dtype=np.float32)
    m = min(len(a), len(b))
    sims = np.einsum("ij,ij->i", a[:m], b[:m])
    return float(np.percentile(sims, pct))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--index", default="semantic_index")
    ap.add_argument("--run-id", default="sembridge")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    b = cfg["bridge"]
    con = sqlite3.connect(args.db)
    con.executescript(LEADS_DDL)

    pv, prows, pmeta = load_space(args.index, b["press_space"])
    dv, drows, dmeta = load_space(args.index, b["desc_space"])

    # 1. anchors: (member_bioguide, normalized client_name) -> best strength
    anchors = {}
    for a in b["anchors"]:
        try:
            for mb, cn in con.execute(a["sql"]):
                if mb and cn:
                    k = (str(mb), str(cn).strip().upper())
                    anchors[k] = max(anchors.get(k, 0), a["strength"])
        except sqlite3.OperationalError as e:
            print(f"  anchor {a['name']} skipped: {e}")
    print(f"anchors: {len(anchors)} (member, client) pairs")

    # index press chunks by (member, year, quarter); descs by (client, year, quarter)
    press_by = {}
    for i, r in enumerate(prows):
        y, q = quarter_of(r.get("pub_date"))
        mb = r.get("member_bioguide")
        if mb and y:
            press_by.setdefault((str(mb), y, q), []).append(i)
    filt = b.get("specificity_filter")
    desc_by = {}
    for i, r in enumerate(drows):
        cn = (r.get("client_name") or "").strip().upper()
        y = str(r.get("filing_year")) if r.get("filing_year") is not None else None
        q = r.get("period_norm")
        if not cn or not passes_specificity(r.get("text", ""), filt):
            continue
        if cn in b.get("exclude_clients_upper", set()):
            continue
        desc_by.setdefault((cn, y, q), []).append(i)

    exclude = {c.strip().upper() for c in b.get("exclude_clients", [])}
    floor = max(empirical_floor(pv, dv, b.get("null_percentile", 99.5)),
                b.get("min_floor", 0.0))
    margin_min = b.get("min_margin_over_centroid", 0.08)
    print(f"empirical similarity floor (p{b.get('null_percentile',99.5)}): {floor:.3f}")

    # 4. score anchored pairs, same quarter
    cand = []
    for (mb, cn), strength in anchors.items():
        if cn in exclude:
            continue
        for (pmb, y, q), pidx in press_by.items():
            if pmb != mb:
                continue
            didx = desc_by.get((cn, y, q)) if b.get("same_quarter", True) else None
            if not didx:
                continue
            P = np.asarray(pv[sorted(pidx)], dtype=np.float32)
            D = np.asarray(dv[sorted(didx)], dtype=np.float32)
            centroid = D.mean(axis=0)
            centroid /= (np.linalg.norm(centroid) + 1e-9)
            # tiled: P is small per (member,quarter); D per (client,quarter) — safe
            sims = P @ D.T                      # (nP, nD)
            best_d = sims.max(axis=1)           # best desc per press chunk
            cen = P @ centroid                  # press chunk vs client centroid
            margin = best_d - cen
            bi = int(np.argmax(best_d - (cen * 0)))  # top press chunk by best_d
            pk = int(np.argmax(best_d))
            top_sim = float(best_d[pk])
            top_margin = float(margin[pk])
            if top_sim < floor or top_margin < margin_min:
                continue
            dk = int(np.argmax(sims[pk]))
            cand.append({
                "member": mb, "client": cn, "year": y, "quarter": q,
                "sim": top_sim, "margin": top_margin, "strength": strength,
                "p_row": prows[sorted(pidx)[pk]], "d_row": drows[sorted(didx)[dk]],
            })

    # 5. rank + per-client cap + top_n
    inc = {}
    for cn, income in con.execute(
        "SELECT UPPER(client_name), SUM(income) FROM v_senate_latest "
        "WHERE client_name IS NOT NULL GROUP BY UPPER(client_name)"):
        inc[cn] = income or 0
    for c in cand:
        c["rank"] = (c["sim"] + c["margin"]) * math.log10(
            max(10, inc.get(c["client"], 0) or 10)) * (0.5 + c["strength"])
    cand.sort(key=lambda c: -c["rank"])
    seen, chosen = {}, []
    cap = b.get("per_client_cap", 3)
    for c in cand:
        if seen.get(c["client"], 0) >= cap:
            continue
        seen[c["client"]] = seen.get(c["client"], 0) + 1
        chosen.append(c)
        if len(chosen) >= b.get("top_n", 60):
            break

    det = {"id": "semantic_say_do_anchored", "template": "semantic_overlap",
           "params": {"model": pmeta["model"], "floor": round(floor, 4),
                      "margin_min": margin_min},
           "innocent_explanations": b.get("innocent_explanations", []),
           "legal_flag": False}
    for c in chosen:
        p, d = c["p_row"], c["d_row"]
        ptxt = (p.get("text") or "")[:240].replace("\n", " ")
        dtxt = (d.get("text") or "")[:240]
        claim = records_show(
            f"a press release by {p.get('member_name','?')} and a lobbying "
            f"activity for {c['client']} in {c['quarter']} {c['year']} describe "
            f"a closely related matter (semantic similarity {c['sim']:.2f}) — the "
            f"parties are linked by a documented anchor (strength {c['strength']}). "
            f"This is a SEMANTIC lead requiring deterministic confirmation"
        )
        repro = (f"python3 skills/cross-reference/scripts/semantic_query.py "
                 f"--space {b['desc_space']} --query \"{ptxt[:120]}\" --top 5")
        ev = [
            {"locator": {"source_group": p.get("source_group"),
                         "native_id": p.get("native_id")},
             "role": "press", "text": ptxt},
            {"locator": {"source_group": d.get("source_group"),
                         "native_id": d.get("native_id")},
             "role": "lobbying_activity", "text": dtxt},
            {"model": pmeta["model"], "similarity": round(c["sim"], 4),
             "margin_over_centroid": round(c["margin"], 4),
             "anchor_strength": c["strength"], "repro": repro,
             "disclosure": "semantic lead — verify deterministically before promotion"},
        ]
        emit(con, args.run_id, det, "semantic_overlap", claim,
             float(c["rank"]), ev, defam="named_person")
    con.commit()
    print(f"emitted {len(chosen)} anchored semantic leads "
          f"(from {len(cand)} candidates)")


if __name__ == "__main__":
    main()

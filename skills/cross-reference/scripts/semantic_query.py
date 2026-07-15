#!/usr/bin/env python3
"""Semantic retrieval over an embed_index.py space (engine — OPTIONAL layer).

Find records by MEANING: "show me all lobbying on drug-price caps" returns
descriptions about Medicare Part D redesign even when they never say "insulin".
A journalist tool and a building block for semantic_bridge.py.

Needs sentence-transformers only to embed the QUERY (the index is plain numpy).
Exits with the install command if it is missing; every other detector runs
without it.

Usage:
  python3 semantic_query.py --index semantic_index --space activity_descs \\
      --query "insulin out-of-pocket cost cap" --top 10

Precedent-seeded search ("more like this KNOWN case"): put one exemplar text
per paragraph (blank-line separated) in a file — passages from a confirmed
case's records, or the journalist's description of the pattern — and:
  python3 semantic_query.py --space activity_descs --seeds known_case.txt --top 20
Results merge across seeds by max score, noting which seed matched. Caveats
travel with the results: similar language is NOT similar conduct; seed matches
are LEADS for the verification ladder, never findings.
"""
import argparse
import json
import os
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required: pip install numpy")


def load_space(index_dir, space):
    sdir = os.path.join(index_dir, space)
    vecs = np.load(os.path.join(sdir, "vectors.npy"), mmap_mode="r")
    rows = [json.loads(l) for l in open(os.path.join(sdir, "rows.jsonl"))]
    meta = json.load(open(os.path.join(sdir, "meta.json")))
    return vecs, rows, meta


def embed_queries(texts, model_name, revision, device):
    try:
        from sentence_transformers import SentenceTransformer
        import torch
    except ImportError:
        sys.exit(
            "Semantic query needs sentence-transformers (OPTIONAL layer):\n"
            "  pip install sentence-transformers"
        )
    dev = device or ("mps" if torch.backends.mps.is_available() else "cpu")
    m = SentenceTransformer(model_name, device=dev, revision=revision)
    return m.encode(texts, convert_to_numpy=True,
                    normalize_embeddings=True).astype(np.float32)


def topk(vecs, qv, k):
    # cosine = dot (both normalized). mmap + chunked dot keeps memory flat.
    scores = np.empty(vecs.shape[0], dtype=np.float32)
    step = 100_000
    for i in range(0, vecs.shape[0], step):
        blk = np.asarray(vecs[i:i + step], dtype=np.float32)
        scores[i:i + step] = blk @ qv
    idx = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
    idx = idx[np.argsort(-scores[idx])]
    return idx, scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="semantic_index")
    ap.add_argument("--space", required=True)
    ap.add_argument("--query", help="single query text")
    ap.add_argument("--seeds", help="file of exemplar texts (blank-line separated) "
                                    "from a KNOWN case — find records like them")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--device", default=None)
    ap.add_argument("--json", action="store_true", help="emit JSON not text")
    args = ap.parse_args()
    if not args.query and not args.seeds:
        ap.error("provide --query or --seeds")

    vecs, rows, meta = load_space(args.index, args.space)

    if args.seeds:
        raw = open(args.seeds).read()
        seeds = [s.strip() for s in raw.split("\n\n") if s.strip()]
        if not seeds:
            sys.exit(f"no seed texts found in {args.seeds}")
        labels = [f"seed {i+1}: {s[:60]}..." for i, s in enumerate(seeds)]
    else:
        seeds = [args.query]
        labels = [args.query]

    qvs = embed_queries(seeds, meta["model"], meta.get("model_revision"),
                        args.device)
    # merge across seeds by max score, remember which seed matched best
    best_score = None
    best_seed = None
    for si in range(qvs.shape[0]):
        _, scores = topk(vecs, qvs[si], args.top)
        if best_score is None:
            best_score = scores.copy()
            best_seed = np.zeros(len(scores), dtype=int)
        else:
            better = scores > best_score
            best_score[better] = scores[better]
            best_seed[better] = si
    idx = np.argpartition(-best_score, min(args.top, len(best_score) - 1))[:args.top]
    idx = idx[np.argsort(-best_score[idx])]

    out = []
    for rank, i in enumerate(idx, 1):
        r = dict(rows[i])
        r["_score"] = round(float(best_score[i]), 4)
        r["_rank"] = rank
        if args.seeds:
            r["_matched_seed"] = labels[int(best_seed[i])]
        out.append(r)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    head = f"seeds: {len(seeds)} exemplar(s) from {args.seeds}" if args.seeds \
        else f"query: {args.query!r}"
    print(f"{head}  space: {args.space}  "
          f"model: {meta['model']} ({meta['n_vectors']} vectors)")
    if args.seeds:
        print("NOTE: similar language is not similar conduct — these are leads "
              "for verification, never findings.")
    print()
    for r in out:
        txt = (r.get("text") or "").replace("\n", " ")
        label = r.get("client_name") or r.get("member_name") or ""
        print(f"[{r['_rank']:>2}] {r['_score']:.3f}  {label}")
        print(f"      {txt[:200]}")
        if r.get("_matched_seed"):
            print(f"      matched: {r['_matched_seed']}")
        print(f"      locator: {r.get('source_group')}::{r.get('native_id')}"
              f"  chunk {r.get('chunk_index')}\n")


if __name__ == "__main__":
    main()

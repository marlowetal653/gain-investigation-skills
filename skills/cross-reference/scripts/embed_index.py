#!/usr/bin/env python3
"""Semantic index builder (engine — corpus-agnostic). OPTIONAL layer.

Embeds a text column from the spine into on-disk float16 vectors so
`semantic_query.py` / `semantic_bridge.py` can find records by MEANING, not
just exact words. Deterministic except the embedding floats themselves (see
determinism note). Nothing here writes to spine.db.

Requires sentence-transformers (optional dep). If it is not installed the
script exits with the exact install command — every other detector in this
skill runs without it.

Config (JSON), e.g. packs/<corpus>/semantic.json:
{
  "model": "all-MiniLM-L6-v2",
  "model_revision": "<hf commit hash, optional but recommended>",
  "out_dir": "semantic_index",
  "chunk_words": 170, "chunk_overlap": 40,
  "spaces": [
    {"name": "activity_descs", "table": "norm_lobbying_activities",
     "text_col": "description", "id_cols": ["native_id", "source_group"],
     "where": "description IS NOT NULL AND LENGTH(description) > 20",
     "dedupe": true, "chunk": false},
    {"name": "press_chunks", "table": "norm_press_releases",
     "text_col": "text", "id_cols": ["native_id", "source_group"],
     "carry_cols": ["member_bioguide", "member_name", "pub_date"],
     "where": "text IS NOT NULL", "dedupe": false, "chunk": true}
  ]
}

Each space writes out_dir/<name>/: vectors.npy (float16, L2-normalized),
rows.jsonl (one row per vector: id_cols + carry_cols + chunk_index + text),
meta.json (model, revision, torch version, device, dims, counts, built_at).

`--limit N` (or per-space "limit") caps rows for a fast test slice — use this
to validate the pipeline without embedding the whole corpus.

Usage:
  python3 embed_index.py --db spine.db --config packs/<corpus>/semantic.json
  python3 embed_index.py --db spine.db --config ... --only activity_descs --limit 20000
"""
import argparse
import json
import os
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required: pip install numpy")

try:
    from sentence_transformers import SentenceTransformer
    import torch
except ImportError:
    sys.exit(
        "This OPTIONAL semantic layer needs sentence-transformers.\n"
        "  pip install sentence-transformers\n"
        "Every other detector in this skill runs without it — skip this step "
        "to stay stdlib-only."
    )

import sqlite3


def chunk_words(text, size, overlap):
    """Deterministic word-window chunker. MiniLM truncates ~256 tokens (~190
    words); default size 170 keeps whole chunks inside that window."""
    words = text.split()
    if len(words) <= size:
        return [text]
    step = max(1, size - overlap)
    out = []
    for i in range(0, len(words), step):
        out.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
    return out


def build_space(con, model, meta_base, out_dir, space, limit):
    name = space["name"]
    text_col = space["text_col"]
    id_cols = space["id_cols"]
    carry = space.get("carry_cols", [])
    do_chunk = space.get("chunk", False)
    dedupe = space.get("dedupe", False)
    cwords = space.get("chunk_words", meta_base["chunk_words"])
    coverlap = space.get("chunk_overlap", meta_base["chunk_overlap"])
    lim = space.get("limit", limit)

    sel = list(dict.fromkeys(id_cols + carry + [text_col]))
    q = f'SELECT {", ".join(chr(34)+c+chr(34) for c in sel)} FROM "{space["table"]}"'
    if space.get("where"):
        q += f' WHERE {space["where"]}'
    # deterministic order so rows.jsonl is stable across rebuilds
    q += f' ORDER BY {", ".join(chr(34)+c+chr(34) for c in id_cols)}'
    if lim:
        q += f" LIMIT {int(lim)}"

    rows, texts, seen = [], [], set()
    for r in con.execute(q):
        rec = dict(zip(sel, r))
        txt = (rec.get(text_col) or "").strip()
        if not txt:
            continue
        if dedupe:
            if txt in seen:
                continue
            seen.add(txt)
        pieces = chunk_words(txt, cwords, coverlap) if do_chunk else [txt]
        for ci, piece in enumerate(pieces):
            row = {c: rec.get(c) for c in id_cols + carry}
            row["chunk_index"] = ci
            row["text"] = piece
            rows.append(row)
            texts.append(piece)

    if not texts:
        print(f"  {name}: 0 rows (check where clause)")
        return
    print(f"  {name}: embedding {len(texts)} texts "
          f"({'chunked' if do_chunk else 'whole'}, "
          f"{'deduped' if dedupe else 'all'}) ...", flush=True)
    vecs = model.encode(
        texts, batch_size=256, convert_to_numpy=True,
        normalize_embeddings=True, show_progress_bar=True,
    ).astype(np.float16)

    sdir = os.path.join(out_dir, name)
    os.makedirs(sdir, exist_ok=True)
    np.save(os.path.join(sdir, "vectors.npy"), vecs)
    with open(os.path.join(sdir, "rows.jsonl"), "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    meta = dict(meta_base)
    meta.update({"space": name, "n_vectors": len(rows), "dims": int(vecs.shape[1]),
                 "chunked": do_chunk, "deduped": dedupe, "limit": lim})
    with open(os.path.join(sdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  {name}: wrote {len(rows)} vectors -> {sdir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", help="build just this space")
    ap.add_argument("--limit", type=int, help="cap rows per space (test slice)")
    ap.add_argument("--device", default=None, help="mps|cpu|cuda (default auto)")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    device = args.device or ("mps" if torch.backends.mps.is_available() else "cpu")
    model_name = cfg.get("model", "all-MiniLM-L6-v2")
    model = SentenceTransformer(model_name, device=device,
                                revision=cfg.get("model_revision"))
    # build_at intentionally omitted from arg-less time to keep runs comparable;
    # stamp via a passed value if you need it.
    meta_base = {
        "model": model_name,
        "model_revision": cfg.get("model_revision"),
        "torch": torch.__version__,
        "device": device,
        "chunk_words": cfg.get("chunk_words", 170),
        "chunk_overlap": cfg.get("chunk_overlap", 40),
        "note": ("float16, L2-normalized (cosine = dot). Scores reproducible to "
                 "~1e-3 across devices; a lead's authority is its matched texts + "
                 "locators, never the raw score."),
    }
    out_dir = cfg.get("out_dir", "semantic_index")
    con = sqlite3.connect(args.db)

    spaces = [s for s in cfg["spaces"]
              if not args.only or s["name"] == args.only]
    if not spaces:
        sys.exit(f"no space named {args.only!r} in config")
    print(f"model {model_name} on {device}; out_dir {out_dir}")
    for space in spaces:
        build_space(con, model, meta_base, out_dir, space, args.limit)


if __name__ == "__main__":
    main()

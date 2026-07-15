#!/usr/bin/env python3
"""Deterministic entity-mention scanner (engine — corpus-agnostic).

Builds an SQLite FTS5 index over a text column, then scans for exact
phrase matches of entity names supplied by a config query, writing one
row per (document, entity) pair to a mentions table. All extraction is
FTS phrase search — no LLM involvement, fully reproducible.

Config (JSON):
{
  "docs":     {"table": "norm_press_releases", "text_col": "text",
               "carry_cols": ["pub_date", "member_bioguide", "member_name",
                               "member_chamber", "title", "url"]},
  "entities": {"query": "SELECT DISTINCT client_name FROM ... LIMIT 1000",
               "min_len": 10, "stop_names": ["United States", ...]},
  "out_table": "press_client_mentions"
}

Guards: names shorter than min_len characters or in stop_names are skipped
(short/generic names create false mentions); each name is queried as an
FTS5 exact phrase. Idempotent: out_table is dropped and rebuilt.

Usage:
  python3 mention_scan.py --db spine.db --config packs/<pack>/mentions.json
"""
import argparse
import json
import re
import sqlite3
import sys
import time


def fts_name(table):
    return f"fts_{table}"


def ensure_fts(con, docs):
    t, col = docs["table"], docs["text_col"]
    fts = fts_name(t)
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (fts,)
    ).fetchone()
    if row:
        return fts
    print(f"building FTS5 index {fts} on {t}.{col} ...", flush=True)
    t0 = time.time()
    con.execute(
        f'CREATE VIRTUAL TABLE "{fts}" USING fts5("{col}", content="{t}", content_rowid="rowid")'
    )
    con.execute(f'INSERT INTO "{fts}"("{fts}") VALUES (\'rebuild\')')
    con.commit()
    print(f"  done ({int(time.time() - t0)}s)", flush=True)
    return fts


def clean_phrase(name):
    """Strip suffixes/punctuation that break FTS phrase matching; return
    a quoted phrase of alphanumeric tokens, or None if too weak."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9&.-]*", name)
    # drop trailing corporate suffixes — press text rarely includes them
    SUFFIX = {"inc", "inc.", "llc", "llp", "l.l.c.", "lp", "ltd", "ltd.",
              "corp", "corp.", "corporation", "co", "co.", "company", "plc",
              "n.a.", "na", "the"}
    while tokens and tokens[-1].lower().rstrip(".,") in SUFFIX:
        tokens.pop()
    while tokens and tokens[0].lower() == "the":
        tokens.pop(0)
    if not tokens:
        return None
    phrase = " ".join(tokens)
    return phrase


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    docs, ents, out = cfg["docs"], cfg["entities"], cfg["out_table"]
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")

    fts = ensure_fts(con, docs)

    min_len = ents.get("min_len", 10)
    stop = {s.lower() for s in ents.get("stop_names", [])}
    names = [r[0] for r in con.execute(ents["query"]) if r[0]]
    scan = []
    for name in names:
        phrase = clean_phrase(name)
        if not phrase or len(phrase) < min_len or phrase.lower() in stop:
            continue
        scan.append((name, phrase))
    print(f"{len(names)} entity names -> {len(scan)} scannable phrases "
          f"(min_len={min_len}, {len(names) - len(scan)} skipped)", flush=True)

    carry = docs.get("carry_cols", [])
    carry_sql = ", ".join(f'd."{c}"' for c in carry)
    cols = ["entity_name", "matched_phrase"] + carry + ["source_group", "native_id"]
    con.execute(f'DROP TABLE IF EXISTS "{out}"')
    con.execute(f'CREATE TABLE "{out}" ({", ".join(repr(c)[1:-1] for c in cols)})')

    t0, total = time.time(), 0
    ins = f'INSERT INTO "{out}" VALUES ({", ".join("?" * len(cols))})'
    for i, (name, phrase) in enumerate(scan):
        # FTS5 phrase query: double-quote, escape embedded quotes
        q = '"' + phrase.replace('"', '""') + '"'
        rows = con.execute(
            f'SELECT {carry_sql}, d.source_group, d.native_id '
            f'FROM "{fts}" f JOIN "{docs["table"]}" d ON d.rowid = f.rowid '
            f"WHERE \"{fts}\" MATCH ?", (q,)
        ).fetchall()
        if rows:
            con.executemany(ins, [(name, phrase) + r for r in rows])
            total += len(rows)
        if (i + 1) % 200 == 0:
            con.commit()
            print(f"  {i + 1}/{len(scan)} phrases, {total} mentions "
                  f"({int(time.time() - t0)}s)", flush=True)
    con.execute(
        f'CREATE INDEX IF NOT EXISTS "idx_{out}_ent" ON "{out}"(entity_name)')
    con.commit()
    print(f"{out}: {total} mentions from {len(scan)} phrases "
          f"({int(time.time() - t0)}s)", flush=True)


if __name__ == "__main__":
    sys.exit(main())

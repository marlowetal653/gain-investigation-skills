#!/usr/bin/env python3
"""Build a small, portable EVIDENCE BUNDLE from a full spine.

The bundle is a SQLite file containing exactly what a reviewer (an editor, a
judge, a fact-checker) needs to verify every lead WITHOUT the multi-GB corpus:

  leads         — the complete leads table (statuses, kill reasons, evidence)
  raw_records   — only the verbatim source records referenced by lead evidence
                  (content hashes intact, so records still prove unaltered)
  entities/aliases — the resolved-entity map (small, useful for name lookups)
  bundle_manifest  — what this is, when built from what spine, row counts

show_source.py works against the bundle directly:
  python3 skills/corpus-cleanup/scripts/show_source.py --db evidence_bundle.db --id <native_id>

Usage:
  python3 make_evidence_bundle.py --db spine.db --out evidence_bundle.db
"""
import argparse
import json
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="full spine")
    ap.add_argument("--out", required=True, help="bundle path (overwritten)")
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    import os
    if os.path.exists(args.out):
        os.remove(args.out)
    dst = sqlite3.connect(args.out)

    # locators referenced by any lead's evidence
    locs = set()
    n_leads = 0
    for (ev_json,) in src.execute("SELECT evidence FROM leads"):
        n_leads += 1
        try:
            for item in json.loads(ev_json or "[]"):
                loc = (item or {}).get("locator") or {}
                nid = loc.get("native_id")
                if nid:
                    locs.add((loc.get("source_group"), nid))
        except (ValueError, TypeError):
            continue
    print(f"{n_leads} leads reference {len(locs)} distinct source records")

    # copy schema+rows for the small tables verbatim
    for table in ("leads", "entities", "aliases"):
        row = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row:
            print(f"  {table}: not present in spine, skipped")
            continue
        dst.execute(row[0])
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        ph = ",".join("?" * len(cols))
        dst.executemany(
            f"INSERT INTO {table} VALUES ({ph})",
            src.execute(f"SELECT * FROM {table}"),
        )
        n = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n} rows")

    # raw_records: referenced subset only
    row = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_records'"
    ).fetchone()
    if not row:
        sys.exit("spine has no raw_records table")
    dst.execute(row[0])
    cols = [r[1] for r in src.execute("PRAGMA table_info(raw_records)")]
    ph = ",".join("?" * len(cols))
    n_copied = 0
    for sg, nid in sorted(locs, key=lambda x: (x[0] or "", x[1])):
        if sg:
            rows = src.execute(
                "SELECT * FROM raw_records WHERE source_group=? AND native_id=?",
                (sg, nid)).fetchall()
        else:
            rows = src.execute(
                "SELECT * FROM raw_records WHERE native_id=?", (nid,)).fetchall()
        for r in rows:
            dst.execute(f"INSERT OR IGNORE INTO raw_records VALUES ({ph})", r)
            n_copied += 1
    print(f"  raw_records: {n_copied} referenced records copied")

    dst.execute("CREATE TABLE bundle_manifest (key TEXT, value TEXT)")
    dst.executemany("INSERT INTO bundle_manifest VALUES (?,?)", [
        ("what", "evidence bundle: every lead + the verbatim source records its "
                 "evidence cites; content hashes prove records unaltered"),
        ("built_from", args.db),
        ("leads", str(n_leads)),
        ("source_records", str(n_copied)),
        ("verify_with", "skills/corpus-cleanup/scripts/show_source.py --db <this file>"),
    ])
    dst.execute("CREATE INDEX idx_raw_nid ON raw_records(native_id)")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    import os
    print(f"bundle: {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()

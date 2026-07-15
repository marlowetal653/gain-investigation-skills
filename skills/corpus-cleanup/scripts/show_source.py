"""
show_source: print the raw record behind any provenance locator.

Human verification must never mean "scroll a 450MB JSON file." Every row in the
spine (and every lead citing it) carries (source_group, native_id) — this tool
round-trips that locator to the verbatim record and, where the source is a
standalone file (e.g. one XML per filing), points at the original file too.

Usage:
  python show_source.py --db spine.db --group <source_group> --id <native_id>
  python show_source.py --db spine.db --id <native_id>            # search all groups
  python show_source.py --db spine.db --locator "<group>::<native_id>"
Exit code 1 if not found.
"""
import argparse
import json
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--group", default=None)
    ap.add_argument("--id", dest="native_id", default=None)
    ap.add_argument("--locator", default=None, help="'<source_group>::<native_id>'")
    ap.add_argument("--raw", action="store_true", help="print raw_json only (machine use)")
    args = ap.parse_args()

    group, native_id = args.group, args.native_id
    if args.locator:
        if "::" not in args.locator:
            sys.exit("locator must be '<source_group>::<native_id>'")
        group, native_id = args.locator.split("::", 1)
    if not native_id:
        sys.exit("need --id or --locator")

    con = sqlite3.connect(args.db)
    q = "SELECT source_group, source_file, native_id, content_hash, raw_json FROM raw_records WHERE native_id=?"
    params = [native_id]
    if group:
        q += " AND source_group=?"
        params.append(group)
    rows = con.execute(q, params).fetchall()

    if not rows:
        print(f"NOT FOUND: native_id={native_id!r}" + (f" in group={group!r}" if group else ""))
        sys.exit(1)

    (corpus_root,) = con.execute("SELECT value FROM meta WHERE key='corpus_root'").fetchone() or ("?",)

    for sg, sf, nid, ch, rj in rows:
        if args.raw:
            print(rj)
            continue
        print("=" * 70)
        print(f"source_group : {sg}")
        print(f"source_file  : {corpus_root}/{sf}")
        print(f"native_id    : {nid}")
        print(f"content_hash : {ch}")
        print("-" * 70)
        print(json.dumps(json.loads(rj), indent=2, ensure_ascii=False))
    if len(rows) > 1 and not args.raw:
        print(f"\nNOTE: {len(rows)} records share this native_id (content-hash disambiguates; "
              "usually an amended/duplicate filing). All shown.")


if __name__ == "__main__":
    main()

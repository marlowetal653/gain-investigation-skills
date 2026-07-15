"""
Ingest: verbatim load of an entire corpus into a SQLite spine.

Load-first, discover-second. Every record is written as
  (source_group, source_file, native_id, content_hash, raw_json)
where native_id is a STABLE identifier from the record itself (filing_uuid,
XML filename, press url) -- never an array index, which is not stable across
re-downloads of paginated dumps. content_hash disambiguates true duplicates
(e.g. the 26 duplicate filing_uuids in filings_2025.json) and gives idempotent
re-runs for free: re-ingesting is a no-op.

Deterministic, stdlib only, streaming-safe (large JSON arrays are decoded
incrementally, never json.load'd whole).

Usage:
  python ingest.py --corpus <dir> --db <spine.db> [--only group_substr]
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS raw_records (
    source_group TEXT NOT NULL,   -- structural cluster, e.g. 'senate_filings'
    source_file  TEXT NOT NULL,   -- path relative to corpus root
    native_id    TEXT NOT NULL,   -- stable id from the record itself
    content_hash TEXT NOT NULL,   -- sha256 of canonical JSON
    raw_json     TEXT NOT NULL,
    PRIMARY KEY (source_group, native_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_group_file ON raw_records(source_group, source_file);
"""


def canonical_hash(rec) -> str:
    s = json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()


def iter_json_array_stream(path: Path):
    """Incrementally decode a large JSON array file without loading it whole.
    Yields one element at a time. Assumes top-level is a JSON array."""
    dec = json.JSONDecoder()
    buf = ""
    CHUNK = 1 << 20  # 1MB
    with open(path, encoding="utf-8", errors="replace") as f:
        # find opening bracket
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return
            buf += chunk
            i = buf.find("[")
            if i >= 0:
                buf = buf[i + 1:]
                break
        while True:
            buf = buf.lstrip().lstrip(",").lstrip()
            if buf.startswith("]"):
                return
            try:
                obj, end = dec.raw_decode(buf)
                yield obj
                buf = buf[end:]
            except json.JSONDecodeError:
                chunk = f.read(CHUNK)
                if not chunk:
                    return  # truncated file; stop gracefully
                buf += chunk


def pick_native_id(rec: dict, fallback: str) -> str:
    """Choose the most stable identifier present in a record."""
    for key in ("filing_uuid", "url", "uuid", "id"):
        v = rec.get(key)
        if v not in (None, ""):
            return str(v)
    return fallback


class Ingestor:
    def __init__(self, db_path: Path, corpus: Path):
        self.corpus = corpus
        self.con = sqlite3.connect(db_path)
        self.con.executescript(DDL)
        self.con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        self.con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('corpus_root',?)",
            (str(corpus),),
        )
        self.batch = []
        self.counts = {}

    def add(self, group, source_file, native_id, rec):
        rj = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        h = canonical_hash(rec)
        self.batch.append((group, source_file, native_id, h, rj))
        self.counts[group] = self.counts.get(group, 0) + 1
        if len(self.batch) >= 2000:
            self.flush()

    def flush(self):
        if self.batch:
            self.con.executemany(
                "INSERT OR IGNORE INTO raw_records VALUES (?,?,?,?,?)", self.batch
            )
            self.con.commit()
            self.batch.clear()

    # ---------- source loaders ----------
    def ingest_jsonl_file(self, path: Path, group: str):
        rel = str(path.relative_to(self.corpus))
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                nid = pick_native_id(rec, f"{rel}:line={i}")
                self.add(group, rel, nid, rec)

    def ingest_json_array_file(self, path: Path, group: str):
        rel = str(path.relative_to(self.corpus))
        for i, rec in enumerate(iter_json_array_stream(path)):
            if not isinstance(rec, dict):
                rec = {"_value": rec}
            nid = pick_native_id(rec, f"{rel}:index={i}")
            self.add(group, rel, nid, rec)

    def ingest_xml_file(self, path: Path, group: str):
        rel = str(path.relative_to(self.corpus))
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            self.counts[group + "__parse_errors"] = (
                self.counts.get(group + "__parse_errors", 0) + 1
            )
            return
        rec = {"_root_tag": root.tag, root.tag: common.xml_to_dict(root)}
        # XML filename is the House Clerk filing ID -- the stable native id.
        self.add(group, rel, path.stem, rec)


def group_name_for(path: Path, corpus: Path) -> str:
    """Human-stable group name: collapse digits in the relative dir path.
    e.g. house/2025_1stQuarter_XML -> house/#_#stQuarter_XML"""
    return common.group_key(path, corpus)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--only", default=None, help="only ingest groups containing this substring")
    args = ap.parse_args()

    corpus = Path(args.corpus).resolve()
    t0 = time.time()
    ing = Ingestor(Path(args.db), corpus)

    exts = {".jsonl", ".json", ".xml", ".csv"}
    files = [p for p in sorted(corpus.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    print(f"{len(files)} candidate files")

    last_report = time.time()
    for n, p in enumerate(files):
        group = group_name_for(p, corpus)
        if args.only and args.only not in group:
            continue
        ext = p.suffix.lower()
        if ext == ".jsonl":
            ing.ingest_jsonl_file(p, group)
        elif ext == ".json":
            ing.ingest_json_array_file(p, group)
        elif ext == ".xml":
            ing.ingest_xml_file(p, group)
        if time.time() - last_report > 15:
            done = sum(v for k, v in ing.counts.items() if not k.endswith("__parse_errors"))
            print(f"  [{n+1}/{len(files)} files] {done} records, {time.time()-t0:.0f}s")
            last_report = time.time()

    ing.flush()
    print(f"\ndone in {time.time()-t0:.0f}s")
    for g in sorted(ing.counts):
        print(f"  {g}: {ing.counts[g]}")

    # reconcile against what's actually in the DB
    print("\nDB totals by group:")
    for grp, cnt in ing.con.execute(
        "SELECT source_group, COUNT(*) FROM raw_records GROUP BY source_group ORDER BY source_group"
    ):
        print(f"  {grp}: {cnt}")
    ing.con.close()


if __name__ == "__main__":
    main()

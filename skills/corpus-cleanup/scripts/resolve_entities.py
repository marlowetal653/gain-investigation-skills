"""
Entity resolution: conservative, auditable, generic.

Reads entity mentions from normalized tables (which columns are entity names is
CONFIG, not code), builds:
  entities                canonical entities (auto-merged exact-norm clusters)
  aliases                 every raw spelling -> entity, with source counts
  entity_merge_candidates fuzzy candidates for review (NEVER auto-merged)

Auto-merge policy (all guards must pass — false merges invent connections):
  - identical aggressive-normalized name (case/punct/legal-suffix stripped)
  - len(norm) >= min_norm_len (default 5)
  - same entity_type (as declared per-column in config)
  - name is not an intermediary composite (config regexes, e.g. "on behalf of")
    — those rows keep their full string AND get flagged for the chain parser.

Candidate generation (review table only): same light-norm (case/punct only)
but different aggressive-norm; plus high-token-overlap pairs within a type.

Config shape (per-corpus pack):
{
  "entity_columns": [
    {"table": "norm_press_releases", "column": "member_name", "type": "person"},
    {"table": "norm_lobbying_filings", "column": "client_name", "type": "org"}
  ],
  "min_norm_len": 5,
  "intermediary_patterns": ["\\bon behalf of\\b", "\\bo/?b/?o\\b", ...]
}

Usage:
  python resolve_entities.py --db spine.db --config pack_entities.json
"""
import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict

LEGAL_SUFFIX = re.compile(
    r"\b(l\.?l\.?c|l\.?l\.?p|inc|incorporated|corp|corporation|co|company|ltd|"
    r"limited|lp|pllc|pc|the)\b",
    re.IGNORECASE,
)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_aggressive(s: str) -> str:
    s = s.lower()
    s = LEGAL_SUFFIX.sub(" ", s)
    return NON_ALNUM.sub("", s)


def norm_light(s: str) -> str:
    return NON_ALNUM.sub("", s.lower())


def tokens(s: str) -> frozenset:
    s = LEGAL_SUFFIX.sub(" ", s.lower())
    return frozenset(t for t in re.split(r"[^a-z0-9]+", s) if len(t) > 2)


DDL = """
CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY,
    entity_type TEXT,
    canonical_name TEXT,       -- most frequent raw spelling
    norm_key TEXT,             -- aggressive norm that clustered it
    n_mentions INTEGER,
    is_intermediary_composite INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS aliases (
    entity_id INTEGER,
    raw_name TEXT,
    entity_type TEXT,
    n_mentions INTEGER,
    src_table TEXT,
    src_column TEXT
);
CREATE TABLE IF NOT EXISTS entity_merge_candidates (
    entity_id_a INTEGER,
    entity_id_b INTEGER,
    reason TEXT,               -- 'light_norm_match' | 'token_overlap'
    score REAL,
    status TEXT DEFAULT 'pending',   -- pending | confirmed | rejected
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(entity_type, norm_key);
CREATE INDEX IF NOT EXISTS idx_aliases_entity ON aliases(entity_id);
"""


def apply_confirmed(con):
    """Merge every candidate pair with status='confirmed': repoint the losing
    entity's aliases to the survivor (lower entity_id wins), fold mention
    counts, delete the loser, mark the candidate 'applied'. Review happens
    upstream (a human or LLM sets status='confirmed'); this step is dumb on
    purpose."""
    n = 0
    for cid_a, cid_b in con.execute(
        "SELECT entity_id_a, entity_id_b FROM entity_merge_candidates "
        "WHERE status='confirmed'"
    ).fetchall():
        keep, drop = sorted((cid_a, cid_b))
        # loser may already have been merged away in an earlier pair
        if not con.execute("SELECT 1 FROM entities WHERE entity_id=?", (drop,)).fetchone():
            continue
        if not con.execute("SELECT 1 FROM entities WHERE entity_id=?", (keep,)).fetchone():
            continue
        con.execute("UPDATE aliases SET entity_id=? WHERE entity_id=?", (keep, drop))
        con.execute(
            "UPDATE entities SET n_mentions = n_mentions + "
            "(SELECT n_mentions FROM entities WHERE entity_id=?) WHERE entity_id=?",
            (drop, keep))
        con.execute("DELETE FROM entities WHERE entity_id=?", (drop,))
        n += 1
    con.execute(
        "UPDATE entity_merge_candidates SET status='applied' WHERE status='confirmed'")
    con.commit()
    print(f"applied {n} confirmed merges")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=False)
    ap.add_argument("--apply-confirmed", action="store_true",
                    help="merge candidate pairs previously marked status='confirmed' "
                         "(does NOT rebuild entities; run alone after review)")
    args = ap.parse_args()

    if args.apply_confirmed:
        con = sqlite3.connect(args.db)
        con.execute("PRAGMA journal_mode=WAL")
        apply_confirmed(con)
        return
    if not args.config:
        ap.error("--config is required unless --apply-confirmed")

    with open(args.config) as f:
        cfg = json.load(f)
    min_len = cfg.get("min_norm_len", 5)
    inter_pats = [re.compile(p, re.IGNORECASE) for p in cfg.get("intermediary_patterns", [])]

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        "DROP TABLE IF EXISTS entities; DROP TABLE IF EXISTS aliases;"
        "DROP TABLE IF EXISTS entity_merge_candidates;"
    )
    con.executescript(DDL)

    t0 = time.time()
    # 1. collect raw mentions per (type, raw_name)
    mention_counts = defaultdict(lambda: defaultdict(int))  # type -> raw -> count
    mention_src = {}
    for spec in cfg["entity_columns"]:
        tbl, col, etype = spec["table"], spec["column"], spec["type"]
        q = f'SELECT "{col}", COUNT(*) FROM "{tbl}" WHERE "{col}" IS NOT NULL GROUP BY "{col}"'
        for raw, cnt in con.execute(q):
            raw = raw.strip()
            if not raw:
                continue
            mention_counts[etype][raw] += cnt
            mention_src[(etype, raw)] = (tbl, col)
    print(f"mention collection: {sum(len(v) for v in mention_counts.values())} distinct raw names "
          f"({time.time()-t0:.0f}s)")

    # 2. cluster by aggressive norm WITH guards
    entity_rows = []
    alias_rows = []
    eid = 0
    unmerged_singletons = 0
    norm_to_eid = {}
    for etype, raws in mention_counts.items():
        clusters = defaultdict(list)  # norm -> [(raw, count)]
        for raw, cnt in raws.items():
            is_inter = any(p.search(raw) for p in inter_pats)
            nk = norm_aggressive(raw)
            if is_inter or len(nk) < min_len:
                # guard tripped: singleton entity, no merging
                eid += 1
                canonical = raw
                entity_rows.append((eid, etype, canonical, None, cnt, int(is_inter)))
                tbl, col = mention_src[(etype, raw)]
                alias_rows.append((eid, raw, etype, cnt, tbl, col))
                unmerged_singletons += 1
                continue
            clusters[nk].append((raw, cnt))
        for nk, members in clusters.items():
            eid += 1
            norm_to_eid[(etype, nk)] = eid
            canonical = max(members, key=lambda rc: rc[1])[0]
            total = sum(c for _, c in members)
            entity_rows.append((eid, etype, canonical, nk, total, 0))
            for raw, cnt in members:
                tbl, col = mention_src[(etype, raw)]
                alias_rows.append((eid, raw, etype, cnt, tbl, col))

    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", entity_rows)
    con.executemany("INSERT INTO aliases VALUES (?,?,?,?,?,?)", alias_rows)
    con.commit()
    print(f"entities: {len(entity_rows)} ({unmerged_singletons} guarded singletons), "
          f"aliases: {len(alias_rows)} ({time.time()-t0:.0f}s)")

    # 3. fuzzy CANDIDATES (never merged): same light norm, different aggressive cluster
    cand_rows = []
    by_light = defaultdict(list)
    cur = con.execute(
        "SELECT a.entity_id, a.raw_name, a.entity_type FROM aliases a "
        "JOIN entities e ON e.entity_id=a.entity_id WHERE e.norm_key IS NOT NULL"
    )
    for e_id, raw, etype in cur:
        by_light[(etype, norm_light(raw))].append(e_id)
    for (etype, lk), eids in by_light.items():
        uniq = sorted(set(eids))
        if len(uniq) > 1:
            for a, b in zip(uniq, uniq[1:]):
                cand_rows.append((a, b, "light_norm_match", 0.9,
                                  "pending", f"light_norm={lk[:60]}"))

    # token-overlap candidates within type (bounded: only names sharing a rare token)
    tok_index = defaultdict(list)
    cur = con.execute("SELECT entity_id, entity_type, canonical_name FROM entities "
                      "WHERE norm_key IS NOT NULL AND n_mentions >= 3")
    ents = cur.fetchall()
    for e_id, etype, name in ents:
        for t in tokens(name):
            tok_index[(etype, t)].append(e_id)
    seen_pairs = set()
    for (etype, t), eids in tok_index.items():
        if len(eids) > 30:      # common token, useless signal
            continue
        uniq = sorted(set(eids))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                if (a, b) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
    # score token pairs
    ent_tokens = {e_id: tokens(name) for e_id, _, name in ents}
    for a, b in seen_pairs:
        ta, tb = ent_tokens.get(a, frozenset()), ent_tokens.get(b, frozenset())
        if not ta or not tb:
            continue
        jac = len(ta & tb) / len(ta | tb)
        if jac >= 0.6:
            cand_rows.append((a, b, "token_overlap", round(jac, 3), "pending", ""))

    con.executemany("INSERT INTO entity_merge_candidates VALUES (?,?,?,?,?,?)", cand_rows)
    con.commit()
    print(f"merge candidates (review only): {len(cand_rows)} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

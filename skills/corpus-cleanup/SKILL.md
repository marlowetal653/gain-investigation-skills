---
name: corpus-cleanup
description: Turn any raw document corpus (JSONL, JSON arrays, XML files, CSV) into a queryable, provenance-carrying SQLite spine. Use when handed a directory of records to investigate — before any analysis or cross-referencing. Runs a 5-step loop: deterministic ingest, deterministic profile, LLM-authored field mapping, deterministic normalization + entity resolution, two-sided sanity check.
---

# corpus-cleanup

Convert a raw corpus into a single SQLite file (the **spine**) where every row can be
round-tripped back to its verbatim source record. This skill is a **generic engine**;
everything corpus-specific lives in a per-corpus **pack** of config files that YOU (the
agent) author in step 3. Never edit engine scripts to encode corpus knowledge.

## Ground rules (read first)

- **Engine vs pack.** Scripts in `scripts/` contain zero corpus knowledge. Field names,
  join keys, entity columns, tolerances all live in pack JSON (worked example:
  `packs/example/`). If you feel the urge to hardcode a field name into a script, stop —
  it belongs in the mapping.
- **You never read raw data files.** The scripts touch the full data; you read only the
  few-KB profile report and, later, single records via `show_source.py`. That is the
  entire division of labor.
- **Single-writer SQLite discipline.** The spine runs in WAL mode. Exactly one writer
  process at a time — never run two ingest/normalize/detect steps concurrently against
  the same db, and never spawn subagents that write to it. Readers are fine anytime.
- **Disk space.** The spine grows to roughly the corpus size (verbatim raw_json copies),
  plus WAL overhead during bulk writes. Check headroom before ingesting (`df -h`); keep
  at least ~1.5× corpus size free. After each bulk step, shrink the WAL:
  `sqlite3 spine.db "PRAGMA wal_checkpoint(TRUNCATE);"`

## The 5-step loop

### Step 1 — Ingest (always first, no judgment call)

```
python3 scripts/ingest.py --corpus <data_dir> --db spine.db
```

Loads every record verbatim into `raw_records(source_group, source_file, native_id,
content_hash, raw_json)`. Native IDs come from the records themselves (a uuid field, an
XML filename, a URL) — never array indexes, which are unstable across re-downloads.
Idempotent: re-running is a no-op. Do not ask "is this corpus structured enough?"
before loading — ingest first, decide later. `--only <substr>` restricts to matching
source groups for quick tests.

### Step 2 — Profile (deterministic discovery-as-SQL)

```
python3 scripts/profile.py --db spine.db --out out/profile
```

Produces `profile.json` and `PROFILE_REPORT.md`: per-group field paths, types, fill
rates, cardinality, candidate ID fields, and — critically — **cross-group join
containment computed EXACTLY over the full corpus**, including composite-key detection
(values shaped like `"A-B"` whose halves join to other groups' IDs). Field stats are
sampled; join facts are never sampled — sampling provably produced false joins on real
data.

### Step 3 — You interpret the profile and author the pack (the only LLM step)

Read `PROFILE_REPORT.md` — **only** the report, never the raw data. From it:

1. Name the real-world tables (e.g. filings, activities, contributions, press releases).
2. Map source fields to normalized columns, using the join facts to pick canonical keys.
3. Write the pack files into `packs/<corpus>/`:
   - `mapping.json` — the normalization mapping. Schema is documented in the
     `normalize.py` docstring; the worked example is `packs/example/mapping.json`
     (source groups → column paths, `explode` for array-per-row, `^.` for parent
     scope, `const` columns, `types`, and a `post_sql` hook for derived columns,
     composite-key splits, and indexes).
   - `entities.json` — entity-resolution config: which table/columns are entity names,
     their types, intermediary patterns. Example: `packs/example/entities.json`.
4. Record WHY in the mapping's `_comment` (which measured join facts drove the choice).

If a join is < ~95% contained, note the failure mode as data, not as something to paper
over — imperfect joins become innocent explanations downstream.

### Step 4 — Normalize + resolve entities (deterministic, full corpus)

```
python3 scripts/normalize.py --db spine.db --mapping packs/<corpus>/mapping.json
python3 scripts/resolve_entities.py --db spine.db --config packs/<corpus>/entities.json
```

Normalize drops and rebuilds `norm_*` tables each run; every row carries provenance
(`source_group`, `native_id`, `content_hash`, `elem_index`). Entity resolution
auto-merges only under strict guards (identical aggressive-norm, length ≥ 5, same
declared type, not an intermediary composite like "X on behalf of Y"); everything fuzzy
lands in `entity_merge_candidates` for review — false merges invent connections, so
nothing fuzzy is ever merged automatically.

**Review loop:** sample pending candidates (highest score first), judge whether each
pair is truly the same entity (an LLM or human sets `status='confirmed'` or
`'rejected'` via SQL — spot-check a random sample of any batch before confirming it),
then apply:

```
python3 scripts/resolve_entities.py --db spine.db --apply-confirmed
```

Merges repoint aliases to the surviving entity and mark candidates `applied`.
Never confirm a batch by score alone without eyeballing a random sample; common
two-token person names ("John Rose") can collide across distinct people.

Then checkpoint: `sqlite3 spine.db "PRAGMA wal_checkpoint(TRUNCATE);"`

### Step 5 — Sanity, both kinds

**(a) Deterministic:** counts reconcile (norm table rows vs raw_records per group),
join rates match what the profile measured, and re-running step 4 reproduces the same
counts (rebuild is deterministic).

**(b) LLM spot-check:** pick ~10 random rows across the norm tables and round-trip each
through:

```
python3 scripts/show_source.py --db spine.db --group <source_group> --id <native_id>
```

Confirm the normalized values genuinely appear in, and mean the same thing as, the raw
record. A mapping that passes counts but garbles meaning fails here. If a spot-check
fails, fix the mapping (step 3) and re-run step 4 — never patch norm tables by hand.

## On a new corpus

Steps 1, 2, 4, 5a are identical commands. Only step 3 — reading the profile and
authoring the pack — is new thinking. That is the generalization contract.

## Scripts reference

| Script | Role |
|---|---|
| `scripts/ingest.py` | verbatim load → `raw_records`, idempotent, streaming-safe |
| `scripts/profile.py` | full-corpus discovery-as-SQL → profile.json + report |
| `scripts/normalize.py` | generic mapping applier → `norm_*` tables |
| `scripts/resolve_entities.py` | guarded merge → `entities`/`aliases`/candidates |
| `scripts/show_source.py` | locator → verbatim record + original file path |
| `scripts/discover.py` | legacy fast sampler for a first look at huge corpora; its join estimates are NOT trustworthy — profile.py is authoritative |

All stdlib-only, Python 3.9+.

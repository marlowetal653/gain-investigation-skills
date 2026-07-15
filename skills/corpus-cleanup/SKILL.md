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
4. Record WHY in the mapping's `_comment` (which measured join facts drove the choice)
   — **and the grain of every table: what one row means.** Before ANY rate or count,
   dedupe to that grain; a lobbyist listed once-per-issue inflated per-lobbyist counts
   ~10x in testing.

If a join is < ~95% contained, note the failure mode as data, not as something to paper
over — imperfect joins become innocent explanations downstream.

`post_sql` that fills a derived column must create the column first (`ALTER TABLE ...
ADD COLUMN`) — filling a column that doesn't exist can fail silently, and one such
failure blanked a field that downstream joins depended on. Step 5a must verify every
derived column.

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
nothing fuzzy is ever merged automatically. Case/punctuation-only variants are NOT
fuzzy: "AKIN GUMP" vs "Akin Gump" is one entity, and leaving them split double-counts
money — those auto-merge; only truly fuzzy pairs go to review. Work the review queue
by mention count, highest first — high-volume entities move totals most.

**Review loop:** sample pending candidates (highest score first), judge whether each
pair is truly the same entity (an LLM or human sets `status='confirmed'` or
`'rejected'` via SQL — spot-check a random sample of any batch before confirming it),
then apply:

```
python3 scripts/resolve_entities.py --db spine.db --apply-confirmed
```

Merges repoint aliases to the surviving entity and mark candidates `applied`.
Never confirm a batch by score alone without eyeballing a random sample; common
two-token person names ("John Rose") can collide across distinct people. Person merges
get a higher bar than org merges: never match people by last name alone — a lone
"Porter" swept in a dozen unrelated people in testing. Require the full name AND an
affiliated org to agree.

Then checkpoint: `sqlite3 spine.db "PRAGMA wal_checkpoint(TRUNCATE);"`

### Step 5 — Sanity, both kinds

**(a) Deterministic:** counts reconcile (norm table rows vs raw_records per group),
join rates match what the profile measured, and re-running step 4 reproduces the same
counts (rebuild is deterministic). Also check every `post_sql`-derived column is
non-null at the expected rate — a silent post_sql failure looks like success until a
downstream join hits the blank field. And compute any sanity rate at the table's
recorded grain (dedupe first), not over raw rows.

**(b) LLM spot-check:** pick ~10 random rows across the norm tables and round-trip each
through:

```
python3 scripts/show_source.py --db spine.db --group <source_group> --id <native_id>
```

Confirm the normalized values genuinely appear in, and mean the same thing as, the raw
record. A mapping that passes counts but garbles meaning fails here. If a spot-check
fails, fix the mapping (step 3) and re-run step 4 — never patch norm tables by hand.

### Step 6 — Offer the meaning index (optional, journalist's call)

Once the spine passes sanity, **offer** to build the semantic index now, while the
data work is fresh — don't wait for the journalist to need it mid-investigation.
Explain it in plain language, roughly:

> "One more optional step while I have everything loaded: I can build a *meaning
> index* of your records. It lets me search by what records are ABOUT rather than
> the exact words they use — find every record about late payments even when they
> say 'delinquent remittance', compare documents by meaning, and take a known case
> and find others like it. It's a one-time step per dataset: a small free download
> (~90MB) and roughly [estimate from corpus size] of indexing. Every search after
> that is instant. Want me to do it now?"

If yes: the cross-reference skill's semantic layer section has the build command
(`embed_index.py` + the pack's `semantic.json`, which you author from the profile —
which text columns are worth indexing). If no, or if `sentence-transformers` isn't
installed: note in `INVESTIGATION_PLAN.md` that the index is available later, and
move on — nothing else depends on it.

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

# Investigator

Download a government database — lobbying, campaign finance, grants, contracts, court
records — drop it in a folder, and this assistant finds the leads worth your time,
with the original document stapled to every claim. It plans the investigation with
you, does the tedious digging, attacks its own findings before showing you anything,
and checks your story against the records before you publish.

## What it works on — and what it doesn't (yet)

**It shines on bulk structured data**: the files that come out of government portals
and bulk-download pages — lobbying disclosures, campaign finance, grant and contract
databases, court docket *exports*, inspection records, your city's checkbook site.
If your records arrived as a spreadsheet, a database download, or a bulk export
(CSV, JSON, JSONL, or XML files), you're in the right place.

**It cannot yet read**: scanned documents or PDFs, handwriting, Word files, emails,
audio, or photos. A stack of scanned FOIA PDFs needs a text-extraction step first —
ask it and it will say so honestly rather than pretend.

---

## What you can do with this

You don't need to be technical — you install it once and talk to it in your own
words. But you should know what it's actually capable of, because the answer is
"more than a search box":

### Cross-check millions of records against each other — in seconds, not weeks

The classic investigative grind: two agencies publish overlapping data (two chambers'
copies of the same disclosures; contracts vs. payments; permits vs. inspections) and
somewhere in the overlap are the discrepancies worth chasing. Doing that by hand means
weeks of spreadsheet hell; most newsrooms never try.

This tool does it as a database operation. Your records become one indexed database,
and cross-checks run as queries — **the AI never reads a million records; it writes
the query, the database does the sweep**. That's the design principle throughout:
extraction and matching are done by fast, deterministic code; the AI's judgment is
saved for interpreting results. Field-tested scale: a 1.1-million-record corpus (8.6GB
of government XML and JSON) cross-checked chamber-against-chamber **in under a
minute**, surfacing every same-quarter pair where the two copies disagree about money,
where an expected counterpart filing simply doesn't exist, and where an ID points to
two different companies (data-quality stories hide there too). Naive matching produces
thousands of false alarms — amended-vs-original versions, rounding rules, snapshot
timing — so the engine encodes those innocent explanations up front and filters them
before you ever see a lead. In that field test, 12,000 raw anomalies collapsed to
~3,700 honest candidates, and adversarial verification narrowed those to the handful
worth a reporter's day.

### Turn badly structured data into something you can actually query

Real government exports are a mess: 400,000 separate XML files with inconsistent
casing, JSON with fields that appear and vanish, IDs that almost-but-don't-quite match
across sources. The prep pipeline handles that without you writing a line of code:

1. **Ingest** — every record is loaded verbatim, fingerprinted (a cryptographic hash
   proves it's never been altered), and tagged with where it came from.
2. **Profile** — the engine measures the data: every field, how often it's filled,
   what type it really is, and — crucially — which fields in one source actually match
   which fields in another (computed exactly, over everything, because sampling lies).
3. **Map** — the AI reads that profile (never your raw data) and writes the recipe
   that turns chaos into clean tables: names standardized, dates parsed, composite IDs
   split, name variants ("ACME LLC" / "Acme, L.L.C.") resolved to one entity with the
   fuzzy cases held for human review.

The result is one SQLite database file where "show me every payment over $100k to
firms that also appear in the contracts data, by year" is a one-second query — and
every row still points back to the exact source file and record it came from.

### Search by meaning, not just keywords (and how that actually works)

Keyword search can't find what it can't spell. Records about the same matter are
written by different people in different vocabularies: one filing says *"capping
out-of-pocket insulin costs"*, another says *"Medicare Part D benefit redesign"* —
same fight, zero shared words.

The optional meaning index fixes this, and the mechanism is simple to picture: the
engine splits your records into small, manageable chunks (a few paragraphs each), and
a small language model — downloaded once, running entirely on your machine, no data
leaves your computer — converts each chunk into a list of numbers (a *vector*) that
encodes what the text is *about*. Chunks about the same subject end up with similar
numbers, regardless of wording. Searching means converting your question into the same
kind of vector and finding the closest matches — across hundreds of thousands of
chunks in a fraction of a second. Build it once per dataset (~90MB download plus
roughly half an hour of indexing); every search after that is instant.

What that unlocks:
- **Ask in plain language**: "everything about delayed safety inspections" finds
  records that say "postponed compliance reviews."
- **Precedent search**: paste a passage from a known, confirmed case — it finds
  records describing the same *kind* of scheme in completely different words.
- **Meaning-level cross-referencing**: couple what officials say publicly with what
  interests pay to influence, even when the two vocabularies never overlap. (Guarded:
  it only pairs parties with a documented connection — otherwise "everyone talks about
  insulin while everyone lobbies insulin" drowns you in coincidence.)
- **Honest limits, disclosed**: a similarity score is never treated as evidence. Every
  meaning-level lead shows you both original texts and requires old-fashioned
  confirmation before it counts.

### Hunt the shapes stories take

Five detector types run over the clean database, each configured per dataset, each
producing leads with the source records attached: **contradictions** (two sources,
same fact, different values), **gaps** (the record that should exist and doesn't),
**outliers** (extremes against the right baseline), **hidden go-betweens** ("X on
behalf of Y" buried in a name field), and **timing couplings** (money and activity
landing suspiciously together). Every lead ships with its innocent explanations listed
first and survives an adversarial gauntlet — separate verification passes that try to
kill it as a misread, a lawful pattern, a mistaken identity, or plain base-rate noise
— before anyone calls it a finding. And a finding must pass one more test: *would this
surprise anyone?* A company lobbying on rules that affect it is expected behavior, not
news.

### Prove every claim, to anyone, in seconds

Everything above would be worthless if you couldn't defend it. Every claim the system
makes carries a locator that resolves to the verbatim source record — one command
prints the original, with a hash proving it's unaltered since ingest. The whole
audit trail (every lead including the killed ones with their kill reasons, plus every
cited source record) exports as a single small file an editor or lawyer can interrogate
without installing anything else. Findings come out as clickable HTML, an Excel
sheet, or a linked case-file (Obsidian vault) — your choice.

### And the workflow around it

- **"Where do I start?"** — it interviews you first, turns a hunch into concrete,
  records-answerable questions, and writes the plan down before touching data. (No
  data yet? [WHAT-DATA.md](WHAT-DATA.md) lists bulk sources that work out of the box.)
- **"Show me everything about this company."** — entity dossiers with every record,
  connection, and dollar, linked to sources.
- **"Fact-check my draft."** — every sentence checked against the records before you
  publish: supported (document attached), partial, or unsupported-and-must-change,
  plus who's still owed a right-of-reply call.
- **"Where were we?"** — the plan and every lead's status live in files, not the AI's
  memory. Close the laptop; next session picks up exactly where you stopped.

What it will NOT do: it never asserts intent or wrongdoing (it reports what records
show), it never buries a limitation, and it asks before anything costly — downloads,
long runs, or queries to outside services.

### Install (three lines, once)

```
git clone https://github.com/marlowetal653/gain-investigation-skills investigator
```
Then inside a Claude Code session:
```
/plugin marketplace add ./investigator
/plugin install investigator
```
Now put your records in a folder and say what you want in your own words.

---

## For technical readers: how it works

### The lifecycle

| Phase | Skill | What it does |
|---|---|---|
| 0. Plan | **strategize** | Interview → 2–4 records-answerable questions → tool mapping + gap analysis → `INVESTIGATION_PLAN.md` |
| 1. Prepare | **corpus-cleanup** | Ingest verbatim → deterministic profile → LLM-authored field mapping (from the profile report, never raw data) → normalize → guarded entity resolution → two-sided sanity check. Output: `spine.db` (SQLite, WAL), every row carrying `source_group` + `native_id` + content hash |
| 2. Scan | **cross-reference** | Config-driven detector templates (contradiction, gap, outlier, intermediary, overlap) + FTS5 exact-phrase mention bridge + optional semantic layer → `leads` table with provenance, innocent explanations, legal flags, defamation tiers |
| 3. Verify | **cross-reference** | Validation ladder: cheap verifier per candidate, then parallel adversarial refuters with distinct lenses (extraction misread / innocent explanation / false merge / base rate). Kill reasons feed back into detector configs |
| 4. Browse | **dossier** | Obsidian vault export (leads/sources/entities as wikilinked notes), entity dossiers, external enrichment via `references/free-apis.md` |
| 5. Gate | **fact-check** | Claim extraction → spine-anchored verification (deterministic first) → external corroboration (archive-before-cite, credibility-weighted) → language/defamation/right-of-reply pass → claim-status table |

### Architecture: generic engine + per-corpus pack

Engine scripts (`skills/*/scripts/`, stdlib-only core) contain **zero corpus
knowledge**. Everything corpus-specific lives in a pack of JSON configs the agent
authors by reading a machine-generated profile report:

- `mapping.json` — raw fields → normalized tables (dotted paths, array explode,
  parent scope, type coercion, post-SQL hook)
- `detectors.json` — detector parameters + innocent explanations (written before
  looking at output)
- `entities.json` — which columns hold entity names; guarded auto-merge rules
- `semantic.json` — optional embedding spaces + anchored-bridge config

Worked example with every knob documented: **`packs/example/`** (a fictional
city-grants corpus). On a new dataset only the pack is new thinking.

### Design principles

- **Provenance or it doesn't exist.** Every claim round-trips to a verbatim record
  via `show_source.py`; locators travel with every lead, note, and fact-check row.
- **Deterministic extraction.** SQL/scripts do extraction and filtering; model
  judgment is reserved for planning, mapping authorship, interpretation, and
  verification.
- **Adversarial verification.** Detector output is candidates, never findings.
- **Editorial doctrine.** Records-show phrasing, no causation claims, defamation
  tiers, right-of-reply, legal flags as leads-requiring-verification.
- **Consent + narration.** Plain-language explanation before each phase; explicit
  consent before costly steps (model downloads, long indexing runs, external queries).

### The semantic layer (optional)

`sentence-transformers` + on-disk float16 vectors (no vector DB). `embed_index.py`
builds spaces (deterministic 170-word chunker inside MiniLM's 256-token window);
`semantic_query.py` retrieves by meaning, merges FTS ranks (`--hybrid`), and supports
precedent-seeded search (`--seeds`); `semantic_bridge.py` couples text↔records by
meaning but **only within anchored entity pairs** (documented relationships), with a
boilerplate specificity filter and an empirical-null similarity floor — unanchored
corpus-wide topic matching produces topical coincidence, not leads. Similarity scores
are never the authority; matched verbatim texts and locators are. Vectors live in
`semantic_index/` (never committed); scores reproduce to ~1e-3 across devices,
model + revision + torch version + device recorded in `meta.json`.

### Standalone usage (no agent)

```
bash bootstrap.sh                    # env check + full usage sequence
python3 skills/corpus-cleanup/scripts/ingest.py  --corpus ./data --db spine.db
python3 skills/corpus-cleanup/scripts/profile.py --db spine.db --out out/profile
# author packs/<yourcorpus>/ from the profile report (see packs/example/)
python3 skills/corpus-cleanup/scripts/normalize.py --db spine.db --mapping packs/<yourcorpus>/mapping.json
python3 skills/cross-reference/scripts/detect.py   --db spine.db --config packs/<yourcorpus>/detectors.json
python3 skills/dossier/scripts/export_obsidian.py --db spine.db --vault out/vault --top 25
```

### Cross-session memory

`INVESTIGATION_PLAN.md` (goals, questions, tool plan) + the `leads` table (every
candidate, status new/verified/promoted/killed, and why) are the investigation's
durable state. A fresh session reads both and resumes.

### Credits

- Fact-check methodology adapted from **Joe Amditis**,
  [claude-skills-journalism](https://github.com/jamditis/claude-skills-journalism) (MIT).
- Credibility-weighting rubric adapted from **Florent Daudens**,
  [ai-journalism-skills](https://huggingface.co/spaces/fdaudens/ai-journalism-skills) (CC BY 4.0).
- Free-APIs reference adapted from Joe Amditis's free-apis-catalog (MIT).

### License

MIT.

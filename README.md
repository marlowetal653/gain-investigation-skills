# Investigator

An AI investigation assistant for journalists. Give it a folder of records —
lobbying filings, court documents, grant awards, a FOIA production, any big pile of
files — and work with it like a colleague: it plans the investigation with you, does
the tedious digging, shows you everything it finds with the original document attached,
and checks your story against the records before you publish.

---

## For journalists: what you can do with this

You don't need to be technical. You install it once (ask a colleague or follow the
three lines below), then you just talk to it. Things you can say:

**"I just got this data dump. Where do I start?"**
It interviews you — what's the story, what do you suspect, what would prove it — and
turns your hunch into a written investigation plan with concrete questions the records
can actually answer. You approve the plan before anything runs.

**"Go through these records and find anything newsworthy."**
It turns your files into one searchable database where every number, name, and date
can be traced back to the original document, then scans for the classic shapes of a
story: two sources that disagree about the same fact, records that should exist but
don't, unusual extremes, hidden go-betweens, and money appearing near messaging.

**"Is this actually a story, or is there an innocent explanation?"**
Every lead it finds comes with the innocent explanations already listed, and before it
shows you anything as promising, it attacks its own findings from several angles
(wrong reading? lawful explanation? mistaken identity? just normal at this scale?).
Leads that die get a recorded reason. You see what survived and why.

**"Show me everything about this company / this person."**
It builds a dossier: every record they appear in, who they're connected to, what
money moved, with links back to sources — browsable as notes (in Obsidian, a free
note-taking app) that you can read like a case file.

**"I remember a case like this from 2019 — find me more like it."**
Give it a passage from a known, confirmed case and it finds records that talk about
the same kind of thing *even in completely different words*. (This one needs a small
one-time setup it will ask you about first — a free download and some indexing time.)

**"Fact-check my draft."**
Before you publish, it checks every sentence against the records: which claims are
supported by documents (with the document attached), which are partly supported,
which have no support and must change. It flags causation words the records can't
back, lists every named person still owed a right-of-reply call, and won't call the
draft ready until that's clean.

**"Where were we?"**
Close your laptop mid-investigation; next session, ask this and it picks up exactly
where you left off — the plan and every lead's status are written down, not in its
memory.

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
| 4. Browse | **investigate** | Obsidian vault export (leads/sources/entities as wikilinked notes), entity dossiers, external enrichment via `references/free-apis.md` |
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
python3 skills/investigate/scripts/export_obsidian.py --db spine.db --vault out/vault --top 25
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

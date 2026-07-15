# Investigator — an agentic records-investigation plugin

A Claude Code plugin for journalists: point it at a directory of records — lobbying
filings, court documents, grant awards, FOIA productions, any JSONL/JSON/XML/CSV dump —
and it plans the investigation with you, turns the files into one searchable
source-traceable database, scans for anomalies, adversarially verifies what it finds,
and hands you leads you can check back to the original documents. It talks to you in
plain language; you don't need to be technical.

## Install & launch

```
git clone https://github.com/marlowetal653/gain-investigation-skills investigator
# in a Claude Code session:
/plugin marketplace add ./investigator
/plugin install investigator
```

Then just describe what you want, in your own words:

> "I have a folder of city grant records. I think a few companies are getting
> favored. Help me investigate."

The `investigator` agent launches itself, interviews you about the story, writes an
investigation plan, and walks you through every step — explaining what it's doing and
why, asking before anything costly. `/investigate ./data` is a shortcut; you never
need to memorize commands.

## The investigation lifecycle

| Phase | Skill | What it does (plain language) |
|---|---|---|
| 0. Plan | **strategize** | Interviews you, narrows a hunch into 2–4 questions records can answer, maps each to tools, writes `INVESTIGATION_PLAN.md` — the file every later session resumes from |
| 1. Prepare | **corpus-cleanup** | Turns raw files into one searchable database (a SQLite "spine") where every value traces back to its original document |
| 2. Scan | **cross-reference** | Config-driven detectors: contradictions between sources, missing counterpart records, extremes, hidden go-betweens, exact-name and meaning-level couplings |
| 3. Verify | **cross-reference** (ladder) | Tries to KILL every promising lead — cheap checks, then independent adversarial agents with different attack angles. Only survivors advance |
| 4. Browse | **investigate** | Exports leads to an Obsidian vault — notes with claims, evidence links, innocent explanations, right-of-reply checklists; builds dossiers on specific people/orgs |
| 5. Publish gate | **fact-check** | Checks every sentence of a draft against the records; external corroboration via public APIs; defamation and right-of-reply pass. Nothing ships unsupported |

## Architecture: generic engine + per-corpus pack

The engine scripts contain **zero knowledge of any specific dataset**. Everything
corpus-specific lives in a "pack" of JSON configs the agent authors by reading a
machine-generated profile report (never the raw data):

- **Engine** (`skills/*/scripts/`): ingest, profile, normalize, resolve entities,
  detect, embed, query, export — deterministic Python, stdlib-only core.
- **Pack** (`packs/<yourcorpus>/`): `mapping.json` (raw fields → normalized tables),
  `detectors.json` (what anomalies to scan for + their innocent explanations),
  `entities.json` (which columns hold names), `semantic.json` (optional meaning-level
  layer). Worked example with every knob documented: **`packs/example/`** — a
  fictional city-grants corpus.

On a new dataset only the pack is new thinking; every other step is the same command.

## Design principles

- **Provenance or it doesn't exist.** Every claim round-trips to a verbatim source
  record (`show_source.py`). The vault, the reports, the fact-check table all carry
  locators.
- **Deterministic extraction.** SQL and scripts do the extraction and filtering; the
  model's judgment is spent on planning, interpretation, and verification — not on
  reading a million records.
- **Adversarial verification.** Detector output is candidates, not findings. Leads
  survive only by beating independent refuters (wrong-extraction / innocent-explanation
  / false-merge / base-rate lenses). Kill reasons are recorded and fed back into
  detector configs.
- **Editorial doctrine baked in.** Records-show phrasing (no causation claims),
  defamation tiers, right-of-reply checklists, legal flags as
  leads-requiring-verification — never as accusations.
- **Plain-language voice.** The agent explains why before how, sizes everything to the
  journalist's goal, asks consent before costly steps (e.g. the semantic layer's
  one-time model download + indexing), and translates every error into what-it-means
  and what-happens-next.

## The semantic layer (optional)

Keyword search finds records that share words. The semantic layer finds records that
share **meaning** — e.g. a query about a drug-price cap surfaces activities filed as
"pharmaceutical pricing policy" with zero shared keywords. It also supports
**precedent-seeded search**: give it passages from a known, confirmed case and it
finds records that talk about the same kind of thing in different words.

It requires one extra install (`pip install sentence-transformers`, a ~90MB model) and
the agent always asks before building the index. Vectors live in `semantic_index/`
(never committed). Similarity scores are never the authority — the matched verbatim
texts and their locators are, and every semantic lead needs deterministic confirmation
before promotion.

## Cross-session memory

Two files are the investigation's memory: `INVESTIGATION_PLAN.md` (goals, questions,
tool plan) and the `leads` table in the spine (every candidate, its status —
new/verified/promoted/killed — and why). A brand-new session reads both and picks up
exactly where the last one stopped.

## Quickstart without the agent (scripts run standalone)

```
bash bootstrap.sh                    # environment check + full usage sequence
python3 skills/corpus-cleanup/scripts/ingest.py  --db spine.db --root ./data
python3 skills/corpus-cleanup/scripts/profile.py --db spine.db
# author packs/<yourcorpus>/ from the profile report (see packs/example/)
python3 skills/corpus-cleanup/scripts/normalize.py --db spine.db --mapping packs/<yourcorpus>/mapping.json
python3 skills/cross-reference/scripts/detect.py   --db spine.db --config packs/<yourcorpus>/detectors.json
python3 skills/investigate/scripts/export_obsidian.py --db spine.db --vault out/vault --top 25
```

## Credits

- Fact-check methodology adapted from **Joe Amditis**,
  [claude-skills-journalism](https://github.com/jamditis/claude-skills-journalism) (MIT).
- Credibility-weighting rubric adapted from **Florent Daudens**,
  [ai-journalism-skills](https://huggingface.co/spaces/fdaudens/ai-journalism-skills) (CC BY 4.0).
- Free-APIs reference adapted from Joe Amditis's free-apis-catalog (MIT).

## License

MIT.

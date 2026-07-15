---
name: cross-reference
description: Run config-driven detectors over a normalized SQLite spine to surface investigative leads — contradictions between sources, missing counterpart filings, outliers, hidden intermediaries, and suspicious overlaps. Use after corpus-cleanup has built the spine, when you want a cold trawl for newsworthy anomalies or to manage the leads pipeline (verify, refute, promote, kill).
---

# cross-reference

Turn a clean spine into a ranked, provenance-carrying **leads table**. Detection is
deterministic and cheap; judgment (verification, refutation, promotion) is where the
LLM spends effort. Never do extraction or filtering with the LLM that a SQL query can
do — that is the efficiency contract this skill is judged on.

## Running detectors

```
python3 scripts/detect.py --db spine.db --config packs/<corpus>/detectors.json
```

`--only <detector_id>` runs one detector; `--run-id <name>` tags the batch for traces.
Apply the pack's views first if it has any (dedupe/latest-filing views):
`sqlite3 spine.db < packs/<corpus>/views.sql`

### Detector templates (generic engine)

| Template | Finds | Key params |
|---|---|---|
| `contradiction` | same fact reported differently by two sources beyond a tolerance | left/right table+value_col, join_on, tolerance |
| `gap` | expected counterpart record missing | left/right tables, join_on, expectation text |
| `outlier` | numeric extremes within a group | table, value_col, group_cols, method (top_n / sigma) |
| `intermediary` | entity strings embedding a hidden principal ("X on behalf of Y") | table, name_col, regex patterns with named groups |
| `overlap` | two edge sets sharing endpoints in a time window | edge specs + window |

### Text-mention bridge (mention_scan.py)

When one corpus is free text (press releases, speeches) and another is structured
(filings), build the bridge deterministically before any overlap/outlier detector:

```
python3 scripts/mention_scan.py --db spine.db --config packs/<corpus>/mentions.json
```

It builds an FTS5 index over the text column and scans for exact-phrase matches of
entity names supplied by a config SQL query (e.g. the highest-value counterparties in
the structured records), writing a mentions table — one row per (document, entity).
Guards: `min_len` and `stop_names` exclude short/generic names that would false-match.
No LLM in the loop; the scan is reproducible. The mentions table then joins to the
structured records (a pack view), and an ordinary `outlier` detector ranks the
couplings.

Config schema by worked example: `packs/example/detectors.json` (one worked entry per
template). Each detector entry carries `id`, `template`, `params`,
**`innocent_explanations`** (the known lawful reasons this pattern occurs — encode
them in config; e.g. a reporting regime that permits rounding becomes a tolerance,
amendment refiling becomes a latest-version view), `legal_flag`, and `severity_hint`.
When authoring a new pack: every detector must ship with its innocent explanations
written down BEFORE you look at its output.

### Hard-won comparison rules

Each of these cost a bad lead in field testing. Apply them to every pack.

- **ID overlap ≠ identity.** High join containment proves the key matches, not that
  the join is CORRECT — one ID mapped to different companies on each side and
  manufactured a fake "$10k vs $130k" contradiction. Every cross-source ID join must
  also require a second field to agree (the templates support `guard_cols`). Many
  guard exclusions are not waste — that pile is a data-quality lead in itself.
- **Compare like versions.** Most raw "contradictions" are amended-vs-original or
  quarterly-vs-termination — different by design, not in dispute. Pin report type AND
  amendment status on BOTH sides: latest-version views plus `left_where`/`right_where`.
- **Blank ≠ conflict.** One side missing is a gap lead; a contradiction requires both
  sides present and disagreeing. Never let NULLs inflate contradiction counts.
- **The newsworthiness gate.** Before promoting any lead, ask: *would this surprise
  anyone?* Expected self-interest — a company lobbying rules that affect it — gets
  downgraded. Elevate only the hidden, unexpected, or contradictory: an undisclosed
  intermediary, someone lobbying their own prosecution, a front group whose name
  conceals its nature, action cutting against the actor's obvious interest. Detectors
  find patterns; the surprise test decides findings.
- **Encode domain rules as innocent explanations UP FRONT.** Every reporting regime
  generates false anomalies by design — filing thresholds, amendment/termination
  codes, double-filing that doubles dollar totals, disclosure windows. Write them
  into `innocent_explanations` when authoring the pack; don't rediscover them by
  tripping over them in detector output (we did).
- **Recency scoping.** Before any full-corpus run, offer the journalist a time window
  ("last year or two?"). Scoped runs are faster and surface fresher leads; save the
  full sweep for later.

## Semantic layer (OPTIONAL — requires an extra install; everything above runs without it)

Lexical scan (`mention_scan.py`) finds records that share exact words. The semantic
layer finds records that share **meaning** — e.g. a statement about "capping a drug's
out-of-pocket price" and a filing described as "pharmaceutical pricing policy" with no
shared keyword. This needs `sentence-transformers` (a ~90MB model download).
All detectors above, and the whole pipeline, work without it; if it is not installed
the semantic scripts exit with the exact install command.

**Journalist consent gate — mandatory.** NEVER start an embedding build silently.
Before running `embed_index.py`, explain to the journalist in plain language: what it
buys them (find same-matter records regardless of wording), what it costs (one-time
~90MB model download + roughly 30–60 minutes to index this corpus, done once per
dataset), and ask them to confirm. Then wait for a yes.

Three scripts:

1. **`embed_index.py`** — builds on-disk float16 vectors for one or more "spaces"
   (a table's text column). Deterministic chunker (170-word windows, inside MiniLM's
   256-token limit); `dedupe` for repetitive columns like activity descriptions.
   `--limit N` builds a fast test slice. Config: `packs/example/semantic.json`.
   ```
   python3 scripts/embed_index.py --db spine.db --config packs/<corpus>/semantic.json
   ```
2. **`semantic_query.py`** — retrieval by meaning; a journalist tool ("show me every
   record about this kind of matter") and a building block. `--hybrid` merges FTS
   keyword ranks (only for spaces whose table has an FTS index). `--seeds <file>` is
   **precedent-seeded search**: give it one or more exemplar passages from a KNOWN,
   confirmed case and it finds records that talk about the same kind of thing in
   different words — plain-language framing for the journalist: "you show me an
   example of the pattern; I find records like it." Seed matches are LEADS for the
   verification ladder, never findings; similar language is not similar conduct, and
   a seed's distinctive phrasing may reflect one drafter's style, not the scheme.
   ```
   python3 scripts/semantic_query.py --space <space> --query "drug price cap" --top 10
   python3 scripts/semantic_query.py --space <space> --seeds known_case.txt --top 20
   ```
3. **`semantic_bridge.py`** — the anchored meaning-level coupling detector.
   **Doctrine:** it scores only entity pairs that already share a documented
   **anchor** (a prior text mention, a recorded payment/contribution link, a shared
   home region). Unanchored corpus-wide topic matching is the null hypothesis of any
   busy domain — many actors discuss the same hot topic with no relationship between
   a given pair. It also drops match-everything boilerplate descriptions (keeps those
   with a reference number or distinctive token) and sets its similarity floor from
   an empirical null of random cross-pairs plus a margin over the counterparty's own
   topic centroid.

**Disclosure + verification rule.** Every semantic lead states the model and score,
carries BOTH verbatim texts and the exact `semantic_query.py` command to reproduce the
match, and is phrased "semantic lead — verify deterministically." A cosine score is
NOT the authority; the two texts and locators are. **Promotion requires an independent
deterministic confirmation** (an FTS hit, a shared named entity, or a human reading
both texts) recorded on the lead. Reproducibility note: chunker output and row ids are
byte-stable; the embedding floats are rank-stable to ~1e-3 across devices, so scores
reproduce approximately, not bit-for-bit — `meta.json` records model, revision, torch
version, and device.

The vectors live under `semantic_index/` (git-ignored, never shipped in the submission
zip); rebuild is one command. Provenance = row locators + chunk offsets, same as
everything else.

## The leads table (cross-session organization)

Every lead lands in `leads` with: `claim` (records-show phrasing), `evidence` (JSON
locators — every input row's `source_group` + `native_id`), `params` (exact detector
config for reproduction), `innocent_explanations`, `legal_flag`, `defamation_tier`,
`score`/`rank_score`, and `status`.

**Lifecycle:** `new → verified → promoted → published`, or `→ killed` at any point.
Status transitions ARE the investigation's memory across sessions — a new session
reads the leads table and knows exactly where things stand. Update status with plain
SQL; never delete leads (killed leads document what was checked and why it died).

## Ranking

Detectors emit a raw `score`; ranking fills `rank_score`. Rank by:
`subject power × money-link directness × event proximity × novelty`. Direct
money-to-named-person beats aggregate-spend beats issue-level correlation. Before
promoting anything, check novelty — has this been reported already? A true, sourced,
already-published fact is not a finding.

## Validation ladder (where LLM effort goes)

- **Candidate → verified:** one cheap verifier per lead. Re-run the emitting query,
  `show_source.py` every cited locator, confirm the raw values match the claim.
- **Verified → promoted:** three adversarial refuters with distinct lenses, each
  returning a structured verdict (claim_attacked, evidence, verdict):
  1. **Misread/extraction** — did the mapping or query distort the source?
  2. **Innocent explanation** — does a listed (or unlisted) lawful explanation cover it?
  3. **False merge / base rate** — is an entity merge inventing the connection, or is
     this pattern so common it's noise?
  A lead is promoted only if all three fail to kill it; log their verdicts in the lead.
- **Subagent doctrine:** spawn subagents ONLY for parallel multi-step units or for the
  adversarial refuters (independence matters there). Never spawn a subagent to do
  extraction or filtering — that's a SQL query. Subagents must not write to the spine
  (single-writer rule); they report back, the main session writes.

## Language policy (applies to every claim, note, and report)

- **Records-show phrasing only:** "Records show A and B. The records do not establish
  intent or causation." Never "X paid Y to do Z."
- Leads naming individuals carry `defamation_tier` (`none|named_org|named_person`);
  `named_person` leads require the full refuter panel plus a right-of-reply placeholder
  before any report use.
- `legal_flag=1` leads (e.g. the gap detector's potential non-filings) are **potential
  statutory violations to be verified, not accusations**. State the statute, state what
  the records show, state the verification still required (late posting, alternate ID,
  terminated engagement). These flags satisfy the legal-violation-flagging requirement
  precisely because they are framed as leads.

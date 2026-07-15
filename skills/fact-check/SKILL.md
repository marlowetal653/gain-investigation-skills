---
name: fact-check
description: Check every claim in a drafted story, findings memo, or report against the investigation's records before publication. Use when a draft exists and someone says "fact-check this", "is this ready to publish?", or before any findings leave the project. Anchors each claim to verbatim source records first, corroborates externally only when records can't answer, enforces records-show language, defamation care, and right-of-reply. Nothing ships with an unsupported claim unresolved.
---

<!--
Methodology adapted from:
- "fact-check-workflow" by Joe Amditis (github.com/jamditis/claude-skills-journalism, MIT)
- credibility-weighting rubric from "fact-checker" by Florent Daudens
  (huggingface.co/spaces/fdaudens/ai-journalism-skills, CC BY 4.0)
Re-anchored to this plugin's spine-first, deterministic-verification doctrine.
-->

# fact-check

The last gate before anything leaves the project. Your job is to try to make
the draft FAIL — a fact-check that only confirms is decoration. Work claim by
claim; the records are the authority, not memory, not vibes, not the model.

Speak to the journalist in plain language (jargon in parentheses only); the
output table is precise, the conversation around it is human.

## Step 1 — Extract claims

Split the draft into atomic claims — one checkable assertion each. Classify:
- **records-claim** — should be provable from the investigation's own database
  (names, amounts, dates, counts, quotes from filings)
- **external-claim** — factual but outside the corpus (a law's requirements, a
  person's title, a public event)
- **characterization** — interpretive framing ("unusually large", "rare",
  "wave of...") — must trace to a measured base rate or be softened
- **opinion/analysis** — allowed, but must be labeled as such in the draft

## Step 2 — Anchor records-claims to the spine (deterministic first)

For each records-claim:
1. Find the supporting rows — the lead's evidence locators
   (`leads.evidence`), or direct SQL against the normalized tables.
2. Round-trip to the verbatim source:
   `python3 skills/corpus-cleanup/scripts/show_source.py --db spine.db --id <native_id>`
3. Compare the draft's exact numbers, names, dates, and quotes against the raw
   record — character by character for quotes, unit by unit for amounts.
4. Status:
   - **SUPPORTED-BY-RECORDS** — verbatim record backs the claim as written
   - **PARTIAL** — record backs part; note exactly which part fails
   - **UNSUPPORTED** — no record found; the claim cannot ship as-is
   - **NEEDS-EXTERNAL** — true test lives outside the corpus → Step 3

Never mark SUPPORTED from memory of the investigation — re-run the lookup.
If a claim rests on a semantic lead, confirm the deterministic anchor and the
underlying texts, never the similarity score.

## Step 3 — External corroboration (only for NEEDS-EXTERNAL)

1. Prefer authoritative public APIs from
   `skills/investigate/references/free-apis.md` (legislative records, campaign
   finance, official registers) over general web search.
2. **Archive before you cite**: save every external URL to an archive service
   (e.g. web.archive.org/save) and record the archived URL alongside the live
   one — cited pages change and die.
3. Weight each source: authority of the source (40%), soundness of its method
   (30%), recency (15%), independent corroboration (15%). Two independent
   strong sources for anything contested; one is never enough for a damaging
   claim.
4. Status as in Step 2, plus **UNVERIFIABLE** (nobody can currently confirm —
   the draft must cut it or attribute it explicitly).

## Step 4 — Language and legal pass

Run the whole draft against the editorial doctrine:
- **Records-show phrasing** — flag every causal verb ("because", "in exchange
  for", "rewarded", "in return") attached to a records-claim; records show
  correlation and sequence, never intent.
- **Defamation tiers** — every named person or organization: is each claim
  about them SUPPORTED, and is the framing the minimum the records support?
  Named private individuals get the highest bar.
- **Right-of-reply** — list every named party; confirm outreach happened or is
  planned, with the ask and the deadline recorded.
- **Corrections protocol** — the draft's publication plan must say where
  corrections will run if something proves wrong.

## Step 5 — Output

Append (or deliver alongside) a fact-check table:

| # | Claim (verbatim from draft) | Type | Status | Evidence locator(s) | Repro command | Flags |

Plus a plain-language summary for the journalist: what's solid, what must
change, what must be cut, who still needs a right-of-reply call. **The draft is
not publishable while any records-claim is UNSUPPORTED or any named party lacks
right-of-reply.** Record status changes on the relevant leads (e.g. promote to
`published` only after this gate passes).

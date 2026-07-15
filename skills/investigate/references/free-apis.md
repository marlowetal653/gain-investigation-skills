# Free public-data APIs for enrichment & corroboration

<!-- Adapted from "free-apis-catalog" by Joe Amditis
     (github.com/jamditis/claude-skills-journalism, MIT). Trimmed to the
     endpoints most useful for records-based accountability work. Verify
     terms/rate limits before heavy use; they change. -->

Consulted by: **strategize** (what outside data could answer a question),
**investigate** (entity-dossier enrichment), **fact-check** (external
corroboration). Prefer these authoritative sources over general web search.

## US legislative & government records

| API | What it answers | Auth | Notes |
|---|---|---|---|
| **api.congress.gov** | bills, votes, cosponsorship, member records (keyed by bioguide_id) | free key | 5,000 req/hr; the canonical member/bill source |
| **api.open.fec.gov** (OpenFEC) | campaign contributions, committee finance, donor lookups | free key (api.data.gov umbrella) | 1,000 req/hr; pairs naturally with lobbying/contribution records |
| **api.govinfo.gov** | Federal Register, congressional documents, public laws, court opinions | free key | bulk-data friendly |
| **api.data.gov** | umbrella key covering dozens of federal APIs (incl. OpenFEC) | one free key | check per-API rate limits |
| **efts.sec.gov/LATEST/search-index?q=** (EDGAR full-text) | SEC filings mentioning a company/person | none | be gentle; identify with a User-Agent |
| **lda.senate.gov/api/v1/** | live Senate lobbying registry (LDA filings, contributions) | none | the live counterpart to any lobbying-corpus snapshot; used to confirm/refute "missing filing" leads |
| **efile.fara.gov** (FARA eFile) | foreign-agent registrations | none (bulk CSV) | cross-ref foreign-principal leads |
| **api.census.gov** | demographics, business patterns, geography | free key | context/base-rate numbers |

## Context & preservation

| API | What it answers | Auth | Notes |
|---|---|---|---|
| **web.archive.org/save/{url}** + **archive.org/wayback/available?url=** | archive a page before citing; find old versions | none | REQUIRED step before citing any live URL (fact-check step 3) |
| **api.gdeltproject.org** | global news mentions over time (who covered what, when) | none | novelty checks: has this been reported? |
| **Unpaywall (api.unpaywall.org)** | legal open-access copies of paywalled research | email param | for method/context citations |

## Usage rules

1. **Archive first, cite second** — every external URL gets a Wayback save;
   record both live and archived URLs in the evidence.
2. **Respect terms** — some keys prohibit commercial use; check before a
   commercial publication relies on one.
3. **Rate limits are real** — batch queries, cache responses locally (a plain
   JSON file next to the spine is fine), never hammer an endpoint in a loop.
4. **Provenance discipline unchanged** — an API response is a source record:
   store what was fetched, from where, when, so the claim round-trips like
   everything else.

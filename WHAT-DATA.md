# What data can I feed this? (a starter list)

You don't need data in hand to start — the strategize step will help you pick. But if
you want to browse, every source below has a bulk-download button and comes in a
format that works out of the box.

| Source | What stories live in it | Where | Format |
|---|---|---|---|
| **Federal lobbying (Senate LDA)** | who pays whom to influence what, quarter by quarter | lda.senate.gov → Search → bulk JSON via the public API | JSON ✅ |
| **Federal lobbying (House Clerk)** | the House copy of the same disclosures — comparing the two chambers is itself a story generator | disclosures.house.gov → downloads | XML ✅ |
| **Campaign finance (FEC)** | contributions, committee spending, donor networks | fec.gov/data → bulk downloads | CSV ✅ |
| **Federal spending** | grants, contracts, loans — who got how much for what | usaspending.gov → download center | CSV/JSON ✅ |
| **Court records (federal)** | dockets, filings, opinions | courtlistener.com → bulk data (RECAP) | JSON ✅ |
| **Foreign agents (FARA)** | who represents foreign governments/companies in the US | efile.fara.gov → bulk CSV | CSV ✅ |
| **Federal Register** | rules, notices, the regulatory paper trail | federalregister.gov/developers | JSON ✅ |
| **Your state's lobbying / contracts portal** | the local version of all of the above — usually less picked-over | search "[state] lobbying disclosure bulk data" | varies — check for CSV/JSON/XML |
| **Your city's open-checkbook site** | vendor payments, grants, payroll | search "[city] open checkbook" or "[city] open data" | CSV ✅ usually |
| **Agency FOIA logs** | what everyone else is asking an agency for | many agencies publish request logs | CSV ✅ / PDF ❌ |

✅ = drop the files in a folder and go. ❌ = needs text extraction first (this tool
can't read scanned PDFs yet — say so to the assistant and it will help you plan
around it).

Tip: the best first corpus is one with **two sources describing the same events**
(e.g. House + Senate copies of lobbying filings, or contracts + checkbook payments) —
disagreements between them are where stories hide.

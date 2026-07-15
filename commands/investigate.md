---
description: Launch the full investigation pipeline on a records corpus — build spine, trawl, verify, export vault.
argument-hint: [corpus-dir] [pack-name]
---

Launch the `investigator` agent to investigate a records corpus end-to-end.

Arguments: `$ARGUMENTS`
- First arg (optional): corpus directory. Default: `./data`.
- Second arg (optional): pack name under `packs/`. If omitted, reuse an existing
  pack that matches the corpus, or author a new one via corpus-cleanup.

Steps:
1. Confirm the corpus directory exists and note its size (disk headroom: the
   spine grows to roughly corpus size — need ~1.5× corpus free).
2. Launch the `investigator` agent (via the Agent tool) with the corpus
   directory and pack name. Let it run its full operating procedure — it begins
   by strategizing with the journalist (interview → written investigation plan)
   unless `INVESTIGATION_PLAN.md` already exists, then builds the spine, trawls,
   adversarially verifies the top leads, exports a scoped vault, and fact-checks
   anything drafted.
3. When it returns, relay the ranked findings summary, the vault path, and the
   leads-table state. Do not present unverified detector output as findings.

If no corpus directory is given and `./data` does not exist, ask the user where
the records are before launching.

---
name: dossier
description: Export leads, entities, and evidence from the SQLite spine into an Obsidian vault — the journalist's working interface — and run targeted entity dossier investigations. Use after cross-reference has populated the leads table, when a human needs to browse, verify, or deep-dive findings, or when investigating a specific named entity.
---

# dossier

The journalist works **inside an Obsidian vault**, not in JSON dumps or chat
transcripts. This skill renders the spine into linked markdown notes and supports
targeted dossier investigations on specific entities.

## Exporting the vault

```
python3 scripts/export_obsidian.py --db spine.db --vault out/vault --top 25
```

Deterministic, read-only over the db, byte-identical re-runs. Produces:

- `README.md` — how to verify a lead, language policy
- `leads/` — one note per lead (top N by rank_score): claim, status, evidence chain,
  innocent explanations, defamation tier, `show_source.py` commands to reproduce
- `sources/` — a stub note per cited locator
- `entities/` — dossier notes for entities the exported leads touch, with
  `[[wikilinks]]` so Obsidian's local graph becomes an interactive ego-network

Missing tables are skipped gracefully — an empty-but-valid vault is always produced,
so this can run at any pipeline stage.

## Scoping rule (hard)

**Never export the full spine.** Hundreds of thousands of entities render as a
hairball and bury the story. Export a scoped subgraph per investigation:
`--top N` for the lead-driven view (default 50, prefer ~25 for a working session), or
`--entity "Name"` (repeatable) for dossiers. Keep every export to **at most a few
hundred nodes**. If a vault feels crowded, split it — one vault per investigation is
cheap; an unreadable graph is not.

## Dossier workflow (targeted entity investigation)

When the question is "what do we hold on X?":

1. **Query the spine first** (read-only SQL): X's rows across `norm_*` tables, aliases
   in `aliases`, pending merges in `entity_merge_candidates`, leads whose evidence
   mentions X. Aggregate money and time ranges with SQL, not by reading rows.
2. **Check the merge candidates** before trusting totals — an unresolved alias can
   split X's activity across two entities (or a bad merge can inflate it).
3. **Export a scoped vault:**
   ```
   python3 scripts/export_obsidian.py --db spine.db --vault out/vault_x --entity "X"
   ```
4. **Verify inside the vault:** each lead note carries its `show_source.py` commands;
   the human (or you) round-trips claims to verbatim records without ever opening a
   raw data file.

New findings from a dossier go back into the `leads` table via the cross-reference
skill's lifecycle (status transitions, validation ladder) — the vault is a rendering
of the spine, never a second source of truth. Re-export after the spine changes.

## Division of labor

- Spine (SQLite) = truth, provenance, status.
- Vault (markdown) = human interface: reading, graph exploration, annotation.
- All vault content obeys the cross-reference language policy (records-show phrasing,
  defamation tiers, right-of-reply placeholders) because it is generated from lead
  rows that already carry it.

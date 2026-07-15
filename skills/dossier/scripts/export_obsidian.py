"""
export_obsidian: deterministically render the spine to an Obsidian vault.

The journalist works INSIDE the vault, not in text dumps. This script is pure
SQL -> markdown: it reads spine.db (read-only), writes a folder of
YAML-frontmatter markdown notes, and never mutates the database.

Vault layout:
  README.md      what this is, how to verify a lead, language policy
  leads/         one note per lead (top N by rank_score)
  sources/       one stub note per locator cited in exported leads' evidence
  entities/      dossier notes for entities touched by exported leads

Determinism contract: same inputs -> byte-identical output files. No
timestamps, no randomness, everything sorted. Safe to re-run; the exporter
rewrites the vault folders it owns.

Defensive contract: tables land in spine.db incrementally. Missing tables
(leads, entities, aliases, raw_records) are skipped with a note, never a
crash. An empty-but-valid vault (README + folders) is always produced.

Usage:
  python export_obsidian.py --db spine.db --vault out/vault
  python export_obsidian.py --db spine.db --vault out/vault --top 25
  python export_obsidian.py --db spine.db --vault out/vault --entity "Acme Corp"

stdlib only.
"""
import argparse
import json
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------- helpers

# Characters illegal or hostile in filenames / Obsidian wikilinks.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_PART = 80  # keep filenames comfortably under OS limits


def slug(text, max_len=60):
    """Deterministic lowercase slug: alnum runs joined by hyphens."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "untitled"


def safe_id(native_id):
    """Filesystem/wikilink-safe rendering of a native_id (often a URL or
    a path-ish string). Deterministic; long ids get a short hash suffix so
    distinct ids never collide after truncation."""
    nid = native_id or ""
    cleaned = _UNSAFE.sub("-", nid).strip("-")
    if not cleaned:
        cleaned = "id"
    if cleaned != nid or len(cleaned) > _MAX_NAME_PART:
        h = hashlib.sha1(nid.encode("utf-8")).hexdigest()[:8]
        cleaned = cleaned[:_MAX_NAME_PART].rstrip("-") + "-" + h
    return cleaned


def group_slug(source_group):
    """Compact slug for a source_group like '.json::senate/#/filings/...'."""
    sg = source_group or ""
    # Drop the extension prefix ('.json::'), keep the meaningful path part.
    if "::" in sg:
        sg = sg.split("::", 1)[1]
    return slug(sg, max_len=40)


def yaml_str(value):
    """Render a scalar for YAML frontmatter safely (always double-quoted
    for strings; JSON string escaping is a valid YAML double-quoted style)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(pairs):
    lines = ["---"]
    for k, v in pairs:
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {yaml_str(item)}")
        else:
            lines.append(f"{k}: {yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines)


def table_exists(con, name):
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def table_columns(con, name):
    return [r[1] for r in con.execute(f"PRAGMA table_info({name})")]


def col(row, columns, name, default=None):
    """Fetch a column from a row tuple by name, tolerating absent columns."""
    if name in columns:
        return row[columns.index(name)]
    return default


def parse_json_field(raw, default):
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def write_note(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- leads

def load_leads(con, top_n):
    if not table_exists(con, "leads"):
        return None  # table missing entirely (distinct from "no rows")
    cols = table_columns(con, "leads")
    order = []
    if "rank_score" in cols:
        order.append("rank_score DESC")
    if "score" in cols:
        order.append("score DESC")
    if "lead_id" in cols:
        order.append("lead_id ASC")
    order_by = ("ORDER BY " + ", ".join(order)) if order else ""
    rows = con.execute(f"SELECT * FROM leads {order_by} LIMIT ?", (top_n,)).fetchall()
    leads = []
    for row in rows:
        ev = parse_json_field(col(row, cols, "evidence"), [])
        if not isinstance(ev, list):
            ev = []
        innocent = parse_json_field(col(row, cols, "innocent_explanations"), [])
        if isinstance(innocent, dict):
            innocent = [f"{k}: {v}" for k, v in sorted(innocent.items())]
        if not isinstance(innocent, list):
            innocent = [str(innocent)]
        leads.append({
            "lead_id": col(row, cols, "lead_id"),
            "detector_id": col(row, cols, "detector_id"),
            "template": col(row, cols, "template"),
            "signal_type": col(row, cols, "signal_type"),
            "claim": col(row, cols, "claim") or "",
            "score": col(row, cols, "score"),
            "rank_score": col(row, cols, "rank_score"),
            "status": col(row, cols, "status"),
            "legal_flag": col(row, cols, "legal_flag"),
            "defamation_tier": col(row, cols, "defamation_tier"),
            "evidence": ev,
            "innocent": innocent,
        })
    return leads


def evidence_locators(evidence):
    """Yield (source_group, native_id, value) triples from an evidence list,
    tolerating shape drift."""
    for item in evidence:
        if not isinstance(item, dict):
            continue
        loc = item.get("locator") or {}
        if not isinstance(loc, dict):
            loc = {}
        sg = loc.get("source_group")
        nid = loc.get("native_id")
        if sg is None and nid is None:
            # tolerate flattened shape {"source_group":..,"native_id":..}
            sg = item.get("source_group")
            nid = item.get("native_id")
        yield sg, nid, item.get("value")


def source_note_name(source_group, native_id):
    return f"SRC {group_slug(source_group)} {safe_id(native_id)}"


CLAIM_FOR_PARENS = re.compile(r"\bfor \(([^)]{4,160})\)")
CLAIM_PARENS = re.compile(r"\(([^)]{4,160})\)")


def lead_label(lead):
    """Human label for a lead: the entity parenthetical after 'for (' when the
    claim has one (gap/contradiction claims), else the LAST parenthetical
    (usually the entity list), else the claim head."""
    claim = lead["claim"] or ""
    m = CLAIM_FOR_PARENS.search(claim)
    if m:
        return m.group(1)
    all_parens = CLAIM_PARENS.findall(claim)
    if all_parens:
        return all_parens[-1]
    return (claim or "lead")[:60]


def lead_note_name(lead):
    return f"LEAD-{lead['lead_id']} {slug(lead_label(lead), max_len=56)}"


def render_lead(lead):
    fm = frontmatter([
        ("status", lead["status"]),
        ("signal_type", lead["signal_type"]),
        ("detector", lead["detector_id"]),
        ("score", lead["score"]),
        ("rank_score", lead["rank_score"]),
        ("legal_flag", lead["legal_flag"]),
        ("defamation_tier", lead["defamation_tier"]),
    ])
    body = [fm, "", "## Claim", "", lead["claim"] or "_(no claim text)_", ""]

    body.append(
        "_How to read this note: the claim above states only what the records show. "
        "Status `" + str(lead["status"]) + "` means "
        + {"new": "nobody has verified this yet",
           "verified": "it survived verification",
           "promoted": "it survived adversarial checks and is story-worthy",
           "published": "it passed the fact-check gate",
           "killed": "it was checked and rejected"}.get(str(lead["status"]), "see the leads table")
        + ". The scores are internal ranking numbers — higher means the pattern is "
        "bigger or rarer; they are not evidence. `legal_flag: 1` means the pattern "
        "COULD indicate a compliance issue and needs verification — it is not an "
        "accusation._")
    body.append("")

    body.append("## Evidence")
    body.append("")
    any_ev = False
    for sg, nid, value in evidence_locators(lead["evidence"]):
        any_ev = True
        if sg is None and nid is None:
            if value in (None, "null", ""):
                body.append("- **No counterpart record found** — the absence itself "
                            "is the signal this lead reports (see the claim).")
            else:
                rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                body.append(f"- Detector context: `{rendered}`")
            continue
        link = source_note_name(sg, nid)
        val = ""
        if value is not None:
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
            val = f" — {rendered}"
        body.append(f"- [[{link}]]{val}")
    if not any_ev:
        body.append("- _(no evidence items recorded)_")
    body.append("")

    body.append("## Innocent explanations")
    body.append("")
    if lead["innocent"]:
        for exp in lead["innocent"]:
            body.append(f"- {exp}")
    else:
        body.append("- _(none recorded — consider before promoting)_")
    body.append("")

    body.append("## How to check this yourself")
    body.append("")
    body.append("- [ ] Open each source note linked under Evidence — the original record is pasted there. Confirm the names, amounts, and dates match the claim.")
    body.append("- [ ] Read the innocent explanations above and rule each in or out.")
    body.append("- [ ] Check for prior coverage (is this already reported?)")
    body.append("- [ ] _For technical colleagues:_ re-run the detector and confirm the lead reproduces (`detect.py --only " + str(lead["detector_id"]) + "`)")

    tier = (lead["defamation_tier"] or "none")
    if str(tier).lower() not in ("none", "null", ""):
        body.append("")
        body.append("## Right of reply")
        body.append("")
        body.append(f"> Defamation tier: **{tier}**. This lead names or implicates identifiable parties.")
        body.append("> Before publication, contact the named parties for comment and record the outreach here.")
        body.append("")
        body.append("- Contacted: _(who / when / how)_")
        body.append("- Response: _(verbatim or summary)_")

    return "\n".join(body)


# ---------------------------------------------------------------- sources

def render_source(con, source_group, native_id):
    content_hash = None
    raw_json = None
    if table_exists(con, "raw_records"):
        row = con.execute(
            "SELECT content_hash, raw_json FROM raw_records WHERE source_group=? AND native_id=? LIMIT 1",
            (source_group, native_id),
        ).fetchone()
        if row:
            content_hash, raw_json = row

    fm = frontmatter([
        ("source_group", source_group),
        ("native_id", native_id),
        ("content_hash", content_hash),
    ])
    locator = f"{source_group}::{native_id}"
    body = [fm, "", "## The original record", "",
            "_This is the record as it arrived, unaltered (the `content_hash` above "
            "proves it hasn't changed since ingest)._", ""]
    if raw_json is None:
        body.append("_Raw record not found in spine.db raw_records — run show_source against the corpus, or re-ingest._")
    else:
        try:
            pretty = json.dumps(json.loads(raw_json), indent=2, ensure_ascii=False, sort_keys=True)
        except (ValueError, TypeError):
            pretty = str(raw_json)
        lines = pretty.splitlines()
        LIMIT = 150
        body.append("```json")
        body.extend(lines[:LIMIT])
        if len(lines) > LIMIT:
            body.append(f"... ({len(lines) - LIMIT} more lines — full record via the command below)")
        body.append("```")
    body.append("")
    body.append("## For technical colleagues")
    body.append("")
    body.append("```bash")
    body.append(f'python skills/corpus-cleanup/scripts/show_source.py --db spine.db --locator "{locator}"')
    body.append("```")
    return "\n".join(body)


# ---------------------------------------------------------------- entities

def find_entities_for_leads(con, leads, extra_names):
    """Entities whose canonical name or alias appears verbatim inside any
    exported lead's evidence values or claim, plus explicitly requested names.
    Returns {entity_id: entity_row_dict}."""
    if not table_exists(con, "entities"):
        return {}
    ecols = table_columns(con, "entities")

    # Gather the searchable text pool from leads.
    pool = []
    for lead in leads or []:
        pool.append(lead["claim"] or "")
        for _, _, value in evidence_locators(lead["evidence"]):
            if value is not None:
                pool.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    haystack = "\n".join(pool).lower()

    matched = {}

    def add_entity(row):
        eid = col(row, ecols, "entity_id")
        if eid is not None and eid not in matched:
            matched[eid] = {
                "entity_id": eid,
                "entity_type": col(row, ecols, "entity_type"),
                "canonical_name": col(row, ecols, "canonical_name") or "",
                "n_mentions": col(row, ecols, "n_mentions"),
            }

    # Explicit --entity requests: match canonical_name or alias, case-insensitive.
    for name in extra_names:
        rows = con.execute(
            "SELECT * FROM entities WHERE lower(canonical_name)=lower(?)", (name,)
        ).fetchall()
        if not rows and table_exists(con, "aliases"):
            rows = con.execute(
                "SELECT e.* FROM entities e JOIN aliases a ON a.entity_id=e.entity_id "
                "WHERE lower(a.raw_name)=lower(?)", (name,)
            ).fetchall()
        for row in rows:
            add_entity(row)
        if not rows:
            print(f"note: --entity {name!r} not found in entities/aliases", file=sys.stderr)

    if haystack:
        # Scan canonical names against the evidence text. Iterating entities
        # in SQL order keeps this deterministic; substring match, len >= 5
        # to avoid junk hits on short norms.
        for row in con.execute("SELECT * FROM entities ORDER BY entity_id"):
            name = (col(row, ecols, "canonical_name") or "")
            if len(name) >= 5 and name.lower() in haystack:
                add_entity(row)

    return matched


def render_entity(con, ent, lead_links):
    fm = frontmatter([
        ("type", ent["entity_type"]),
        ("mentions", ent["n_mentions"]),
    ])
    body = [fm, "", f"# {ent['canonical_name']}", ""]

    body.append("## Aliases")
    body.append("")
    aliases = []
    if table_exists(con, "aliases"):
        acols = table_columns(con, "aliases")
        for row in con.execute(
            "SELECT * FROM aliases WHERE entity_id=? ORDER BY raw_name", (ent["entity_id"],)
        ):
            raw = col(row, acols, "raw_name")
            src = col(row, acols, "src_table")
            n = col(row, acols, "n_mentions")
            detail = ", ".join(str(x) for x in (src, f"{n} mentions" if n is not None else None) if x)
            aliases.append(f"- {raw}" + (f" _({detail})_" if detail else ""))
    if aliases:
        body.extend(aliases)
    else:
        body.append("- _(no aliases recorded)_")
    body.append("")

    body.append("## Leads touching this entity")
    body.append("")
    if lead_links:
        for link in sorted(lead_links):
            body.append(f"- [[{link}]]")
    else:
        body.append("- _(none in this export)_")
    return "\n".join(body)


def entity_note_name(ent):
    return f"ENT {slug(ent['canonical_name'], max_len=60)} {ent['entity_id']}"


# ---------------------------------------------------------------- README

README_TEMPLATE = """# Investigation vault

This folder is an [Obsidian](https://obsidian.md) vault rendered deterministically
from `spine.db` by `skills/investigate/scripts/export_obsidian.py`. Open the folder
in Obsidian (File > Open Vault) — or read the markdown directly; nothing here
requires Obsidian to be legible.

**Nothing in this vault is a conclusion.** Every note states what public records
show. Leads are hypotheses generated by deterministic detectors over lobbying
disclosures and congressional press releases; they are starting points for
reporting, not findings, and certainly not accusations.

## Layout

- `leads/` — one note per detector lead, ranked. The frontmatter carries status,
  signal type, detector, scores, and legal/defamation flags. Each lead links to
  the exact source records behind it.
- `sources/` — one stub per cited record: its provenance locator, content hash,
  a JSON excerpt, and the exact command to print the full raw record.
- `entities/` — dossier stubs for people/organizations touched by exported leads,
  with their recorded aliases and backlinks to leads.

## How to verify a lead

1. Open the lead note. Read the claim — it is phrased as what records show.
2. Follow each `[[SRC ...]]` wikilink under **Evidence** to the source stub.
3. Run the `show_source` command in the stub to print the verbatim raw record
   straight from the database, and confirm the cited value appears in it:

   ```bash
   python skills/corpus-cleanup/scripts/show_source.py --db spine.db --locator "<source_group>::<native_id>"
   ```

4. Work the **Verification** checklist in the lead note: re-run the detector,
   check prior coverage, and rule the **Innocent explanations** in or out.
5. If the lead carries a defamation tier, complete the **Right of reply**
   section before any publication decision.

## Language policy

All generated text uses records-show phrasing: "records show A and B; records do
not establish causation." Leads naming identifiable individuals carry a
`defamation_tier` flag and a right-of-reply placeholder. `legal_flag` marks
possible disclosure-law violations (e.g. missing or late LDA filings) detected
mechanically — a flag is a discrepancy in the records, not a determination that
a law was broken.

## Provenance

Every evidence item resolves to a `(source_group, native_id)` locator plus a
content hash in `spine.db`. Same database in, byte-identical vault out — the
exporter embeds no timestamps, so diffs between exports reflect only changes in
the underlying records and detectors.
{export_notes}"""


# ---------------------------------------------------------------- main

def write_leads_csv(path, leads):
    """leads.csv — double-clicks into Excel/Numbers for editors."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "status", "what_it_is", "claim", "detector",
                    "legal_flag", "named_parties_tier", "innocent_explanations",
                    "evidence_locators"])
        for lead in leads:
            locs = "; ".join(f"{sg}::{nid}" for sg, nid, _ in
                             evidence_locators(lead["evidence"]) if nid)
            w.writerow([lead["lead_id"], lead["status"], lead_label(lead),
                        lead["claim"], lead["detector_id"], lead["legal_flag"],
                        lead["defamation_tier"],
                        " | ".join(lead["innocent"] or []), locs])


def write_leads_html(path, leads, con):
    """One self-contained page an editor opens with a double-click."""
    import html as H
    rows = []
    for lead in leads:
        ev_bits = []
        for sg, nid, _ in evidence_locators(lead["evidence"]):
            if nid is None:
                continue
            excerpt = ""
            if table_exists(con, "raw_records"):
                r = con.execute("SELECT raw_json FROM raw_records WHERE native_id=? LIMIT 1",
                                (nid,)).fetchone()
                if r:
                    excerpt = r[0][:600]
            ev_bits.append(
                f"<details><summary>Source record <code>{H.escape(str(nid))}</code></summary>"
                f"<pre>{H.escape(excerpt)}…</pre></details>")
        innocents = "".join(f"<li>{H.escape(x)}</li>" for x in (lead["innocent"] or []))
        rows.append(f"""
<article>
  <h3>{H.escape(lead_label(lead))} <small>[{H.escape(str(lead['status']))}]</small></h3>
  <p>{H.escape(lead['claim'] or '')}</p>
  {'<p><strong>⚑ flagged: possible compliance relevance — needs verification, not an accusation.</strong></p>' if lead['legal_flag'] else ''}
  <p><em>Innocent explanations to rule out:</em></p><ul>{innocents}</ul>
  {''.join(ev_bits)}
</article><hr>""")
    doc = f"""<!doctype html><meta charset="utf-8">
<title>Investigation leads</title>
<style>body{{font:16px/1.5 Georgia,serif;max-width:52rem;margin:2rem auto;padding:0 1rem}}
h3 small{{color:#777;font-weight:normal}}pre{{white-space:pre-wrap;background:#f6f6f6;padding:.5rem;font-size:12px}}
details{{margin:.4rem 0}}article{{margin:1.5rem 0}}</style>
<h1>Investigation leads ({len(leads)})</h1>
<p>Every claim states only what the records show. Each lead lists the innocent
explanations that must be ruled out, and the original records are attached under
each entry. Nothing here is an accusation.</p>
{''.join(rows)}"""
    Path(path).write_text(doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Export spine.db to an Obsidian vault.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--top", type=int, default=50, help="max leads to export (by rank_score)")
    ap.add_argument("--entity", action="append", default=[],
                    help="also export this entity's dossier (repeatable)")
    ap.add_argument("--csv", action="store_true",
                    help="also write <vault>/leads.csv (opens in Excel)")
    ap.add_argument("--html", action="store_true",
                    help="also write <vault>/index.html (self-contained, double-click to open)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"database not found: {db_path}")
    # Read-only connection — this tool must never write to the spine.
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    vault = Path(args.vault)
    for sub in ("leads", "sources", "entities"):
        (vault / sub).mkdir(parents=True, exist_ok=True)

    notes = []  # export notes surfaced in README

    # ---- leads
    leads = load_leads(con, args.top)
    n_leads = 0
    if leads is None:
        notes.append("`leads` table was not present in spine.db at export time; "
                     "the `leads/` folder is empty. Re-run the export once detectors have run.")
        leads = []
    elif not leads:
        notes.append("`leads` table exists but contains no rows; the `leads/` folder is empty.")
    for lead in leads:
        name = lead_note_name(lead)
        write_note(vault / "leads" / f"{name}.md", render_lead(lead))
        n_leads += 1

    # ---- sources referenced by exported leads
    seen = set()
    n_sources = 0
    for lead in leads:
        for sg, nid, _ in evidence_locators(lead["evidence"]):
            if sg is None and nid is None:
                continue
            key = (sg, nid)
            if key in seen:
                continue
            seen.add(key)
            name = source_note_name(sg, nid)
            write_note(vault / "sources" / f"{name}.md", render_source(con, sg, nid))
            n_sources += 1
    if not table_exists(con, "raw_records"):
        notes.append("`raw_records` table missing — source stubs have no content hash or excerpt.")

    # ---- entities
    n_entities = 0
    if table_exists(con, "entities"):
        ents = find_entities_for_leads(con, leads, args.entity)
        # Backlinks: which lead notes mention each entity's canonical name.
        for eid in sorted(ents):
            ent = ents[eid]
            links = []
            cname = ent["canonical_name"].lower()
            for lead in leads:
                pool = (lead["claim"] or "").lower() + "\n" + "\n".join(
                    (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)).lower()
                    for _, _, v in evidence_locators(lead["evidence"]) if v is not None
                )
                if cname and cname in pool:
                    links.append(lead_note_name(lead))
            write_note(vault / "entities" / f"{entity_note_name(ent)}.md",
                       render_entity(con, ent, links))
            n_entities += 1
    else:
        notes.append("`entities` table was not present in spine.db at export time; "
                     "the `entities/` folder is empty.")

    # ---- README
    export_notes = ""
    if notes:
        export_notes = "\n## Export notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    write_note(vault / "README.md", README_TEMPLATE.format(export_notes=export_notes))

    if args.csv:
        write_leads_csv(vault / "leads.csv", leads)
    if args.html:
        write_leads_html(vault / "index.html", leads, con)

    con.close()
    print(f"vault: {vault}")
    print(f"  leads:    {n_leads}")
    print(f"  sources:  {n_sources}")
    print(f"  entities: {n_entities}")
    if args.csv:
        print(f"  leads.csv: open in Excel/Numbers")
    if args.html:
        print(f"  index.html: double-click to browse")
    for n in notes:
        print(f"  note: {n}")


if __name__ == "__main__":
    main()

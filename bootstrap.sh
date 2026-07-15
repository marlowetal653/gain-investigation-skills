#!/usr/bin/env bash
# bootstrap.sh — environment check + usage map for the investigation engine.
# Idempotent: run as many times as you like; it changes nothing, only checks.
set -u

ok=1

echo "== Investigation engine bootstrap check =="
echo "   (This check is for technical users running the pipeline by hand."
echo "    If you're using the investigator plugin inside Claude Code, the"
echo "    agent handles all of this — you don't need to run anything here.)"

# Python 3.9+
if command -v python3 >/dev/null 2>&1; then
    pyver=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        echo "  [ok] python3 $pyver"
    else
        echo "  [!!] python3 $pyver found, but 3.9+ is required."
        ok=0
    fi
else
    echo "  [!!] python3 not found. Install Python 3.9+ (https://www.python.org)."
    ok=0
fi

# sqlite3 CLI (used for views.sql, WAL checkpoints, ad-hoc queries)
if command -v sqlite3 >/dev/null 2>&1; then
    echo "  [ok] sqlite3 CLI $(sqlite3 --version | cut -d' ' -f1)"
else
    echo "  [--] sqlite3 CLI not found — OPTIONAL. The pipeline still runs (sqlite3 is"
    echo "       built into Python); the CLI is only a convenience for ad-hoc queries."
    echo "       macOS: ships with the OS / brew install sqlite    Debian: apt install sqlite3"
fi

# stdlib-only confirmation
echo "  [ok] no third-party packages required (core is stdlib-only; see requirements.txt)"

echo
if [ "$ok" -eq 1 ]; then
    echo "Environment ready."
else
    echo "Fix the [!!] items above, then re-run ./bootstrap.sh"
fi

cat <<'USAGE'

== Pipeline usage (in order) ==

  1. Ingest the corpus verbatim into the spine (always first):
       python3 skills/corpus-cleanup/scripts/ingest.py --corpus data --db spine.db

  2. Profile it (deterministic discovery; writes PROFILE_REPORT.md):
       python3 skills/corpus-cleanup/scripts/profile.py --db spine.db --out out/profile

  3. Author the per-corpus pack from the profile report (LLM/human step):
       write packs/<corpus>/mapping.json, entities.json
       (worked example: packs/example/ — see skills/corpus-cleanup/SKILL.md)

  4. Normalize + resolve entities:
       python3 skills/corpus-cleanup/scripts/normalize.py --db spine.db --mapping packs/<corpus>/mapping.json
       python3 skills/corpus-cleanup/scripts/resolve_entities.py --db spine.db --config packs/<corpus>/entities.json
       sqlite3 spine.db "PRAGMA wal_checkpoint(TRUNCATE);"

  5. Run detectors (cold trawl -> leads table):
       sqlite3 spine.db < packs/<corpus>/views.sql
       python3 skills/cross-reference/scripts/detect.py --db spine.db --config packs/<corpus>/detectors.json

  6. Export the journalist vault (open in Obsidian):
       python3 skills/dossier/scripts/export_obsidian.py --db spine.db --vault out/vault --top 25

  Verify any claim back to its verbatim source record:
       python3 skills/corpus-cleanup/scripts/show_source.py --db spine.db --locator "<group>::<native_id>"

Optional semantic layer (meaning-level search + anchored say-vs-do bridge):
       pip install sentence-transformers    # ~90MB model downloaded on first use
       python3 skills/cross-reference/scripts/embed_index.py --db spine.db --config packs/<corpus>/semantic.json
     Not installed? The semantic scripts print this command and every other step runs.

Notes: the spine grows to roughly corpus size — keep ~1.5x corpus free disk.
One writer at a time against spine.db (WAL mode; readers are always fine).
USAGE

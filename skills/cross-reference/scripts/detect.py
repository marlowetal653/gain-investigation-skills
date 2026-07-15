"""
Detect: generic, config-driven lead detectors over a normalized spine.

GENERIC ENGINE — no corpus knowledge. Each detector is a TEMPLATE parameterized
entirely by the per-corpus pack config. Every emitted lead carries full
provenance (source locators of every input row + the exact parameters that
produced it) and is written to the `leads` table with status='new'.

Detector templates (v1):
  contradiction  — same fact reported by two sources; report where they differ
                   beyond a tolerance. params: left/right (table, value_col),
                   join_on, tolerance, label_cols. Optional:
                     left_where / right_where — ANDed SQL fragments (each must
                       start with "AND", may reference the l./r. aliases) to
                       pin e.g. report type or amendment status so both sides
                       compare the same version of a filing.
                     guard_cols: [[left_col, right_col], ...] — after the ID
                       join, ALSO require these columns to agree (TRIM +
                       case-insensitive) before calling it a contradiction.
                       Protects against ID collisions where the same ID points
                       to different real-world entities on each side. Rows
                       excluded by a guard are counted and printed; rows where
                       a guard column is NULL on either side pass (absence is
                       not disagreement).
                   Both value_cols must be NOT NULL — missing-vs-present is a
                   gap, not a contradiction; the count of one-side-NULL pairs
                   is printed for visibility.
  gap            — expected counterpart missing. params: left table + key,
                   right table + key, expectation description, left_where.
                   Optional guard_cols (same shape/semantics as contradiction):
                   an ID-joined counterpart only counts as present if the
                   guard columns also agree; guard-excluded matches are
                   counted and printed.
  outlier        — numeric field extreme within a group. params: table,
                   value_col, group_cols, method (top_n | sigma), threshold.
                   Prints input row count and distinct group count so grain
                   inflation (per-row vs per-filing aggregation) is visible.
  intermediary   — entity-name strings that embed a hidden principal.
                   params: table, name_col, patterns (regex w/ named groups
                   'intermediary' and 'principal').
  overlap        — two edge sets sharing endpoints within a time window
                   (e.g. money-to-X while lobbying-X). params: left/right
                   (table, entity_col, date_col), window_days, min_amount.

Every detector is PRE-FLIGHTED before any runs: all tables/views named in its
params must exist and all referenced columns must resolve (PRAGMA table_info).
A detector that fails pre-flight (or errors at runtime) is reported as
"[SKIPPED] detector <id>: ..." and the rest still run; if any were skipped the
process exits 2 after a summary so callers know the pack is incomplete.

Config shape (pack):
{
  "detectors": [
    {"id": "...", "template": "contradiction", "params": {...},
     "innocent_explanations": ["..."], "legal_flag": false, "severity_hint": 2}
  ]
}

Usage:
  python detect.py --db spine.db --config detectors.json [--only id]
"""
import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict

LEADS_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY,
    detector_id TEXT,
    template TEXT,
    signal_type TEXT,
    claim TEXT,                  -- records-show phrasing, one line
    score REAL,                  -- pre-ranking raw score from the detector
    rank_score REAL,             -- filled by rank.py
    status TEXT DEFAULT 'new',   -- new|verified|killed|promoted|published
    legal_flag INTEGER DEFAULT 0,
    defamation_tier TEXT,        -- none|named_org|named_person
    evidence TEXT,               -- JSON: [{table, locators:[{source_group,native_id}], values}]
    params TEXT,                 -- JSON: exact detector params (reproducibility)
    innocent_explanations TEXT,  -- JSON list
    created_run TEXT
);
"""


def records_show(text):
    """Language policy: leads state what records show, never causation."""
    return f"Records show {text}. The records do not establish intent or causation."


def emit(con, run_id, det, signal_type, claim, score, evidence, defam="none", legal=None):
    con.execute(
        "INSERT INTO leads (detector_id, template, signal_type, claim, score, status,"
        " legal_flag, defamation_tier, evidence, params, innocent_explanations, created_run)"
        " VALUES (?,?,?,?,?,'new',?,?,?,?,?,?)",
        (det["id"], det["template"], signal_type, claim, score,
         int(det.get("legal_flag", False) if legal is None else legal), defam,
         json.dumps(evidence), json.dumps(det.get("params", {})),
         json.dumps(det.get("innocent_explanations", [])), run_id),
    )


# ---------------- pre-flight ----------------
_PROV_COLS = ("source_group", "native_id")


def _requirements(det):
    """Map a detector config to {table_or_view: {required columns}}.
    Raises KeyError/TypeError on malformed params (caller treats as failure)."""
    p, t = det["params"], det["template"]
    req = defaultdict(set)
    if t == "contradiction":
        L, R = p["left"], p["right"]
        req[L["table"]].update((L["value_col"],) + _PROV_COLS)
        req[R["table"]].update((R["value_col"],) + _PROV_COLS)
        for a, b in p["join_on"]:
            req[L["table"]].add(a)
            req[R["table"]].add(b)
        for a, b in p.get("guard_cols", []):
            req[L["table"]].add(a)
            req[R["table"]].add(b)
        req[L["table"]].update(p.get("label_cols", []))
    elif t == "gap":
        L, R = p["left"], p["right"]
        req[L["table"]].update(_PROV_COLS)
        req[R["table"]].add("native_id")
        for a, b in p["join_on"]:
            req[L["table"]].add(a)
            req[R["table"]].add(b)
        for a, b in p.get("guard_cols", []):
            req[L["table"]].add(a)
            req[R["table"]].add(b)
        req[L["table"]].update(p.get("label_cols", []))
        if p.get("score_col"):
            req[L["table"]].add(p["score_col"])
    elif t == "outlier":
        req[p["table"]].update((p["value_col"],) + _PROV_COLS)
        req[p["table"]].update(p.get("group_cols", []))
    elif t == "intermediary":
        req[p["table"]].update((p["name_col"],) + _PROV_COLS)
    elif t == "overlap":
        L, R = p["left"], p["right"]
        req[L["table"]].update((L["entity_col"], L["date_col"]) + _PROV_COLS)
        if L.get("amount_col"):
            req[L["table"]].add(L["amount_col"])
        req[R["table"]].update((R["entity_col"], R["date_col"]) + _PROV_COLS)
    return req


def preflight(con, det):
    """Return a list of human-readable problems (empty = detector runnable):
    every table/view in the params must exist in sqlite_master and every
    referenced column must resolve via PRAGMA table_info. left_where /
    right_where are raw SQL and cannot be statically checked; runtime errors
    in them are caught by the runner and reported as [SKIPPED] too."""
    try:
        req = _requirements(det)
    except (KeyError, TypeError, ValueError) as e:
        return [f"required param ({e})"]
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    problems = []
    for tbl in sorted(req):
        if tbl not in existing:
            problems.append(f"table/view '{tbl}'")
            continue
        have = {r[1] for r in con.execute(f'PRAGMA table_info("{tbl}")')}
        missing = sorted(c for c in req[tbl] if c not in have)
        if missing:
            problems.append(f"column(s) {', '.join(missing)} in '{tbl}'")
    return problems


def _guard_sql(guard_cols):
    """Agreement predicate for guard column pairs: TRIM + case-insensitive
    equality; a NULL on either side passes (absence is not disagreement)."""
    return " AND ".join(
        f'(l."{a}" IS NULL OR r."{b}" IS NULL OR '
        f'TRIM(LOWER(l."{a}")) = TRIM(LOWER(r."{b}")))'
        for a, b in guard_cols)


# ---------------- templates ----------------
def t_contradiction(con, det, run_id):
    p = det["params"]
    L, R = p["left"], p["right"]
    tol = p.get("tolerance", 0)
    join_on = p["join_on"]  # [[left_col, right_col], ...]
    on_sql = " AND ".join(f'l."{a}" = r."{b}"' for a, b in join_on)
    where_extra = f'{p.get("left_where", "")} {p.get("right_where", "")}'
    guards = p.get("guard_cols", [])
    guard_ok = _guard_sql(guards)
    base_from = f'FROM "{L["table"]}" l JOIN "{R["table"]}" r ON {on_sql}'
    base_where = f'''
    WHERE l."{L['value_col']}" IS NOT NULL AND r."{R['value_col']}" IS NOT NULL
      AND ABS(l."{L['value_col']}" - r."{R['value_col']}") > ?
      {where_extra}
    '''
    # blank != conflict: missing-vs-present pairs belong to the gap template.
    (n_null,) = con.execute(f'''
        SELECT COUNT(*) {base_from}
        WHERE (l."{L['value_col']}" IS NULL) <> (r."{R['value_col']}" IS NULL)
          {where_extra} {f"AND {guard_ok}" if guards else ""}
        ''').fetchone()
    print(f"  excluded {n_null} pairs where one side was NULL "
          f"(missing-vs-present is a gap, not a contradiction)")
    if guards:
        (n_guard,) = con.execute(
            f"SELECT COUNT(*) {base_from} {base_where} AND NOT ({guard_ok})",
            (tol,)).fetchone()
        gdesc = ", ".join(a if a == b else f"{a}/{b}" for a, b in guards)
        print(f"  guard excluded {n_guard} id-matched rows where {gdesc} "
              f"disagreed — possible id collisions")
    q = f'''
    SELECT l."{L['value_col']}", r."{R['value_col']}",
           l.source_group, l.native_id, r.source_group, r.native_id,
           {", ".join(f'l."{c}"' for c in p.get("label_cols", []))}
    {base_from}
    {base_where}
    {f"AND {guard_ok}" if guards else ""}
    '''
    n = 0
    for row in con.execute(q, (tol,)):
        lv, rv, lsg, lid, rsg, rid = row[:6]
        labels = row[6:]
        diff = abs(lv - rv)
        claim = records_show(
            f"two disclosures of the same engagement ({' / '.join(str(x) for x in labels)}) "
            f"report different values: {lv} vs {rv} (difference {round(diff,2)}, "
            f"tolerance {tol})"
        )
        ev = [{"locator": {"source_group": lsg, "native_id": lid}, "value": lv},
              {"locator": {"source_group": rsg, "native_id": rid}, "value": rv}]
        emit(con, run_id, det, "contradiction", claim, diff, ev, defam="named_org")
        n += 1
    return n


def t_gap(con, det, run_id):
    p = det["params"]
    L, R = p["left"], p["right"]
    join_on = p["join_on"]
    on_sql = " AND ".join(f'l."{a}" = r."{b}"' for a, b in join_on)
    guards = p.get("guard_cols", [])
    if guards:
        # a counterpart only counts as present if the ID join AND the guard
        # columns agree — an id collision must not mask a real gap. Count how
        # many id-only matches the guards rejected so collisions are visible.
        guard_ok = _guard_sql(guards)
        (n_guard,) = con.execute(f'''
            SELECT COUNT(*) FROM "{L["table"]}" l JOIN "{R["table"]}" r ON {on_sql}
            WHERE NOT ({guard_ok}) {p.get("left_where", "")}
            ''').fetchone()
        gdesc = ", ".join(a if a == b else f"{a}/{b}" for a, b in guards)
        print(f"  guard excluded {n_guard} id-matched rows where {gdesc} "
              f"disagreed — possible id collisions")
        on_sql = f"{on_sql} AND {guard_ok}"
    label_cols = ", ".join(f'l."{c}"' for c in p.get("label_cols", [])) or "NULL"
    score_col = f'l."{p["score_col"]}"' if p.get("score_col") else "NULL"
    q = f'''
    SELECT l.source_group, l.native_id, {score_col}, {label_cols}
    FROM "{L['table']}" l LEFT JOIN "{R['table']}" r ON {on_sql}
    WHERE r.native_id IS NULL {p.get("left_where", "")}
    '''
    n = 0
    for row in con.execute(q):
        lsg, lid, score = row[:3]
        labels = row[3:]
        claim = records_show(
            f"{p.get('expectation', 'an expected counterpart record')} is missing for "
            f"({' / '.join(str(x) for x in labels)})"
        )
        ev = [{"locator": {"source_group": lsg, "native_id": lid}, "value": "present"},
              {"missing_in": R["table"]}]
        emit(con, run_id, det, "gap", claim, float(score or 1.0), ev, defam="named_org",
             legal=det.get("legal_flag", True))
        n += 1
    return n


def t_outlier(con, det, run_id):
    p = det["params"]
    group_cols = p.get("group_cols", [])
    gsel = ", ".join(f'"{c}"' for c in group_cols) or "'all'"
    method = p.get("method", "top_n")
    # grain visibility: aggregates SUM every input row per group, so a table
    # with many rows per underlying filing inflates totals by that multiple.
    (rows_in,) = con.execute(
        f'SELECT COUNT(*) FROM "{p["table"]}" WHERE "{p["value_col"]}" IS NOT NULL'
    ).fetchone()
    (n_groups,) = con.execute(
        f'SELECT COUNT(*) FROM (SELECT 1 FROM "{p["table"]}" '
        f'WHERE "{p["value_col"]}" IS NOT NULL GROUP BY {gsel})'
    ).fetchone()
    ratio = f", {rows_in / n_groups:.1f} rows/group" if n_groups else ""
    print(f"  grain: {rows_in} input rows across {n_groups} distinct groups{ratio}"
          f" — if the table is finer-grained than one row per filing,"
          f" aggregates are inflated by that multiple")
    if method == "top_n":
        q = f'''
        SELECT {gsel}, SUM("{p['value_col']}") AS total, COUNT(*) AS n,
               MIN(source_group), MIN(native_id)
        FROM "{p['table']}" WHERE "{p['value_col']}" IS NOT NULL
        GROUP BY {gsel} ORDER BY total DESC LIMIT ?
        '''
        rows = con.execute(q, (p.get("n", 25),)).fetchall()
    else:
        raise ValueError(f"unknown outlier method {method}")
    n = 0
    for row in rows:
        labels, total, cnt, sg, nid = row[:-4], row[-4], row[-3], row[-2], row[-1]
        claim = records_show(
            f"aggregate {p['value_col']} of {round(total,2)} across {cnt} records for "
            f"({' / '.join(str(x) for x in labels)}), among the highest in the corpus"
        )
        ev = [{"locator": {"source_group": sg, "native_id": nid},
               "value": f"example record; aggregate={total}, n={cnt}"}]
        emit(con, run_id, det, "outlier", claim, float(total or 0), ev, defam="named_org")
        n += 1
    return n


def t_intermediary(con, det, run_id):
    p = det["params"]
    pats = [re.compile(rx, re.IGNORECASE) for rx in p["patterns"]]
    q = f'''SELECT "{p['name_col']}", source_group, native_id, COUNT(*)
            FROM "{p['table']}" WHERE "{p['name_col']}" IS NOT NULL
            GROUP BY "{p['name_col']}"'''
    n = 0
    for name, sg, nid, cnt in con.execute(q):
        for rx in pats:
            m = rx.search(name)
            if not m:
                continue
            gd = m.groupdict()
            inter = (gd.get("intermediary") or "").strip()
            prin = (gd.get("principal") or "").strip()
            if not prin:
                continue
            claim = records_show(
                f"a disclosure names '{inter or name}' as filer/client while the underlying "
                f"principal is '{prin}' ({cnt} filings)"
            )
            ev = [{"locator": {"source_group": sg, "native_id": nid},
                   "value": name, "parsed": {"intermediary": inter, "principal": prin}}]
            emit(con, run_id, det, "hidden_principal", claim, float(cnt), ev, defam="named_org")
            n += 1
            break
    return n


def t_overlap(con, det, run_id):
    p = det["params"]
    L, R = p["left"], p["right"]
    win = p.get("window_days", 90)
    min_amt = p.get("min_amount", 0)
    amt_col = f'l."{L["amount_col"]}"' if L.get("amount_col") else "NULL"
    q = f'''
    SELECT l."{L['entity_col']}", {amt_col}, l."{L['date_col']}", r."{R['date_col']}",
           l.source_group, l.native_id, r.source_group, r.native_id
    FROM "{L['table']}" l JOIN "{R['table']}" r
      ON l."{L['entity_col']}" = r."{R['entity_col']}"
    WHERE l."{L['date_col']}" IS NOT NULL AND r."{R['date_col']}" IS NOT NULL
      AND ABS(JULIANDAY(l."{L['date_col']}") - JULIANDAY(r."{R['date_col']}")) <= ?
      {"AND " + amt_col + " >= ?" if L.get("amount_col") else ""}
    '''
    args = (win, min_amt) if L.get("amount_col") else (win,)
    n = 0
    for ent, amt, ld, rd, lsg, lid, rsg, rid in con.execute(q, args):
        claim = records_show(
            f"'{ent}' appears in {L['table']} (dated {ld}"
            + (f", amount {amt}" if amt is not None else "")
            + f") and in {R['table']} (dated {rd}) within {win} days"
        )
        ev = [{"locator": {"source_group": lsg, "native_id": lid}, "value": {"date": ld, "amount": amt}},
              {"locator": {"source_group": rsg, "native_id": rid}, "value": {"date": rd}}]
        emit(con, run_id, det, "timing_overlap", claim, float(amt or 1), ev, defam="named_org")
        n += 1
    return n


TEMPLATES = {"contradiction": t_contradiction, "gap": t_gap, "outlier": t_outlier,
             "intermediary": t_intermediary, "overlap": t_overlap}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", default=None)
    ap.add_argument("--run-id", default="manual")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(LEADS_DDL)

    t0 = time.time()
    # pre-flight EVERY selected detector before running ANY: a pack authored
    # against a different corpus must degrade to skips, never a mid-run crash.
    skipped = []
    runnable = []
    for det in cfg["detectors"]:
        if args.only and det["id"] != args.only:
            continue
        fn = TEMPLATES.get(det["template"])
        if fn is None:
            print(f"[SKIPPED] detector {det['id']}: missing template "
                  f"'{det['template']}'")
            skipped.append(det["id"])
            continue
        problems = preflight(con, det)
        if problems:
            print(f"[SKIPPED] detector {det['id']}: missing {'; '.join(problems)}")
            skipped.append(det["id"])
            continue
        runnable.append((det, fn))

    for det, fn in runnable:
        # idempotency: clear this detector's leads from this run id before re-emit
        con.execute("DELETE FROM leads WHERE detector_id=? AND created_run=?",
                    (det["id"], args.run_id))
        try:
            n = fn(con, det, args.run_id)
        except Exception as e:
            con.rollback()  # keep the previous run's leads for this detector
            print(f"[SKIPPED] detector {det['id']}: runtime error ({e})")
            skipped.append(det["id"])
            continue
        con.commit()
        print(f"{det['id']} ({det['template']}): {n} leads ({time.time()-t0:.0f}s)")

    (total,) = con.execute("SELECT COUNT(*) FROM leads").fetchone()
    print(f"total leads in table: {total}")
    if skipped:
        print(f"{len(skipped)} detectors skipped — pack incomplete")
        raise SystemExit(2)


if __name__ == "__main__":
    main()

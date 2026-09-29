#!/usr/bin/env python3
"""lifecycle_gate.py — spec R4 as one shared predicate (TR-199).

SPEC-MODEL-LIFECYCLE.md R4: "No anonymous dates. A lifecycle date without
`lifecycle_source` is invalid" — the provenance law of TR-064 applied to the
lifecycle fields: a claim that cannot name its evidence does not exist.

R4 was already enforced at TWO entry points (router_seed.py lifecycle-overlay
ingest, router_ui_page.registry_edit) but NOT on the direct table writers
(plan sweep, pricing, muse-code, clinepass sync, models.dev sync, provider
import, rank/release backfills, maintain's reprice mirror, the web discount
editor). TR-199 measured the result: 20 models.jsonl rows + 1
temporary_discounts row carried a retirement date with no provenance.

This module is the ONE predicate every write path (and the read-side validate
gate) now calls. Rules:

  * A row that carries a non-empty `valid_to` or `available_from` (or any other
    DATE_KEYS member) MUST carry a non-empty `lifecycle_source`.
  * Blank strings and whitespace are not provenance (fail loud, not fail vague).
  * Tables that model dates differently (benchmarks.valid_from is a sample
    date, not a lifecycle date) are simply not listed in DATE_KEYS and are
    never gated.
  * `gate_data_dir(exempt={'models'})` exists for the READ side only: the
    committed table legitimately holds hundreds of undated rows with
    lifecycle_source NULL, and TR-199 chose stamping the 20 DATED rows over
    rewriting the whole table with a blanket value. A blanket source on every
    undated row would be exactly the invented-provenance R7 forbids, so the
    read gate exempts models and the writers use the strict rule (a writer
    emitting a dated models row without provenance still fails).
"""
import json
import os

DATE_KEYS = ('valid_to', 'available_from')

#: table -> which of DATE_KEYS are lifecycle dates on that table. Absent
#: table = the gate does not apply (the table has no lifecycle dates).
TABLE_KEYS = {
    'models': DATE_KEYS,
    'temporary_discounts': DATE_KEYS,
    'providers': DATE_KEYS,
}


class LifecycleGateError(ValueError):
    """A write was refused: a lifecycle date carries no lifecycle_source."""


def _has_date(row):
    return any(row.get(k) for k in DATE_KEYS)


def has_date(row):
    """Public form of the date test (UI paths pre-check bodies with this)."""
    return _has_date(row)


def extract_source(row):
    """Provenance string of a row, for refusal messages ('<unset>' when absent)."""
    src = row.get('lifecycle_source')
    if src is None or (isinstance(src, str) and not src.strip()):
        return '<unset>'
    return str(src)


def find_offenders(table, rows):
    """[(index, row)] among `rows` whose date has no lifecycle_source."""
    if table not in TABLE_KEYS:
        return []
    return [(i, r) for i, r in enumerate(rows)
            if isinstance(r, dict) and _has_date(r) and not str(r.get('lifecycle_source') or '').strip()]


def describe_offender(table, row, index=None):
    lane = f"{row.get('provider')}/{row.get('model')}" if row.get('model') is not None \
        else str(row.get('id') or row)
    idx = f"[{index}] " if index is not None else ''
    dates = {k: row.get(k) for k in TABLE_KEYS.get(table, DATE_KEYS) if row.get(k)}
    return (f"{idx}{table} {lane}: lifecycle date {dates} without "
            f"lifecycle_source (R4 no anonymous dates — "
            f"spec R4 / TR-199); name the evidence or remove the date")


def gate_rows(table, rows):
    """Raise LifecycleGateError if any row in `rows` carries an anonymous date."""
    bad = find_offenders(table, rows)
    if bad:
        raise LifecycleGateError('; '.join(describe_offender(table, r, i)
                                           for i, r in bad[:5])
                                 + (f' (+{len(bad) - 5} more)' if len(bad) > 5 else ''))
    return rows


def gate_file(path, table=None, exempt=()):
    """Gate one data/tables/*.jsonl by path. Returns offender descriptions
    ([] = clean). `exempt` is a table-name allowlist for READ-side checks."""
    table = table or os.path.splitext(os.path.basename(path))[0]
    if table not in TABLE_KEYS or table in exempt:
        return []
    bad = []
    with open(path, encoding='utf-8') as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if _has_date(row) and not str(row.get('lifecycle_source') or '').strip():
                bad.append(describe_offender(table, row, i))
    return bad


def gate_data_dir(data_dir, exempt=('models',)):
    """Gate every gated table under a data dir. Returns {table: [descriptions]}
    for the tables that offend ([]-valued tables are omitted). Read-side
    default exempts models (see module docstring)."""
    out = {}
    for table in TABLE_KEYS:
        if table in exempt:
            continue
        path = os.path.join(data_dir, f'{table}.jsonl')
        if not os.path.exists(path):
            continue
        bad = gate_file(path, table)
        if bad:
            out[table] = bad
    return out

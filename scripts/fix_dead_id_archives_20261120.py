#!/usr/bin/env python3
"""Dead-id / catalog-absent archives, 2026-11-20 — re-applied onto current main.

WHY THIS SCRIPT EXISTS
----------------------
A 2026-11-20 tick ended without committing and left 12 dirty files in the main
checkout. That snapshot is MIXED: it is built on a pre-72f339f9 base, so most of
it would REVERT landed work (38 neuralwatt lanes re-enabled, 23 market-verify
`note` fields dropped, benchmarks.jsonl a full 11-20 market ingest behind), while
a small part of it is genuinely NEWER than main and exists nowhere else.

This script re-applies ONLY the newer part, onto current main, so the stale
working tree can be discarded without losing evidence. It is a line-wise
rewrite: every line it does not target is copied byte-for-byte.

WHAT IT APPLIES
---------------
1. models.jsonl — 4 dead-id / catalog-absent archives, each with dated
   provenance (chat-probe 404 or live-catalog absence):

     grok-build/grok-build-0.1                  disabled + archive (404 does-not-exist)
     openrouter/x-ai/grok-build-0.1             archive (absent from the 477-id live catalog)
     openrouter/cohere/north-mini-code:free     archive (no cohere/north-mini-* ids remain)
     openrouter/thinkingmachines/inkling-2-small:free
                                                archive (free SKU dropped; paid sibling lives)

2. data/lifecycle.jsonl — the 4 matching overlay rows, so a reseed cannot
   resurrect the lanes (the overlay is the durable half of the archive).

3. data/tables/plan_terms.jsonl — grok-build: `grok-build-0.1` leaves
   `included_models`. REQUIRED, not cosmetic: the plan-inclusion regime lock
   (tests/test_plan_included_lanes_never_disabled.py) fails when a plan lists a
   lane that is archived, so the archive and this edit are one change.

4. The 11-20 probe-tick rows that main lacks (same tick, same evidence class):
   - model_notes.jsonl   xkiro/anthropic/claude-haiku-5.5 entitlement-wall note
   - probe_gaps.jsonl    grok-build/grok-build-0.1 HTTP 404 does-not-exist
   - provider_rules.jsonl xkiro `anthropic-official-tools-403` — the reusable
     rule for the 403 class main only documents per-lane in null_reasons.jsonl
     (anthropic/claude-sonnet-5.5, 2026-11-15). Without the rule the next probe
     of an xkiro anthropic lane reads as lane death.

WHAT IT DELIBERATELY DOES NOT APPLY (stale — main is newer)
-----------------------------------------------------------
  * the 38 neuralwatt `disabled` reverts (main: 49/49 disabled; pinned by
    tests/test_regression.py::test_owner_cancelled_lanes_stay_disabled)
  * the 23 market-verify `note` drops and the price_evidence / vision /
    thinking / cap_* / release_date reverts on 37 openrouter rows
  * benchmarks.jsonl (main carries the 11-20 market ingest: +81 rows, 2935
    rows with newer provenance)
  * the plan_terms.jsonl re-serialisation (compact separators) — only the
    grok-build row is touched here
  * model_catalog.jsonl's models.dev row for openrouter/mistralai/pixtral-large-2502
    (that lane is already archived on main since 11-19; a spec row for a
    carrier-removed SKU adds no routing value and the null-census does not
    judge archived lanes)
  * .gitreins/tasks.yaml (a gitreins task-state flip, not model data)

Idempotent: re-running is a no-op. `--check` reports without writing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "data" / "tables" / "models.jsonl"
LIFECYCLE = ROOT / "data" / "lifecycle.jsonl"
PLAN_TERMS = ROOT / "data" / "tables" / "plan_terms.jsonl"
NOTES = ROOT / "data" / "tables" / "model_notes.jsonl"
PROBE_GAPS = ROOT / "data" / "tables" / "probe_gaps.jsonl"
PROVIDER_RULES = ROOT / "data" / "tables" / "provider_rules.jsonl"

DATE = "2026-11-20"

# (provider, model) -> fields to set on the models.jsonl row.
ARCHIVE_ROWS: dict[tuple[str, str], dict] = {
    ('grok-build', 'grok-build-0.1'): {       'archive': True,
        'disabled': True,
        'disabled_reason': "dead-id 2026-11-20: chat probe HTTP 404 'The model grok-build-0.1 does "
                           "not exist or your team does not have access' (hourly probe 10:01Z; "
                           'scanner DEAD-ID class, live-catalog fetch OK) + id absent from the '
                           "live openrouter catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd "
                           'GET https://openrouter.ai/api/v1/models; no x-ai/grok-build-* ids '
                           'remain) and from xkiro/opencode-go mirrors (11-20 removed lists) — '
                           'removed upstream',
        'lifecycle_checked_at': '2026-11-20',
        'lifecycle_source': 'dead-id 2026-11-20: hourly chat probe 404 does-not-exist + absent '
                            'from openrouter live catalog (public fetch 2026-11-20T10:40Z, 477 '
                            "ids, UA'd GET https://openrouter.ai/api/v1/models) + "
                            'xkiro/opencode-go removed lists — SuperGrok sub provider keeps its '
                            'remaining grok-4.x lanes; the Build agent SKU was pulled upstream',
        'valid_to': '2026-11-20'},
    ('openrouter', 'cohere/north-mini-code:free'): {       'archive': True,
        'lifecycle_checked_at': '2026-11-20',
        'lifecycle_source': 'archived 2026-11-20: :free variant absent from the live openrouter '
                            "catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                            'https://openrouter.ai/api/v1/models) and NO cohere/north-mini-* ids '
                            'remain at all (free and paid both dropped upstream) — XK-404-001-R3 / '
                            '11-06 nemotron-3.5-content-safety:free precedent',
        'valid_to': '2026-11-20'},
    ('openrouter', 'thinkingmachines/inkling-2-small:free'): {       'archive': True,
        'lifecycle_checked_at': '2026-11-20',
        'lifecycle_source': 'archived 2026-11-20: :free variant absent from the live openrouter '
                            "catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                            'https://openrouter.ai/api/v1/models) while the paid sibling '
                            'thinkingmachines/inkling-2-small is STILL listed - the free SKU was '
                            'dropped upstream, the paid lane is untouched - 11-06 nemotron '
                            'precedent',
        'valid_to': '2026-11-20'},
    ('openrouter', 'x-ai/grok-build-0.1'): {       'archive': True,
        'lifecycle_checked_at': '2026-11-20',
        'lifecycle_source': 'archived 2026-11-20: id absent from the live openrouter catalog '
                            "(public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                            'https://openrouter.ai/api/v1/models); NO x-ai/grok-build-* ids remain '
                            'at all — upstream dropped the SKU (was verified live+priced in the '
                            '11-18 market cross-check; one-day flip, market moves in days)',
        'valid_to': '2026-11-20'},
}

# Rows appended to data/lifecycle.jsonl (the durable overlay half of an archive).
LIFECYCLE_ROWS: list[dict] = [
    {   'archive': True,
    'disabled': True,
    'disabled_reason': "dead-id 2026-11-20: chat probe HTTP 404 'The model grok-build-0.1 does not "
                       "exist or your team does not have access' (hourly probe 10:01Z; scanner "
                       'DEAD-ID class, live-catalog fetch OK) + id absent from the live openrouter '
                       "catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                       'https://openrouter.ai/api/v1/models; no x-ai/grok-build-* ids remain) and '
                       'from xkiro/opencode-go mirrors (11-20 removed lists) — removed upstream',
    'lifecycle_checked_at': '2026-11-20',
    'lifecycle_source': 'dead-id 2026-11-20: hourly chat probe 404 does-not-exist + absent from '
                        "openrouter live catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd "
                        'GET https://openrouter.ai/api/v1/models) + xkiro/opencode-go removed '
                        'lists — SuperGrok sub provider keeps its remaining grok-4.x lanes; the '
                        'Build agent SKU was pulled upstream',
    'model': 'grok-build-0.1',
    'provider': 'grok-build',
    'valid_to': '2026-11-20'},
    {   'archive': True,
    'lifecycle_checked_at': '2026-11-20',
    'lifecycle_source': 'archived 2026-11-20: id absent from the live openrouter catalog (public '
                        "fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                        'https://openrouter.ai/api/v1/models); NO x-ai/grok-build-* ids remain at '
                        'all — upstream dropped the SKU (was verified live+priced in the 11-18 '
                        'market cross-check; one-day flip, market moves in days)',
    'model': 'x-ai/grok-build-0.1',
    'provider': 'openrouter',
    'valid_to': '2026-11-20'},
    {   'archive': True,
    'lifecycle_checked_at': '2026-11-20',
    'lifecycle_source': 'archived 2026-11-20: :free variant absent from the live openrouter '
                        "catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                        'https://openrouter.ai/api/v1/models) and NO cohere/north-mini-* ids '
                        'remain at all (free and paid both dropped upstream) — XK-404-001-R3 / '
                        '11-06 nemotron-3.5-content-safety:free precedent',
    'model': 'cohere/north-mini-code:free',
    'provider': 'openrouter',
    'valid_to': '2026-11-20'},
    {   'archive': True,
    'lifecycle_checked_at': '2026-11-20',
    'lifecycle_source': 'archived 2026-11-20: :free variant absent from the live openrouter '
                        "catalog (public fetch 2026-11-20T10:40Z, 477 ids, UA'd GET "
                        'https://openrouter.ai/api/v1/models) while the paid sibling '
                        'thinkingmachines/inkling-2-small is STILL listed - the free SKU was '
                        'dropped upstream, the paid lane is untouched - 11-06 nemotron precedent',
    'model': 'thinkingmachines/inkling-2-small:free',
    'provider': 'openrouter',
    'valid_to': '2026-11-20'},
]

# Single-line appends: (path, row, key-columns) — the key columns make the append idempotent.
APPENDS: list[tuple[Path, dict, tuple]] = [
    (NOTES, {   'model': 'anthropic/claude-haiku-5.5',
    'note': "entitlement wall 2026-11-20: hourly chat probe HTTP 403 'This model accepts requests "
            "only from Anthropic's official tools — Claude Code (terminal and IDE extensions), "
            'Claude Desktop and Cowork — and from xKCode when you sign in to it with your xKiro '
            'account. Requests from other third-party tools, apps or your own code are not '
            "supported.' — raw-API calls from our fleet are unsupported by policy; id remains in "
            'the carrier catalog (access restriction, not removal). The probe lanes list covers '
            'haiku only; sibling anthropic lanes may carry the same policy — unprobed today, no '
            'verdict.',
    'provider': 'xkiro',
    'source': 'probe health.jsonl 2026-11-20T10:01:06Z',
    'valid_from': '2026-11-20'}, ('provider', 'model')),
    (PROBE_GAPS, {   'action': 'id-absent-from-provider-catalog (disable or replace — agent)',
    'candidate': None,
    'error': 'HTTP 404: {"code":"not-found","error":"The model grok-build-0.1 does not exist or '
             'your team 7a182ced-188b-4abf-be61-28425af04a28 does not have access to it. If you '
             'believe this is a mistake, please contact support and quote your team ID and the '
             'model name."}',
    'model': 'grok-build-0.1',
    'provider': 'grok-build',
    'source': 'auto',
    'ts': '2026-11-20T10:01:06+00:00'}, ('provider', 'model')),
    (PROVIDER_RULES, {   'detail': 'HTTP 403 on xkiro anthropic lanes: Anthropic policy serves these ids ONLY to '
              'official Anthropic tools (Claude Code/Desktop/Cowork) and xKCode signed in with an '
              'xKiro account — raw-API calls from third-party agents (our fleet) are unsupported '
              'BY POLICY, not by outage. Not lane death (id remains in the carrier catalog), not '
              'auth. Probe target, never DEAD-ID; lane is unrouteable for our seat — treat like a '
              'permanent entitlement gate.',
    'explains_probe': ['accepts requests only from Anthropic.s official tools'],
    'provider': 'xkiro',
    'rule': 'anthropic-official-tools-403',
    'source': 'probe health.jsonl 2026-11-20T10:01:06Z + model_note '
              'xkiro/anthropic-claude-haiku-5.5',
    'valid_from': '2026-11-20',
    'valid_to': None}, ('provider', 'rule')),
]

# plan_terms: the dead SKU must leave the plan's included list, or the
# plan-inclusion regime lock fails on the archive.
PLAN_TERMS_PROVIDER = "grok-build"
PLAN_TERMS_DROP = "grok-build-0.1"
PLAN_TERMS_NOTE_SUFFIX = (
    ' | 2026-11-20: grok-build-0.1 dropped from included_models — SKU removed upstream (chat probe 404 does-not-exist, hourly probe 10:01Z; absent from live openrouter catalog 477 ids and xkiro/opencode-go mirrors); archive tripped test_plan_included_lanes_never_disabled. Re-add only if the SKU returns upstream.'
)


def _read(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _identity(row: dict, keys: tuple) -> tuple:
    return tuple(row.get(k) for k in keys)


def apply_models(check: bool) -> int:
    lines = _read(MODELS)
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    touched = 0
    for line in lines:
        row = json.loads(line)
        key = (row.get("provider"), row.get("model"))
        patch = ARCHIVE_ROWS.get(key)
        if patch is None:
            out.append(line)
            continue
        seen.add(key)
        before = {f: row.get(f) for f in patch}
        row.update(patch)
        if before != {f: row.get(f) for f in patch}:
            touched += 1
        out.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    missing = sorted(set(ARCHIVE_ROWS) - seen)
    if missing:
        print(f"REFUSING: archive target(s) absent from models.jsonl: {missing}", file=sys.stderr)
        return 2
    print(f"models.jsonl: {touched} row(s) changed, {len(ARCHIVE_ROWS)} target(s) present")
    if not check and touched:
        _write(MODELS, out)
    return 0


def apply_lifecycle(check: bool) -> int:
    lines = _read(LIFECYCLE)
    have = set()
    for line in lines:
        row = json.loads(line)
        have.add((row.get("provider"), row.get("model"), row.get("valid_to")))
    add = [r for r in LIFECYCLE_ROWS
           if (r.get("provider"), r.get("model"), r.get("valid_to")) not in have]
    print(f"lifecycle.jsonl: {len(add)} overlay row(s) to append")
    if not check and add:
        _write(LIFECYCLE, lines + [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in add])
    return 0


def apply_plan_terms(check: bool) -> int:
    lines = _read(PLAN_TERMS)
    out: list[str] = []
    touched = 0
    for line in lines:
        row = json.loads(line)
        if row.get("provider") != PLAN_TERMS_PROVIDER:
            out.append(line)
            continue
        included = row.get("included_models")
        if not isinstance(included, list):
            print("REFUSING: grok-build plan_terms row has no included_models list", file=sys.stderr)
            return 2
        if PLAN_TERMS_DROP not in included:
            out.append(line)  # already applied
            continue
        row["included_models"] = [m for m in included if m != PLAN_TERMS_DROP]
        note = row.get("note") or ""
        if PLAN_TERMS_NOTE_SUFFIX not in note:
            row["note"] = note + PLAN_TERMS_NOTE_SUFFIX
        out.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        touched += 1
    if touched > 1:
        print(f"REFUSING: expected at most 1 grok-build plan_terms edit, made {touched}",
              file=sys.stderr)
        return 2
    if touched == 0:
        print(f"plan_terms.jsonl: already applied ({PLAN_TERMS_DROP!r} not in included_models)")
    else:
        print(f"plan_terms.jsonl: dropped {PLAN_TERMS_DROP!r} from included_models")
    if not check and touched:
        _write(PLAN_TERMS, out)
    return 0


def apply_appends(check: bool) -> int:
    by_path: dict[Path, list[tuple[dict, tuple]]] = {}
    for path, row, keys in APPENDS:
        by_path.setdefault(path, []).append((row, keys))
    for path, items in by_path.items():
        lines = _read(path)
        existing = [json.loads(line) for line in lines]
        add = []
        for row, keys in items:
            ident = _identity(row, keys)
            if any(_identity(r, keys) == ident for r in existing):
                continue
            add.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        print(f"{path.name}: {len(add)} row(s) to append")
        if not check and add:
            _write(path, lines + add)
    return 0


def main(argv: list[str]) -> int:
    check = "--check" in argv
    rc = 0
    for fn in (apply_models, apply_lifecycle, apply_plan_terms, apply_appends):
        rc = fn(check) or rc
        if rc:
            return rc
    print("OK" + (" (check only, nothing written)" if check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""TR-203 regression battery — policy-gate-missing-row, the self-explaining
exclusion for a provider absent from quota-state.json's providers section.

History (the defect this pins):
  - Pre-TR-203 the quota gate was fail-closed on absence (absent != open), but
    the exclusion prose was generic and the mirror lanes (xkiro-2,
    opencode-go-2, commandcode-2) had no rows — they silently vanished from
    every chain.
  - ccce1f2 + 443b746 "fixed" that by treating an ABSENT provider as OPEN
    (pass-through). That traded a silent exclusion for a silent ROUTE: fresh,
    typo'd, or deliberately-unlisted providers all became routable and the
    providers section's silence carried policy weight. It also broke the
    pinned absent-state fail-closed contract
    (tests/test_regression.py::test_absent_state_is_fail_closed_all_excluded).
  - TR-203 (this file): absent => EXCLUDED with the machine-code
    `policy-gate-missing-row` and a reason naming quota-state.json, in BOTH
    the primary chain and the fallback-lane path. Coverage of the file itself
    is a separate gate (tests/test_policy_gate_coverage.py + the audit
    script); intentional exceptions are declared in quota-state.json's
    top-level `intentionally_ungated` list (still excluded — the mark only
    documents the silence for the audit).

Hermetic: a synthetic registry + a temp state dir (monkeypatched MR) — no
dependency on the live data/tables or the machine's gate state.
"""
import datetime
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_spawn  # noqa: E402

CODE = "policy-gate-missing-row"


# --------------------------------------------------------------- fixtures ---
# Lane layout (mirrors tests/test_quota_gate.py's split, so the fallback
# differential is controlled):
#   PRIMARY (clears reasoning>=3)   zai-glm $0.10 < deepseek $0.20
#   FALLBACK-ONLY (tier 2)          ghost-payg (order 1), deepseek-foreman
#                                   (order 2) — reachable only through the
#                                   always-run lane list.
TABLES = {
    'models': [
        {'provider': 'zai-glm', 'model': 'glm-5.3-flash', 'normalized_price': 0.1,
         'plan_tier': 0, 'context_limit': 400000, 'data_class': 'public'},
        {'provider': 'deepseek', 'model': 'deepseek-flash', 'normalized_price': 0.2,
         'plan_tier': 1, 'context_limit': 400000, 'data_class': 'public'},
        {'provider': 'ghost-payg', 'model': 'ghost-lane', 'normalized_price': 0.3,
         'plan_tier': 2, 'context_limit': 400000, 'data_class': 'public'},
        {'provider': 'deepseek-foreman', 'model': 'deepseek-flash-fmn',
         'normalized_price': 0.35, 'plan_tier': 2, 'context_limit': 400000,
         'data_class': 'public'},
    ],
    'model_tier': [
        {'model': 'glm-5.3-flash', 'category': 'reasoning', 'tier': 4},
        {'model': 'deepseek-flash', 'category': 'reasoning', 'tier': 3},
        {'model': 'ghost-lane', 'category': 'reasoning', 'tier': 2},
        {'model': 'deepseek-flash-fmn', 'category': 'reasoning', 'tier': 2},
    ],
    'providers': [{'id': 'zai-glm'}, {'id': 'deepseek'},
                  {'id': 'ghost-payg'}, {'id': 'deepseek-foreman'}],
    'projects': [{'id': 'demo', 'profile': 'P0_DEMO'}],
    'task_profiles': [{'id': 'P0_DEMO', 'title': 'demo profile'}],
    'task_profile_requirements': [
        {'task_id': 'P0_DEMO', 'category': 'reasoning', 'level': 3}],
    'category_levels': [{'category': 'reasoning'}],
    'level_defs': [{'level': -5}, {'level': 5}],
    'fallback_lanes': [
        {'provider': 'ghost-payg', 'model': 'ghost-lane', 'order': 1},
        {'provider': 'deepseek-foreman', 'model': 'deepseek-flash-fmn',
         'order': 2},
    ],
}

LISTED = ('zai-glm', 'deepseek', 'deepseek-foreman')


def _write_registry(tmp_path, tables=None):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables or TABLES}))
    return str(reg)


def _state_dir(tmp_path, providers, ungated=None, health=None):
    """State dir whose providers section holds EXACTLY `providers` — absence
    of a fixture provider from this dict is the condition under test."""
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    doc = {'updated': 'test', 'providers': dict(providers)}
    if ungated is not None:
        doc['intentionally_ungated'] = list(ungated)
    (d / 'quota-state.json').write_text(json.dumps(doc))
    if health is not None:
        (d / 'health-state.json').write_text(json.dumps({'providers': health}))
    return str(d)


def _resolve(monkeypatch, tmp_path, state_dir, health=None):
    monkeypatch.setattr(router_spawn, 'REGISTRY', _write_registry(tmp_path))
    monkeypatch.setattr(router_spawn, 'MR', state_dir)
    monkeypatch.delenv('LEDGER_FILE', raising=False)
    return router_spawn.resolve(project='demo', use_health=True, sort='price')


def _exclusion(r, provider):
    hits = [e for e in r['exclusions'] if e.get('provider') == provider]
    return hits[0] if hits else None


# ------------------------------------------------------- primary chain -----

def test_absent_provider_excluded_with_code_and_file_reason(monkeypatch, tmp_path):
    """AC1 primary chain: zai-glm has NO quota-state row -> excluded with the
    policy-gate-missing-row code and a reason naming quota-state.json; the
    head advances to the next listed hop."""
    state = _state_dir(tmp_path, {p: {'status': 'open'} for p in
                                  ('deepseek',)})
    r = _resolve(monkeypatch, tmp_path, state)
    assert r['head']['provider'] == 'deepseek'  # the listed survivor
    hit = _exclusion(r, 'zai-glm')
    assert hit, 'absent provider must be EXCLUDED, not passed through'
    why = '; '.join(hit['why'])
    assert why.startswith(f'{CODE}: ')
    assert 'quota-state.json' in why
    assert hit['codes'] == [CODE]


def test_listed_open_control_passes_the_gate(monkeypatch, tmp_path):
    """Control for the differential: the SAME lane with a listed open row is
    routable — the exclusion is caused by the missing row, nothing else."""
    state = _state_dir(tmp_path, {p: {'status': 'open'} for p in
                                  ('zai-glm', 'deepseek')})
    r = _resolve(monkeypatch, tmp_path, state)
    assert r['head']['provider'] == 'zai-glm'   # cheapest, now routable
    assert _exclusion(r, 'zai-glm') is None


def test_listed_gated_still_reports_quota_gated(monkeypatch, tmp_path):
    """The pre-existing prose/code for a PRESENT non-open row is untouched."""
    state = _state_dir(tmp_path, {'zai-glm': {'status': 'gated',
                                              'reason': 'policy hold'},
                                  'deepseek': {'status': 'open'}})
    r = _resolve(monkeypatch, tmp_path, state)
    hit = _exclusion(r, 'zai-glm')
    assert hit
    assert '; '.join(hit['why']).startswith('quota GATED: policy hold')
    assert hit['codes'] == ['quota-gated']


def test_intentionally_ungated_still_excluded_but_self_explaining(monkeypatch, tmp_path):
    """The audit's exemption list does NOT open the lane: an intentionally
    ungated provider is excluded with the same code, and the reason says so."""
    state = _state_dir(tmp_path, {'deepseek': {'status': 'open'}},
                       ungated=['zai-glm'])
    r = _resolve(monkeypatch, tmp_path, state)
    hit = _exclusion(r, 'zai-glm')
    assert hit
    why = '; '.join(hit['why'])
    assert why.startswith(f'{CODE}: ')
    assert 'intentionally ungated' in why
    assert hit['codes'] == [CODE]
    assert r['head']['provider'] == 'deepseek'


def test_exclusion_code_is_in_the_machine_vocabulary():
    """The code is registered (TR-142 vocabulary) and maps from the prose —
    a dashboard can bucket it without regexing."""
    assert CODE in router_spawn.EXCLUSION_REASON_CODES
    assert router_spawn.exclusion_codes(
        [f'{CODE}: provider not listed in quota-state.json']) == [CODE]


# ----------------------------------------------------- fallback-lane path ---

def test_fallback_skips_lane_whose_provider_has_no_row(monkeypatch, tmp_path):
    """AC1 fallback chain: with every primary hop DOWN, the always-run list
    decides the head. ghost-payg (order 1) has NO quota-state row -> skipped;
    deepseek-foreman (order 2, listed open) serves. The control arm proves the
    missing row — not the fallback machinery — moved the head."""
    health = {'zai-glm': {'status': 'DOWN'}, 'deepseek': {'status': 'DOWN'}}

    control_state = _state_dir(
        tmp_path, {p: {'status': 'open'} for p in ('zai-glm', 'deepseek',
                                                   'ghost-payg',
                                                   'deepseek-foreman')},
        health=health)
    control = _resolve(monkeypatch, tmp_path, control_state)
    assert control['degraded_fallback'] is True
    assert control['head']['provider'] == 'ghost-payg'   # fallback order 1

    missing_state = _state_dir(
        tmp_path, {'deepseek-foreman': {'status': 'open'}},  # ghost-payg ABSENT
        health=health)
    r = _resolve(monkeypatch, tmp_path, missing_state)
    assert r['degraded_fallback'] is True
    assert r['head']['provider'] == 'deepseek-foreman'   # order 2 serves
    assert 'ghost-payg' not in [c['provider'] for c in r['chain']]

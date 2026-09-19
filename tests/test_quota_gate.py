"""TR-060 regression battery — plan-window quota gates for exhausted lanes.

The defect (live audit 2026-09-17): openai-codex answered 429 'usage limit has
been reached' (plan_type=prolite, resets_at epoch) and zai-glm answered
'Weekly/Monthly Limit Exhausted. Your limit will reset at <ts>'. The router's
only automatic reaction was an api_down circuit that cools in 30 min while the
plan window lasts hours/days, so the lane was re-picked on the next tick, it
re-failed, and every affected session fell back to the PAYG deepseek default
(276/276 fallback-billed sessions in the window).

Covered here:
  - a GATED provider (reset_at in the FUTURE) is excluded from the chain, named
    in exclusions[] with the exact gate_reason, and the head ADVANCES;
  - reset_at in the PAST auto-clears (lane back in the chain, no edit needed);
  - a nested providers.<p>.quota_exhausted entry gates too;
  - a gated entry with no / unparseable reset_at stays GATED (never a silent
    open) and says so in the reason;
  - an explicit 'open'/'cleared' status is not gated;
  - the FALLBACK-LANE path honors the same gate (a quota-dead provider must not
    come back as the fallback head) — with a control arm proving the gate is
    what moved the head;
  - gates_loaded is untouched (TR-025 pins that dict exactly) and the new
    quota_gates block reports the truth;
  - `router quota set/clear/status` round-trips through the real script,
    preserves unrelated keys, refuses a malformed reset_at (exit 2, no write),
    and resolves the SAME state file the spawn path reads.

Hermetic: a synthetic registry + a temp state dir (monkeypatched MR) — no
dependency on the live data/tables or the machine's gate state.
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

from conftest import SEED_TIMEOUT  # noqa: E402
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_spawn  # noqa: E402
import router_quota  # noqa: E402

UTC = datetime.timezone.utc


def _utc(delta_hours):
    """ISO-8601 UTC stamp `delta_hours` from now (negative = the past)."""
    return (datetime.datetime.now(UTC)
            + datetime.timedelta(hours=delta_hours)).isoformat(timespec='seconds')


# --------------------------------------------------------------- fixtures ---
# Lane layout (deliberate):
#   PRIMARY (clears reasoning>=3)   zai-glm $0.10 < openai-codex $0.40 (tier 0)
#                                   < deepseek $0.20 (tier 1)   <- next hop
#   FALLBACK-ONLY (tier 2: fails the profile requirement, so it can only be
#   reached through the always-run lane list)  deepseek-foreman, crof
# That split is what makes the fallback test a CONTROLLED differential: the
# already-run lane it uses cannot be in the primary chain, so gating it is the
# only variable between the control and gated arms.
TABLES = {
    'models': [
        {'provider': 'zai-glm', 'model': 'glm-5.3-flash', 'normalized_price': 0.1,
         'plan_tier': 0, 'context_limit': 400000, 'data_class': 'public'},
        {'provider': 'openai-codex', 'model': 'gpt-5.6-sol',
         'normalized_price': 0.4, 'plan_tier': 0, 'context_limit': 400000,
         'data_class': 'public'},
        {'provider': 'deepseek', 'model': 'deepseek-v4-flash',
         'normalized_price': 0.2, 'plan_tier': 1, 'context_limit': 400000,
         'data_class': 'public'},
        {'provider': 'deepseek-foreman', 'model': 'deepseek-v4-flash-fmn',
         'normalized_price': 0.3, 'plan_tier': 2, 'context_limit': 400000,
         'data_class': 'public'},
        {'provider': 'crof', 'model': 'crof-a', 'normalized_price': 0.5,
         'plan_tier': 2, 'context_limit': 400000, 'data_class': 'public'},
    ],
    'model_tier': [
        {'model': 'glm-5.3-flash', 'category': 'reasoning', 'tier': 4},
        {'model': 'gpt-5.6-sol', 'category': 'reasoning', 'tier': 4},
        {'model': 'deepseek-v4-flash', 'category': 'reasoning', 'tier': 3},
        {'model': 'deepseek-v4-flash-fmn', 'category': 'reasoning', 'tier': 2},
        {'model': 'crof-a', 'category': 'reasoning', 'tier': 2},
    ],
    'providers': [{'id': 'zai-glm'}, {'id': 'openai-codex'},
                  {'id': 'deepseek'}, {'id': 'deepseek-foreman'},
                  {'id': 'crof'}],
    'projects': [{'id': 'demo', 'profile': 'P0_DEMO'}],
    'task_profiles': [{'id': 'P0_DEMO', 'title': 'demo profile'}],
    'task_profile_requirements': [
        {'task_id': 'P0_DEMO', 'category': 'reasoning', 'level': 3}],
    'category_levels': [{'category': 'reasoning'}],
    'level_defs': [{'level': -5}, {'level': 5}],
    'fallback_lanes': [
        {'provider': 'deepseek-foreman', 'model': 'deepseek-v4-flash-fmn',
         'order': 1},
        {'provider': 'crof', 'model': 'crof-a', 'order': 2},
    ],
}


def _write_registry(tmp_path, tables=None):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables or TABLES}))
    return str(reg)


def _state_dir(tmp_path, quota=None, health=None, providers=None):
    """State dir where NOTHING is gated except what the test passes in.

    Every fixture provider is written OPEN by default: the resolver's quota
    policy is FAIL-CLOSED on a missing provider entry (`status` absent !=
    'open'), so a bare {'providers': {}} would gate the whole registry and
    every assertion would be vacuous. Gate state comes in through
    quota_exhausted (top-level or nested under a provider entry).
    """
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    extra = dict(quota or {})
    extra.pop('providers', None)
    doc = {'updated': 'test',
           'providers': dict(providers if providers is not None
                             else OPEN_PROVIDERS)}
    doc.update(extra)
    (d / 'quota-state.json').write_text(json.dumps(doc))
    if health is not None:
        (d / 'health-state.json').write_text(json.dumps({'providers': health}))
    return str(d)


OPEN_PROVIDERS = {p: {'status': 'open'} for p in
                  ('zai-glm', 'openai-codex', 'deepseek', 'deepseek-foreman',
                   'crof')}


def _resolve(monkeypatch, tmp_path, state_dir, tables=None, use_health=True,
             project='demo'):
    monkeypatch.setattr(router_spawn, 'REGISTRY', _write_registry(tmp_path, tables))
    monkeypatch.setattr(router_spawn, 'MR', state_dir)
    monkeypatch.delenv('LEDGER_FILE', raising=False)
    return router_spawn.resolve(project=project, use_health=use_health, sort='price')


def _gate(provider, reset_at, reason='HTTP 429: plan limit reached',
          status='gated', **extra):
    entry = {'status': status, 'reason': reason, 'reset_at': reset_at,
             'detected_at': _utc(-1)}
    entry.update(extra)
    return {provider: entry}


def _find(rows, provider, model):
    return [r for r in rows if r.get('provider') == provider
            and r.get('model') == model]


def _why(r, provider, model):
    hits = _find(r['exclusions'], provider, model)
    assert hits, f'{provider}/{model} is not excluded: {r["exclusions"]}'
    return '; '.join(hits[0]['why'])


# ------------------------------------------------------ loader unit tests ---

def test_gate_active_only_while_reset_is_in_the_future():
    doc = {'quota_exhausted': {
        'zai-glm': {'status': 'gated', 'reason': 'weekly limit', 'reset_at': _utc(6)},
        'openai-codex': {'status': 'gated', 'reason': 'plan limit', 'reset_at': _utc(-6)},
    }}
    gates = router_spawn.load_quota_gates(doc)
    assert gates['zai-glm']['active'] is True
    assert gates['zai-glm']['expired'] is False
    assert gates['openai-codex']['active'] is False
    assert gates['openai-codex']['expired'] is True


def test_gate_reason_is_the_documented_shape():
    gates = router_spawn.load_quota_gates(
        {'quota_exhausted': _gate('zai-glm', '2026-09-20T04:07:32+00:00',
                                  reason='Weekly/Monthly Limit Exhausted')},
        now='2026-09-19T10:00:00+00:00')
    assert gates['zai-glm']['gate_reason'] == (
        'quota exhausted: Weekly/Monthly Limit Exhausted '
        '(resets 2026-09-20T04:07:32+00:00)')


def test_gated_entry_without_reset_stays_gated():
    """No reset time is NOT 'open': the documented fallback is gated until an
    operator clears it (the router must never silently re-pick a dead lane)."""
    gates = router_spawn.load_quota_gates(
        {'quota_exhausted': {'zai-glm': {'status': 'gated', 'reason': 'no reset in body'}}})
    assert gates['zai-glm']['active'] is True
    assert gates['zai-glm']['gate_reason'].endswith('(no reset time)')


def test_unparseable_reset_stays_gated_and_echoes_the_value():
    gates = router_spawn.load_quota_gates(
        {'quota_exhausted': {'zai-glm': {'status': 'gated', 'reason': 'x',
                                         'reset_at': 'soon-ish'}}})
    assert gates['zai-glm']['active'] is True
    assert gates['zai-glm']['gate_reason'].endswith('(resets soon-ish)')


@pytest.mark.parametrize('status', ['open', 'cleared', 'expired', 'OPEN'])
def test_explicit_open_status_is_not_gated(status):
    gates = router_spawn.load_quota_gates(
        {'quota_exhausted': {'zai-glm': {'status': status, 'reset_at': _utc(12)}}})
    assert gates['zai-glm']['active'] is False
    assert gates['zai-glm']['expired'] is False


def test_nested_provider_entry_is_honored():
    """The other spelling the brief allows: providers.<p>.quota_exhausted."""
    gates = router_spawn.load_quota_gates({'providers': {'zai-glm': {
        'status': 'open', 'quota_exhausted': {'status': 'gated', 'reason': 'nested',
                                             'reset_at': _utc(3)}}}})
    assert gates['zai-glm']['active'] is True
    assert 'nested' in gates['zai-glm']['gate_reason']


def test_non_dict_quota_state_never_raises():
    for doc in (None, [], 'garbage', {'quota_exhausted': []},
                {'quota_exhausted': {'zai-glm': 'not-a-dict'}}):
        assert router_spawn.load_quota_gates(doc) == {}


# --------------------------------------------------- resolve() gate tests ---

def test_gated_provider_excluded_from_chain_with_reason(monkeypatch, tmp_path):
    """AC1 + AC2: the exhausted lane is excluded with the gate_reason and the
    head ADVANCES to the next eligible lane (no oscillation)."""
    reset = _utc(20)
    state = _state_dir(tmp_path, quota={
        'updated': 'test', 'providers': {},
        'quota_exhausted': _gate('zai-glm', reset,
                                 reason="Weekly/Monthly Limit Exhausted. "
                                        "Your limit will reset at 2026-09-20 04:07:32")})
    r = _resolve(monkeypatch, tmp_path, state)
    assert r['chain'][0]['provider'] == 'openai-codex'   # head advanced
    assert r['head']['provider'] == 'openai-codex'
    assert not _find(r['chain'], 'zai-glm', 'glm-5.3-flash')
    reason = _why(r, 'zai-glm', 'glm-5.3-flash')
    assert reason == ('quota exhausted: Weekly/Monthly Limit Exhausted. '
                      'Your limit will reset at 2026-09-20 04:07:32 '
                      f'(resets {reset})')
    assert any('quota exhausted:' in g and 'zai-glm' in g
               for g in r['gate_reasons'])
    assert [g['provider'] for g in r['quota_gates']['gated']] == ['zai-glm']
    assert r['quota_gates']['source'].endswith('quota-state.json')


def test_all_gated_head_advances_to_the_payg_tail(monkeypatch, tmp_path):
    """Both plan-exhausted lanes gated → the head is the next eligible lane,
    not a re-pick of a dead lane."""
    quota = {'updated': 'test', 'providers': {}, 'quota_exhausted': {}}
    quota['quota_exhausted'].update(_gate('zai-glm', _utc(20), reason='weekly'))
    quota['quota_exhausted'].update(_gate('openai-codex', _utc(20), reason='plan'))
    r = _resolve(monkeypatch, tmp_path, _state_dir(tmp_path, quota=quota))
    assert r['head']['provider'] == 'deepseek'
    assert sorted(g['provider'] for g in r['quota_gates']['gated']) == \
        ['openai-codex', 'zai-glm']


def test_past_reset_at_auto_clears_the_lane(monkeypatch, tmp_path):
    """AC1 (the other half): once reset_at passes the lane is eligible again
    with NO edit to the state file — and the stale entry is still reported."""
    quota = {'updated': 'test', 'providers': {},
             'quota_exhausted': _gate('zai-glm', _utc(-2), reason='weekly')}
    r = _resolve(monkeypatch, tmp_path, _state_dir(tmp_path, quota=quota))
    assert r['head']['provider'] == 'zai-glm'
    assert 'zai-glm' not in [e['provider'] for e in r['exclusions']]
    assert r['quota_gates']['gated'] == []
    assert [g['provider'] for g in r['quota_gates']['expired']] == ['zai-glm']


def test_set_then_expire_transitions_the_same_entry(monkeypatch, tmp_path):
    """One entry, two verdicts: future reset gates, past reset clears. This is
    the whole auto-clear contract in a single state file."""
    path = tmp_path / 'state'
    path.mkdir()
    qpath = path / 'quota-state.json'
    qpath.write_text(json.dumps({'updated': 'test',
                                 'providers': dict(OPEN_PROVIDERS),
                                 'quota_exhausted': _gate('zai-glm', _utc(4))}))
    gated = _resolve(monkeypatch, tmp_path, str(path))
    assert gated['head']['provider'] == 'openai-codex'
    qpath.write_text(json.dumps({'updated': 'test',
                                 'providers': dict(OPEN_PROVIDERS),
                                 'quota_exhausted': _gate('zai-glm', _utc(-4))}))
    cleared = _resolve(monkeypatch, tmp_path, str(path))
    assert cleared['head']['provider'] == 'zai-glm'


def test_nested_gate_excludes_in_resolve(monkeypatch, tmp_path):
    state = _state_dir(tmp_path, providers={
        'zai-glm': {'status': 'open',
                    'quota_exhausted': {'status': 'gated', 'reason': 'nested weekly',
                                        'reset_at': _utc(9)}},
        'openai-codex': {'status': 'open'},
        'deepseek': {'status': 'open'}})
    r = _resolve(monkeypatch, tmp_path, state)
    assert r['head']['provider'] == 'openai-codex'
    assert 'nested weekly' in _why(r, 'zai-glm', 'glm-5.3-flash')


def test_malformed_section_is_fail_open(monkeypatch, tmp_path):
    """A broken quota_exhausted section must not gate anything and must not
    break the resolve (fail-open is sacred — AGENTS.md)."""
    state = _state_dir(tmp_path, quota={'updated': 'test', 'providers': {},
                                        'quota_exhausted': 'oops'})
    r = _resolve(monkeypatch, tmp_path, state)
    assert 'error' not in r
    assert r['head']['provider'] == 'zai-glm'
    assert r['quota_gates']['gated'] == []


def test_gates_loaded_dict_is_unchanged(monkeypatch, tmp_path):
    """TR-025 pins gates_loaded exactly — the new visibility lives in its own
    top-level key so that contract (and its test) stays intact."""
    r = _resolve(monkeypatch, tmp_path, _state_dir(tmp_path))
    assert set(r['gates_loaded']) == {'health', 'circuit', 'quota', 'ledger',
                                      'ledger_rows'}
    assert r['gates_loaded']['quota'] is True
    assert set(r['quota_gates']) == {'gated', 'expired', 'source'}


# ------------------------------------------------- fallback-lane behavior ---

def test_fallback_lane_honors_the_quota_gate(monkeypatch, tmp_path):
    """A quota-dead provider must not reappear as the FALLBACK head.

    The primary chain is emptied by health (all three requirement-clearing
    providers DOWN), so the always-run list decides the head: deepseek-foreman
    (order 1, tier 2 = primary-ineligible by design) then crof (order 2).
    The control arm proves the gate — not the health state — moved the head.
    """
    health = {'zai-glm': {'status': 'DOWN'}, 'openai-codex': {'status': 'DOWN'},
              'deepseek': {'status': 'DOWN'}}
    control_state = _state_dir(tmp_path, quota={'updated': 't', 'providers': {}},
                               health=health)
    control = _resolve(monkeypatch, tmp_path, control_state)
    assert control['head']['provider'] == 'deepseek-foreman'   # fallback order 1
    assert control['degraded_fallback'] is True

    gated_state = _state_dir(tmp_path, quota={
        'updated': 't', 'providers': {},
        'quota_exhausted': _gate('deepseek-foreman', _utc(15),
                                 reason='PAYG foreman lane plan-gated')},
        health=health)
    gated = _resolve(monkeypatch, tmp_path, gated_state)
    assert gated['head']['provider'] == 'crof'                 # next always-run lane
    assert gated['degraded_fallback'] is True
    assert 'deepseek-foreman' not in [c['provider'] for c in gated['chain']]


def test_fallback_lane_returns_when_the_gate_expires(monkeypatch, tmp_path):
    health = {'zai-glm': {'status': 'DOWN'}, 'openai-codex': {'status': 'DOWN'},
              'deepseek': {'status': 'DOWN'}}
    state = _state_dir(tmp_path, quota={
        'updated': 't', 'providers': {},
        'quota_exhausted': _gate('deepseek-foreman', _utc(-1), reason='stale gate')},
        health=health)
    r = _resolve(monkeypatch, tmp_path, state)
    assert r['head']['provider'] == 'deepseek-foreman'


# ------------------------------------------------------------ writer CLI ----

def _quota(*args, timeout=60):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'router_quota.py'),
                           *args], capture_output=True, text=True, timeout=timeout)


def test_set_writes_the_entry_and_preserves_unrelated_keys(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    (d / 'quota-state.json').write_text(json.dumps({
        'updated': '2026-09-16', 'note': 'hand-written policy file',
        'providers': {'grok-build': {'status': 'gated', 'reason': 'pool unpublished'}},
        'diversity': {'max_consecutive_per_provider': 2},
        'models': {'zai-glm/glm-5.3-flash': {'concurrency_limit': 4}}}))
    reset = _utc(6)
    p = _quota('set', 'zai-glm', 'Weekly/Monthly Limit Exhausted', reset,
               '--state-dir', str(d), '--json')
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    doc = json.load(open(d / 'quota-state.json'))
    # the gate landed
    ent = doc['quota_exhausted']['zai-glm']
    assert ent['status'] == 'gated' and ent['reset_at'] == reset
    assert ent['reason'] == 'Weekly/Monthly Limit Exhausted'
    assert ent['detected_at'] and out['entry'] == ent
    # everything else survived verbatim (read-modify-write, never a rewrite)
    assert doc['providers'] == {'grok-build': {'status': 'gated',
                                               'reason': 'pool unpublished'}}
    assert doc['diversity'] == {'max_consecutive_per_provider': 2}
    assert doc['models'] == {'zai-glm/glm-5.3-flash': {'concurrency_limit': 4}}
    assert doc['note'] == 'hand-written policy file'
    assert doc['updated'] != '2026-09-16'


def test_set_accepts_epoch_reset_and_past_reset_warns(tmp_path):
    """Provider 429 bodies carry epoch `resets_at` (OpenAI) — pasteable — and a
    past reset is RECORDED but flagged as already auto-cleared."""
    d = tmp_path / 'state'
    epoch = int(datetime.datetime.now(UTC).timestamp()) + 3600
    p = _quota('set', 'openai-codex', 'usage limit has been reached', str(epoch),
               '--state-dir', str(d), '--json')
    assert p.returncode == 0, p.stderr
    ent = json.loads(p.stdout)['entry']
    assert ent['reset_at'] == datetime.datetime.fromtimestamp(
        epoch, UTC).isoformat(timespec='seconds')

    p2 = _quota('set', 'zai-glm', 'weekly', _utc(-1), '--state-dir', str(d))
    assert p2.returncode == 0
    assert 'auto-clears immediately' in p2.stderr


def test_set_rejects_a_malformed_reset_without_writing(tmp_path):
    """Operator error is detectable (exit 2) and the state file is untouched —
    a typo must never create a permanently gated lane."""
    d = tmp_path / 'state'
    d.mkdir()
    before = json.dumps({'updated': 'x', 'providers': {}})
    (d / 'quota-state.json').write_text(before)
    p = _quota('set', 'zai-glm', 'weekly', 'next tuesday', '--state-dir', str(d))
    assert p.returncode == 2, p.stdout + p.stderr
    assert 'ISO-8601' in p.stderr
    assert (d / 'quota-state.json').read_text() == before


def test_set_rejects_a_pair_as_provider(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    p = _quota('set', 'zai-glm/glm-5.3-flash', 'weekly', _utc(2),
               '--state-dir', str(d))
    assert p.returncode == 2
    assert 'bare provider id' in p.stderr


def test_status_classifies_gated_vs_expired(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    (d / 'quota-state.json').write_text(json.dumps({
        'updated': 't', 'providers': {},
        'quota_exhausted': dict(_gate('zai-glm', _utc(5), reason='weekly'),
                                **_gate('openai-codex', _utc(-5), reason='plan'))}))
    p = _quota('status', '--json', '--state-dir', str(d))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert [g['provider'] for g in out['gated']] == ['zai-glm']
    assert [g['provider'] for g in out['expired']] == ['openai-codex']
    text = _quota('status', '--state-dir', str(d))
    assert 'GATED' in text.stdout and 'expired' in text.stdout


def test_status_reports_an_explicitly_open_entry(tmp_path):
    """A recorded-but-OPEN entry must be visible as open — not look like a
    missing entry (the 'open' key used to be permanently empty)."""
    d = tmp_path / 'state'
    d.mkdir()
    (d / 'quota-state.json').write_text(json.dumps({
        'updated': 't', 'providers': {},
        'quota_exhausted': {'zai-glm': {'status': 'open', 'reason': 'plan refilled',
                                        'reset_at': _utc(5)}}}))
    p = _quota('status', '--json', '--state-dir', str(d))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['gated'] == [] and out['expired'] == []
    assert [r['provider'] for r in out['open']] == ['zai-glm']
    assert 'open' in _quota('status', '--state-dir', str(d)).stdout


def test_clear_removes_only_the_named_gate(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    (d / 'quota-state.json').write_text(json.dumps({
        'updated': 't', 'providers': {'crof': {'status': 'gated', 'reason': 'R-10'}},
        'quota_exhausted': dict(_gate('zai-glm', _utc(5)),
                                **_gate('openai-codex', _utc(5)))}))
    p = _quota('clear', 'zai-glm', '--state-dir', str(d), '--json')
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['cleared'] == ['zai-glm']
    doc = json.load(open(d / 'quota-state.json'))
    assert list(doc['quota_exhausted']) == ['openai-codex']
    # the POLICY gate (providers.<p>.status) is a different mechanism — untouched
    assert doc['providers']['crof']['status'] == 'gated'

    p2 = _quota('clear', '--all', '--state-dir', str(d), '--json')
    assert p2.returncode == 0
    doc2 = json.load(open(d / 'quota-state.json'))
    assert 'quota_exhausted' not in doc2
    assert doc2['providers']['crof']['status'] == 'gated'


def test_clear_removes_a_nested_hand_written_gate(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    (d / 'quota-state.json').write_text(json.dumps({
        'updated': 't', 'providers': {'zai-glm': {
            'status': 'open',
            'quota_exhausted': {'status': 'gated', 'reason': 'nested',
                                'reset_at': _utc(5)}}}}))
    p = _quota('clear', 'zai-glm', '--state-dir', str(d))
    assert p.returncode == 0
    doc = json.load(open(d / 'quota-state.json'))
    assert 'quota_exhausted' not in doc['providers']['zai-glm']
    assert doc['providers']['zai-glm']['status'] == 'open'


def test_clear_usage_errors(tmp_path):
    d = tmp_path / 'state'
    d.mkdir()
    assert _quota('clear', '--state-dir', str(d)).returncode == 2
    assert _quota('clear', '--all', 'zai-glm', '--state-dir', str(d)).returncode == 2


def test_status_on_absent_file_is_fail_open(tmp_path):
    d = tmp_path / 'nothing-here'
    p = _quota('status', '--json', '--state-dir', str(d))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['present'] is False and out['gated'] == []


# ------------------------------------------- writer/resolver share a file ---

def test_quota_writer_and_spawn_reader_resolve_the_same_file(monkeypatch, tmp_path):
    """The invariant that makes the feature work: `router quota` writes the very
    file the RESOLVER reads (both resolve ROUTER_STATE_DIR with the script
    default). A writer pointed anywhere else is a silent no-op."""
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'sdir'))
    assert router_quota.state_file() == os.path.join(
        router_spawn.MR, 'quota-state.json')


def test_end_to_end_gate_written_by_the_cli_is_honored_by_the_resolver(tmp_path):
    """AC1/AC2 end to end, through the real scripts and a shared state dir:
    `router_quota.py set` → the spawn resolve excludes the lane and advances
    the head to the next eligible hop."""
    state = tmp_path / 'state'
    state.mkdir()
    # the live shape: the state file already holds the OPEN provider policy;
    # `router quota set` must ADD the gate without disturbing it
    (state / 'quota-state.json').write_text(json.dumps(
        {'updated': '2026-09-16', 'providers': dict(OPEN_PROVIDERS)}))
    reg = _write_registry(tmp_path)
    reset = _utc(8)
    p = _quota('set', 'zai-glm', 'Weekly/Monthly Limit Exhausted', reset,
               '--state-dir', str(state))
    assert p.returncode == 0, p.stderr
    env = dict(os.environ, ROUTER_STATE_DIR=str(state), ROUTING_REGISTRY=reg,
               ROUTING_DATA_DIR=str(tmp_path / 'data'))
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'router_spawn.py'),
                           'demo', '--format', 'json'],
                          capture_output=True, text=True, timeout=SEED_TIMEOUT, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out['head']['provider'] == 'openai-codex'
    assert 'zai-glm' not in [c['provider'] for c in out['chain']]
    assert any('quota exhausted: Weekly/Monthly Limit Exhausted' in g
               and f'(resets {reset})' in g for g in out['gate_reasons'])
    assert out['quota_gates']['gated'][0]['provider'] == 'zai-glm'


def test_status_shows_the_plan_gate(tmp_path):
    """An operator must SEE the gate (`router status`) — an invisible gate is a
    silent zero-chain — and the classification comes from the resolver's own
    classifier, so status can never disagree with resolve()."""
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'quota-state.json').write_text(json.dumps({
        'updated': 't', 'providers': dict(OPEN_PROVIDERS),
        'quota_exhausted': dict(_gate('zai-glm', _utc(5), reason='weekly limit'),
                                **_gate('openai-codex', _utc(-5), reason='plan limit'))}))
    env = dict(os.environ, ROUTER_STATE_DIR=str(state),
               ROUTING_REGISTRY=_write_registry(tmp_path))
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'router_status.py'),
                           '--format', 'json'],
                          capture_output=True, text=True, timeout=SEED_TIMEOUT, env=env)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    plan = doc['quota']['quota_exhausted']
    assert [g['provider'] for g in plan['gated']] == ['zai-glm']
    assert [g['provider'] for g in plan['expired']] == ['openai-codex']
    assert plan['gated'][0]['reset_at'] == _utc(5)
    text = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'router_status.py'),
                           '--format', 'text'],
                          capture_output=True, text=True, timeout=SEED_TIMEOUT, env=env)
    assert 'plan-gate  zai-glm GATED until' in text.stdout, (
        # TR-068: this assertion failed ONCE in a loaded 453-test run and has not
        # reproduced in 99 executions since. Dump everything the classification
        # saw, so a recurrence is diagnosable without a rerun.
        f"plan-gate line missing.\nrc={text.returncode}\n"
        f"gated={plan['gated']}\nexpired={plan['expired']}\n"
        f"stdout:\n{text.stdout[-1200:]}\nstderr:\n{text.stderr[-600:]}")


def test_cli_dispatch_does_not_redirect_the_quota_state_dir(tmp_path, monkeypatch):
    """`router quota` must keep the SPAWN default state dir (see cli.py): the
    scheduler invokes the script directly, so a data-home redirect would write
    a gate the fleet never reads."""
    from task_router import cli
    monkeypatch.setenv('TASK_ROUTER_HOME', str(tmp_path / 'dh'))
    assert cli._home_env_exports()['quota'] == {}
    state = tmp_path / 'sdir'
    state.mkdir()
    monkeypatch.setenv('ROUTER_STATE_DIR', str(state))
    rc = cli.main(['quota', 'set', 'zai-glm', 'weekly plan limit', _utc(5), '--json'])
    assert rc == 0
    doc = json.load(open(state / 'quota-state.json'))
    assert doc['quota_exhausted']['zai-glm']['status'] == 'gated'
    # the data-home bootstrap sample the CLI may have created holds NO gate —
    # proof the write went to the state dir the RESOLVER reads, not the wrapper's
    dh_file = tmp_path / 'dh' / 'quota-state.json'
    assert not dh_file.exists() or 'quota_exhausted' not in json.load(open(dh_file))

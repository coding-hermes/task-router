"""TR-361: a SLOW always-run lane beats a 503 on the degraded path.

Measured live 2026-11-03 (reproduced 2026-11-06 by the foreman): satellite
ticks died with gateway HTTP 503 `unservable-rating` — 157 in 24h — because
router_spawn._resolve_fallback skipped any fallback lane whose provider or
model the hourly probe marked SLOW. SLOW is a LATENCY signal, not a failure
(the primary chain treats it as soft — excluded unless --allow-slow), but on
this degraded path the flat skip made it FATAL: under host load every
fallback provider goes SLOW at once, all always-run lanes vanish together,
and the resolver returns `no chain ...` for measured matrices like
agent_tick=3 delegation=2 guard=2 terminal=3.

These tests pin the health-TIER contract that replaces the flat skip:

  * tier 0 (provider and model both OK) first — ordering untouched;
  * tier 1 (probe-marked SLOW) served ONLY when no tier-0 lane exists in
    any stage, every hop carrying `slow: true` + `slow_detail`, and the
    resolve payload naming the trade in gate_reasons;
  * still FATAL: provider DOWN, provider DISABLED, quota-gated — a dead
    lane is still dead even when another candidate is only SLOW.

Fixtures follow tests/test_degraded_path_contract.py: committed tables,
isolated ROUTER_STATE_DIR per test, no network, no live state
(~/.hermes/model-router is never touched).
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import router_spawn  # noqa: E402

#: The two MEASURED 2026-11-03 matrices — both 503'd pre-fix (load bursts
#: added 28 more on 2026-11-05 with the same shape).
MEASURED_MATRICES = [
    {'agent_tick': 3, 'delegation': 2, 'guard': 2, 'terminal': 3},
    {'agent_tick': 3, 'delegation': 3, 'terminal': 2},
]

#: An UNSATISFIABLE rating against the committed tables (same derivation as
#: tests/test_degraded_path_contract.py: math has one tier-5 holder, security
#: two — disjoint sets). The primary chain can never serve it, so the
#: resolver's outcome is decided entirely by the fallback stage.
UNSAT_MATRIX = {'math': 5, 'security': 5}

#: The order-1 always-run lane (data/tables/fallback_lanes.jsonl) — the head
#: in every all-slow scenario, unchanged by this fix.
DEGRADED_HEAD = ('xkiro', 'openai/gpt-6.1-terra')  # 2026-11-09 generation refresh: fallback order-1 retargeted gpt-5.6-terra (retired) -> gpt-6.1-terra


def _committed_tables():
    tables = {}
    for fn in sorted((REPO / 'data' / 'tables').glob('*.jsonl')):
        tables[fn.name[:-len('.jsonl')]] = [
            json.loads(ln) for ln in fn.open() if ln.strip()]
    return tables


def _open_providers(tables):
    """Every provider PRESENT and OPEN (the resolver's policy plane is
    fail-closed on absence)."""
    provs = {r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
    provs |= {m.get('provider') for m in (tables.get('models') or []) if m.get('provider')}
    return {p: {'status': 'open'} for p in sorted(provs)}


def _slow_health_doc():
    """Every fallback provider AND each one's fallback model marked SLOW —
    the measured 2026-11-03 shape (probe latency under host load)."""
    health = {}
    for f in _committed_tables().get('fallback_lanes') or []:
        entry = health.setdefault(
            f['provider'], {'status': 'SLOW', 'latency_ms': 19000, 'models': {}})
        entry['models'][f['model']] = {'status': 'SLOW', 'latency_ms': 19000}
    return health


def _prep_registry(monkeypatch, tmp_path, tables):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables}))
    monkeypatch.setattr(router_spawn, 'REGISTRY', str(reg))


def _state_dir(tmp_path, providers_doc, health_doc):
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    json.dump({'updated': 'test', 'providers': providers_doc},
              open(d / 'quota-state.json', 'w'))
    json.dump({'providers': health_doc}, open(d / 'health-state.json', 'w'))
    json.dump({'pairs': {}}, open(d / 'circuit-state.json', 'w'))
    return str(d)


def _resolve_with_health(monkeypatch, tmp_path, health_doc, matrix):
    """Full resolver run against the committed tables with a hand-built
    health plane — the same seam --profile-req exercises (adhoc reqs)."""
    tables = _committed_tables()
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setattr(
        router_spawn, 'MR',
        _state_dir(tmp_path, _open_providers(tables), health_doc))
    return router_spawn.resolve(
        adhoc=[f'{k}={v}' for k, v in sorted(matrix.items())])


# ------------------------------------------------------- the named contract ---

def test_degraded_path_serves_slow_fallback_lane_instead_of_no_chain(
        monkeypatch, tmp_path):
    """THE contract: all fallback providers/models SLOW + an unsatisfiable
    rating -> a served chain (>= 1 hop) with degraded_fallback=true, the hop
    carrying the slow marker, and a gate_reason that names the trade —
    never the pre-fix `no chain` error doc."""
    reqs = sorted(UNSAT_MATRIX.items())
    assert not router_spawn._build_chain(_committed_tables(), reqs), \
        'precondition: the matrix must be unsatisfiable against the committed tables'
    r = _resolve_with_health(monkeypatch, tmp_path, _slow_health_doc(),
                             UNSAT_MATRIX)
    assert 'error' not in r, \
        f'a SLOW lane must serve instead of erroring: {r.get("error")}'
    chain = r.get('chain') or []
    assert len(chain) >= 1, 'at least one always-run lane must serve'
    assert r.get('degraded_fallback') is True
    head = chain[0]
    assert (head['provider'], head['model']) == DEGRADED_HEAD, \
        'the tier-1 pass keeps the existing lane order'
    assert head.get('slow') is True, 'the served hop must carry the slow marker'
    detail = head.get('slow_detail')
    assert isinstance(detail, str) and detail, 'slow_detail must name the marks'
    assert 'SLOW' in detail and head['provider'] in detail
    reasons = [str(x) for x in (r.get('gate_reasons') or [])]
    assert any('SLOW' in x and 'no healthy always-run lane' in x for x in reasons), \
        f'the payload must name the trade, got: {reasons}'


# --------------------------------------------- the measured incident matrices ---

@pytest.mark.parametrize('matrix', MEASURED_MATRICES,
                         ids=['tick3-deleg2-guard2-term3', 'tick3-deleg3-term2'])
def test_measured_2026_11_03_matrix_degrades_not_errors(
        monkeypatch, tmp_path, matrix):
    """Both measured matrices must degrade to a served slow chain, not the
    503 body (pre-fix: `no chain — no eligible model and no fallback lane
    could serve` on exactly these)."""
    r = _resolve_with_health(monkeypatch, tmp_path, _slow_health_doc(), matrix)
    assert 'error' not in r, \
        f'the measured matrix must not error: {r.get("error")}'
    chain = r.get('chain') or []
    assert chain, 'the measured matrix must degrade to a served chain'
    assert r.get('degraded_fallback') is True
    assert chain[0].get('slow') is True, \
        'the served head is a tier-1 (SLOW) hop and must say so'


# ------------------------------------------------------- dead lanes stay dead ---

@pytest.mark.parametrize('mode', ['down', 'disabled', 'quota-gated'])
def test_fatal_lane_still_excluded_when_others_are_slow(mode):
    """Negative control: a fallback provider that is DOWN / DISABLED /
    quota-gated is STILL excluded even when every other candidate is only
    SLOW — the fix is about SLOW, never a route onto a dead lane."""
    tables = _committed_tables()
    qs = _open_providers(tables)
    health = _slow_health_doc()
    victim = 'xkiro'  # order 1 — would be the head if it could still serve
    if mode == 'down':
        health[victim]['status'] = 'DOWN'
    elif mode == 'disabled':
        health[victim]['status'] = 'DISABLED'
    else:
        qs[victim] = {'status': 'closed'}  # quota GATED
    lanes = router_spawn._resolve_fallback(
        tables, qs, health, {},
        sorted(MEASURED_MATRICES[0].items()))
    assert lanes, 'SLOW lanes behind the dead one must still serve'
    assert lanes[0]['provider'] != victim, \
        f'{mode} provider must never be the head'
    assert all(lane['provider'] != victim for lane in lanes), \
        f'{mode} provider must be absent from the whole chain'
    assert lanes[0].get('slow') is True, \
        'the chain is served by the tier-1 (SLOW) pass'


# ------------------------------------------------------------ the tier-0 control ---

def test_healthy_fallback_lane_serves_without_slow_markers(monkeypatch, tmp_path):
    """Control: when a healthy (all-OK) fallback lane exists, THAT lane is
    the result with no slow marker anywhere and no SLOW gate_reason — tier 0
    is untouched, and tier-1 lanes are not admitted behind it."""
    health = _slow_health_doc()
    health['opencode-go'] = {
        'status': 'OK', 'latency_ms': 900,
        'models': {'glm-5.3-flash': {'status': 'OK', 'latency_ms': 900}}}
    r = _resolve_with_health(monkeypatch, tmp_path, health, UNSAT_MATRIX)
    assert 'error' not in r
    chain = r.get('chain') or []
    assert chain, 'the healthy lane must serve the unsatisfiable rating'
    assert (chain[0]['provider'], chain[0]['model']) == \
        ('opencode-go', 'glm-5.3-flash')
    assert len(chain) == 1, \
        'no SLOW lane may ride behind a healthy one (tier-1 needs a tier-0 void)'
    assert all('slow' not in hop for hop in chain), \
        'a tier-0 serve must carry no slow marker'
    assert r.get('degraded_fallback') is True
    assert not any('SLOW' in str(x) for x in (r.get('gate_reasons') or [])), \
        'no SLOW gate_reason when the served lane is healthy'

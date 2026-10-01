"""TR-194: an EMPTY LADDER is not the same answer as ALL-LANES-GATED.

The 2026-09-26 incident produced bare `no-hops` ledger rows for ratings
nothing in the registry could satisfy. c49b18a fixed the resolver (the
eligibility-stage early return no longer dead-ends before the fallback
section); this file pins the DISTINCTION the fix creates, so a future edit
cannot re-collapse the two outcomes:

  * EMPTY LADDER (unsatisfiable rating) — the resolver returns a STRUCTURED
    error doc (error + reasons list + exclusions list); the proxy names the
    row `unservable-rating` on the failure_reason plane.
  * ALL-LANES-GATED (eligible hops existed, every one excluded) — the
    resolver returns the structured fail-closed SUCCESS shape (head None +
    a full exclusion list, pinned upstream by
    test_regression.py::test_absent_state_is_fail_closed_all_excluded); the
    proxy keeps the plain `no-hops` reason byte-for-byte.
  * SERVED-DEGRADED — nothing eligible but an always-run lane serves the
    request, carrying `requirements_unmet` per hop (TR-176's degraded path).

Three layers: the resolver payload shapes (in-process, hermetic MR),
the falsifier's own classification battery
(scripts/router_degraded_path_falsifier.py, run against FROZEN sample rows —
never the live ledger), and the wired proxy_chat empty-chain exit (driven
through the resolver SUBPROCESS, since the proxy resolves out-of-process and
an in-process stub would prove nothing about the wiring).

No network, no live state: ROUTER_STATE_DIR is provisioned per test (the
TR-177 rule test_envelope_step_count.py already uses) and _proxy_record is
stubbed so nothing touches the real ledger.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import router_server as rsrv  # noqa: E402
import router_spawn  # noqa: E402
import router_degraded_path_falsifier as falsifier  # noqa: E402

#: The measured incident rating (live ledger, 2026-09-26): guard>=2 +
#: terminal>=2 + mechanical>=1 + agent_tick>=2 + tool_use>=2 +
#: long_horizon>=1 has NO clearing lane in the COMMITTED tables either
#: (eligible=0 — verified against data/tables at the time of writing).
UNSAT_MATRIX = {'guard': 2, 'terminal': 2, 'mechanical': 1, 'agent_tick': 2,
                'tool_use': 2, 'long_horizon': 1}
UNSAT_REQS = sorted(UNSAT_MATRIX.items())
#: A profile with deep eligible coverage in the same tables (P1_CODING,
#: eligible=235) — the SATISFIABLE arm.
SAT_PROFILE = 'P1_CODING'
#: The always-run lane that serves an unsatisfiable rating degraded
#: (fallback_lanes order 1).
DEGRADED_HEAD = ('xkiro', 'z-ai/glm-5.3-flash')


def _committed_tables():
    tables = {}
    for fn in sorted((REPO / 'data' / 'tables').glob('*.jsonl')):
        tables[fn.name[:-len('.jsonl')]] = [
            json.loads(ln) for ln in fn.open() if ln.strip()]
    return tables


def _open_providers(tables):
    """Every provider PRESENT and OPEN (the resolver's policy plane is
    fail-closed on absence, so an empty quota doc gates the fallback lanes)."""
    provs = {r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
    provs |= {m.get('provider') for m in (tables.get('models') or []) if m.get('provider')}
    return {p: {'status': 'open'} for p in sorted(provs)}


def _prep_registry(monkeypatch, tmp_path, tables):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables}))
    monkeypatch.setattr(router_spawn, 'REGISTRY', str(reg))


def _open_state_dir(tmp_path, providers_doc):
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    json.dump({'updated': 'test', 'providers': providers_doc},
              open(d / 'quota-state.json', 'w'))
    json.dump({'providers': {}}, open(d / 'health-state.json', 'w'))
    json.dump({'pairs': {}}, open(d / 'circuit-state.json', 'w'))
    return str(d)


# ------------------------------------------------- resolver payload shapes ----

def test_resolver_empty_ladder_is_structured_not_bare(monkeypatch, tmp_path):
    """EMPTY LADDER: an unsatisfiable rating (nothing eligible, no always-run
    lane in the tables) must come back as the STRUCTURED error doc (reasons +
    exclusions lists present) — never the pre-c49b18a bare {'error': ...} shape.
    The fallback table is deliberately absent so the degraded stage has nothing
    to serve and the post-fallback error doc is the outcome under test."""
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setattr(router_spawn, 'MR',
                        _open_state_dir(tmp_path, _open_providers(tables)))
    r = router_spawn.resolve(adhoc=[f'{c}={v}' for c, v in UNSAT_REQS])
    assert 'error' in r, 'unsatisfiable rating must error (no fallback could serve)'
    assert isinstance(r.get('reasons'), list), 'the error doc must carry a reasons list'
    assert isinstance(r.get('exclusions'), list), 'the error doc must carry an exclusions list'
    # and the proxy classification names it, so the ROW can:
    assert rsrv._resolver_had_no_eligible_hop(r) is True


def test_resolver_all_lanes_gated_keeps_fail_closed_shape(monkeypatch, tmp_path):
    """ALL-LANES-GATED: eligible hops exist, all excluded — the structured
    fail-closed SUCCESS shape (head None + full exclusion list), never the
    error doc. The distinction is the whole point of the row."""
    tables = _committed_tables()
    _prep_registry(monkeypatch, tmp_path, tables)
    # Absent state dir: EVERY eligible provider is quota-gated (TR-203
    # fail-closed policy plane), while the fallback lanes stay empty (their
    # providers have no quota row either) -> head None + exclusions only.
    monkeypatch.setattr(router_spawn, 'MR', str(tmp_path / 'no-such-state'))
    r = router_spawn.resolve(adhoc=['agent_tick=1'])
    assert 'error' not in r, 'all-gated is a success shape, not an error'
    assert r.get('head') is None
    excl = r.get('exclusions') or []
    assert excl, 'every eligible hop must appear with its exclusion'
    assert rsrv._resolver_had_no_eligible_hop(r) is False, \
        'all-gated must NOT classify as unservable-rating'


def test_resolver_empty_ladder_vs_all_gated_distinguishable(monkeypatch, tmp_path):
    """THE contract: the two cases return distinguishable structured outcomes —
    error doc vs head-None chain — and the proxy classifier splits them."""
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setattr(router_spawn, 'MR',
                        _open_state_dir(tmp_path, _open_providers(tables)))
    empty = router_spawn.resolve(adhoc=[f'{c}={v}' for c, v in UNSAT_REQS])
    monkeypatch.setattr(router_spawn, 'MR', str(tmp_path / 'no-such-state'))
    gated = router_spawn.resolve(adhoc=['agent_tick=1'])
    # distinguishable BY SHAPE, on the resolver payload:
    assert ('error' in empty) != ('error' in gated)
    assert isinstance(empty.get('reasons'), list) and \
        isinstance(empty.get('exclusions'), list), \
        'the empty-ladder error doc must be the structured one'
    assert gated['exclusions'] and 'error' not in gated and gated['head'] is None
    # distinguishable BY CLASSIFICATION, on the row plane:
    assert rsrv._resolver_had_no_eligible_hop(empty) is True
    assert rsrv._resolver_had_no_eligible_hop(gated) is False


def test_resolver_degraded_fallback_carries_requirements_unmet(monkeypatch, tmp_path):
    """SERVED-DEGRADED: nothing eligible but the always-run lane serves —
    with the unmet requirement NAMED per hop (the c49b18a degraded path)."""
    tables = _committed_tables()
    chains = router_spawn._build_chain(tables, UNSAT_REQS)
    assert not chains, 'precondition: the matrix must be unsatisfiable here'
    lanes = router_spawn._resolve_fallback(
        tables, _open_providers(tables), {}, {}, UNSAT_REQS)
    assert lanes, 'an always-run lane must serve the unsatisfiable rating degraded'
    head = lanes[0]
    assert (head['provider'], head['model']) == DEGRADED_HEAD
    assert head.get('fallback') is True
    unmet = head.get('requirements_unmet')
    assert isinstance(unmet, list) and unmet, 'the unmet requirement must be NAMED'
    for cat, lvl, have in unmet:
        assert cat in UNSAT_MATRIX and have < lvl
    # the proxy's chain-evidence projection must not drop it (AC2: the row
    # keeps the requirement evidence):
    ev = rsrv._chain_evidence({'chain': lanes}, lanes)
    assert ev['chain'][0].get('requirements_unmet') == unmet


# --------------------------------------------- the falsifier's classification --

def test_falsifier_classifier_pins_the_three_verdicts():
    """The falsifier's classify_outcome must split all three shapes — and
    classify the PRE-c49b18a bare doc as BARE-NO-HOPS (the falsifier can fail)."""
    post = {'error': 'no chain — no eligible model and no fallback lane could serve',
            'reasons': [], 'exclusions': []}
    pre = {'error': 'no chain — profile has no eligible models'}
    fb = {'degraded_fallback': True,
          'fallback': [{'provider': 'xkiro', 'model': 'z-ai/glm-5.3-flash',
                        'requirements_unmet': [('guard', 2, -1)]}]}
    gated = {'chain': [], 'exclusions': [{'provider': 'p', 'why': ['quota GATED']}]}
    sat = {'chain': [{'provider': 'xkiro', 'model': 'm'}]}
    assert falsifier.classify_outcome(post) == (
        falsifier.FAIL_CLOSED, {'error': post['error'], 'reasons': 0, 'exclusions': 0})
    assert falsifier.classify_outcome(pre)[0] == falsifier.BARE_NO_HOPS
    v, detail = falsifier.classify_outcome(fb)
    assert v == falsifier.SERVED_DEGRADED
    assert detail['fallback_head']['provider'] == 'xkiro'
    # an all-gated payload is NOT unsatisfiable evidence for the falsifier:
    v, _ = falsifier.classify_outcome(gated)
    assert v != falsifier.FAIL_CLOSED
    assert falsifier.classify_outcome(sat)[0] == falsifier.SATISFIABLE


def test_falsifier_replay_on_committed_tables(monkeypatch):
    """The falsifier's own replay machinery, run against the COMMITTED tables
    (frozen, no ledger): the unsatisfiable matrix must come out SERVED-DEGRADED
    with the unmet named — and the satisfiable matrix must NOT be counted."""
    tables = _committed_tables()
    res = falsifier.falsify(tables, [
        ({'session_id': 'frozen-unsat', 'ts': 0,
          'required_categories': dict(UNSAT_MATRIX),
          'complexity_source': 'classifier', 'hops_attempted': 0,
          'profile_id': None}, dict(UNSAT_MATRIX)),
        ({'session_id': 'frozen-sat', 'ts': 0,
          'required_categories': {'agent_tick': 1},
          'complexity_source': 'classifier', 'hops_attempted': 1,
          'profile_id': None}, {'agent_tick': 1}),
    ])
    assert res['n'] == 2
    assert res['unsatisfiable'] == 1, 'only the unsatisfiable row counts'
    assert res['after']['bare_no_hops_violations'] == 0
    assert res['after']['served_degraded'] == 1
    row = next(r for r in res['rows'] if r['session_id'] == 'frozen-unsat')
    assert row['verdict'] == falsifier.SERVED_DEGRADED
    assert row.get('unmet'), 'the verdict row must name the unmet requirement'
    assert row['derivation_disagreement'] is False


def test_falsifier_detects_the_pre_fix_shape():
    """Mutation RED (deliberate, in-memory): classify the outcome the PRE-c49b18a
    resolver produced for this rating — the falsifier must call it BARE-NO-HOPS.
    This is what makes the falsifier able to fail."""
    tables = _committed_tables()
    res = falsifier.falsify(tables, [
        ({'session_id': 'frozen-unsat', 'ts': 0,
          'required_categories': dict(UNSAT_MATRIX),
          'complexity_source': 'classifier', 'hops_attempted': 0,
          'profile_id': None}, dict(UNSAT_MATRIX)),
    ])
    assert res['unsatisfiable'] == 1
    assert res['before']['unsatisfiable_bare_no_hops'] == 1, \
        'before-semantics: the unsatisfiable rating WAS the bare no-hops'
    pre = {'error': 'no chain — profile has no eligible models'}
    verdict, _ = falsifier.classify_outcome(pre)
    assert verdict == falsifier.BARE_NO_HOPS


# ----------------------------------------------------- the wired proxy path ----

def _proxy_env(monkeypatch, tmp_path, tables, providers_doc):
    """Hermetic proxy run: temp registry + state dir via env (the resolver runs
    in a SUBPROCESS and copies os.environ), ledger write stubbed."""
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setenv('ROUTER_STATE_DIR',
                       _open_state_dir(tmp_path, providers_doc))
    monkeypatch.setenv('ROUTING_REGISTRY', str(tmp_path / 'registry.json'))
    rec = []

    def _record(provider, model, ok, requirements, **kw):
        # positional signature mirrors _proxy_record: requirements is the 4th
        # positional; keep it so row-level assertions can read the evidence.
        rec.append({'provider': provider, 'model': model, 'ok': ok,
                    'required_categories': (requirements or {}).get('matrix'),
                    **kw})
    monkeypatch.setattr(rsrv, '_proxy_record', _record)
    return rec


def test_proxy_empty_ladder_row_is_named_unservable_rating(monkeypatch, tmp_path):
    """Wired exit: an unsatisfiable rating through proxy_chat must produce the
    no-hops row with failure_reason='unservable-rating' (additive; route_outcome
    stays the 'no-hops' the baseline counts), with the rating evidence on the
    row (required_categories + complexity_source).

    The rating is injected through the JEV scorer seam (x-router-scorer: jev
    runs router_jev IN-PROCESS), stubbed to return the incident matrix — so the
    real requirements -> _proxy_chain SUBPROCESS -> empty-chain exit is what is
    exercised, not a stubbed _proxy_chain. The state plane is the ABSENT dir:
    fail-closed on absence gates every provider, so the fallback stage has
    nothing to serve and the resolver returns the unsatisfiable error doc —
    no upstream is ever called (a stub that leaked would 401)."""
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    rec = _proxy_env(monkeypatch, tmp_path, tables, {})
    import router_jev
    monkeypatch.setattr(router_jev, 'classify',
                        lambda text: {'matrix': dict(UNSAT_MATRIX),
                                      'complexity_sig': None,
                                      'problems': []})
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'hi'}]},
        {'x-router-scorer': 'jev'})
    assert status == 503
    env = out['_router']
    assert env['terminal_reason'] == 'no-hops'
    assert not env.get('registry_missing')
    assert rec, 'the no-hops row must be written'
    row = rec[-1]
    assert row['route_outcome'] == 'no-hops'
    assert row['failure_reason'] == 'unservable-rating'
    # the rating evidence rides the row on BOTH paths:
    assert row['required_categories'] == UNSAT_MATRIX
    assert row['complexity_source'] == 'jev'


def test_proxy_all_lanes_gated_keeps_plain_no_hops(monkeypatch, tmp_path):
    """Wired exit: the ALL-GATED case keeps failure_reason='no-hops'
    byte-for-byte — the new name must never swallow the plain case."""
    tables = _committed_tables()
    # empty quota doc -> every provider gated; the fallback lanes gate too
    # (no quota rows) -> head None, plain no-hops.
    rec = _proxy_env(monkeypatch, tmp_path, tables, {})
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'hi'}]},
        {'x-router-profile': SAT_PROFILE})
    assert status == 503
    env = out['_router']
    assert env['terminal_reason'] == 'no-hops'
    assert not env.get('registry_missing')
    row = rec[-1]
    assert row['route_outcome'] == 'no-hops'
    assert row['failure_reason'] == 'no-hops', \
        'a plain gating no-hops keeps its plain reason'
    assert row['required_categories'] is None  # profile path: no matrix
    assert row['complexity_source'] == 'declared'


def test_proxy_evidence_free_chainless_payload_keeps_plain_no_hops(monkeypatch, tmp_path):
    """The naming claims only what the resolver payload POSITIVELY says. A
    chainless payload with NO error doc (an evidence-free shape — e.g. a
    degraded resolver) is not proof of an unsatisfiable rating, so it keeps
    the plain 'no-hops' reason. This arm is exactly what
    tests/test_proxy_stats_row.py::test_the_no_eligible_hop_exit_still_leaves_a_row
    stubs; both must agree."""
    assert rsrv._resolver_had_no_eligible_hop({'chain': []}) is False
    assert rsrv._resolver_had_no_eligible_hop({}) is False
    assert rsrv._resolver_had_no_eligible_hop(None) is False
    assert rsrv._resolver_had_no_eligible_hop(
        {'chain': [], 'exclusions': [{'provider': 'p', 'why': ['x']}]}) is False

"""TR-066 Path A tests: the side-channel executor walks a chain, records one
outcome row per attempt, advances on TRANSPORT failure only, honours max_hops,
and names its stats fallback. Stub commands only — no network, no live lanes."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_chain_run as rcr  # noqa: E402
import router_outcomes as ro    # noqa: E402


CHAIN = [
    {'hop': 1, 'provider': 'p1', 'model': 'm-fail', 'usd_1m': 0.1,
     'outcomes': {'complexity_sig': None, 'stats_fallback': 'unconditioned'}},
    {'hop': 2, 'provider': 'p2', 'model': 'm-ok', 'usd_1m': 0.2,
     'outcomes': {'complexity_sig': 'abc', 'stats_fallback': None}},
    {'hop': 3, 'provider': 'p3', 'model': 'm-ok2', 'usd_1m': 0.3,
     'outcomes': {}},
]


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Hermetic store + breaker state so nothing touches the live fleet."""
    store = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    calls = []

    def fake_breaker(provider, model, ok, reason=''):
        calls.append((provider, model, ok))
    monkeypatch.setattr(rcr, '_breaker', fake_breaker)
    return store, calls


def _cmd(exit_for):
    """A stub caller: exits per the model name, so hop 1 fails and hop 2 wins."""
    script = "import sys; sys.exit(0 if '{model}' in " + json.dumps(list(exit_for)) + " else 1)"
    return f'{sys.executable} -c "{"import sys; sys.exit(0)"}"'
    # real template below (kept simple: bash test on ROUTER_MODEL)


def _bash_cmd(succeed_models):
    joined = '|'.join(succeed_models)
    return ('bash -c \'case "$ROUTER_MODEL" in ' + joined +
            ') exit 0 ;; *) exit 7 ;; esac\'')


def test_transport_failure_advances_to_next_hop(isolated):
    store, calls = isolated
    summary = rcr.run_chain(CHAIN, _bash_cmd(['m-ok']), max_hops=3,
                            session_id='sess-1', profile_id='P1_CODING',
                            requirements={'code_gen': 2})
    assert summary['success'] is True
    assert summary['final'] == {'provider': 'p2', 'model': 'm-ok'}
    outcomes = [a['outcome'] for a in summary['attempts']]
    assert outcomes == ['transport-failure', 'success']
    # breaker evidence: failure then success, in order
    assert calls == [('p1', 'm-fail', False), ('p2', 'm-ok', True)]
    # one outcome row per attempt, both written back
    rows = [json.loads(l) for l in open(store) if l.strip()]
    assert len(rows) == 2
    assert [r['success'] for r in rows] == [False, True]
    assert rows[0]['profile_id'] == 'P1_CODING'
    assert rows[0]['complexity_sig'] == ro.complexity_sig({'code_gen': 2})


def test_exhausted_chain_reports_honestly(isolated):
    store, calls = isolated
    summary = rcr.run_chain(CHAIN, _bash_cmd(['nothing-succeeds']), max_hops=2,
                            session_id='sess-2')
    assert summary['success'] is False and summary['exhausted'] is True
    assert len(summary['attempts']) == 2          # max_hops respected
    assert all(not ok for _p, _m, ok in calls)


def test_max_hops_bounds_the_walk(isolated):
    _store, _calls = isolated
    summary = rcr.run_chain(CHAIN, _bash_cmd(['m-ok2']), max_hops=1, session_id='s')
    assert len(summary['attempts']) == 1 and summary['success'] is False


def test_dry_run_plans_without_executing_or_recording(isolated):
    store, calls = isolated
    summary = rcr.run_chain(CHAIN, _bash_cmd(['m-ok']), max_hops=3,
                            session_id='s', dry_run=True)
    assert all(a['outcome'] == 'planned' for a in summary['attempts'])
    assert calls == []
    assert not os.path.exists(store) or not open(store).read().strip()


def test_hop_facts_are_carried_for_the_caller(isolated):
    _store, _calls = isolated
    summary = rcr.run_chain(CHAIN, _bash_cmd(['m-ok']), max_hops=3, session_id='s')
    first = summary['attempts'][0]
    assert first['provider'] == 'p1' and first['usd_1m'] == 0.1
    assert first['stats_fallback'] == 'unconditioned'   # TR-066 R5: named
    assert first['rc'] == 7 and first['error']           # reason recorded


def test_outcome_rows_dedupe_on_rerun(isolated):
    store, _calls = isolated
    rcr.run_chain(CHAIN, _bash_cmd(['m-ok']), max_hops=3, session_id='same-sess',
                  requirements={'code_gen': 2})
    before = len([l for l in open(store) if l.strip()])
    rcr.run_chain(CHAIN, _bash_cmd(['m-ok']), max_hops=3, session_id='same-sess',
                  requirements={'code_gen': 2})
    after = len([l for l in open(store) if l.strip()])
    assert before == after, 'same (source, session, model) must not double-count'

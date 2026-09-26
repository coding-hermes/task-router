"""TR-182: a router-internal outcome must never become a provider circuit event.

Two measured defects live in this file's subject:

  * `_proxy_record('none', 'none', False, ...)` is how the proxy records a row for a request
    that had NO hop (nothing eligible after gating). That call also shelled out to the circuit
    CLI unchanged, so it opened a breaker for a provider literally called `none`, with the
    router's own error text as the reason -- observed live in circuit-state.json.
  * every failure was recorded with NO --class, so all of them took the HARD default: three
    slow hops removed a whole provider for 30 minutes (provider-level breaker). That is the
    measured mechanism of the 2026-09-25 lockup: 65 failed rows -> provider-wide breakers ->
    every chain gated -> every request died as "no open hop".

The class is the blast radius, so it is pinned here: a timeout is overload (pair-level, 120s),
a rate window is quota_window (pair-level, 300s), and only real transport failure is api_down.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import router_server as rs  # noqa: E402
import router_outcomes as ro  # noqa: E402


def test_a_timeout_is_not_recorded_as_a_provider_outage():
    assert rs._circuit_class('hop-wall-timeout') == 'overload'
    assert rs._circuit_class('idle-timeout (no bytes for 180s)') == 'overload'


def test_a_rate_window_is_its_own_class():
    assert rs._circuit_class('429 from upstream') == 'quota_window'
    assert rs._circuit_class('rate limit exceeded') == 'quota_window'


def test_only_real_transport_failure_earns_the_hard_class():
    assert rs._circuit_class('connection refused') == 'api_down'
    assert rs._circuit_class('upstream down') == 'api_down'
    assert rs._circuit_class('') == 'api_down'


def test_the_no_hops_row_never_touches_the_circuit(monkeypatch, tmp_path):
    """The ledger row is still written; the circuit must not hear about it."""
    calls = []
    monkeypatch.setattr(rs, '_subprocess_text', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(ro, 'append_row_fast', lambda *a, **k: None)
    monkeypatch.setattr(ro, 'accumulate_row', lambda *a, **k: None)
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    rs._proxy_record('none', 'none', False, {'matrix': {}}, reason='no open hop for this request',
                     route_outcome='no-hops', hops_attempted=0)
    assert [c for c in calls if 'record-' in str(c)] == [], \
        'a request with no hop must not record a provider failure'


def test_a_real_lane_failure_still_records_its_class(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rs, '_subprocess_text', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(ro, 'append_row_fast', lambda *a, **k: None)
    monkeypatch.setattr(ro, 'accumulate_row', lambda *a, **k: None)
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    rs._proxy_record('xkiro', 'openai/gpt-6-luna', False, {'matrix': {}},
                     reason='hop-wall-timeout')
    joined = [str(c) for c in calls]
    assert any('record-failure' in j for j in joined), 'a real lane failure must still be recorded'
    assert any('overload' in j for j in joined), 'and it must carry the soft class for a timeout'

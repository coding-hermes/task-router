"""TR-340: the router's own gate text must never become a provider failure.

Measured live 2026-10-19 20:47-21:05Z, verified again by the dogfood lane at HEAD 387f0e8:

  * a gateway restart window produced genuine 'connection refused' records, AND
  * every docs/readme resolve returned this router's own
    `503 {"error": "no open hop for this request"}` with chain_length 0, and
  * the proxy then recorded THAT text as an ``api_down`` failure on whichever pair the
    ladder had been trying -- opening 22 pairs across 12 UNRELATED providers and 5
    provider-level breakers inside 17 minutes, while the hourly probe simultaneously
    reported 22/28 providers OK and deepseek held a $453.98 balance.

TR-172's identity guard could not stop it: the pairs it opened were REAL pairs, so the
``provider == 'none'`` early return never fired. The class that decided the blast radius
was ``_circuit_class``'s substring fallback, which returned the HARD ``api_down`` for every
shape that was not timeout- or 429-shaped -- including the router's own refusal text.

The rows pinned here are the row's own acceptance criteria:
"a forced NO-OPEN-HOP resolve opens zero pairs and zero provider breakers (regression test),
and a chain with no live hop fails fast without poisoning unrelated pairs."

Both arms run against the REAL ``router_circuit.py`` CLI subprocess with a scratch
``ROUTER_STATE_DIR``, so the assertions read the actual circuit store rather than a mock.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import router_server as rs  # noqa: E402
import router_outcomes as ro  # noqa: E402

#: Every shape the router's own gate emits, as it reaches the recorder in the wild.
ROUTER_GENERATED_REASONS = (
    'no open hop for this request',
    '503 no open hop for this request',
    'no-hops',
    'unservable-rating',
    'registry-missing',
)


def _state(tmp_path):
    """The circuit store as written by the real CLI subprocess."""
    path = Path(tmp_path) / 'circuit-state.json'
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _pairs(state):
    return state.get('pairs') or {}


def _provider_breakers(state):
    return (state.get('v2') or {}).get('provider_breakers') or {}


def _quiet_ledger(monkeypatch):
    """Keep the outcome ledger out of the scratch dir's way (not under test here)."""
    monkeypatch.setattr(ro, 'append_row_fast', lambda *a, **k: None)
    monkeypatch.setattr(ro, 'accumulate_row', lambda *a, **k: None)


def test_router_generated_text_is_recognised(monkeypatch):
    for reason in ROUTER_GENERATED_REASONS:
        assert rs._is_router_generated(reason), reason
    # ... and nothing else is. A predicate that over-matches would silence the
    # genuine provider failures TR-182/TR-288 depend on.
    for benign in ('connection refused', '429 from upstream', 'hop-wall-timeout',
                   'upstream down', 'transport-error', ''):
        assert not rs._is_router_generated(benign), benign
    # Fail-open: non-str input must not raise.
    assert rs._is_router_generated(None) is False
    assert rs._is_router_generated({'error': 'x'}) is False


def test_the_class_of_router_text_is_never_the_hard_api_down():
    for reason in ROUTER_GENERATED_REASONS:
        assert rs._circuit_class(reason) != 'api_down', reason
    # The shared kind table now decides a kind (TR-288/TR-190): a 5xx kind is a
    # transient capacity failure and must never take the hard class.
    assert rs._circuit_class('5xx-overloaded') == 'overload'
    assert rs._circuit_class('upstream-5xx') == 'overload'
    assert rs._circuit_class('402-no-credit') == 'out_of_credit'
    assert rs._circuit_class('429-capacity') == 'quota_window'


def test_tr182_class_pins_survive():
    """TR-340 must not soften a POSITIVE provider signal."""
    assert rs._circuit_class('connection refused') == 'api_down'
    assert rs._circuit_class('upstream down') == 'api_down'
    assert rs._circuit_class('') == 'api_down'
    assert rs._circuit_class('429 from upstream') == 'quota_window'
    assert rs._circuit_class('rate limit exceeded') == 'quota_window'
    assert rs._circuit_class('hop-wall-timeout') == 'overload'


def test_control_a_real_transport_failure_still_opens_the_breaker(monkeypatch, tmp_path):
    """CONTROL for the treatment below: the harness really does write the store.

    Without this arm, "zero pairs" could be an artifact of a recorder that never
    reaches the circuit at all. A genuine transport failure on a real pair must
    still earn the hard class and, at three in the window, its provider breaker.
    """
    _quiet_ledger(monkeypatch)
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    for model in ('deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-v4-lite'):
        rs._proxy_record('deepseek', model, False, {'matrix': {}},
                         reason='connection refused', route_outcome='failed')
    state = _state(tmp_path)
    assert _pairs(state), 'the control arm wrote no pair at all -- harness is broken'
    assert _provider_breakers(state), \
        'three genuine transport failures must still open the provider breaker'


def test_a_forced_no_open_hop_resolve_opens_zero_pairs_and_zero_breakers(monkeypatch, tmp_path):
    """THE ACCEPTANCE CRITERION, in two arms.

    (a) the documented failure path: `_proxy_record('none', 'none', ...)` with
        route_outcome='no-hops' (this half is TR-172's) -- five times, so a
        counting bug cannot hide behind the three-failure threshold;
    (b) the LIVE half this row was filed for: a REAL pair whose failed hop
        carried the router's own refusal text.
    """
    _quiet_ledger(monkeypatch)
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))

    for _ in range(5):
        rs._proxy_record('none', 'none', False, {'matrix': {}},
                         reason='no open hop for this request',
                         route_outcome='no-hops', hops_attempted=0)

    for reason in ROUTER_GENERATED_REASONS:
        for model in ('deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-v4-lite'):
            rs._proxy_record('deepseek', model, False, {'matrix': {}},
                             reason=reason, route_outcome='failed')

    state = _state(tmp_path) if (Path(tmp_path) / 'circuit-state.json').exists() else {}
    assert _pairs(state) == {}, \
        'the router gated itself: %r' % (_pairs(state),)
    assert _provider_breakers(state) == {}, \
        'unrelated provider breakers opened: %r' % (_provider_breakers(state),)


def test_the_ledger_row_is_still_written_for_a_router_generated_failure(monkeypatch, tmp_path):
    """TR-340 silences the CIRCUIT, never the ledger.

    The operator must still be able to read that the resolve failed and why; only
    the breaker call is suppressed.
    """
    rows = []
    monkeypatch.setattr(rs, '_subprocess_text',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('the circuit must not be called: %r' % (a,))))
    monkeypatch.setattr(ro, 'append_row_fast', lambda path, row: rows.append(row))
    monkeypatch.setattr(ro, 'accumulate_row', lambda path, row: rows.append(row))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    rs._proxy_record('deepseek', 'deepseek-v4-pro', False, {'matrix': {}},
                     reason='no open hop for this request', route_outcome='failed')
    assert rows, 'the outcome row must survive the TR-340 guard'

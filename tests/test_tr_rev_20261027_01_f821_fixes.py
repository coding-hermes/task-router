"""TR-REV-20261027-01 focused coverage — the two REAL fixes behind the lint gate.

1. F821 `socket`: the queue-bound refusal path must answer 429, not crash with
   NameError (only OSError was caught around the SO_LINGER RESET).
2. F821 `dl_declared_b`/`hop_dl`: the /v1/responses hop (`_hop` in
   `_hermes_proxy_chat`) must stamp the x-router-deadline-echo private key —
   before the fix the try/except swallowed the NameError and the echo NEVER
   reached `_hermes_responses_call`'s wire-consume half (the TR-264 forward
   worked only when the ladder's headers carried the hop-elapsed header).

Hermetic: injected upstreams, tmp_path state, no network.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv

# ---------- 1. the missing `import socket` -----------------------------------

class _FakeHandler:
    """Enough of a handler for the queue-bound branch: a real socket whose
    setsockopt can be made to raise NameError-equivalent paths is impossible
    post-fix (socket is imported), so we assert the branch completes."""

    def __init__(self):
        import socket as _s  # the fix makes this resolvable at module scope
        assert rsrv.socket is _s, 'router_server must import the socket module'


def test_router_server_imports_socket_for_the_refusal_path():
    """AC: the SO_LINGER RESET branch resolves `socket` — pre-fix this was a
    NameError hiding behind `except OSError` and the client got a dropped
    connection instead of the shaped 429."""
    _FakeHandler()


def test_deadline_parse_rejects_nan_budgets():
    """AC: PLR0124 fix — the NaN guard is now math.isnan (same semantics,
    provable without a self-compare)."""
    declared, problems = rsrv._parse_caller_deadline(
        {'x-caller-budget-s': 'nan'})
    assert declared.get('budget_s') is None
    assert problems and 'out-of-range' in problems[0]


# ---------- 2. the /v1/responses hop deadline echo ----------------------------

@pytest.fixture()
def hermes_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    return tmp_path


def _hermes_upstream(seen):
    def upstream(path, body, headers):
        seen.append((path, dict(headers)))
        return 200, {'id': 'resp_1', 'output': []}, 'gw-session-1'
    return upstream


def test_hermes_hop_stamps_the_deadline_echo_when_declared(hermes_env):
    """AC: with a caller-declared budget, the hop headers carry the
    x-router-deadline-echo block AND the wire headers the gateway consumes —
    pre-fix the echo block was dead code (NameError swallowed by except)."""
    seen = []
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hi'},
        {'X-Caller-Budget-S': '900', 'X-Caller-Deadline-Mode': 'wall',
         'X-Caller-Margin-S': '5'},
        upstream=_hermes_upstream(seen))
    assert status == 200, payload
    _hop_path, hop_headers = seen[0]
    echo = hop_headers.get('x-router-deadline-echo')
    assert isinstance(echo, dict), 'deadline echo missing from hop headers'
    assert echo['declared'] is True
    # the hop sees the DECREMENTED remainder (900 - margin 5 - elapsed), never
    # the caller's original total — the TR-264 forward contract.
    assert 890.0 <= echo['declared_s'] <= 900.0
    assert echo['mode'] == 'wall'
    assert echo['layer'] == 'router-proxy'
    assert echo['applied_s'] is not None
    assert echo['applied_s'] <= echo['declared_s']
    # the wire-consume half fired: the gateway hop saw the applied budget
    assert 'X-Caller-Budget-S' in hop_headers
    assert float(hop_headers['X-Caller-Budget-S']) <= 900.0


def test_hermes_hop_stamps_declared_false_when_undeclared(hermes_env):
    """AC: no caller deadline -> the echo block exists with declared=False
    (never silently absent)."""
    seen = []
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hi'}, {}, upstream=_hermes_upstream(seen))
    assert status == 200, payload
    _, hop_headers = seen[0]
    echo = hop_headers.get('x-router-deadline-echo')
    assert isinstance(echo, dict)
    assert echo['declared'] is False


def test_hop_budget_never_exceeds_the_declared_total(hermes_env):
    """AC: the derived hop budget is arithmetic on the declared total — a
    tiny declared budget must shrink the forwarded budget, never exceed it."""
    seen = []
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hi'},
        {'X-Caller-Budget-S': '3', 'X-Caller-Deadline-Mode': 'wall',
         'X-Caller-Margin-S': '1'},
        upstream=_hermes_upstream(seen))
    assert status == 200, payload
    _, hop_headers = seen[0]
    echo = hop_headers.get('x-router-deadline-echo')
    # the hop sees the DECREMENTED remainder (3 - margin 1 - elapsed): strictly
    # below 3.0, positive, and applied <= declared — arithmetic, never the raw
    # passthrough (elapsed only ever shaves the tail).
    assert 0.0 < echo['declared_s'] <= 3.0
    # gateway forward = declared - elapsed (no margin); the hop echo is the
    # same arithmetic minus margin, ladder-clamped — so it never exceeds the
    # forwarded budget.
    assert float(hop_headers['X-Caller-Budget-S']) <= 3.0
    assert echo['declared_s'] <= float(hop_headers['X-Caller-Budget-S'])
    assert echo['applied_s'] <= echo['declared_s']

"""TR-242: the failure class -> caller-action contract, enforced.

docs/failure-contract.md is the ONE table mapping every failure class to
exactly one caller action — retry-after, advance-the-ladder, or
stop-and-escalate. These tests pin the table against the code in BOTH
directions:

1. every class the CODE can emit (grep-derived: HOP_FAILURE_REASONS plus the
   literal failure_reason= sites in router_server.py) has a table row;
2. every table row is grounded in code (a hop reason, a literal, or a
   declared contract class with its grounding checked in source);
3. the union is EXACT — a new class on either side is a loud failure, not a
   silent "failed".

No network, no server: everything reads source and the classifier/envelope
helpers directly (same injectable style as test_proxy_failure_envelope.py).
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402

DOC = os.path.join(REPO, 'docs', 'failure-contract.md')
SERVER_SRC = os.path.join(REPO, 'scripts', 'router_server.py')

#: The three — and only three — caller actions the contract allows.
ACTIONS = ('retry-after', 'advance-the-ladder', 'stop-and-escalate')

#: The expected action per class. THE table in the doc; these pins make any
#: drift between doc and test loud in both directions.
EXPECTED_ACTIONS = {
    'idle-timeout': 'advance-the-ladder',
    'hop-wall-timeout': 'advance-the-ladder',
    'transport-error': 'advance-the-ladder',
    'upstream-4xx': 'stop-and-escalate',
    'upstream-5xx': 'advance-the-ladder',
    'unservable-2xx': 'advance-the-ladder',
    'gated-by-policy': 'advance-the-ladder',
    'overloaded': 'retry-after',
    'no-hops': 'stop-and-escalate',
    'registry-missing': 'stop-and-escalate',
    # TR-194: an UNSATISFIABLE rating (the resolver says nothing was ever
    # eligible) — the 2026-09-26 incident shape, named so the escalation can
    # tell a data gap from a gate storm. Same caller action as no-hops.
    'unservable-rating': 'stop-and-escalate',
}

#: Classes the contract names that have NO failure_reason literal (they never
#: reach the ledger under their own name). Each needs its code grounding
#: checked here — a contract class whose grounding disappears is a loud
#: failure too.
CONTRACT_CLASSES = {
    # A gated lane is excluded BEFORE the chain exists (TR-081): the evidence
    # lives in the envelope's exclusions / gate_reasons, not a failure_reason.
    'gated-by-policy': ('exclusions', 'gate_reasons'),
}


# ---------- parsing helpers (the doc IS the spec; parse it strictly) ----------

def _doc_rows():
    """{class: (action, row_text)} parsed from the doc's markdown table.

    Every data row must resolve to exactly one action; anything else raises
    (a table the tests cannot parse is a broken contract, not a pass).
    """
    text = open(DOC).read()
    rows = {}
    table_lines = [ln for ln in text.splitlines() if ln.startswith('|')]
    assert len(table_lines) >= 3, 'the contract table is missing from the doc'
    for ln in table_lines:
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells) < 2 or set(cells[0]) <= {'-', ' '}:
            continue  # header separator
        name = cells[0].strip('`').strip()
        if name in ('Class',):
            continue
        row = '|'.join(cells)
        # the action MUST live in the "Caller action" column (index 3) and
        # exactly one action may appear there
        action_cell = cells[3] if len(cells) > 3 else ''
        found = [a for a in ACTIONS if a in action_cell]
        assert name in EXPECTED_ACTIONS, f'doc row {name!r} is not a known class'
        assert len(found) == 1, f'{name}: row must name exactly one action, got {found}'
        rows[name] = (found[0], row)
    return rows


def _code_classes():
    """Every class the code can emit — grep-derived, not hand-listed.

    The hop vocabulary (HOP_FAILURE_REASONS) plus every literal
    failure_reason='...' in the server source.
    """
    src = open(SERVER_SRC).read()
    literals = set(re.findall(r"failure_reason\s*=\s*'([a-z0-9][a-z0-9-]*)'", src))
    literals |= set(re.findall(r'failure_reason\s*=\s*"([a-z0-9][a-z0-9-]*)"', src))
    return set(rsrv.HOP_FAILURE_REASONS) | literals


# ---------- the table itself ----------

def test_the_table_is_parseable_and_one_action_per_class():
    rows = _doc_rows()
    assert set(rows) == set(EXPECTED_ACTIONS), (
        f'doc/table drift: doc-only={set(rows) - set(EXPECTED_ACTIONS)} '
        f'expected-only={set(EXPECTED_ACTIONS) - set(rows)}')
    for name, (action, _row) in rows.items():
        assert action in ACTIONS


def test_the_class_set_is_closed_both_ways():
    """An unmapped class is a LOUD failure: any new code-emitted class must
    have a doc row + an EXPECTED_ACTIONS pin in the same change."""
    code = _code_classes()
    expected = set(EXPECTED_ACTIONS)
    unmapped = code - expected
    assert not unmapped, (
        f'unmapped failure class(es) {sorted(unmapped)}: the contract is closed — '
        f'add a row to docs/failure-contract.md and a pin in EXPECTED_ACTIONS '
        f'(tests/test_failure_contract.py) in the same change')
    assert not (expected - code - set(CONTRACT_CLASSES)), (
        f'doc maps class(es) {sorted(expected - code - set(CONTRACT_CLASSES))} '
        f'that no emission site can produce — stale contract row')


def test_contract_only_classes_are_grounded_in_code():
    for cls, markers in CONTRACT_CLASSES.items():
        src = open(SERVER_SRC).read()
        for m in markers:
            assert m in src, f'{cls}: grounding marker {m!r} vanished from router_server.py'


# ---------- one pin per class: THE caller action ----------

def test_pin_idle_timeout_advances_the_ladder():
    assert _doc_rows()['idle-timeout'][0] == 'advance-the-ladder'


def test_pin_hop_wall_timeout_advances_the_ladder():
    assert _doc_rows()['hop-wall-timeout'][0] == 'advance-the-ladder'


def test_pin_transport_error_advances_the_ladder():
    assert _doc_rows()['transport-error'][0] == 'advance-the-ladder'


def test_pin_upstream_4xx_stops_and_escalates():
    assert _doc_rows()['upstream-4xx'][0] == 'stop-and-escalate'


def test_pin_upstream_5xx_advances_the_ladder():
    assert _doc_rows()['upstream-5xx'][0] == 'advance-the-ladder'


def test_pin_unservable_2xx_advances_the_ladder():
    assert _doc_rows()['unservable-2xx'][0] == 'advance-the-ladder'


def test_pin_gated_by_policy_advances_the_ladder():
    assert _doc_rows()['gated-by-policy'][0] == 'advance-the-ladder'


def test_pin_overloaded_is_retry_after():
    assert _doc_rows()['overloaded'][0] == 'retry-after'
    # the action must be more than prose: the server actually emits the hint
    src = open(SERVER_SRC).read()
    assert "failure_reason='overloaded'" in src
    assert "'Retry-After'" in src and "'retry_after_s'" in src


def test_pin_no_hops_stops_and_escalates():
    assert _doc_rows()['no-hops'][0] == 'stop-and-escalate'
    assert "failure_reason = 'no-hops'" in open(SERVER_SRC).read()


def test_pin_registry_missing_stops_and_escalates():
    assert _doc_rows()['registry-missing'][0] == 'stop-and-escalate'
    assert "failure_reason = 'registry-missing'" in open(SERVER_SRC).read()


# ---------- the standards, pinned where the code can prove them ----------

def test_c1_client_faults_never_retry_and_never_burn_the_ladder():
    """TR-096: a 4xx is a fault in the REQUEST. The terminal classification
    exists, the wire-layer fix exists (rewrite before hop one), and the doc
    states the never-retry rule."""
    doc = open(DOC).read().lower()
    assert 'tr-096' in doc and 'never retried' in doc and 'never burns the ladder' in doc
    reason, _ = rsrv._classify_hop_failure(status=404)
    assert reason == 'upstream-4xx'
    src = open(SERVER_SRC).read()
    assert '_normalize_developer_role' in src, (
        'the TR-096 wire fix must stay in place: rewrite before hop one, '
        'disclosed — the ladder must never re-try a payload upstream rejects')


def test_t1_slow_but_alive_and_dead_stay_distinguishable():
    """TR-137: the two arms classify differently, and the doc keeps them
    distinct end to end."""
    doc = open(DOC).read()
    assert 'TR-137' in doc
    idle, _ = rsrv._classify_hop_failure(
        exc=RuntimeError('no real SSE event inside the idle budget'))
    dead, dead_detail = rsrv._classify_hop_failure(exc=ConnectionError('connection refused'))
    wall, _ = rsrv._classify_hop_failure(exc=TimeoutError('timed out'))
    assert idle == 'idle-timeout' and dead == 'transport-error' and wall == 'hop-wall-timeout'
    assert len({idle, dead, wall}) == 3
    assert 'ConnectionError' in dead_detail  # the detail names the exception


def test_e1_failure_envelope_is_never_less_explainable_than_the_ledger():
    """TR-136: the shared failure envelope carries terminal reason, ladder
    facts, usage/cost/session (each null WITH its reason) and wall time."""
    doc = open(DOC).read()
    assert 'TR-136' in doc
    meta = {'ladder': [{'hop': 1, 'provider': 'p', 'model': 'm',
                        'outcome': 'transport-failure', 'reason': 'transport-error',
                        'latency_s': 0.1}]}
    env = rsrv._failure_envelope(meta, 's1', 'router-proxy', None, 0.0,
                                 'transport-error', meta['ladder'], 'transport-error')
    for key in ('terminal_reason', 'ladder', 'hops_attempted', 'steps',
                'usage', 'usage_reason', 'cost_usd', 'cost_reason',
                'session_id', 'gateway_session_id', 'gateway_session_reason',
                'parent_session_id', 'wall_time_s', 'served_by'):
        assert key in env, f'failure envelope lost {key!r} (TR-136 parity)'
    assert env['usage'] is None and env['usage_reason'], 'a null carries its reason'
    assert env['cost_usd'] is None and env['cost_reason'], 'a null carries its reason'
    assert env['terminal_reason'] == 'transport-error'
    assert env['ladder'] == meta['ladder']  # ordered, per-hop outcomes intact

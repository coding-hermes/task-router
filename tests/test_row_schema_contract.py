"""TR-189: the proxy ledger row's SHAPE is a contract.

Origin: `skipped_hops` was an INTEGER count for the field's whole life; a change
silently made it a LIST, broke three test files and went red in the guard before
anyone could say which field had moved. Nothing asserted the shape at the
source. This file pins the TYPE of every field a consumer reads, driven off ONE
real row produced by the code path (proxy_chat -> _proxy_chat_inner ->
_proxy_record -> router_outcomes), so the next shape drift fails HERE with the
field named — not three files away.

Contract notes:
- the row's evidence is FLATTENED onto the row: there is no nested
  `chain_evidence` key on new rows; `chain`, `exclusions`, `skipped_hops`,
  `skipped_hops_source`, ... are top-level row fields (router_server
  `_proxy_record`).
- `skipped_hops_detail` is BOTH: it rides on the chain-evidence structure AND, since the projection
  bug was fixed, reaches the row itself - the row must be able to say WHICH positions were skipped,
  not only how many.
  structure `_chain_evidence()` builds, which the failure envelope and the
  flow view read (see tests/test_ui_page.py). It is pinned at the same source.
- a malformed resolver payload must still produce a row: fail-open is sacred
  (TR-184). Non-object exclusions are dropped, a dict among them is real
  evidence and is KEPT.
- every test isolates router state into tmp_path (ROUTER_STATE_DIR /
  ROUTING_OUTCOMES_FILE): a unit test must not write the live gates or ledger.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


# ---------------------------------------------------------------------------
# The CONTRACT: (field, allowed types) for every field a consumer reads.
# A union means both shapes are legal on real rows (e.g. cost_usd is a float
# when the hop was priced, None when the row honestly does not know).
# ---------------------------------------------------------------------------
ROW_FIELD_TYPES = (
    ('skipped_hops', (int,)),
    ('skipped_hops_source', (str,)),
    ('skipped_hops_detail', (list,)),
    ('skipped_hops_truncated', (bool,)),
    ('exclusions', (list,)),
    ('chain', (list, type(None))),
    ('cost_usd', (float, type(None))),
    ('hops_attempted', (int, type(None))),
    ('session_id', (str,)),
    # TR-173: the caller's own X-Hermes-Session-Key — str when the caller sent
    # one (validated), None when it sent none. Never '' standing for "unknown".
    ('caller_session_key', (str, type(None))),
)

#: The evidence structure's own fields consumers read (`_chain_evidence` output).
EVIDENCE_FIELD_TYPES = (
    ('skipped_hops_detail', (list,)),
)

#: List fields whose ELEMENTS are objects (a string in there is not evidence).
LIST_OF_OBJECTS_FIELDS = ('exclusions', 'chain')


def _type_violation(field, allowed, value):
    """The failure message IS the feature: field name + expected + actual type."""
    return (f"row schema contract: field '{field}' expected "
            f"{' or '.join(t.__name__ for t in allowed)}, "
            f"got {type(value).__name__} (value: {repr(value)[:120]})")


def _assert_contract(row, evidence=None):
    """Assert the row (and optionally its chain-evidence) matches the contract.

    Every violation names the field, the expected type(s) and the actual type —
    so a silent type change reads as `field 'skipped_hops' expected int, got
    list`, never as a bare TypeError three consumers away.
    """
    for field, allowed in ROW_FIELD_TYPES:
        value = row.get(field)
        # bool is a subclass of int, so int fields must reject it — but a field whose contract IS
        # bool must accept it. Exclude bool only when bool is not an allowed type.
        ok = isinstance(value, allowed) and (True if bool in allowed else not isinstance(value, bool))
        assert ok, _type_violation(field, allowed, value)
    for field in LIST_OF_OBJECTS_FIELDS:
        items = row.get(field)
        if isinstance(items, list):
            assert all(isinstance(e, dict) for e in items), (
                f"row schema contract: field '{field}' must be a list of objects, "
                f"got element types {[type(e).__name__ for e in items]}")
    if evidence is not None:
        for field, allowed in EVIDENCE_FIELD_TYPES:
            value = evidence.get(field)
            # bool is a subclass of int in Python, so the int fields must reject it — but a field
            # whose contract IS bool must accept it. Exclude bool only when it is not allowed.
            ok = isinstance(value, allowed) and (True if bool in allowed else not isinstance(value, bool))
            assert ok, _type_violation(field, allowed, value)
            assert all(isinstance(e, dict) for e in value), (
                f"row schema contract: field '{field}' must be a list of objects, "
                f"got element types {[type(e).__name__ for e in value]}")


@pytest.fixture(autouse=True)
def _isolated_router_state(tmp_path, monkeypatch):
    """Repo law: no test writes the LIVE router state. Both env gates point into
    tmp_path, so circuit writes and the outcome store land in this test's dir."""
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig-tr189',
                       'profile_id': None, 'problems': [],
                       'model': 'deepseek-flash', 'prompt_version': 'v1'}))
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _served_row_with_evidence(ledger, monkeypatch, session='sess-tr189'):
    """Drive ONE real request through proxy_chat; return (row, chain_evidence).

    The evidence is captured by spying on `_chain_evidence` — the exact dict the
    row builder consumed for this row, not a re-derivation by the test.
    """
    seen = {}
    real = rsrv._chain_evidence

    def spy(resolved, chain):
        ev = real(resolved, chain)
        seen['evidence'] = ev
        return ev

    monkeypatch.setattr(rsrv, '_chain_evidence', spy)
    resolved = {'chain': [
            {'hop': 1, 'provider': 'p1', 'model': 'm1', 'usd_1m': 0.25,
             'in_per_m': 0.05, 'out_per_m': 0.2},
            {'hop': 2, 'provider': 'p2', 'model': 'm2', 'usd_1m': 0.5,
             'in_per_m': 0.1, 'out_per_m': 0.4}],
        'exclusions': [{'hop': 3, 'provider': 'p3', 'model': 'm3',
                        'codes': ['circuit-open'], 'why': ['circuit OPEN']}],
        'sort': 'price'}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: resolved)
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'schema contract probe'}]},
        {'x-router-session': session},
        upstream=lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}],
                                        'usage': {'prompt_tokens': 10,
                                                  'completion_tokens': 2}}))
    assert status == 200, payload
    served = [r for r in _rows(ledger) if r.get('route_outcome') == 'served']
    assert served, f'no served row was written to {ledger}: {_rows(ledger)}'
    return served[0], seen['evidence']


# ---------------------------------------------------------------------------
# 1. The type contract, off one real row.
# ---------------------------------------------------------------------------
def test_one_real_row_pins_every_consumer_field_type(ledger, monkeypatch):
    row, evidence = _served_row_with_evidence(ledger, monkeypatch)
    _assert_contract(row, evidence)
    # union fields: prove the REAL side of each union on this row — tolerance
    # for None must not quietly mean "always None".
    assert isinstance(row['cost_usd'], float) and row['cost_usd'] > 0, \
        f"a priced served row carries a real float cost_usd, got {row['cost_usd']!r}"
    assert isinstance(row['hops_attempted'], int) and row['hops_attempted'] >= 1
    assert isinstance(row['chain'], list) and row['chain'], \
        'a served row carries its option chain'
    assert row['skipped_hops'] == 1
    assert row['skipped_hops_source'] == 'derived-from-exclusions'
    assert [e['hop'] for e in evidence['skipped_hops_detail']] == [3]


# ---------------------------------------------------------------------------
# 2. A type change fails WITH THE FIELD NAMED.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('container,field,bad_value,expected_name', [
    # the 2026-09-26 regression, verbatim: the count became a list
    ('row', 'skipped_hops', [0], 'int'),
    ('evidence', 'skipped_hops_detail', 2, 'list'),
    ('row', 'session_id', 42, 'str'),
    ('row', 'cost_usd', '0.01', 'float'),
])
def test_a_type_change_fails_with_the_field_named(ledger, monkeypatch, container,
                                                  field, bad_value, expected_name):
    row, evidence = _served_row_with_evidence(ledger, monkeypatch)
    mutated_row, mutated_evidence = dict(row), dict(evidence)
    if container == 'row':
        mutated_row[field] = bad_value
    else:
        mutated_evidence[field] = bad_value
    with pytest.raises(AssertionError) as caught:
        _assert_contract(mutated_row, mutated_evidence)
    message = str(caught.value)
    assert field in message, f'failure must name the field: {message}'
    assert expected_name in message, f'failure must name the EXPECTED type: {message}'
    assert type(bad_value).__name__ in message, \
        f'failure must name the ACTUAL type: {message}'


# ---------------------------------------------------------------------------
# 3. Malformed resolver payloads still produce a row; only objects survive,
#    and a dict in a mixed list is real evidence and is KEPT.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('bad_exclusions,expected_keep', [
    ('garbage', 0),      # a string payload is not evidence
    (None, 0),           # absent is zero, with the derived source saying so
    (7, 0),              # the exact shape that raised TypeError before TR-184
    ([{'hop': 1, 'provider': 'p1', 'model': 'm1', 'codes': ['circuit-open'],
       'why': ['down']}, 'not-a-dict'], 1),
])
def test_malformed_resolver_exclusions_still_produce_a_row(ledger, monkeypatch,
                                                           bad_exclusions,
                                                           expected_keep):
    resolved = {'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1',
                           'usd_1m': 0.1, 'in_per_m': 0.05, 'out_per_m': 0.1}],
                'exclusions': bad_exclusions, 'sort': 'price'}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: resolved)
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'malformed resolver probe'}]},
        {'x-router-session': f'sess-tr189-bad-{expected_keep}'},
        upstream=lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}],
                                        'usage': {'prompt_tokens': 4,
                                                  'completion_tokens': 1}}))
    assert status == 200, payload
    served = [r for r in _rows(ledger) if r.get('route_outcome') == 'served']
    assert served, f'the malformed resolver payload still produced a row: {_rows(ledger)}'
    row = served[0]
    _assert_contract(row)
    assert isinstance(row['exclusions'], list)
    assert all(isinstance(e, dict) for e in row['exclusions']), row['exclusions']
    assert len(row['exclusions']) == expected_keep, row['exclusions']
    # the surviving dict (if any) is intact — real evidence is kept, not shredded
    if expected_keep:
        assert row['exclusions'][0]['hop'] == 1
        assert row['exclusions'][0]['codes'] == ['circuit-open']
    # the count agrees with what survived, and says which authority produced it
    assert row['skipped_hops'] == expected_keep
    assert row['skipped_hops_source'] == 'derived-from-exclusions'


# ---------------------------------------------------------------------------
# 4. A no-hop row still explains itself, and carries no chain.
# ---------------------------------------------------------------------------
def test_no_hop_row_carries_the_explaining_fields_and_no_chain(ledger, monkeypatch):
    resolved = {'chain': [],
                'exclusions': [
                    {'hop': 1, 'provider': 'xkiro', 'model': 'glm-5.3-flash',
                     'why': ['circuit OPEN (provider-level, api_down)'],
                     'codes': ['circuit-open']},
                    {'hop': 2, 'provider': 'clinepass', 'model': 'deepseek-v4-flash',
                     'why': ['quota GATED: blocked'], 'codes': ['quota-gated']}],
                'gate': {'quota': 'blocked'}, 'sort': 'price'}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: resolved)

    def _must_not_be_called(*a, **k):
        raise AssertionError('an empty chain must not reach any upstream')

    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'no-hops probe'}]},
        {'x-router-session': 'sess-tr189-nohop'},
        upstream=_must_not_be_called)
    assert status == 503
    no_hops = [r for r in _rows(ledger) if r.get('route_outcome') == 'no-hops']
    assert no_hops, f'no no-hops row was written: {_rows(ledger)}'
    row = no_hops[0]
    # the SAME type contract holds on this row (cost_usd/hops_attempted on their
    # None/int sides, session_id a string, exclusions a list of objects)
    _assert_contract(row)
    # the fields a reader needs to explain WHY nothing was eligible
    assert row['hops_attempted'] == 0
    assert row['chain_length'] == 0
    assert [e['codes'] for e in row['exclusions']] == [['circuit-open'],
                                                       ['quota-gated']]
    assert row['skipped_hops'] == 2
    assert isinstance(row['skipped_hops_source'], str) and row['skipped_hops_source']
    assert row['gate'] == {'quota': 'blocked'}
    assert isinstance(row['session_id'], str) and row['session_id']
    # and it does NOT carry a chain: empty (or absent) — never a fabricated hop
    assert not row.get('chain'), \
        f"a no-hop row must not carry a chain, got {row.get('chain')!r}"

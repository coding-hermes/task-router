"""TR-188: the proxy_e2e battery's PARSING + assertion logic, offline.

The live battery drives one real proxied request; these tests drive the LOGIC
against fake rows so a malformed row or a fake zero can never pass the battery
silently. No network, no live services: every case feeds synthetic
response/ledger/flow payloads into proxy_e2e.evaluate() and the pure
classifiers, exactly what the live run executes at step time.

Covered, per the row:
  * interpretable    — served-with-chain / no-hops-with-reason / failed-with-reason PASS
  * ambiguous        — served-without-hop, served-without-chain, no-hops-without-reason,
                       unknown outcome FAIL, each named
  * fake-zero        — cost 0.0 with no basis FAILs; 0.0 WITH a basis and
                       None WITH a basis pass (unknown is reported as unknown)
  * missing artefact — flow found=true with an empty artefacts_available still
                       passes (missing is reported, not fatal), while a
                       found=false or shape-broken flow fails
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import proxy_e2e as pe  # noqa: E402


# --------------------------------------------------------------------------- fixtures

def _served_row(**over):
    row = {
        'source_system': 'router-proxy',
        'session_id': 'router-proxy:sess-e2e-1',
        'parent_session_id': 'sess-e2e-1',
        'provider': 'p1', 'model': 'm1',
        'success': True,
        'route_outcome': 'served',
        'failure_reason': None,
        'hops_attempted': 2,
        'served_by_hop': 3,
        'max_hops': 3,
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1'}],
        'chain_length': 3,
        'cost_usd': 0.0042,
        'price_basis': 'public split (in_per_m/out_per_m)',
        'gateway_session_id': 'gw-1',
    }
    row.update(over)
    return row


def _nohops_row(**over):
    row = {
        'source_system': 'router-proxy',
        'session_id': 'router-proxy:sess-e2e-2',
        'parent_session_id': 'sess-e2e-2',
        'provider': 'none', 'model': 'none',
        'success': False,
        'route_outcome': 'no-hops',
        'failure_reason': 'no-hops',
        'degrade_reason': 'nothing eligible after gating',
        'hops_attempted': 0,
        'served_by_hop': None,
        'exclusions': [{'hop': 1, 'provider': 'p1', 'model': 'm1', 'codes': ['quota_window']}],
        'cost_usd': None,
        'price_basis': 'no usage block; price not applied',
    }
    row.update(over)
    return row


def _flow(found=True, avail=('envelope', 'ledger_row', 'gateway_session'), missing=(), **over):
    d = {
        'found': found,
        'session_id': 'router-proxy:sess-e2e-1',
        'request': {'cost_usd': 0.0042, 'served_lane': 'p1/m1'},
        'hops': {'attempted': 2, 'served_position': 3},
        'artefacts_read': {'envelope': True, 'ledger_row': True, 'gateway_session': True},
        'artefacts_available': list(avail),
        'artefacts_missing': list(missing),
        'note': '1 ledger row(s)',
    }
    d.update(over)
    return d


_OK_RESPONSE = {'status': 200, 'body': {'choices': [{'message': {'content': 'ok'}}]}, 'error': None}


def _checks(summary):
    return {c['name']: c for c in summary['checks']}


# --------------------------------------------------------------------------- interpretable

def test_served_with_chain_is_interpretable_and_summary_carries_the_facts():
    s = pe.evaluate(_OK_RESPONSE, [_served_row()], _flow(), 'router-proxy:sess-e2e-1')
    assert s['ok'] is True
    assert s['outcome'] == 'served'
    assert s['served_hop'] == 3
    assert s['hops'] == {'attempted': 2, 'served_position': 3}
    assert s['cost']['usd'] == 0.0042 and s['cost']['basis']
    assert _checks(s)['row-interpretable']['ok'] is True
    assert 'served-with-chain' in _checks(s)['row-interpretable']['detail']


def test_nohops_with_reason_is_interpretable():
    s = pe.evaluate(_OK_RESPONSE, [_nohops_row()], _flow(avail=('ledger_row',), missing=('envelope', 'gateway_session')), 'router-proxy:sess-e2e-2')
    c = _checks(s)
    assert c['row-interpretable']['ok'] is True
    assert 'no-hops-with-reason' in c['row-interpretable']['detail']


def test_failed_with_reason_is_interpretable():
    row = _served_row(route_outcome='failed', success=False, served_by_hop=None,
                      hops_attempted=3, cost_usd=None,
                      price_basis='no usage block; price not applied',
                      failure_reason='upstream 502: bad gateway',
                      chain=[], chain_length=None)
    s = pe.evaluate({'status': 502, 'body': {'error': 'no hop served a response'}, 'error': None},
                    [row], _flow(avail=('ledger_row',), missing=('envelope', 'gateway_session')),
                    'router-proxy:sess-e2e-1')
    c = _checks(s)
    # a structured failure is an allowed step-1 result, and the row itself is interpretable
    assert c['response-ok-or-structured']['ok'] is True
    assert c['row-interpretable']['ok'] is True
    assert 'failed-with-reason' in c['row-interpretable']['detail']


# --------------------------------------------------------------------------- ambiguous

def test_served_without_served_by_hop_is_ambiguous():
    s = pe.evaluate(_OK_RESPONSE, [_served_row(served_by_hop=None)], _flow(), 'x')
    c = _checks(s)
    assert s['ok'] is False
    assert c['row-interpretable']['ok'] is False
    assert 'served_by_hop is missing' in c['row-interpretable']['detail']


def test_served_without_chain_evidence_is_ambiguous():
    s = pe.evaluate(_OK_RESPONSE, [_served_row(chain=[], chain_length=None)], _flow(), 'x')
    c = _checks(s)
    assert c['row-interpretable']['ok'] is False
    assert 'no chain evidence' in c['row-interpretable']['detail']


def test_nohops_without_reason_is_ambiguous():
    s = pe.evaluate(_OK_RESPONSE, [_nohops_row(degrade_reason=None, failure_reason=None,
                                               exclusions=None)], _flow(), 'x')
    c = _checks(s)
    assert c['row-interpretable']['ok'] is False
    assert 'WHY' in c['row-interpretable']['detail']


def test_unknown_outcome_is_ambiguous():
    s = pe.evaluate(_OK_RESPONSE, [_served_row(route_outcome=None)], _flow(), 'x')
    c = _checks(s)
    assert c['row-interpretable']['ok'] is False
    assert 'ambiguous-outcome-missing' in c['row-interpretable']['detail']


def test_ambiguous_rows_fail_the_whole_battery():
    s = pe.evaluate(_OK_RESPONSE, [_served_row(), _served_row(served_by_hop=None)], _flow(), 'x')
    assert s['ok'] is False


# --------------------------------------------------------------------------- fake zero / cost

def test_fake_zero_fails_the_battery():
    row = _served_row(cost_usd=0.0, price_basis=None)
    s = pe.evaluate(_OK_RESPONSE, [row], _flow(), 'x')
    c = _checks(s)
    assert c['no-fake-zero']['ok'] is False
    assert 'fake-zero' in c['no-fake-zero']['detail']
    assert s['ok'] is False


def test_zero_with_a_basis_passes_and_reports_the_basis():
    row = _served_row(cost_usd=0.0, price_basis='plan-included (public_price 0.0)')
    v = pe.classify_cost(row)
    assert v['ok'] is True and v['verdict'] == 'zero-with-basis'
    s = pe.evaluate(_OK_RESPONSE, [row], _flow(), 'x')
    assert _checks(s)['no-fake-zero']['ok'] is True
    assert s['cost']['basis'] == 'plan-included (public_price 0.0)'


def test_cost_none_with_a_basis_is_fine_and_reported_as_unknown():
    row = _nohops_row()
    v = pe.classify_cost(row)
    assert v['ok'] is True and v['verdict'] == 'unknown-with-basis'
    assert 'no usage block' in v['reason']
    s = pe.evaluate(_OK_RESPONSE, [row], _flow(avail=('ledger_row',), missing=('envelope', 'gateway_session')), 'x')
    assert s['ok'] is True
    assert s['cost']['usd'] is None and s['cost']['verdict'] == 'unknown-with-basis'


def test_cost_none_WITHOUT_a_basis_fails():
    row = _nohops_row(price_basis=None)
    s = pe.evaluate(_OK_RESPONSE, [row], _flow(avail=('ledger_row',), missing=('envelope', 'gateway_session')), 'x')
    c = _checks(s)
    assert c['no-fake-zero']['ok'] is False
    assert 'None with no price_basis' in c['no-fake-zero']['detail']


# --------------------------------------------------------------------------- flow / artefacts

def test_flow_found_with_artefact_lists_passes_even_when_an_artefact_is_missing():
    s = pe.evaluate(_OK_RESPONSE, [_served_row()],
                    _flow(avail=('envelope', 'ledger_row'), missing=('gateway_session',)), 'x')
    c = _checks(s)
    assert c['flow-found-with-artefacts']['ok'] is True
    assert s['artefacts_missing'] == ['gateway_session']


def test_flow_not_found_fails():
    s = pe.evaluate(_OK_RESPONSE, [_served_row()], _flow(found=False), 'x')
    c = _checks(s)
    assert c['flow-found-with-artefacts']['ok'] is False


def test_flow_shape_without_artefact_lists_fails():
    broken = _flow()
    del broken['artefacts_available']
    del broken['artefacts_missing']
    s = pe.evaluate(_OK_RESPONSE, [_served_row()], broken, 'x')
    c = _checks(s)
    assert c['flow-found-with-artefacts']['ok'] is False


# --------------------------------------------------------------------------- step 1: response

def test_structured_failure_passes_step_1_but_transport_error_does_not():
    ok = pe.evaluate({'status': 503, 'body': {'error': 'no open hop', '_router': {}}, 'error': None},
                     [_nohops_row()],
                     _flow(found=False), 'x')
    assert _checks(ok)['response-ok-or-structured']['ok'] is True

    dead = pe.evaluate({'status': None, 'body': None, 'error': 'URLError: connection refused'},
                       [], None, 'x')
    c = _checks(dead)
    assert c['response-ok-or-structured']['ok'] is False
    assert c['ledger-row-exists']['ok'] is False
    assert dead['ok'] is False


def test_unparseable_failure_body_fails_step_1():
    s = pe.evaluate({'status': 502, 'body': None, 'error': None}, [], None, 'x')
    c = _checks(s)
    assert c['response-ok-or-structured']['ok'] is False


# --------------------------------------------------------------------------- parsing helpers

def test_ledger_parsing_tolerates_a_torn_tail_line():
    text = json.dumps(_served_row()) + '\n{"session_id": "torn' + '\n'
    rows, malformed = pe.parse_ledger_rows(text)
    assert len(rows) == 1 and malformed == 1


def test_session_matching_uses_row_and_parent_ids():
    rows = [
        {'session_id': 'router-proxy:tick1', 'parent_session_id': 'tick1'},
        {'session_id': 'hermes:other', 'parent_session_id': 'other'},
        {'session_id': 'router-proxy:tick2', 'parent_session_id': 'tick2'},
    ]
    assert [r['session_id'] for r in pe.rows_for_session(rows, 'router-proxy:tick1')] == ['router-proxy:tick1']
    assert len(pe.rows_for_session(rows, 'tick2')) == 1


def test_human_tail_names_the_six_facts(capsys):
    s = pe.evaluate(_OK_RESPONSE, [_served_row()], _flow(), 'router-proxy:sess-e2e-1')
    tail = pe.human_tail(s)
    for needle in ('session id', 'outcome', 'cost', 'hops attempted', 'served hop', 'artefacts missing'):
        assert needle in tail, f'the human tail must carry: {needle}'
    # unknown cost must be SAID, not rendered as $0
    s2 = pe.evaluate(_OK_RESPONSE, [_nohops_row()],
                     _flow(found=False), 'router-proxy:sess-e2e-2')
    assert 'UNKNOWN' in pe.human_tail(s2)


def test_check_order_is_stable_and_complete():
    """The brief fixes the assertion order; the summary must carry it in order."""
    s = pe.evaluate(_OK_RESPONSE, [_served_row()], _flow(), 'x')
    assert [c['name'] for c in s['checks']] == pe.CHECK_ORDER

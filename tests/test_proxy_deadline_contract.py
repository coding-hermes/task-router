"""TR-264 tests — the caller-declared deadline contract.

The contract: a caller may DECLARE its total budget and deadline shape as
request headers (X-Caller-Budget-S, X-Caller-Deadline-Mode, ...); this layer's
hop budget then becomes arithmetic (declared minus elapsed minus margin,
ladder-clamped), the hop forwarded downstream carries the DECREMENTED
remainder (never the original), the response ECHOES what was applied, and the
ledger row carries the full deadline block — declared, applied, aligned, and
the reason whenever the ladder forced us stricter than the caller's remaining
budget. Absent headers keep the TR-241 configured defaults and stamp
declared=false with the reason on every surface (a null never stands alone).

Hermetic: the upstream is injected, no network, no registry dependency
(the chain and the metering stubs mirror tests/test_proxy_metering.py).
"""
import json
import os
import sys
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)  # isolated
    return tmp_path


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m,
                       'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


def _wire(monkeypatch, hops):
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(*hops))
    monkeypatch.setattr(rsrv, '_proxy_requirements',
                        lambda *a, **k: ('classifier', {
                            'matrix': {'code_gen': 1}, 'complexity_sig': 'abc',
                            'profile_id': None}))


def _recorder(monkeypatch):
    rows = []
    monkeypatch.setattr(rsrv, '_proxy_record',
                        lambda provider, model, ok, req, **kw:
                        rows.append({'provider': provider, 'model': model,
                                     'ok': ok, **kw}))
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *a, **k: '')
    return rows


DECLARED = {'X-Caller-Name': 'deadline-test',
            'X-Caller-Budget-S': '900',
            'X-Caller-Deadline-Mode': 'wall',
            'X-Caller-Margin-S': '5'}

BODY = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hi'}]}


def _served_upstream(seen=None):
    def upstream(path, body, headers):
        if seen is not None:
            seen.setdefault('headers', []).append(dict(headers))
        return 200, {'choices': [{'message': {'role': 'assistant',
                                              'content': 'ok'},
                                   'finish_reason': 'stop'}]}
    return upstream


# ---------- A: declared headers drive the budget and the row ----------

def test_declared_headers_derive_the_budget_and_stamp_the_row(
        proxy_env, monkeypatch):
    _wire(monkeypatch, [('p1', 'm1')])
    rows = _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY, dict(DECLARED),
                                  upstream=_served_upstream())
    assert status == 200
    row = rows[0]
    deadline = row['deadline']
    # every declared field present on the row (criterion A)
    assert deadline['declared'] is True
    assert deadline['declared_s'] == pytest.approx(900.0)
    assert deadline['mode'] == 'wall'
    assert deadline['applied_s'] > 0.0
    assert deadline['layer'] == 'router-proxy'
    assert deadline['aligned'] is True
    assert deadline['stricter_reason'] is None
    # the applied hop budget honours the declared arithmetic
    assert deadline['applied_s'] <= 900.0 - 5.0
    assert deadline['remaining_declared_s'] <= 895.0
    # the envelope carries the same verdict
    assert out['_router']['deadline']['declared_s'] == pytest.approx(900.0)


# ---------- B: the forced-stricter path names itself ----------

def test_stricter_path_is_stamped_on_headers_and_row(proxy_env, monkeypatch):
    _wire(monkeypatch, [('p1', 'm1')])
    rows = _recorder(monkeypatch)
    # a buffered 1800s budget exceeds the caller-patience ceiling -> clamped
    big = dict(DECLARED, **{'X-Caller-Budget-S': '1800'})
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY, big,
                                  upstream=_served_upstream())
    assert status == 200
    deadline = out['_router']['deadline']
    ceiling = rsrv.ROUTER_CALLER_PATIENCE_S - rsrv.ROUTER_HOP_LADDER_MARGIN_S
    assert deadline['applied_s'] == pytest.approx(ceiling)
    assert deadline['applied_s'] < 1800.0
    assert deadline['aligned'] is False
    assert deadline['stricter_reason']
    row_deadline = rows[0]['deadline']
    assert row_deadline['stricter_reason'] == deadline['stricter_reason']
    # the SAME run stamps the wire header (aligned + stricter arms in one)
    echo = out['_router_headers']
    assert echo['X-Applied-Stricter-Than-Caller'] == deadline['stricter_reason']
    # ...and the ALIGNED arm carries no such header
    _wire(monkeypatch, [('p1', 'm1')])
    rows2 = _recorder(monkeypatch)
    status2, out2 = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                    dict(DECLARED),
                                    upstream=_served_upstream())
    assert status2 == 200
    assert 'X-Applied-Stricter-Than-Caller' not in out2['_router_headers']
    assert rows2[0]['deadline']['aligned'] is True


# ---------- C: no headers -> configured defaults + declared=false ----------

def test_absent_headers_keep_defaults_and_stamp_declared_false(
        proxy_env, monkeypatch):
    _wire(monkeypatch, [('p1', 'm1')])
    rows = _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY, {},
                                  upstream=_served_upstream())
    assert status == 200
    deadline = rows[0]['deadline']
    assert deadline['declared'] is False
    assert deadline['declared_s'] is None
    assert deadline['declared_reason']
    assert deadline['applied_s'] is None
    assert deadline['applied_reason']
    assert out['_router_headers']['X-Applied-Declared'] == 'false'
    assert out['_router_headers']['X-Applied-Layer'] == 'router-proxy'


# ---------- D: the forwarded budget is DECREMENTED ----------

def test_forwarded_budget_is_decremented_not_passed_through(
        proxy_env, monkeypatch):
    seen = {}
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    status, _out = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                   dict(DECLARED),
                                   upstream=_served_upstream(seen))
    assert status == 200
    fwd = {k.lower(): v for k, v in seen['headers'][0].items()}
    assert fwd['x-caller-budget-s'] != '900', (
        'TR-264: an unchanged passthrough is the defect this test exists to '
        'catch — the forwarded budget must be decremented')
    assert float(fwd['x-caller-budget-s']) <= 900.0 - 5.0
    assert fwd['x-caller-deadline-mode'] == 'wall'
    assert fwd['x-caller-margin-s'] == '5.000000'
    assert 'x-caller-name' not in {k.lower() for k in fwd}


def test_second_hop_forwards_the_remaining_budget(proxy_env, monkeypatch):
    """Hop 1 fails with a non-retryable code, hop 2 sees hop 1's time charged.

    TR-288 composes with TR-264: a transient 503 RETRIES the same hop (same
    budget, by design), so this test uses a 402/no-credit code — RETRY_POLICY
    says 'skip', the ladder advances, and the deadline must charge hop 1's
    elapsed time before hop 2 leaves.
    """
    seen = {}
    calls = {'n': 0}

    def flaky(path, body, headers):
        calls['n'] += 1
        seen.setdefault('headers', []).append(dict(headers))
        if calls['n'] == 1:
            return 402, {'error': 'no credit on hop one'}
        return 200, {'choices': [{'message': {'role': 'assistant',
                                              'content': 'ok'},
                                       'finish_reason': 'stop'}]}

    _wire(monkeypatch, [('p1', 'm1'), ('p2', 'm2')])
    _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                  dict(DECLARED), upstream=flaky)
    assert status == 200
    assert len(seen['headers']) == 2
    h1 = {k.lower(): v for k, v in seen['headers'][0].items()}
    h2 = {k.lower(): v for k, v in seen['headers'][1].items()}
    b1 = float(h1['x-caller-budget-s'])
    b2 = float(h2['x-caller-budget-s'])
    assert b1 <= 895.0
    assert b2 < b1, 'the second hop must see a SMALLER budget (time charged)'


def test_undeclared_request_forwards_no_deadline_headers(
        proxy_env, monkeypatch):
    seen = {}
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    rsrv.proxy_chat('/v1/chat/completions', BODY, {},
                    upstream=_served_upstream(seen))
    fwd = {k.lower() for k in seen['headers'][0]}
    assert 'x-caller-budget-s' not in fwd
    assert 'x-router-hop-elapsed-s' not in fwd


# ---------- E: the response echoes what was applied ----------

def test_response_echoes_the_applied_surface(proxy_env, monkeypatch):
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                  dict(DECLARED),
                                  upstream=_served_upstream())
    echo = out['_router_headers']
    assert echo['X-Applied-Layer'] == 'router-proxy'
    assert float(echo['X-Applied-Budget-S']) == pytest.approx(900.0)
    assert echo['X-Applied-Deadline-Mode'] == 'wall'
    assert 0.0 < float(echo['X-Applied-Hop-Budget-S']) <= 895.0
    assert echo['X-Applied-Declared'] == 'true'


# ---------- F: the budget actually REACHES the hop at the wire ----------

def test_declared_budget_reaches_the_default_upstream_socket(
        proxy_env, monkeypatch):
    """End to end through the REAL default upstream caller: the declared
    budget is what lands on the socket timeout, and the forwarded header is
    the decremented remainder. This is the criterion-D arm against the true
    transport site, not an injected upstream."""
    captured = {}

    class _Resp(io.BytesIO if False else object):
        pass

    class _Fake:
        status = 200
        headers = {'Content-Type': 'application/json'}

        def read(self):
            return json.dumps({'choices': [{'message': {'role': 'assistant',
                                                        'content': 'ok'}}],
                               'usage': {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        captured['timeout'] = timeout
        captured['headers'] = {k.lower(): v for k, v in req.headers.items()}
        return _Fake()

    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions', BODY, dict(DECLARED),
        upstream=lambda p, b, h: rsrv._proxy_upstream_default(p, b, h,
                                                              _opener=opener))
    assert status == 200
    assert captured['timeout'] <= 895.0
    assert captured['headers']['x-caller-budget-s'] != '900'
    assert float(captured['headers']['x-caller-budget-s']) <= 895.0


def test_chain_time_stays_inside_the_declared_budget(proxy_env, monkeypatch):
    """Criterion F, timing arm: for a declared 900s the OLD default hop
    budget (180s) would have violated nothing, but for a declared budget
    BELOW the old default the ladder must obey the DECLARATION, not the
    default. The applied hop budget and the whole chain must fit inside
    declared - margin."""
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    tight = dict(DECLARED, **{'X-Caller-Budget-S': '120'})
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY, tight,
                                  upstream=_served_upstream())
    assert status == 200
    deadline = out['_router']['deadline']
    assert deadline['applied_s'] <= 115.0 + 1e-6   # never above declared-margin
    assert deadline['applied_s'] > 114.9           # micro-elapsed is charged
    # the socket saw the applied budget, not the 180s default; the real
    # clock always charges a few hundred microseconds before the hop leaves,
    # so the bound is a float comparison, not a string match
    applied_hdr = float(out['_router_headers']['X-Applied-Hop-Budget-S'])
    assert 114.9 < applied_hdr <= 120.0
    assert deadline['aligned'] is True


def test_old_default_would_have_violated_a_900s_declaration(
        proxy_env, monkeypatch):
    """The criterion-F differential: 900s declared vs the 180s default. The
    ladder must pick the DECLARED budget (895s after margin), which is
    strictly larger than the old default — the declaration widened the hop,
    never narrowed it."""
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                  dict(DECLARED),
                                  upstream=_served_upstream())
    echo = out['_router_headers']
    assert float(echo['X-Applied-Hop-Budget-S']) == pytest.approx(895.0)
    assert float(echo['X-Applied-Hop-Budget-S']) > 180.0, (
        'the old 180s default must NOT be what applied to a 900s declaration')


# ---------- TR-265: the deadline-report subcommand ----------

def _deadline_row(layer='router-proxy', declared=True, declared_s=900.0,
                  applied_s=180.0, mode='wall', margin=5.0, aligned=False,
                  ts=1791100000.0, **extra):
    """One outcome row shaped exactly like the live store's TR-264 block
    (scripts/router_server.py:_dl_deadline / _no_deadline)."""
    dl = {'declared': declared,
          'declared_s': declared_s if declared else None,
          'mode': mode if declared else None,
          'applied_s': applied_s if declared else None,
          'layer': layer if declared else 'router-proxy',
          'aligned': aligned if declared else True,
          'stricter_reason': (None if aligned or not declared
                              else 'ladder clamp (test)'),
          'margin_s': margin,
          'ts': ts}
    dl.update(extra)
    return {'source_system': 'router-proxy', 'session_id': 'proxy-test',
            'provider': 'p', 'model': 'm', 'ts': ts, 'deadline': dl}


def test_report_subcommand_classifies_binding_guess_and_other(
        tmp_path, monkeypatch, capsys):
    import router_outcomes as ro
    store = tmp_path / 'outcomes.jsonl'
    rows = [
        # 3 binding rows at the proxy (the 2026-10-03 shape: hop budget
        # squeezed far below the declared caller budget)
        _deadline_row(ts=1791100000.0),
        _deadline_row(ts=1791100060.0),
        _deadline_row(ts=1791100120.0),
        # 1 binding row at a different layer (aligned=True but applied
        # < declared - margin is still arithmetic binding: the flag is the
        # ladder's opinion, the numbers are the evidence)
        _deadline_row(layer='gateway', applied_s=90.0, aligned=True,
                      ts=1791100180.0),
        # 2 guesses: declared=false and declared key missing entirely
        _deadline_row(declared=False, ts=1791100240.0),
        {'source_system': 'router-proxy', 'provider': 'p', 'model': 'm',
         'ts': 1791100300.0, 'deadline': {'declared': False,
                                          'declared_s': None,
                                          'applied_s': None}},
        # 1 no-deadline row (counts in rows_scanned only) and 1 malformed
        # block (declared=True, no numbers -> 'other', never mis-classed)
        {'source_system': 'hermes', 'provider': 'x', 'model': 'y',
         'ts': 1791100360.0},
        {'source_system': 'router-proxy', 'provider': 'p', 'model': 'm',
         'ts': 1791100420.0, 'deadline': {'declared': True}},
    ]
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    store.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    rc = ro.main(['deadline-report'])
    assert rc == 0
    out = capsys.readouterr().out
    assert '=== Deadline Alignment Report ===' in out
    assert 'Window: all (no --window specified)' in out
    assert 'Rows scanned: 8' in out
    assert 'Rows with deadline block: 7' in out
    assert 'Binding constraints (narrowest layer): 4' in out
    assert 'Guess class (declared=false): 2' in out
    assert 'Other (malformed/foreign deadline blocks): 1' in out
    # histogram: counts re-derivable from the raw rows above (columns are
    # padded to the widest layer name, so match on token, not literal space)
    import re
    assert re.search(r'^  router-proxy\s+3$', out, re.M)
    assert re.search(r'^  gateway\s+1$', out, re.M)
    # summary arithmetic: 3/4 = 75.0%, 2/7 = 28.6%
    assert 'Narrowest link is the router-proxy in 75.0% of binding' in out
    assert '28.6% of deadline-bearing rows are guesses.' in out


def test_report_window_filters_on_ts_and_validates_input(
        tmp_path, monkeypatch, capsys):
    import router_outcomes as ro
    store = tmp_path / 'outcomes.jsonl'
    # 2026-10-03 00:00:00 UTC = 1790985600.0 (verified: date -u -d); two rows
    # inside the day, one out
    in_day = 1790985600.0 + 3600.0
    rows = [_deadline_row(ts=in_day), _deadline_row(ts=in_day + 60.0),
            _deadline_row(ts=1790985600.0 - 1.0)]   # the day before
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    store.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    rc = ro.main(['deadline-report',
                  '--window', '2026-10-03/2026-10-03'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'Window: 2026-10-03/2026-10-03' in out
    assert 'Rows scanned: 3' in out
    assert 'Rows with deadline block: 2' in out
    assert 'Binding constraints (narrowest layer): 2' in out
    # a malformed window is the ONE loud failure: exit 2, message names it
    rc = ro.main(['deadline-report', '--window', 'not-a-window'])
    assert rc == 2
    err = capsys.readouterr().err
    assert 'START/END' in err


def test_report_fails_open_on_missing_and_empty_store(
        tmp_path, monkeypatch, capsys):
    import router_outcomes as ro
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE',
                       str(tmp_path / 'missing.jsonl'))
    assert ro.main(['deadline-report']) == 0
    out = capsys.readouterr().out
    assert 'Rows scanned: 0' in out
    assert 'Rows with deadline block: 0' in out
    assert 'Binding constraints (narrowest layer): 0' in out
    assert 'Guess class (declared=false): 0' in out
    assert '(none: no request declared a deadline' in out
    # empty (zero-byte) store: same zeros, exit 0
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('')
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(empty))
    assert ro.main(['deadline-report']) == 0
    assert 'Rows scanned: 0' in capsys.readouterr().out


def test_report_classify_matches_the_2026_10_03_defect_shape():
    """The TR-243 census: 47 x 180.08s hop-wall-timeouts against a 30m
    caller. A block with applied_s=180.08 vs declared_s=1800 must classify
    as BINDING at the router-proxy layer, and the same numbers re-derive
    from the raw rows via deadline_report() — the same computation, two
    entry points, one answer."""
    import router_outcomes as ro
    dl = {'declared': True, 'declared_s': 1800.0, 'mode': 'wall',
          'applied_s': 180.08, 'layer': 'router-proxy', 'aligned': False,
          'stricter_reason': 'ladder wall backstop', 'margin_s': 5.0}
    assert ro.classify_deadline_row(dl) == 'binding'
    rep = ro.deadline_report([_deadline_row(declared_s=1800.0,
                                            applied_s=180.08)])
    assert rep['binding_total'] == 1
    assert rep['by_layer'] == {'router-proxy': 1}
    # equal-to-declared (within margin) is NOT binding: arithmetic only
    even = dict(dl, applied_s=1795.0)      # declared - margin exactly
    assert ro.classify_deadline_row(even) is None


def test_malformed_headers_fail_open_with_a_reason(proxy_env, monkeypatch):
    _wire(monkeypatch, [('p1', 'm1')])
    _recorder(monkeypatch)
    junk = {'X-Caller-Budget-S': 'not-a-number',
            'X-Caller-Deadline-Mode': 'diagonal',
            'X-Caller-Margin-S': '-4'}
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY, junk,
                                  upstream=_served_upstream())
    assert status == 200                     # the request was SERVED
    deadline = rows0 = None
    deadline = out['_router']['deadline']
    assert deadline['declared'] is False     # junk declares nothing
    assert deadline['declared_reason']
    problems = deadline.get('problems') or []
    assert any('not-a-number' in p for p in problems)
    assert any('diagonal' in p for p in problems)
    assert any('-4' in p for p in problems)


def test_every_row_carries_a_deadline_block_even_on_rejection(
        proxy_env, monkeypatch):
    """The 429 refusal row also carries the deadline verdict (declared=false
    with the reason: nothing ran against the budget)."""
    _wire(monkeypatch, [('p1', 'm1')])
    rows = _recorder(monkeypatch)
    # force the refusal by exhausting the in-flight slots
    real_admission = rsrv._admission

    class _AlwaysFull:
        def __enter__(self):
            raise rsrv._ProxyOverloaded('full (test)')

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rsrv, '_admission', lambda: _AlwaysFull())
    status, out = rsrv.proxy_chat('/v1/chat/completions', BODY,
                                  dict(DECLARED),
                                  upstream=_served_upstream())
    assert status == 429
    refusal_dl = out['_router']['deadline']
    assert refusal_dl['declared'] is True
    assert refusal_dl['applied_s'] is None
    assert refusal_dl['applied_reason']

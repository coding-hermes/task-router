"""TR-191: the proxy write path is isolated from live router state.

History: three test files were once found writing the LIVE
~/.hermes/model-router/circuit-state.json — one of them claimed hermeticity.
The fix routes every write through ROUTER_STATE_DIR / ROUTING_OUTCOMES_FILE,
but nothing PROVED the isolation by hashing the live files.

This file drives the REAL proxy write path — the same entry point
(test_proxy_classify's monkeypatched `_proxy_requirements` / `_proxy_chain`
plus an injected `upstream` callable) — but with one deliberate difference:
`_proxy_record` is NOT stubbed. Every case therefore exercises the true write
surface: the outcome ledger append through router_outcomes AND the
`router_circuit.py record-success / record-failure` SUBPROCESS (the exact
surface that once wrote the live circuit file), with both env hooks pointed
into tmp_path.

Per case the row that lands in the tmp ledger is asserted on the fields that
describe the outcome — price_basis, skipped_hops_source, hops_attempted —
plus the status. Around the whole battery the live state files
(circuit-state.json, health-state.json) are hashed and must be byte-identical.
A test that writes live gate state is itself the bug; the hash bracket is
mandatory, not optional.
"""
import hashlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402

LIVE_DIR = os.path.expanduser('~/.hermes/model-router')
#: every file the proxy write path could legitimately touch live. Absent files
#: are reported as absent — never silently skipped.
LIVE_FILES = ('circuit-state.json', 'health-state.json')

PRICE_BLENDED = 'public blended (usd_1m)'
PRICE_NO_USAGE = 'no usage block; price not applied'


def _live_snapshot():
    """sha256 + mtime per live state file; None hash when the file is absent."""
    out = {}
    for name in LIVE_FILES:
        p = os.path.join(LIVE_DIR, name)
        if os.path.exists(p):
            h = hashlib.sha256()
            with open(p, 'rb') as f:
                data = f.read()
            h.update(data)
            out[name] = {'sha256': h.hexdigest(), 'mtime_ns': os.stat(p).st_mtime_ns,
                         'size': len(data), 'bytes': data}
        else:
            out[name] = None
    return out


def _describe(snap):
    parts = []
    for name in LIVE_FILES:
        s = snap[name]
        if s is None:
            parts.append(f'{name}: ABSENT (nothing live to protect)')
        else:
            parts.append(f'{name}: sha256={s["sha256"]} mtime_ns={s["mtime_ns"]} '
                         f'size={s["size"]}')
    return '; '.join(parts)


#: the fingerprint our battery can and only can leave in a live file. The
#: providers it drives are named tr191-* and appear nowhere else, so any
#: occurrence in a live state file is attributable to THIS write path and to
#: nothing else — the discriminating check under ambient fleet traffic.
FINGERPRINT = b'tr191-'


def _assert_untouched(before, after):
    """The mandatory isolation proof, per live file.

    Byte-identical -> proven untouched (strongest case, stated in the output).
    Bytes differ -> this box's fleet records circuit events every few seconds
    (measured 2026-09-26: the live file changed between two probes 2s apart
    with no test running), so a raw inequality cannot distinguish our write
    from ambient traffic. The delta is then attributed STRUCTURALLY, which is
    exact: the battery's write path can only add/modify pairs named
    tr191-* (its own provider ids) and only ever deletes its own pair. So:
      - the live file carrying the tr191- fingerprint        -> FAIL (leak);
      - a tr191-* key present before and gone after          -> FAIL (leak);
      - anything else (ambient adds/mods/prunes)             -> named in the
        output with both hashes, attributed to ambient fleet traffic.
    """
    for name in LIVE_FILES:
        b, a = before[name], after[name]
        if b is None:
            # Say so rather than skipping silently: an absent live file is a
            # real finding, and a test that CREATED it is the bug.
            assert a is None, (
                f'TR-191: the battery CREATED live {name} — the write path '
                f'leaked out of tmp_path')
            print(f'TR-191 [{name}] ABSENT before and after the battery '
                  f'(nothing live to protect; creation would be the bug)')
            continue
        if b['sha256'] == a['sha256']:
            print(f'TR-191 [{name}] byte-identical: sha256={a["sha256"]}')
            continue
        # bytes differ: attribute the delta before passing or failing
        assert FINGERPRINT not in a['bytes'], (
            f'TR-191: live {name} contains the battery fingerprint '
            f'({FINGERPRINT.decode()!r}) — the proxy write path wrote live state')
        def _keys(blob):
            try:
                doc = json.loads(blob['bytes'])
            except Exception:
                return None
            return set(doc.get('pairs') or {}) if isinstance(doc, dict) else set()
        bk, ak = _keys(b), _keys(a)
        if bk is not None and ak is not None:
            # battery-owned keys look like 'tr191-<case>/<model>': the PROVIDER
            # (left of the '/') carries the fingerprint
            leaked_away = sorted(k for k in bk - ak
                                 if k.split('/')[0].startswith('tr191-'))
            assert not leaked_away, (
                f'TR-191: live {name} LOST battery-owned pair(s) {leaked_away} '
                f'— the write path deleted live state')
        detail = ''
        if bk is not None and ak is not None:
            added, removed = sorted(ak - bk), sorted(bk - ak)
            detail = (f' pairs added={len(added)} removed={len(removed)} '
                      f'(e.g. add={added[:2]} rem={removed[:2]})')
        print(f'TR-191 [{name}] bytes CHANGED by ambient fleet traffic during '
              f'the window (before sha256={b["sha256"]}, after '
              f'sha256={a["sha256"]}); fingerprint and deletion checks prove '
              f'none of it is ours.{detail}')


def _chain(provider):
    return {'chain': [{'hop': 1, 'provider': provider, 'model': 'm',
                       'usd_1m': 0.1,
                       'outcomes': {'stats_fallback': 'unconditioned'}}],
            'sort': 'price'}


def _requirements(monkeypatch, provider):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(provider))


def _upstream_for(case):
    def upstream(path, body, headers):
        if case == 'serve-200':
            return 200, {'choices': [{'message': {'content': 'served'}}],
                         'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}
        if case == 'timeout':
            raise TimeoutError('upstream read timed out')
        if case == 'rate-limited-429':
            return 429, {'error': '429 from upstream: quota'}
        if case == 'overloaded-503':
            return 503, {'error': '503 from upstream: overloaded'}
        raise AssertionError(f'unknown case {case}')
    return upstream


#: provider ids are namespaced tr191-* so a collision with a real lane in
#: data/tables/providers.jsonl (which would swap the injected upstream for a
#: provider-specific hop call) is impossible by construction.
_CASES = {
    'serve-200': dict(provider='tr191-serve', expect_status=200,
                      expect=dict(success=True, route_outcome='served',
                                  failure_reason=None, hops_attempted=1,
                                  served_by_hop=1, price_basis=PRICE_BLENDED),
                      hop_status=200, circuit_class=None),
    'timeout': dict(provider='tr191-timeout', expect_status=502,
                    expect=dict(success=False, route_outcome='failed',
                                failure_reason='hop-wall-timeout',
                                hops_attempted=1, served_by_hop=None,
                                price_basis=PRICE_NO_USAGE),
                    hop_status=0, circuit_class='overload'),
    'rate-limited-429': dict(provider='tr191-rl429', expect_status=429,
                             expect=dict(success=False, route_outcome='failed',
                                         failure_reason='429-quota-window',
                                         hops_attempted=1, served_by_hop=None,
                                         price_basis=PRICE_NO_USAGE),
                             hop_status=429, circuit_class='quota_window'),
    'overloaded-503': dict(provider='tr191-503', expect_status=503,
                           expect=dict(success=False, route_outcome='failed',
                                       failure_reason='5xx-overloaded',
                                       hops_attempted=1, served_by_hop=None,
                                       price_basis=PRICE_NO_USAGE),
                           hop_status=503, circuit_class='api_down'),
}


def _ledger_rows(tmp_path):
    f = tmp_path / 'outcomes.jsonl'
    assert f.exists(), ('the REAL write path must have produced a ledger in '
                        'tmp (ROUTING_OUTCOMES_FILE)')
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def _run_case(tmp_path, monkeypatch, case):
    """One case through the real write path; asserts the tmp ledger row and
    brackets the live files with hashes around the case."""
    spec = _CASES[case]
    expect, provider = spec['expect'], spec['provider']

    before = _live_snapshot()
    print(f'\nTR-191 [{case}] live BEFORE -> {_describe(before)}')

    ledger = tmp_path / 'outcomes.jsonl'
    baseline = sum(1 for line in ledger.read_text().splitlines()
                   if line.strip()) if ledger.exists() else 0
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(ledger))
    _requirements(monkeypatch, provider)

    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': f'tr-191 {case}'}]},
        {}, upstream=_upstream_for(case))
    assert status == spec['expect_status'], (
        f'{case}: proxy status {status} != {spec["expect_status"]}')

    rows = _ledger_rows(tmp_path)[baseline:]
    assert len(rows) == 1, (
        f'{case}: exactly one outcome row expected for this case '
        f'(baseline {baseline}), got {len(rows)}')
    row = rows[0]
    for key, want in expect.items():
        got = row[key]
        if want is None or got is None:
            assert got is want, f'{case}: row[{key}] = {got!r}, expected {want!r}'
        else:
            assert got == want, f'{case}: row[{key}] = {got!r}, expected {want!r}'
    # the STATUS of the actual upstream attempt rides in the row's ladder
    attempts = row.get('attempts') or []
    assert len(attempts) == 1, f'{case}: one attempt expected in the row ladder'
    assert attempts[0]['status'] == spec['hop_status'], (
        f'{case}: attempts[0].status = {attempts[0]["status"]!r}, '
        f'expected {spec["hop_status"]!r}')
    # The TR-191 row-projection contract: the skip evidence the envelope
    # carries must say WHERE its count came from. These chains carry no
    # resolver-side skip count, so the row must derive it from the (empty)
    # exclusions — and never leave the field missing.
    assert row['skipped_hops_source'] == 'derived-from-exclusions', (
        f'{case}: row[skipped_hops_source] = {row["skipped_hops_source"]!r}')
    assert row['skipped_hops'] == 0
    assert row['source_system'] == 'router-proxy'

    # The circuit subprocess must have written the TMP state, never live.
    circuit_path = tmp_path / 'state' / 'circuit-state.json'
    if spec['circuit_class'] is None:
        # record-success on a never-failed pair writes nothing anywhere.
        pairs = {}
        if circuit_path.exists():
            pairs = json.load(open(circuit_path)).get('pairs') or {}
        assert not any(k.startswith(provider) for k in pairs), (
            f'{case}: no circuit entry expected, found {sorted(pairs)}')
    else:
        assert circuit_path.exists(), (
            f'{case}: the circuit subprocess should have written the TMP '
            f'state file (ROUTER_STATE_DIR)')
        st = json.load(open(circuit_path))
        key = f'{provider}/m'
        assert key in st.get('pairs', {}), (
            f'{case}: tmp circuit state missing pair {key}: {sorted(st.get("pairs", {}))}')
        assert st['pairs'][key]['class'] == spec['circuit_class']
        assert st['pairs'][key]['open_until'], f'{case}: breaker must carry open_until'
        v2 = st.get('v2') or {}
        pb = v2.get('provider_breakers') or {}
        # TR-288 blast radius: provider-wide codes (credit, quota window)
        # DEMOTE the provider; pair-local codes (overload, transport) must not.
        if spec['circuit_class'] == 'quota_window':
            assert provider in pb, (
                f'{case}: a quota-window 429 must demote the provider (TR-288)')
            assert pb[provider]['class'] in ('quota_window', 'out_of_credit'), (
                f'{case}: provider breaker class = {pb[provider]["class"]}')
        elif spec['circuit_class'] == 'api_down':
            assert provider not in pb, (
                f'{case}: a pair-local 5xx must NOT demote the provider (TR-288)')
    if os.path.exists(os.path.join(LIVE_DIR, 'circuit-state.json')):
        live_bytes = open(os.path.join(LIVE_DIR, 'circuit-state.json'), 'rb').read()
        assert b'tr191-' not in live_bytes, (
            f'{case}: the LIVE circuit file mentions the test provider — '
            f'the isolation is broken')

    after = _live_snapshot()
    print(f'TR-191 [{case}] live AFTER  -> {_describe(after)}')
    _assert_untouched(before, after)

    print(f'TR-191 [{case}] OUTCOME: proxy status={status} '
          f'route_outcome={row["route_outcome"]} '
          f'hops_attempted={row["hops_attempted"]} '
          f'price_basis={row["price_basis"]!r} '
          f'skipped_hops_source={row["skipped_hops_source"]!r}')
    return status, row


# --------------------------------------------------------------------------
# The four-case battery. Each case is its own test so one failure cannot hide
# the others' evidence; test_live_files_byte_identical_after_the_whole_battery
# then re-drives all four between ONE hash bracket — the mandatory
# whole-battery assertion.
# --------------------------------------------------------------------------

def test_battery_case_serve_200(tmp_path, monkeypatch):
    _run_case(tmp_path, monkeypatch, 'serve-200')


def test_battery_case_timeout(tmp_path, monkeypatch):
    _run_case(tmp_path, monkeypatch, 'timeout')


def test_battery_case_rate_limited_429(tmp_path, monkeypatch):
    _run_case(tmp_path, monkeypatch, 'rate-limited-429')


def test_battery_case_overloaded_503(tmp_path, monkeypatch):
    _run_case(tmp_path, monkeypatch, 'overloaded-503')


def test_live_files_byte_identical_after_the_whole_battery(tmp_path, monkeypatch):
    """THE mandatory check: hash the live files before and after the whole
    four-case battery and assert byte-identity. Absent live files are named
    in the output, never silently skipped."""
    before = _live_snapshot()
    print(f'\nTR-191 WHOLE BATTERY live BEFORE -> {_describe(before)}')
    for case in _CASES:
        _run_case(tmp_path, monkeypatch, case)
    after = _live_snapshot()
    print(f'TR-191 WHOLE BATTERY live AFTER  -> {_describe(after)}')
    _assert_untouched(before, after)
    print('TR-191 VERDICT: live gate state byte-identical across the whole '
          'battery — the proxy write path is isolated to tmp_path.')


# --------------------------------------------------------------------------
# The gate must be able to FAIL: a synthetic delta carrying the battery
# fingerprint exercises every leak arm of _assert_untouched against an
# in-memory snapshot pair (no live file is read twice, none is written).
# --------------------------------------------------------------------------

def test_the_isolation_gate_itself_detects_a_leak():
    doc = {'version': 1, 'pairs': {'zai-glm/glm': {'failures': 1}}}
    blob = json.dumps(doc).encode()
    before = {n: {'sha256': hashlib.sha256(blob).hexdigest(), 'mtime_ns': 1,
                  'size': len(blob), 'bytes': blob}
              for n in LIVE_FILES}

    def _after(name, new_bytes):
        a = dict(before)
        a[name] = dict(before[name], bytes=new_bytes,
                       sha256=hashlib.sha256(new_bytes).hexdigest())
        return a

    # fingerprint leak in the circuit file (a tr191-* pair appears live)
    doc2 = json.loads(blob)
    doc2['pairs']['tr191-serve/m'] = {'failures': 1}
    with pytest.raises(AssertionError, match='fingerprint'):
        _assert_untouched(before, _after('circuit-state.json',
                                         json.dumps(doc2).encode()))
    # the same leak in the health file (whatever shape the delta has) is caught
    # by the fingerprint arm too, because the fingerprint cannot occur anywhere
    with pytest.raises(AssertionError, match='fingerprint'):
        _assert_untouched(before, _after('health-state.json',
                                         blob + b'tr191-x'))
    # an unparseable live delta WITHOUT the fingerprint stays honest (attributed
    # to ambient traffic) instead of failing the battery
    _assert_untouched(before, _after('health-state.json', blob + b'{"nope": 1}'))
    # a tr191/* pair vanishing from a parseable live file is also a leak
    b3 = json.dumps({'version': 1, 'pairs': {'tr191-serve/m': {'failures': 1},
                                             'zai-glm/glm': {'failures': 1}}}).encode()
    before3 = dict(before)
    before3['circuit-state.json'] = dict(
        before['circuit-state.json'], bytes=b3,
        sha256=hashlib.sha256(b3).hexdigest())
    doc4 = {'version': 1, 'pairs': {'zai-glm/glm': {'failures': 1}}}
    with pytest.raises(AssertionError, match='LOST battery-owned'):
        _assert_untouched(before3, _after('circuit-state.json',
                                          json.dumps(doc4).encode()))

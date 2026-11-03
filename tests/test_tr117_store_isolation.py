"""TR-117 — the suite must not feed the live ledger (fixture rows in the averages).

The defect (measured 2026-11-03): proxy-driving tests that call
``router_server.proxy_chat`` without their own ROUTING_OUTCOMES_FILE appended
fixture rows straight into the LIVE ``data/state/outcomes.jsonl``. The
fingerprint is exact and countable in the live ledger: provider ``p1``/``p2``,
model ``m1``/``m2``, ``profile_id='P1'``, ``complexity_source='declared'`` —
664 'profile:P1' rows at the time of the census. Those rows then flow into the
rolling averages the hourly rebuild writes and the resolve sort consumes:
fake ``p1/m1`` buckets compete with real lanes in the merged view, and the
complexity-keyed averages carry input nothing measured.

Root cause: conftest.py defaulted the UNSERVABLE census (TR-344) and the
demotion log (TR-357) to throwaway paths under the documented rule "the suite
must grade the tree, not write into it" — but never the outcome store itself.

The fix under test:
  1. conftest defaults ROUTING_OUTCOMES_FILE and ROUTING_AVERAGES_FILE to a
     throwaway session path (same pattern, same rule).
  2. this file drives the REAL proxy write path (record NOT stubbed) with the
     exact fixture shape that leaked and no per-test env: the row must land in
     the resolved (throwaway) store while the live ledger is bracketed
     byte-identical around the call. Remove the conftest default and this
     test goes red on the live store — the leak class is pinned shut.
"""
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import router_outcomes as ro  # noqa: E402
import router_server as rsrv  # noqa: E402

LIVE_LEDGER = os.path.join(ro.REPO, 'data', 'state', 'outcomes.jsonl')
LIVE_AVERAGES = os.path.join(ro.REPO, 'data', 'state', 'outcomes-averages.jsonl')


def _live_fingerprint(path):
    """(size, mtime_ns, tail sha256) — an append changes size, mtime and the
    tail bytes; a clobber changes all three. Cheap enough to bracket every
    write-path test, strong enough to catch every leak arm."""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(max(0, st.st_size - 2 * 1024 * 1024))
        h.update(f.read())
    return st.st_size, st.st_mtime_ns, h.hexdigest()


def _gate_fingerprint(path):
    """Full-bytes fingerprint for the (small) live gate files. Absent files
    fingerprint as None — a test that CREATES one is the leak."""
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return os.path.getsize(path), os.stat(path).st_mtime_ns, h.hexdigest()


def test_conftest_defaults_resolve_away_from_the_live_tree():
    """With no explicit env, the store resolvers must NOT return the live
    runtime files (the conftest throwaway wins over the repo default)."""
    for resolved, live in ((ro.outcomes_path(), LIVE_LEDGER),
                           (ro.averages_path(), LIVE_AVERAGES)):
        assert os.path.abspath(resolved) != os.path.abspath(live), (
            'TR-117: the suite default resolves to the live runtime file '
            f'{resolved} — the conftest throwaway default regressed')
        assert os.path.abspath(resolved).startswith(
            os.path.abspath(os.environ.get('TMPDIR') or '/tmp')), (
            f'TR-117: the suite default is not a throwaway path: {resolved}')


def test_the_no_env_write_path_lands_in_the_throwaway_not_the_tree(
        monkeypatch, tmp_path):
    """The real ingest write with NO env set (conftest default in force):
    the row lands in the resolved store; the live ledger is untouched."""
    before = _live_fingerprint(LIVE_LEDGER)
    row = ro.normalize_row({'source_system': 'router-proxy',
                            'session_id': 'tr117-default-path',
                            'provider': 'p1', 'model': 'm1'}, now_s=1.0)
    ok, reason = ro.append_row_fast(ro.outcomes_path(), row)
    assert ok is True, reason
    after = _live_fingerprint(LIVE_LEDGER)
    assert after == before, (
        'TR-117: a default-env ingest appended to the LIVE ledger '
        f'({before[:2]} -> {after[:2]})')
    landed = [json.loads(line) for line in open(ro.outcomes_path()) if line.strip()]
    assert any(r.get('session_id') == 'tr117-default-path' for r in landed)


def _live_untouched_around(before):
    after = _live_fingerprint(LIVE_LEDGER)
    assert after == before, (
        'TR-117: the real proxy write path appended to the LIVE ledger '
        f'(size {before[0]} -> {after[0]}, tail {before[2][:12]} -> '
        f'{after[2][:12]}) — the conftest throwaway default did not hold')


def test_proxy_write_path_with_fixture_shape_lands_in_the_throwaway(
        monkeypatch, tmp_path):
    """THE leak, reproduced and pinned: the exact fixture shape measured in
    the live ledger (declared profile 'P1', p1 402-no-credit then p2 served)
    driven through proxy_chat with the REAL _proxy_record and NO outcomes
    env. The rows must land in the resolved throwaway store, carrying the
    same fingerprint fields, and the live ledger AND live gate files must be
    untouched around the call (ROUTER_STATE_DIR steers to tmp like every
    sibling battery; the ledger comes from the conftest default)."""
    before_ledger = _live_fingerprint(LIVE_LEDGER)
    before_gates = {name: _gate_fingerprint(os.path.join(
        os.path.expanduser('~/.hermes/model-router'), name))
        for name in ('quota-state.json', 'circuit-state.json')}

    store = ro.outcomes_path()
    baseline = 0
    if os.path.exists(store):
        with open(store) as f:
            baseline = sum(1 for line in f if line.strip())

    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1', 'usd_1m': 0.1,
                   'outcomes': {'stats_fallback': 'unconditioned'}},
                  {'hop': 2, 'provider': 'p2', 'model': 'm2', 'usd_1m': 0.2,
                   'outcomes': {'stats_fallback': 'unconditioned'}}],
        'sort': 'price'})

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            return 402, {'error': {'message': 'insufficient credits'}}
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}

    status, payload = rsrv.proxy_chat('/v1/chat/completions',
                                      {'messages': [{'role': 'user',
                                                     'content': 'tr117'}]},
                                      {}, upstream=upstream)
    assert status == 200
    assert payload['_router']['served_by']['provider'] == 'p2'
    _live_untouched_around(before_ledger)
    for name, before in before_gates.items():
        after = _gate_fingerprint(os.path.join(
            os.path.expanduser('~/.hermes/model-router'), name))
        assert after == before, (
            f'TR-117: the demotion path wrote the LIVE {name} '
            f'({before} -> {after}) — the state-dir resolution leaked')

    with open(store) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) == baseline + 2, (
        'one row per ATTEMPT expected (the measured leak was a pair: p1 '
        f'failed + p2 served), got {len(rows) - baseline}')
    failed, served = rows[-2], rows[-1]
    # the measured leak fingerprint: this test writes EXACTLY the shape the
    # live ledger carried 664 of — so a regression here is that leak again.
    for row in (failed, served):
        assert row['source_system'] == 'router-proxy'
        assert row['profile_id'] == 'P1'
        assert row['complexity_source'] == 'declared'
    assert failed['provider'] == 'p1' and failed['model'] == 'm1'
    assert failed['failure_reason'] == '402-no-credit'
    assert served['provider'] == 'p2' and served['model'] == 'm2'
    assert served['success'] is True

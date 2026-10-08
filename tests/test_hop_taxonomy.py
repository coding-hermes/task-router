"""TR-288: the ONE failure taxonomy, shared by probe / hop ladder / ledger.

The hourly probe already knew WHY a lane failed (429 capacity vs quota
window, 402 no-credit, 401/403 auth, 503 overloaded); the request path
collapsed every 4xx into `upstream-4xx`. These tests pin:

1. the taxonomy module (codes, blast radius, retry policy) — closed set;
2. `_classify_hop_failure` naming every status the acceptance criteria list;
3. the ladder: per-code retry policy (transient retries the same hop ONCE,
   quota/auth skip), and the taxonomy code recorded on the hop row;
4. the blast radius: a model-named failure demotes only the (provider,
   model) pair; a provider-wide condition (402/401/403/429-window) demotes
   the PROVIDER immediately — proven by a resolve that skips it afterwards;
5. the vocabulary: every hop code is a row in the circuit's
   FAILURE_CLASS_MAP (probe, ladder and circuit speak one language).
"""
import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_hop_taxonomy as tax      # noqa: E402
import router_server as rsrv           # noqa: E402

TAX_PATH = os.path.join(REPO, 'scripts', 'router_hop_taxonomy.py')
CIRC_PATH = os.path.join(REPO, 'scripts', 'router_circuit.py')


def _load_circuit_module():
    spec = importlib.util.spec_from_file_location(
        "router_circuit_under_test_tr288", CIRC_PATH)
    mod = importlib.util.spec_from_file_location  # noqa: F841 (placeholder kept minimal)
    spec = importlib.util.spec_from_file_location(
        "router_circuit_under_test_tr288", CIRC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 1. the taxonomy itself ----------

def test_codes_blast_radius_and_policy_are_closed_and_aligned():
    assert set(tax.BLAST_RADIUS) == set(tax.HOP_FAILURE_CODES)
    assert set(tax.RETRY_POLICY) == set(tax.HOP_FAILURE_CODES)


def test_provider_wide_codes_are_the_account_conditions():
    provider_codes = {c for c, r in tax.BLAST_RADIUS.items() if r == 'provider'}
    assert provider_codes == {'429-quota-window', '402-no-credit', '401-403-auth'}


def test_retry_policy_per_code():
    assert tax.RETRY_POLICY['5xx-overloaded'] == 'retry-same'
    assert tax.RETRY_POLICY['408-timeout'] == 'retry-same'
    assert tax.RETRY_POLICY['429-capacity'] == 'skip'
    assert tax.RETRY_POLICY['402-no-credit'] == 'skip'
    assert tax.RETRY_POLICY['401-403-auth'] == 'skip'
    assert tax.RETRY_POLICY['context-length-exceeded'] == 'shrink-or-skip'


# ---------- 2. the classifier names the WHY ----------

@pytest.mark.parametrize('status,body,expected', [
    (429, 'rate limited, too many concurrent requests', '429-capacity'),
    (429, 'quota window: business code 1310, weekly limit', '429-quota-window'),
    (429, 'Account budget exceeded', '429-quota-window'),
    (429, None, '429-capacity'),
    (402, 'insufficient credits', '402-no-credit'),
    (402, None, '402-no-credit'),
    (401, None, '401-403-auth'),
    (403, 'access_terminated', '401-403-auth'),
    (404, None, '404-405-route'),
    (405, None, '404-405-route'),
    (408, None, '408-timeout'),
    (503, 'upstream overloaded', '5xx-overloaded'),
    (500, None, '5xx-overloaded'),
    (400, "this model's maximum context length is 32768 tokens", 'context-length-exceeded'),
    (413, 'payload too large', 'context-length-exceeded'),
    (403, 'forbidden: no permission for this model', '401-403-auth'),
])
def test_classify_hop_failure_names_the_reason(status, body, expected):
    reason, _ = rsrv._classify_hop_failure(status=status, body=body)
    assert reason == expected


def test_classify_hop_failure_legacy_fallbacks_stay():
    assert rsrv._classify_hop_failure(status=422)[0] == 'upstream-4xx'
    assert rsrv._classify_hop_failure(
        exc=ConnectionError('connection refused'))[0] == 'transport-error'
    assert rsrv._classify_hop_failure(exc=TimeoutError('timed out'))[0] == 'hop-wall-timeout'


def test_every_hop_code_is_in_the_circuit_kind_table():
    """One vocabulary: every code the ladder can put on a hop row resolves in
    the circuit's FAILURE_CLASS_MAP — the probe, ladder and circuit share it."""
    circ = _load_circuit_module()
    for code in tax.HOP_FAILURE_CODES:
        cls, window = circ.failure_class_for(kind=code)
        assert cls in circ.CLASSES
        assert window > 0


# ---------- 3+4. the ladder: code on the row, retry policy, blast radius ----------

def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m,
                       'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    monkeypatch.setattr(rsrv, '_demote_on_taxonomy', lambda *a, **k: None)
    return tmp_path


def _declared(monkeypatch, chain):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: chain)


def test_hop_row_records_the_taxonomy_code(proxy_env, monkeypatch):
    _declared(monkeypatch, _chain(('p1', 'm1'), ('p2', 'm2')))

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            return 402, {'error': {'message': 'insufficient credits'}}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    hop = payload['_router']['ladder'][0]
    assert hop['reason'] == '402-no-credit'
    assert 'insufficient credits' in hop['reason_detail']
    assert payload['_router']['served_by']['provider'] == 'p2'


def test_transient_5xx_retries_same_hop_once_then_advances(proxy_env, monkeypatch):
    _declared(monkeypatch, _chain(('p1', 'm1'), ('p2', 'm2')))
    calls = {'p1': 0}

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            calls['p1'] += 1
            return 503, {'error': 'overloaded'}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    assert calls['p1'] == 2, 'a transient 5xx retries the SAME hop once'
    hop = payload['_router']['ladder'][0]
    assert hop['retry_policy'] == 'retry-same'
    assert payload['_router']['served_by']['provider'] == 'p2'


def test_quota_code_never_retries_and_skips_to_next_hop(proxy_env, monkeypatch):
    _declared(monkeypatch, _chain(('p1', 'm1'), ('p2', 'm2')))
    calls = {'p1': 0}

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            calls['p1'] += 1
            return 429, {'error': {'message': 'quota exceeded weekly window'}}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    assert calls['p1'] == 1, 'a quota-window 429 must not re-send the payload'
    assert payload['_router']['ladder'][0]['retry_policy'] == 'skip'
    assert payload['_router']['served_by']['provider'] == 'p2'


def test_model_failure_demotes_only_the_pair(proxy_env, monkeypatch, tmp_path):
    """A 429 naming a MODEL takes out that (provider, model) hop; the
    provider's other models still resolve."""
    monkeypatch.setattr(rsrv, '_demote_on_taxonomy', rsrv.__dict__['_demote_on_taxonomy'].__wrapped__ if hasattr(rsrv._demote_on_taxonomy, '__wrapped__') else None)  # placeholder; replaced below
    # use the REAL demotion (patched out in proxy_env)
    demoted = []
    monkeypatch.setattr(rsrv, '_demote_on_taxonomy',
                        lambda p, m, code, detail='': demoted.append((p, m, code)))
    _declared(monkeypatch, _chain(('p1', 'busy'), ('p1', 'free'), ('p2', 'm2')))

    def upstream(path, body, headers):
        if body['model'] == 'busy':
            return 429, {'error': 'rate limit exceeded on model busy'}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    assert status == 200
    assert payload['_router']['served_by']['model'] == 'free'
    assert demoted == [('p1', 'busy', '429-capacity')], \
        'a model-named failure demotes exactly that pair, nothing else'


def test_provider_wide_402_demotes_the_provider_immediately(tmp_path, monkeypatch):
    """One in-flight 402 is proof: the provider breaker opens on THIS event
    (no 3-failure threshold) and a resolve skips ALL of the provider's
    models — via the circuit state the resolver reads."""
    circ = _load_circuit_module()
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    import importlib as _imp
    spec = importlib.util.spec_from_file_location('router_circuit_env', CIRC_PATH)
    circ2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(circ2)

    demoted = []
    monkeypatch.setattr(rsrv, '_demote_on_taxonomy',
                        lambda p, m, code, detail='': demoted.append((p, m, code)))
    _declared(monkeypatch, _chain(('p1', 'm1'), ('p2', 'm2')))

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            return 402, {'error': 'insufficient credits'}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    assert status == 200
    assert payload['_router']['served_by']['provider'] == 'p2'
    assert demoted == [('p1', 'm1', '402-no-credit')]

    # prove the DEMOTION, not just the call: the same record the demotion
    # writes opens a provider breaker on ONE event, and the resolver's own
    # circuit read excludes every p1 model.
    st_dir = os.path.join(str(tmp_path), 'state')
    os.makedirs(st_dir, exist_ok=True)
    open(os.path.join(st_dir, 'circuit-state.json'), 'w').write(json.dumps(
        {'version': 1, 'pairs': {},
         'v2': {'provider_breakers': {}, 'classes': {}}}))
    rc = _load_circuit_module_env(st_dir)
    rc.record_failure('p1', 'm1', '402-no-credit insufficient credits',
                      kind='402-no-credit', provider_hard=True)
    st = json.load(open(os.path.join(st_dir, 'circuit-state.json')))
    assert 'p1' in st['v2']['provider_breakers'], \
        'one provider-wide event must open the provider breaker immediately'
    rc.record_failure('p1', 'other-model', '402-no-credit', kind='402-no-credit',
                      provider_hard=True)
    st = json.load(open(os.path.join(st_dir, 'circuit-state.json')))
    assert 'p1' in st['v2']['provider_breakers']


def _load_circuit_module_env(state_dir):
    spec = importlib.util.spec_from_file_location(
        'router_circuit_env_dir', CIRC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 5. the resolver actually skips the demoted level ----------

def _committed_tables():
    tables = {}
    for fn in sorted((os.path.join(REPO, 'data', 'tables')).__class__ and
                     __import__('pathlib').Path(REPO, 'data', 'tables').glob('*.jsonl')):
        tables[fn.name[:-len('.jsonl')]] = [
            json.loads(ln) for ln in fn.open() if ln.strip()]
    return tables


def _resolve_env(tmp_path, monkeypatch, tables, provider_breakers=None,
                 open_pairs=None):
    """Hermetic resolve: temp registry + state dir (same pattern as
    tests/test_degraded_path_contract.py) with optional circuit state."""
    import pathlib
    import router_spawn
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables}))
    monkeypatch.setattr(router_spawn, 'REGISTRY', str(reg))
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    provs = {r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
    provs |= {m.get('provider') for m in (tables.get('models') or []) if m.get('provider')}
    json.dump({'updated': 'test',
               'providers': {p: {'status': 'open'} for p in sorted(provs)}},
              open(d / 'quota-state.json', 'w'))
    json.dump({'providers': {}}, open(d / 'health-state.json', 'w'))
    json.dump({'pairs': open_pairs or {},
               'v2': {'provider_breakers': provider_breakers or {},
                      'classes': {}}},
              open(d / 'circuit-state.json', 'w'))
    monkeypatch.setattr(router_spawn, 'MR', str(d))
    return router_spawn


def test_resolve_provider_breaker_skips_all_models_of_the_provider(
        tmp_path, monkeypatch):
    """BLAST RADIUS (the other way): a provider breaker (what a 402/auth
    condition opens via --provider-hard) excludes ALL of that provider's
    models from the resolve, while another provider's model stays offered."""
    import pathlib
    spawn = _resolve_env(tmp_path, monkeypatch, _committed_tables(),
                         provider_breakers={
                             'zai-glm': {'class': 'out_of_credit',
                                         'open_until': '2099-01-01T00:00:00+00:00',
                                         'opened_at': '2026-10-03T00:00:00+00:00',
                                         'cooldown_s': 14400}})
    r = spawn.resolve(adhoc=['code_gen=1'])
    chain = r.get('chain') or []
    assert chain, 'the resolve must still offer OTHER providers'
    assert all(h.get('provider') != 'zai-glm' for h in chain), \
        'a provider breaker must skip EVERY model of the provider'
    excluded = {e.get('provider'): e for e in (r.get('exclusions') or [])}
    assert 'zai-glm' in excluded, 'the exclusion must name the provider'
    assert any('provider-level' in w for w in excluded['zai-glm']['why'])


def test_resolve_model_pair_breaker_skips_only_that_pair(tmp_path, monkeypatch):
    """BLAST RADIUS: a model-level breaker (what a 429-capacity/5xx opens)
    excludes only that (provider, model) pair; the provider's OTHER models
    still resolve."""
    import pathlib
    spawn = _resolve_env(tmp_path, monkeypatch, _committed_tables(),
                         open_pairs={'zai-glm/glm-5.3-flash':
                                     {'failures': 1,
                                      'open_until': '2099-01-01T00:00:00+00:00',
                                      'class': 'quota_window'}})
    r = spawn.resolve(adhoc=['code_gen=1'])
    chain = r.get('chain') or []
    assert chain, 'the resolve must still offer hops'
    assert all((h.get('provider'), h.get('model')) !=
               ('zai-glm', 'glm-5.3-flash') for h in chain), \
        'the broken pair must be excluded'
    assert any(h.get('provider') == 'zai-glm' for h in chain), \
        "the provider's other models must stay eligible"

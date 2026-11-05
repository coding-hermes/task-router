#!/usr/bin/env python3
"""TR-364 — gate doctrine: a provider is never gated by one pair failure, a
transport quirk, or a stale reason.

Three measured defects, one red-then-green test each:

  1. openrouter's provider-level breaker opened on ONE pair failure
     (openrouter/openai/gpt-6-luna) and hid 339 live lanes for 30 minutes.
     Doctrine: an upstream error gates the PAIR; the provider closes only
     when a MAJORITY of its pairs fail within the class window
     (failing_pairs > total_pairs/2), with the counts recorded in the state
     file. The red leg is one --provider-hard failure on a seat whose
     registry universe is 4 lanes: pre-fix it opened the provider breaker
     immediately; post-fix the pair gates and the provider stays closed,
     and the decision counts land in v2.provider_majority.

  2. xkiro reported aggregate status OK while its error field said "Plan
     quota exhausted for the 7-day window (1400 USD)" and the gate read
     only the status word. Doctrine: the gate resolves the contradiction
     in favor of the quota-class error and records WHICH signal won
     (resolved_by). Red pre-fix: quota_error_gates does not exist.

  3. the groq probe sent no User-Agent, received Cloudflare 403
     "error code: 1010", and the harness-caused refusal was recorded as a
     provider failure. Doctrine: probe-defect is a distinct outcome that
     never reads as provider-down and never opens a breaker. Red pre-fix:
     PROBE_DEFECT / is_probe_defect / _probe_headers do not exist.

Hermetic: no network. The circuit legs run the real CLI in a child
interpreter with ROUTER_STATE_DIR and ROUTING_REGISTRY pointed at tmp
fixtures (the test_circuit_v2 pattern); the probe legs stub urllib.request;
the gate legs are pure functions over synthetic health maps.
"""
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error

import pytest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, 'scripts')
CIRCUIT = os.path.join(SCRIPTS, 'router_circuit.py')

sys.path.insert(0, SCRIPTS)
import router_spawn  # noqa: E402
import provider_health_probe as php  # noqa: E402


def _load_circuit():
    """router_circuit under test, isolated from any other module cache."""
    spec = importlib.util.spec_from_file_location('router_circuit_tr364', CIRCUIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------- fixtures --------

def _registry_fixture(path):
    """A registry whose offer universe for tr364-seat is exactly 4 lanes
    (same rankability shape provider_health_probe.rankable_lanes counts),
    plus one lane of another provider to prove per-provider filtering."""
    doc = {'tables': {'models': [
        {'provider': 'tr364-seat', 'model': f'lane-{c}', 'normalized_price': 0.5}
        for c in 'abcd'
    ] + [
        {'provider': 'other-seat', 'model': 'unrelated', 'normalized_price': 0.1},
    ]}}
    with open(path, 'w') as f:
        json.dump(doc, f)
    return str(path)


def _circuit_env(tmp_path, registry_path):
    return dict(os.environ,
                ROUTER_STATE_DIR=str(tmp_path),
                ROUTING_REGISTRY=registry_path)


def _run_circuit(env, *args):
    return subprocess.run([sys.executable, CIRCUIT, *args],
                          capture_output=True, text=True, timeout=60, env=env)


def _state(tmp_path):
    with open(os.path.join(str(tmp_path), 'circuit-state.json')) as f:
        return json.load(f)


# ------------------------------------------------- defect 1: pair majority ---

def test_one_pair_failure_gates_pair_never_provider(tmp_path):
    """RED pre-fix (one --provider-hard failure opened the provider breaker);
    GREEN post-fix: the PAIR gates, the provider stays closed, and the
    majority decision counts are recorded in the state file."""
    reg = _registry_fixture(tmp_path / 'registry.json')
    env = _circuit_env(tmp_path, reg)
    p = _run_circuit(env, 'record-failure', 'tr364-seat', 'lane-a',
                     '--provider-hard', 'cloudflare 403 on one lane')
    assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    # an upstream error gates THE PAIR — immediately, even with --provider-hard
    pair = st['pairs']['tr364-seat/lane-a']
    assert pair['class'] == 'api_down'
    assert pair['failures'] == 1
    assert pair['open_until'] > datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds')
    # ...but ONE failing pair out of 4 is no majority: no provider breaker
    breakers = st.get('v2', {}).get('provider_breakers') or {}
    assert 'tr364-seat' not in breakers, (
        f'one pair failure must never close the provider; got {breakers}')
    # the decision and its counts are recorded in the state file
    maj = (st.get('v2', {}).get('provider_majority') or {}).get('tr364-seat')
    assert maj, 'state file records no majority decision for tr364-seat'
    assert maj['failing_pairs'] == 1
    assert maj['total_pairs'] == 4
    assert maj['opened'] is False
    assert maj['class'] == 'api_down'


def test_same_pair_failing_repeatedly_is_still_one_pair(tmp_path):
    """RED pre-fix (three api_down EVENTS on one lane met the old
    3-event threshold); GREEN post-fix: events are not pairs — three
    failures of the SAME lane leave the provider closed."""
    reg = _registry_fixture(tmp_path / 'registry.json')
    env = _circuit_env(tmp_path, reg)
    for _ in range(3):
        p = _run_circuit(env, 'record-failure', 'tr364-seat', 'lane-a',
                         '--class', 'api_down', 'connection refused')
        assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    assert st['pairs']['tr364-seat/lane-a']['failures'] == 3
    breakers = st.get('v2', {}).get('provider_breakers') or {}
    assert 'tr364-seat' not in breakers, (
        'three failures of ONE pair must never close the provider')
    maj = (st.get('v2', {}).get('provider_majority') or {}).get('tr364-seat')
    assert maj['failing_pairs'] == 1, (
        'events must count as DISTINCT PAIRS, not raw events')
    assert maj['total_pairs'] == 4
    assert maj['opened'] is False


def test_majority_pair_failures_open_provider_breaker(tmp_path):
    """The doctrine's other edge: a MAJORITY of the seat's pairs failing in
    the class window DOES open the provider breaker, with the counts on the
    breaker entry and in the majority audit slot."""
    reg = _registry_fixture(tmp_path / 'registry.json')
    env = _circuit_env(tmp_path, reg)
    for lane in ('lane-a', 'lane-b', 'lane-c'):
        p = _run_circuit(env, 'record-failure', 'tr364-seat', lane,
                         '--class', 'api_down', 'connection refused')
        assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    pb = (st.get('v2', {}).get('provider_breakers') or {}).get('tr364-seat')
    assert pb, '3 of 4 pairs failing must open the provider breaker'
    assert pb['class'] == 'api_down'
    assert pb['failing_pairs'] == 3
    assert pb['total_pairs'] == 4
    assert pb['open_until'] > datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds')
    maj = (st.get('v2', {}).get('provider_majority') or {}).get('tr364-seat')
    assert maj == {'class': 'api_down', 'failing_pairs': 3, 'total_pairs': 4,
                   'opened': True, 'decided_at': maj['decided_at']}


def test_unmapped_registry_provider_falls_back_to_observed_pairs(tmp_path):
    """Fail-open denominator: a provider absent from the registry counts the
    distinct pairs the circuit state has actually seen, so the majority rule
    still has an honest universe for unknown seats."""
    reg = _registry_fixture(tmp_path / 'registry.json')
    env = _circuit_env(tmp_path, reg)
    for lane in ('x1', 'x2', 'x3'):
        p = _run_circuit(env, 'record-failure', 'ghost-seat', lane,
                         '--class', 'api_down', 'connection refused')
        assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    pb = (st.get('v2', {}).get('provider_breakers') or {}).get('ghost-seat')
    assert pb, 'all observed pairs failing must open the provider breaker'
    assert pb['failing_pairs'] == 3
    assert pb['total_pairs'] == 3


def test_single_unknown_pair_stays_short_of_majority(tmp_path):
    """The doctrine's red edge for a seat the registry does not know: the
    observed-pairs fallback carries a floor of 2, so ONE failing pair
    (--provider-hard included) can never satisfy a majority."""
    reg = _registry_fixture(tmp_path / 'registry.json')
    env = _circuit_env(tmp_path, reg)
    p = _run_circuit(env, 'record-failure', 'ghost2-seat', 'only-lane',
                     '--provider-hard', '--class', 'api_down',
                     'connection refused')
    assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    assert 'ghost2-seat' not in (st.get('v2', {}).get('provider_breakers') or {}), (
        'one pair failure must never close the provider, registry-known or not')
    maj = (st.get('v2', {}).get('provider_majority') or {}).get('ghost2-seat')
    assert maj['failing_pairs'] == 1
    assert maj['total_pairs'] == 2, 'the fallback denominator floors at 2'
    assert maj['opened'] is False


# --------------------------------------- defect 2: OK status vs quota error --

# The measured xkiro shape, byte-for-byte from health.jsonl 2026-11-04:
XKIRO_QUOTA_ERROR = ('HTTP 429: {"error":{"message":"Plan quota exhausted for '
                     'the 7-day window (1400 USD). Retry after 107661s.",'
                     '"type":"rate_limit_error","code":"rate_limit_exceeded"}}')


def test_ok_status_with_quota_error_resolves_to_gated():
    """RED pre-fix (no resolver existed); GREEN post-fix: OK status beside a
    quota-class error is GATED and the row records that the ERROR won."""
    hs = {'xkiro': {'status': 'OK', 'model': 'openai/gpt-5.6-terra',
                    'error': XKIRO_QUOTA_ERROR, 'ts': '2026-11-04T05:00:59+00:00',
                    'models': {'openai/gpt-5.6-terra': {'status': 'OK'}}}}
    gates = router_spawn.quota_error_gates(hs)
    assert 'xkiro' in gates, 'a quota-class error beside OK must gate the provider'
    g = gates['xkiro']
    assert g['active'] is True
    assert g['resolved_by'] == 'error'
    assert '7-day window' in g['error']


def test_gate_down_status_and_non_quota_errors_are_not_contradictions():
    """resolved_by='status' when the status already gates (no contradiction);
    non-quota errors (the live groq 'OK | HTTP 404' shape) gate nothing; a
    TRANSIENT rate-limit beside OK stays routable (the free-pool negative
    control: a rate limit is transient, an exhausted pool is not)."""
    hs = {
        'quota-down': {'status': 'DOWN', 'error': XKIRO_QUOTA_ERROR},
        'http404': {'status': 'OK', 'error': 'HTTP 404: model does not exist'},
        'transient-429': {'status': 'OK',
                          'error': 'HTTP 429 Too Many Requests (rate limited)'},
        'clean-ok': {'status': 'OK', 'error': None},
        'broken': 'not-a-dict',
    }
    gates = router_spawn.quota_error_gates(hs)
    assert gates['quota-down']['active'] is False
    assert gates['quota-down']['resolved_by'] == 'status'
    assert 'http404' not in gates, 'a non-quota error beside OK is no contradiction'
    assert 'transient-429' not in gates, (
        'a transient rate-limit beside OK must not gate the provider')
    assert 'clean-ok' not in gates
    assert 'broken' not in gates


def test_quota_error_gate_is_countable_in_exclusions():
    """The why-row carries the load-bearing 'health quota-error' prefix, so
    exclusion_codes() maps it to a countable code (never 'unknown')."""
    g = router_spawn.quota_error_gates(
        {'xkiro': {'status': 'OK', 'error': XKIRO_QUOTA_ERROR}})['xkiro']
    why = router_spawn.quota_error_why(g)
    assert why.startswith('health quota-error')
    assert 'resolved_by=error' in why
    assert router_spawn.exclusion_codes([why]) == ['health-quota-error']


# ----------------------------------------- defect 3: probe-defect outcome ----

CF_1010_BODY = b'<html>\n<body>error code: 1010</body>\n</html>'
GROQ_BASE = 'https://api.groq.com/openai/v1'
GROQ_MODEL = 'qwen/qwen3.6-27b'


class _OkResponse:
    """Minimal urlopen() stand-in for a 200 chat/completions answer."""

    def read(self):
        return b'{"choices":[{"message":{"content":"pong"}}]}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(monkeypatch, behavior):
    """Replace urllib.request.urlopen inside the probe module. `behavior`
    receives the Request and either returns a response or raises; every
    request's User-Agent header is captured."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['ua'] = req.get_header('User-agent')
        captured['host'] = req.full_url
        return behavior(req)

    monkeypatch.setattr(php.urllib.request, 'urlopen', fake_urlopen)
    return captured


def test_cloudflare_403_classifies_probe_defect_never_down(monkeypatch):
    """RED pre-fix (a CF 403 fell through to status DOWN); GREEN post-fix:
    harness-caused refusals classify PROBE_DEFECT with failure_code
    'probe-defect' — no provider-down outcome, no breaker vocabulary."""
    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden',
                                     {}, io.BytesIO(CF_1010_BODY))
    captured = _stub_urlopen(monkeypatch, behavior)
    r = php.ping(GROQ_BASE, 'sk-hermetic', GROQ_MODEL)
    assert captured['ua'] == php.UA, 'the refused request must still carry the probe UA'
    assert r['status'] == php.PROBE_DEFECT
    assert r['status'] != 'DOWN'
    assert r['failure_code'] == 'probe-defect'
    assert '1010' in r['error']


def test_probe_defect_is_not_provider_down_and_not_breaker_evidence():
    """A probe-defect outcome must be unrepresentable as provider-down: the
    aggregate never returns DOWN from defects alone, and 'probe-defect' is
    not a kind the circuit can map to a breaker-opening class."""
    rc = _load_circuit()
    models = {m: {'status': php.PROBE_DEFECT} for m in ('lane-a', 'lane-b', 'lane-c')}
    status, stats = php.aggregate(models)
    assert status == php.PROBE_DEFECT
    assert status != 'DOWN'
    assert stats['down'] == 0
    assert stats['probe_defect'] == 3
    # one healthy sibling keeps the provider usable (Bane 08-28 rule intact)
    mixed = {'lane-a': {'status': 'OK'}, 'lane-b': {'status': php.PROBE_DEFECT}}
    assert php.aggregate(mixed)[0] == 'OK'
    # breaker doctrine: 'probe-defect' is NOT in the failure-kind table, so a
    # probe-defect outcome can never resolve to a breaker-opening class
    assert 'probe-defect' not in rc.FAILURE_CLASS_MAP
    assert rc.failure_class_for(fclass='overload')[0] == 'overload'


def test_user_agent_always_sent_and_success_path(monkeypatch):
    """RED pre-fix (_probe_headers absent, extra headers could shadow the UA);
    GREEN post-fix: the UA is structurally forced on every request, and a
    normally-signed request takes the OK success path."""
    def behavior(req):
        return _OkResponse()
    captured = _stub_urlopen(monkeypatch, behavior)
    r = php.ping(GROQ_BASE, 'sk-hermetic', GROQ_MODEL)
    assert r['status'] == 'OK'
    assert captured['ua'] == php.UA
    # per-provider contract headers merge but can never shadow the UA
    merged = php._probe_headers({'x-opencode-session': 'sess-1',
                                 'User-Agent': 'shadowed-ua'})
    assert merged['User-Agent'] == php.UA
    assert merged['x-opencode-session'] == 'sess-1'
    assert php._probe_headers(None)['User-Agent'] == php.UA


def test_cloudflare_signature_needs_the_403():
    """The classifier is narrow: CF markers on other codes are not
    probe-defects, and a plain 403 without a CF signature stays DOWN-path."""
    assert php.is_probe_defect(403, 'error code: 1010') is True
    assert php.is_probe_defect(403, 'cf-ray: 8f6b…') is True
    assert php.is_probe_defect(403, 'cloudflare nonce required') is True
    assert php.is_probe_defect(403, '{"error":"invalid api key"}') is False
    assert php.is_probe_defect(429, 'error code: 1010') is False
    assert php.is_probe_defect(None, CF_1010_BODY) is False

"""TR-344: an UNSERVABLE RATING at fleet scale must be counted, alarmed and classified.

The 2026-10-20 10:20-10:54 local incident: 190 distinct scheduler lanes failed
with 503 `no eligible model for this rating … requirements_unmet: the rating
outruns the fleet` in 34 minutes (lifetime 322 such ticks, concentrated in the
`-qa`/`-releng` lanes rated terminal=3 tool_use=3). TR-194/195 had already made
the resolver fail closed and made the no-hops RATE measurable — and nothing
alarmed, because:

  * the rate arm divides no-hops by ALL resolves, and the proxy also serves
    hundreds of normal requests: 190 starved lanes could not move a 24h rate
    past its baseline threshold;
  * nothing counted unservable ratings as their own population, so "which
    rating, on which lane family, how often" needed a log grep;
  * the ledger row said `unservable-rating` but not WHICH lane family, so a
    supervisor tick could not tell routing starvation from a gateway outage.

This file pins the three acceptance criteria, in the order the row states them:

  AC1  the rating-resolver records a per-unservable-rating counter, keyed by
       (required_categories fingerprint, lane_class), queryable without log
       grepping through GET /api/ui/unservable.
  AC2  the TR-195 baseline alarm FIRES at the observed volume — proven by a
       replay of the incident window (190 ratings in 34 min), NOT by a single
       synthetic request, and proven to be the VOLUME arm that fires (the rate
       arm is asserted silent on the same fixture, so the test cannot pass
       vacuously).
  AC3  a scheduler tick that hits 503 unservable is recorded as its own
       failure_reason class — with required_categories and lane_class on the
       row — driven through the wired proxy exit (resolver SUBPROCESS), not a
       hand-built row.

No network, no live state: the registry and state plane are provisioned per
test, the outcome store and the counter store are temp files, and _proxy_record
is stubbed where the row (not the store) is the artefact under test.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import router_nohops as rn        # noqa: E402
import router_server as rsrv      # noqa: E402
import router_spawn               # noqa: E402

#: The incident's rating shape (terminal=3, tool_use=3) plus the two categories
#: that make it UNSERVABLE in the committed tables: math has exactly one tier-5
#: holder and security exactly two, and the sets are disjoint — so no lane can
#: clear math>=5 AND security>=5 (derived from data/tables, 2026-10-06).
UNSAT_MATRIX = {'math': 5, 'security': 5}
#: The incident-shaped superset — still unsatisfiable (a superset of an empty
#: intersection stays empty), and it carries the real terminal/tool_use cells.
UNSAT_MATRIX_WIDE = {'math': 5, 'security': 5, 'terminal': 3, 'tool_use': 3}
#: The REAL 2026-10-20 lane rating as it landed on the ledger rows (the
#: -qa/-releng lanes were rated terminal=3 tool_use=3). Used by the replay
#: fixture, where the matrix only has to look like production.
INCIDENT_MATRIX = {'agent_tick': 2, 'terminal': 3, 'tool_use': 3,
                   'delegation': 2, 'long_horizon': 2, 'review': 2}

#: The recorded baseline (data/nohops-baseline.json, measured 2026-10-07):
#: rate 0.177329 + headroom 0.05 = threshold 0.227329. Named here so the
#: "the rate arm is silent" premise is explicit rather than assumed.
BASELINE = {'rate': 0.177329, 'threshold': 0.227329,
            'kind': 'nohops-baseline', 'window_h': 168.0}
THRESHOLD = 0.227329


# ---------------------------------------------------------------------------
# fixtures: a hermetic registry + state plane + both stores
# ---------------------------------------------------------------------------

def _committed_tables():
    """The committed registry tables (the resolver's own data, frozen)."""
    tables = {}
    for fn in sorted((REPO / 'data' / 'tables').glob('*.jsonl')):
        tables[fn.name[:-len('.jsonl')]] = [
            json.loads(ln) for ln in fn.open() if ln.strip()]
    return tables


def _open_providers(tables):
    """Every provider PRESENT and OPEN (the resolver's policy plane is
    fail-closed on absence, so an empty quota doc gates the fallback lanes)."""
    provs = {r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
    provs |= {m.get('provider') for m in (tables.get('models') or [])
              if m.get('provider')}
    return {p: {'status': 'open'} for p in sorted(provs)}


def _prep_registry(monkeypatch, tmp_path, tables):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables}))
    monkeypatch.setattr(router_spawn, 'REGISTRY', str(reg))


def _open_state_dir(tmp_path, providers_doc):
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    json.dump({'updated': 'test', 'providers': providers_doc},
              open(d / 'quota-state.json', 'w'))
    json.dump({'providers': {}}, open(d / 'health-state.json', 'w'))
    json.dump({'pairs': {}}, open(d / 'circuit-state.json', 'w'))
    return str(d)


@pytest.fixture()
def hermetic(monkeypatch, tmp_path):
    """No committed baseline, no webhook, no live events log, temp stores."""
    monkeypatch.delenv('ROUTER_NOHOPS_THRESHOLD', raising=False)
    monkeypatch.delenv('ROUTER_NOHOPS_HEADROOM', raising=False)
    monkeypatch.delenv('ROUTER_NOHOPS_MIN_N', raising=False)
    monkeypatch.delenv('ROUTER_NOHOPS_BURST_MIN', raising=False)
    monkeypatch.delenv('ROUTER_NOHOPS_BURST_WINDOW_H', raising=False)
    monkeypatch.setattr(rn, 'BASELINE_PATH', str(tmp_path / 'no-baseline.json'))
    monkeypatch.setattr(rn, 'EVENTS_PATH', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(rn, 'WEBHOOK_URL', '')
    monkeypatch.setenv('ROUTER_UNSERVABLE_FILE',
                       str(tmp_path / 'unservable-counters.jsonl'))
    return tmp_path


def _counter_rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(ln) for ln in open(path, encoding='utf-8')
            if ln.strip()]


# ---------------------------------------------------------------------------
# AC1: the resolver's per-unservable-rating counter
# ---------------------------------------------------------------------------

def test_unsatisfiable_ratings_are_proven_unsatisfiable_here():
    """Precondition for every AC1 assertion below: the two fixture matrices
    really have NO eligible lane in the committed tables (a satisfiable matrix
    would make the counter test vacuous)."""
    tables = _committed_tables()
    for matrix in (UNSAT_MATRIX, UNSAT_MATRIX_WIDE):
        assert not router_spawn._build_chain(tables, sorted(matrix.items())), \
            f'fixture precondition broken: {matrix} is satisfiable now'


def test_the_resolver_counts_each_unservable_rating_with_its_matrix_and_lane(
        monkeypatch, tmp_path, hermetic):
    """AC1: 5 unservable ratings -> 5 counter rows, 2 matrices, 2 lane classes.

    Driven through router_spawn.resolve() — the ONE place a rating is proven
    unservable — so the counter cannot drift from the 503 path.
    """
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setattr(router_spawn, 'MR',
                        _open_state_dir(tmp_path, _open_providers(tables)))

    drive = [(UNSAT_MATRIX, 'lore-qa'), (UNSAT_MATRIX, 'lore-qa'),
             (UNSAT_MATRIX_WIDE, 'task-router-releng'),
             (UNSAT_MATRIX_WIDE, 'task-router-releng'),
             (UNSAT_MATRIX_WIDE, 'task-router-releng-local')]
    for matrix, lane in drive:
        r = router_spawn.resolve(adhoc=[f'{c}={v}' for c, v in sorted(matrix.items())],
                                 lane=lane)
        assert 'no chain' in str(r.get('error')), \
            f'{matrix} on {lane} must dead-end (no fallback table) — got {r}'
        assert r.get('required_categories') == matrix, \
            'the error doc must carry the rating the counter keyed on'
        assert r.get('lane_class') == rn.lane_class(lane), \
            'the error doc lane_class must be the census classifier verdict'

    path = os.environ['ROUTER_UNSERVABLE_FILE']
    rows = _counter_rows(path)
    assert len(rows) == 5, f'one row per unservable rating, got {len(rows)}: {rows}'
    for row in rows:
        # the four AC1 fields, plus the provenance that keeps a default honest
        assert isinstance(row['ts'], float) and row['ts'] > 0
        assert row['matrix_hash'] == rn.matrix_hash(row['required_categories'])
        assert row['lane_class'] in rn.LANE_CLASSES
        assert isinstance(row['required_categories'], dict)
    assert {r['lane_class'] for r in rows} == {'qa', 'releng'}
    assert len({r['matrix_hash'] for r in rows}) == 2
    assert [r['lane_class'] for r in rows].count('qa') == 2
    # a trailing qualifier ('-local') must not hide the family it qualifies
    assert [r['lane_class'] for r in rows].count('releng') == 3
    # the matrix fingerprint is order-insensitive (dict order is not a rating)
    assert rn.matrix_hash({'terminal': 3, 'tool_use': 3}) == \
        rn.matrix_hash({'tool_use': 3, 'terminal': 3})
    assert rn.matrix_hash(UNSAT_MATRIX) != rn.matrix_hash(UNSAT_MATRIX_WIDE)


def test_the_counter_file_is_append_only_and_survives_a_write_failure(
        monkeypatch, tmp_path, hermetic):
    """Fail-open: a counter path that cannot be written is a None, never an
    exception — a resolve must keep its 503 semantics on a broken disk."""
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('x')  # a FILE where a directory is needed
    monkeypatch.setenv('ROUTER_UNSERVABLE_FILE',
                       str(blocker / 'nested' / 'counters.jsonl'))
    assert rn.record_unservable(matrix=UNSAT_MATRIX, lane='lore-qa') is None
    # …and the resolver still answers with its structured error doc
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setattr(router_spawn, 'MR',
                        _open_state_dir(tmp_path, _open_providers(tables)))
    r = router_spawn.resolve(
        adhoc=[f'{c}={v}' for c, v in sorted(UNSAT_MATRIX.items())], lane='lore-qa')
    assert 'no chain' in str(r.get('error'))
    assert r.get('lane_class') is None  # no row -> no claim, never a fabricated one


def test_the_census_is_queryable_without_grepping_logs(monkeypatch, hermetic):
    """AC1: GET /api/ui/unservable -> {window_h, buckets[{ts, matrix_hash,
    lane_class, count}], top_n} from the append-only file. A row outside the
    window is EXCLUDED, and the exclusion is visible (total, not a silent zero)."""
    now = time.time()
    path = os.environ['ROUTER_UNSERVABLE_FILE']
    # 3 qa + 2 releng inside the window, one 2 days old (outside a 24h window)
    for i in range(3):
        rn.record_unservable(matrix=UNSAT_MATRIX, lane='lore-qa',
                             path=path, ts=now - 600 - i * 30)
    for i in range(2):
        rn.record_unservable(matrix=UNSAT_MATRIX_WIDE, lane='task-router-releng',
                             path=path, ts=now - 900 - i * 30)
    rn.record_unservable(matrix=UNSAT_MATRIX, lane='lore-qa',
                         path=path, ts=now - 48 * 3600)

    app = rsrv.RouterApplication('read-only', None)
    status, res = app.dispatch('GET', '/api/ui/unservable',
                               query={'window_h': '24', 'top_n': '5'})
    assert status == 200
    assert res['window_h'] == 24.0 and res['top_n'] == 5
    assert res['total'] == 5, res
    assert res['rows_scanned'] == 6, 'the store was read in full, then windowed'
    buckets = res['buckets']
    assert buckets and all({'ts', 'matrix_hash', 'lane_class', 'count'} <= set(b)
                           for b in buckets)
    assert sum(b['count'] for b in buckets) == 5, 'bucket counts must reconcile'
    by_class = {}
    for b in buckets:
        by_class[b['lane_class']] = by_class.get(b['lane_class'], 0) + b['count']
    assert by_class == {'qa': 3, 'releng': 2}
    assert res['by_lane_class'] == {'qa': 3, 'releng': 2}
    assert len({b['matrix_hash'] for b in buckets}) == 2
    assert res['distinct_matrices'] == 2
    # top-n is a ranking over the window, largest first
    assert [t['count'] for t in res['top']] == [3, 2]

    # the same window widened includes the 48h-old row — the window is real
    status, wide = app.dispatch('GET', '/api/ui/unservable',
                                query={'window_h': '72'})
    assert status == 200 and wide['total'] == 6

    # …and the CLI prints the SAME document (one function, two surfaces)
    out = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'router_nohops.py'),
         'unservable', '--window-h', '24', '--json'],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, 'ROUTER_UNSERVABLE_FILE': path})
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc['buckets'] == res['buckets'] and doc['total'] == res['total']


def test_the_route_is_in_the_served_openapi_contract():
    """A census nobody can call is not queryable: the route must be in the
    served contract (the muster parity tests derive dispatch coverage from it)."""
    spec = rsrv.OPENAPI['paths']['/api/ui/unservable']
    assert spec['get']['operationId'] == 'getUiUnservable'
    assert {p['name'] for p in spec['get']['parameters']} == \
        {'window_h', 'top_n', 'scan_limit'}


# ---------------------------------------------------------------------------
# AC2: the alarm fires at the OBSERVED volume (replay, not one request)
# ---------------------------------------------------------------------------

def _incident_store(tmp_path, n_unservable=190, n_served=1500,
                    burst_minutes=34.0, matrix=None, now=None):
    """A fixture ledger shaped like the 2026-10-20 window: `n_unservable`
    distinct lanes failing inside `burst_minutes`, on top of the normal traffic
    the proxy served in the same day. Returns (path, now)."""
    now = now or time.time()
    matrix = dict(matrix or INCIDENT_MATRIX)
    rows = []
    span = burst_minutes * 60.0
    for i in range(n_unservable):
        rows.append({
            'ts': now - 60.0 - (i * span / max(1, n_unservable)),
            'route_outcome': 'no-hops', 'failure_reason': 'unservable-rating',
            'required_categories': dict(matrix),
            'source_system': 'router-proxy',
            'session_id': f'lane-{i:04d}', 'provider': 'none',
            'model': 'none', 'hops_attempted': 0,
        })
    for i in range(n_served):
        rows.append({
            'ts': now - 120.0 - (i * 23.0 * 3600 / max(1, n_served)),
            'route_outcome': 'served', 'failure_reason': None,
            'source_system': 'router-proxy',
            'session_id': f'served-{i:04d}', 'provider': 'zai-glm',
            'model': 'glm-5.2', 'hops_attempted': 1,
        })
    path = tmp_path / 'outcomes.jsonl'
    with open(path, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r) + '\n')
    return str(path), now


def test_the_2026_10_20_window_alerts_on_volume_not_on_rate(tmp_path, hermetic):
    """AC2 (the incident, replayed): 190 unservable ratings in 34 minutes MUST
    alarm — and the RATE arm must be silent on the same rows, because that is
    why nothing fired on 2026-10-20. Both halves are asserted, so this test
    cannot pass vacuously."""
    store, now = _incident_store(tmp_path)
    rows, meta = rn.load_store_rows(store)
    assert meta['rows_scanned'] == 1690 and not meta.get('error')

    result = rn.evaluate(rows, window_h=24.0, now_s=now, baseline=BASELINE)
    p = result['payload']
    assert p['nohops'] == 190 and p['resolves'] == 1690
    assert p['rate'] == round(190 / 1690, 6)
    assert p['rate'] < p['threshold'], 'premise: the rate arm could not see this'
    assert p['rate_breached'] is False, \
        'the RATE arm must be silent — otherwise this test proves nothing'
    assert p['burst']['nohops'] == 190, p['burst']
    assert p['burst']['breached'] is True
    assert p['alert'] is True and p['breach_kind'] == 'burst'
    assert 'BURST' in rn.alert_text(p), 'the alert text must name the arm'

    # the same verdict through the CLI an operator (or cron) actually runs
    cli = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'router_nohops.py'),
         'check', '--alert-dry-run'],
        capture_output=True, text=True, timeout=180,
        env={**os.environ,
             'ROUTING_OUTCOMES_FILE': store,
             'ROUTER_NOHOPS_EVENTS_FILE': str(tmp_path / 'events.jsonl'),
             'ROUTER_NOHOPS_THRESHOLD': str(THRESHOLD),
             'ROUTER_UNSERVABLE_FILE': str(tmp_path / 'counters.jsonl')})
    assert cli.returncode == 1, f'the check must exit 1 on a breach: {cli.stdout}'
    assert 'verdict=ALERT' in cli.stdout, cli.stdout
    assert 'kind=burst' in cli.stdout, cli.stdout
    assert 'verdict=OK' not in cli.stdout


def test_a_volume_below_the_burst_floor_does_not_alarm(tmp_path, hermetic):
    """The negative arm: the SAME shape at 20 ratings is noise, not starvation.
    Without this, AC2 would be satisfied by an alarm that always fires."""
    store, now = _incident_store(tmp_path, n_unservable=20)
    rows, _ = rn.load_store_rows(store)
    p = rn.evaluate(rows, window_h=24.0, now_s=now, baseline=BASELINE)['payload']
    assert p['burst']['nohops'] == 20 and p['burst']['breached'] is False
    assert p['alert'] is False and p['breach_kind'] is None

    cli = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'router_nohops.py'),
         'check', '--alert-dry-run'],
        capture_output=True, text=True, timeout=180,
        env={**os.environ,
             'ROUTING_OUTCOMES_FILE': store,
             'ROUTER_NOHOPS_EVENTS_FILE': str(tmp_path / 'events.jsonl'),
             'ROUTER_NOHOPS_THRESHOLD': str(THRESHOLD),
             'ROUTER_UNSERVABLE_FILE': str(tmp_path / 'counters.jsonl')})
    assert cli.returncode == 0, cli.stdout
    assert 'verdict=OK' in cli.stdout and 'would NOT alert' in cli.stdout


def test_the_burst_floor_is_the_real_knob(tmp_path, hermetic, monkeypatch):
    """Red/green on the floor itself: the 20-rating fixture DOES alert when the
    floor is lowered to 20 — proving the volume arm, not some other path,
    decides the verdict."""
    store, now = _incident_store(tmp_path, n_unservable=20)
    rows, _ = rn.load_store_rows(store)
    monkeypatch.setenv('ROUTER_NOHOPS_BURST_MIN', '20')
    p = rn.evaluate(rows, window_h=24.0, now_s=now, baseline=BASELINE)['payload']
    assert p['burst']['min'] == 20 and p['burst']['breached'] is True
    assert p['alert'] is True and p['breach_kind'] == 'burst'


def test_the_burst_window_is_clamped_to_the_evaluated_window(tmp_path, hermetic):
    """The volume arm counts inside the burst window, never outside it, and
    never wider than the window the caller asked about."""
    store, now = _incident_store(tmp_path, n_unservable=190, burst_minutes=34.0)
    rows, _ = rn.load_store_rows(store)
    # a 30-minute evaluation may only count what fits in 30 minutes
    b30 = rn.nohops_burst(rows, now_s=now, window_h=0.5)
    assert b30['window_h'] == 0.5
    assert 0 < b30['nohops'] <= 190
    b24 = rn.nohops_burst(rows, now_s=now, window_h=24.0)
    assert b24['window_h'] == rn.burst_window_h() == 1.0
    assert b24['nohops'] == 190


# ---------------------------------------------------------------------------
# AC3: the 503 is its own failure class, WITH the lane family on the row
# ---------------------------------------------------------------------------

def _proxy_env(monkeypatch, tmp_path, tables, providers_doc):
    """Hermetic proxy run: temp registry + state dir via env (the resolver runs
    in a SUBPROCESS and copies os.environ), ledger write captured not stored."""
    _prep_registry(monkeypatch, tmp_path, tables)
    monkeypatch.setenv('ROUTER_STATE_DIR',
                       _open_state_dir(tmp_path, providers_doc))
    monkeypatch.setenv('ROUTING_REGISTRY', str(tmp_path / 'registry.json'))
    rec = []

    def _record(provider, model, ok, requirements, **kw):
        rec.append({'provider': provider, 'model': model, 'ok': ok,
                    'required_categories': (requirements or {}).get('matrix'),
                    **kw})
    monkeypatch.setattr(rsrv, '_proxy_record', _record)
    return rec


def test_a_503_unservable_tick_is_its_own_class_with_its_lane_family(
        monkeypatch, tmp_path, hermetic):
    """AC3 (wired): an unsatisfiable rating through proxy_chat must leave a row
    with failure_reason='unservable-rating' — NOT 'no-hops'/'gateway_transport'
    — plus required_categories and lane_class from the caller's declaration.
    The resolver subprocess is real, so the census row is real too."""
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    rec = _proxy_env(monkeypatch, tmp_path, tables, {})
    import router_jev
    monkeypatch.setattr(router_jev, 'classify',
                        lambda text: {'matrix': dict(UNSAT_MATRIX),
                                      'complexity_sig': None,
                                      'problems': []})
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tick the fleet'}]},
        {'x-router-scorer': 'jev', 'x-router-lane': 'task-router-releng'})
    assert status == 503
    assert 'no eligible model for this rating' in out['error']
    assert out['failure_reason'] == 'unservable-rating'
    assert out['lane_class'] == 'releng'
    assert out['_router']['terminal_reason'] == 'no-hops'
    row = rec[-1]
    assert row['failure_reason'] == 'unservable-rating', row
    assert row['route_outcome'] == 'no-hops', \
        'the outcome vocabulary TR-195 counts stays untouched'
    assert row['required_categories'] == UNSAT_MATRIX
    assert row['lane_class'] == 'releng'
    assert row['lane_class_source'] == 'declared'
    # the counter saw it too — through the resolver SUBPROCESS (one choke point)
    rows = _counter_rows(os.environ['ROUTER_UNSERVABLE_FILE'])
    assert len(rows) == 1, rows
    assert rows[0]['lane_class'] == 'releng'
    assert rows[0]['matrix_hash'] == rn.matrix_hash(UNSAT_MATRIX)


def test_a_503_unservable_without_a_lane_declaration_says_primary_and_why(
        monkeypatch, tmp_path, hermetic):
    """A caller that names no lane still gets a class (the documented 'primary'
    catch-all) and the reason it is one — never a silent guess presented as a
    fact. The failure CLASS is unaffected."""
    tables = _committed_tables()
    tables.pop('fallback_lanes', None)
    rec = _proxy_env(monkeypatch, tmp_path, tables, {})
    import router_jev
    monkeypatch.setattr(router_jev, 'classify',
                        lambda text: {'matrix': dict(UNSAT_MATRIX),
                                      'complexity_sig': None,
                                      'problems': []})
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tick the fleet'}]},
        {'x-router-scorer': 'jev'})
    assert status == 503 and out['failure_reason'] == 'unservable-rating'
    row = rec[-1]
    assert row['failure_reason'] == 'unservable-rating'
    assert row['lane_class'] == 'primary'
    assert row['lane_class_source'] == 'default-no-lane-hint'


def test_an_all_gated_no_hops_keeps_the_plain_reason_and_no_lane_claim(
        monkeypatch, tmp_path, hermetic):
    """Control (TR-194 preserved): a plain gating no-hops is NOT relabelled and
    is NOT counted as an unservable rating."""
    tables = _committed_tables()
    rec = _proxy_env(monkeypatch, tmp_path, tables, {})
    status, out = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'hi'}]},
        {'x-router-profile': 'P1_CODING'})
    assert status == 503
    row = rec[-1]
    assert row['failure_reason'] == 'no-hops', \
        'a gating no-hops keeps its own reason (attribution, not relabelling)'
    assert row['lane_class'] is None
    assert _counter_rows(os.environ['ROUTER_UNSERVABLE_FILE']) == [], \
        'only a PROVEN unservable rating may enter the census'


def test_lane_class_covers_every_documented_family():
    """The enum is a closed set (the AC names seven classes); the classifier
    maps real fleet lane names onto it and nothing else."""
    assert set(rn.LANE_CLASSES) == {'qa', 'releng', 'docs', 'readme', 'review',
                                    'foreman', 'primary'}
    cases = {'lore-qa': 'qa', 'task-router-qa': 'qa',
             'task-router-releng': 'releng', 'task-router-releng-local': 'releng',
             'heading-docs': 'docs', 'crier-readme': 'readme',
             'bunker-review': 'review', 'coding-hermes-foreman': 'foreman',
             'coding-hermes-scheduler': 'primary', 'task-router': 'primary',
             'lore_sync_docs': 'docs', None: 'primary', '': 'primary'}
    for name, expected in cases.items():
        assert rn.lane_class(name) == expected, (name, rn.lane_class(name))

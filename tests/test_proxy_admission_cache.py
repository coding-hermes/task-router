"""TR-172: the proxy bounds its work and rates an identical prompt once.

Origin: the 2026-09-25 incident. The scheduler bounds work by ACCEPTED SLOTS and its
failure retry is bounded + backed off, so a failing call cost it little; this side
accepted every attempt and spent an upstream model call on each — including a rating
call carrying the caller's whole context (~46k tokens). Measured: 495 proxied requests
in 78 minutes produced 430 no-hops rows (zero upstream hops) while still paying for a
classification each, and API calls per session went 6.5 -> 40 (6.1x).

Contracts pinned here:
  * over the cap, the caller gets a RETRYABLE refusal (429 + Retry-After), not a hang
  * the refusal is VISIBLE: a ledger row, and the numbers in /health
  * work under the cap is unaffected (the honest regression guard)
  * an identical prompt is rated ONCE; a FAILED rating is never cached
  * both guards are inert for normal traffic (no behaviour change when not saturated)

TR-239 adds: the ingress admission bounds are DERIVED from the enabled-lane
count, and a fleet-shaped scheduler tick (N lanes arriving together, each
request retryable, tasks x max_hops=3 hops) is SERVED — served fraction
>= 95%, zero refusals, any refusal named by class and counted.
"""
import json
import os
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_ingress as ri    # noqa: E402  (the real LaneGuard/Admission classes)
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    # keep the REAL requirements function reachable: the cache test needs it, since the
    # cache lives inside it. Stubbing it wholesale would test the stub.
    monkeypatch.setattr(rsrv, '_REAL_PROXY_REQUIREMENTS', rsrv._proxy_requirements, raising=False)
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 1}, 'complexity_sig': 'sig', 'profile_id': None,
                       'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1'}], 'sort': 'price'})
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _ok():
    return lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}], 'usage': {}})


@pytest.fixture(autouse=True)
def _fresh_admission(monkeypatch):
    """Each test gets its own cap so a saturated one cannot leak into the next."""
    monkeypatch.setattr(rsrv, '_ADMISSION_SEM', None)
    for k in ('accepted', 'rejected', 'waiting', 'inflight', 'peak_inflight'):
        rsrv._ADMISSION[k] = 0
    with rsrv._CLASSIFY_LOCK:
        rsrv._CLASSIFY_CACHE.clear()
        rsrv._CLASSIFY_CACHE_AT.clear()
        for k in rsrv._CLASSIFY_STATS:
            rsrv._CLASSIFY_STATS[k] = 0
    yield
    rsrv._ADMISSION_SEM = None


def test_work_under_the_cap_is_untouched(ledger, monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '2')
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                      {'x-router-session': 's-ok'}, upstream=_ok())
    assert status == 200, payload
    assert rsrv._admission_stats()['accepted'] == 1
    assert rsrv._admission_stats()['inflight'] == 0, 'the slot is released on the way out'


def test_over_the_cap_the_caller_gets_a_retryable_refusal(ledger, monkeypatch):
    """A refusal, not a hang and not a silent drop."""
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '1')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '0')     # no waiting room at all
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_WAIT_S', '0')
    gate = rsrv._admission()
    gate.__enter__()                                     # occupy the only slot
    try:
        status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                          {'x-router-session': 's-refused'}, upstream=_ok())
    finally:
        gate.__exit__()
    assert status == 429, payload
    assert payload['retry_after_s'] >= 1
    assert payload['_router']['failure_reason'] == 'overloaded'
    assert payload['_router']['usage'] is None and payload['_router']['cost_usd'] is None, \
        'a refusal must not claim to have spent anything'
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'rejected'][0]
    assert row['failure_reason'] == 'overloaded' and row['hops_attempted'] == 0
    assert rsrv._admission_stats()['rejected'] >= 1


def test_the_refusal_carries_retry_after_on_the_wire_not_only_in_the_body():
    """A real client honours the header; a body-only hint is one nobody must read."""
    import inspect
    src = inspect.getsource(rsrv)
    assert "headers['Retry-After']" in src, 'the 429 must set Retry-After'
    assert 'retry_after_s' in src


def test_health_exposes_the_bound_and_the_cache(ledger, monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '3')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '7')
    payload = rsrv.RouterApplication('read-only', None).dispatch('GET', '/health')[1]
    assert payload['admission']['max_inflight'] == 3
    assert payload['admission']['queue_max'] == 7
    assert 'rejected' in payload['admission'] and 'inflight' in payload['admission']
    assert payload['classify_cache']['hits'] == 0 and payload['classify_cache']['entries'] == 0


def test_an_identical_prompt_is_rated_once(ledger, monkeypatch):
    calls = {'n': 0}

    class FakeClassify:
        @staticmethod
        def classify(text):
            calls['n'] += 1
            return {'matrix': {'code_gen': 2}, 'complexity_sig': 'sig-x', 'confidence': 0.9,
                    'model': 'fake-scorer', 'problems': []}

    monkeypatch.setitem(sys.modules, 'router_classify', FakeClassify)
    monkeypatch.setattr(rsrv, '_proxy_requirements', rsrv._REAL_PROXY_REQUIREMENTS)
    body = {'messages': [{'role': 'user', 'content': 'the very same tick prompt'}]}
    for _ in range(3):
        rsrv.proxy_chat('/v1/chat/completions', body, {'x-router-session': 's-cache'},
                        upstream=_ok())
    assert calls['n'] == 1, f'the rating is a model call; identical prompts must not repay it ({calls["n"]})'
    st = rsrv.classify_cache_stats()
    assert st['hits'] == 2 and st['puts'] == 1 and st['hit_rate'] == pytest.approx(2 / 3, abs=0.01)


def test_a_failed_rating_is_never_cached(monkeypatch):
    """A broken scorer must recover on the NEXT request, so its failure is not frozen."""
    class Failing:
        @staticmethod
        def classify(text):
            return {'matrix': None, 'complexity_sig': None, 'problems': ['no JSON object']}

    monkeypatch.setitem(sys.modules, 'router_classify', Failing)
    rsrv.classify_cache_put('some prompt', {'matrix': None})
    assert rsrv.classify_cache_stats()['entries'] == 0, 'a failure is not a rating'


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_CACHE_MAX', '3')
    for i in range(6):
        rsrv.classify_cache_put(f'prompt-{i}', {'matrix': {'code_gen': i}})
    st = rsrv.classify_cache_stats()
    assert st['entries'] == 3, 'unbounded memory growth in a long-lived proxy is a leak'
    assert st['evictions'] == 3


def test_router_internal_outcomes_do_not_open_provider_breakers(monkeypatch):
    calls = []
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *args, **kwargs: calls.append(args))
    rsrv._proxy_record('none', 'none', False, {}, source='router-proxy',
                       route_outcome='rejected', failure_reason='overloaded')
    rsrv._proxy_record('none', 'none', False, {}, source='router-proxy',
                       route_outcome='no-hops', failure_reason='no-hops')
    assert calls == [], 'router-internal failures must not trip provider-wide breakers'


def test_an_expired_rating_is_not_served(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_CACHE_TTL_S', '1')
    rsrv.classify_cache_put('expiring', {'matrix': {'code_gen': 1}})
    hit, _ = rsrv.classify_cache_get('expiring')
    assert hit is True
    rsrv._CLASSIFY_CACHE_AT[rsrv._classify_cache_key('expiring')] = time.time() - 10
    hit2, val2 = rsrv.classify_cache_get('expiring')
    assert hit2 is False and val2 is None, 'a stale rating from another generation must not decide a lane'


def test_concurrent_work_is_actually_bounded(ledger, monkeypatch):
    """The point of the whole change: N callers cannot all be in flight at once."""
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '2')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '0')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_WAIT_S', '0')
    seen, lock = [], threading.Lock()

    def slow(*a, **k):
        with lock:
            seen.append(rsrv._admission_stats()['inflight'])
        time.sleep(0.15)
        return 200, {'choices': [{'message': {'content': 'ok'}}], 'usage': {}}

    out = []
    def worker(i):
        out.append(rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                   {'x-router-session': f's{i}'}, upstream=slow)[0])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max(seen) <= 2, f'never more than the cap in flight, saw {seen}'
    assert out.count(429) >= 3, f'excess callers must be refused, got {out}'
    assert rsrv._admission_stats()['peak_inflight'] <= 2


# ---------------------------------------------------------------------------
# TR-239 — the bounds are a function of the enabled-lane count, and a
# fleet-shaped scheduler tick is SERVED under them.
# ---------------------------------------------------------------------------

#: Fleet-shaped fixture: 48 enabled lanes arriving together. The env override
#: (a) pins the count the derivation uses, so the test never depends on which
#: registry surface the checkout happens to expose.
FLEET_LANES = 48
#: Per-lane task concurrency in one tick — deliberately ONE MORE than the
#: derivation's TICK_TASKS_PER_LANE=2: the bound must absorb a tick that
#: slightly over-delivers per lane, not only the model's exact shape.
TICK_TASKS = 3
#: The documented load model: every task may traverse up to max_hops=3 router
#: hops, each hop one ingress request, each retryable.
HOPS_PER_TASK = 3

#: The refusal classes the row names (the Refusal messages today): queue full
#: / no slot within N s / global backstop full / circuit open — split by
#: which governor raised them.
_REFUSAL_CLASSES = (
    ('lane_queue_full',
     lambda r: r.startswith('lane-busy') and 'queue full' in r),
    ('lane_no_slot_within_wait_s',
     lambda r: r.startswith('lane-busy') and 'no slot within' in r),
    ('shared_queue_full',
     lambda r: r.startswith('overloaded') and 'queue full' in r),
    ('shared_no_slot_within_wait_s',
     lambda r: r.startswith('overloaded') and 'no slot within' in r),
    ('global_backstop_full', lambda r: 'global backstop full' in r),
    ('circuit_open', lambda r: r.startswith('endpoint-circuit-open')),
)


def _refusal_class(reason):
    """A refusal is always assigned to a NAMED class, never left anonymous."""
    for name, match in _REFUSAL_CLASSES:
        if match(reason or ''):
            return name
    return 'unclassified'


def _run_tick(governor, lanes, tasks, hops, retries=1):
    """Drive ONE scheduler tick against the REAL governor.

    `lanes` lanes arrive TOGETHER (one barrier), each carrying `tasks`
    concurrent tasks; each task walks its `hops` requests SEQUENTIALLY (hop
    i+1 only after hop i settles — the real shape). Every request is
    retryable work, so a refusal is retried a bounded `retries` times; every
    refusal is counted by named class.

    Returns (served, refusals_by_class, first_attempts).
    """
    barrier = threading.Barrier(lanes * tasks)
    counts = {'served': 0, 'first_attempts': 0}
    refusals = {}
    lock = threading.Lock()

    def task_worker(lane_name):
        barrier.wait(60)
        for _hop in range(hops):
            for attempt in range(1 + retries):
                if attempt == 0:
                    with lock:
                        counts['first_attempts'] += 1
                try:
                    lease = governor.hold(lane_name)
                except ri.Refusal as refused:
                    cls = _refusal_class(refused.reason)
                    with lock:
                        refusals[cls] = refusals.get(cls, 0) + 1
                    continue
                with lock:
                    counts['served'] += 1
                lease.settle('ok', ok=True)
                break

    threads = [threading.Thread(target=task_worker, args=(f'tick-lane-{i}',))
               for i in range(lanes) for _ in range(tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return counts['served'], refusals, counts['first_attempts']


def test_a_fleet_shaped_scheduler_tick_is_served_under_the_derived_bounds(monkeypatch):
    """The point of TR-239: a fleet tick is SERVED, not refused.

    Arrivals are shaped like ONE scheduler tick: FLEET_LANES enabled lanes
    arriving together, TICK_TASKS tasks each, HOPS_PER_TASK = max_hops=3
    requests per task (the asserted hop multiplier: arrivals = lanes x tasks
    x hops). Under the derived per-lane bound the served fraction must be
    >= 95% — and for this authenticated fleet traffic, ZERO refusals; any
    refusal would be reported by named class with a count.
    """
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    monkeypatch.setenv('ROUTER_INGRESS_LANE_COUNT', str(FLEET_LANES))
    guard = ri.LaneGuard()          # the Ingress default: DERIVED per-lane bound
    arrivals = FLEET_LANES * TICK_TASKS * HOPS_PER_TASK
    served, refusals, first = _run_tick(guard, FLEET_LANES, TICK_TASKS, HOPS_PER_TASK)

    assert first == arrivals, \
        f'one tick = lanes x tasks x hops = {arrivals} arrivals, drove {first}'
    fraction = served / arrivals
    total_refusals = sum(refusals.values())
    assert fraction >= 0.95, \
        f'fleet served fraction {fraction:.3f} < 0.95; refusals by class: {refusals}'
    assert total_refusals == 0, \
        f'authenticated fleet traffic must not be refused under the derived bound: {refusals}'

    stats = guard.stats()
    assert stats['inflight'] == 0, 'the tick drains: every slot is released on the way out'
    per_lane = stats['lanes']
    assert len(per_lane) == FLEET_LANES, 'every lane of the tick was really admitted'
    assert max(s['peak_inflight'] for s in per_lane.values()) <= guard.per_lane_inflight, \
        'serving the fleet never exceeded the per-lane cap'
    print(f'fleet tick vs LaneGuard: served {served}/{arrivals} ({fraction:.1%}), '
          f'refusals by class: {refusals or "none"}, '
          f'derived per-lane queue={guard.per_lane_queue}')


def test_a_fleet_shaped_tick_through_the_shared_pool_is_served_too(monkeypatch):
    """The legacy shared pool (Admission), under its DERIVED bounds, serves a
    fleet tick as well: width grows with the fleet and the queue holds one
    whole tick's waiting room."""
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    monkeypatch.setenv('ROUTER_INGRESS_LANE_COUNT', str(FLEET_LANES))
    shared = ri.Admission()
    assert (shared.max_inflight, shared.queue_max) == ri._derived_shared_bounds(FLEET_LANES)
    arrivals = FLEET_LANES * TICK_TASKS * HOPS_PER_TASK
    served, refusals, first = _run_tick(shared, FLEET_LANES, TICK_TASKS, HOPS_PER_TASK)

    assert first == arrivals, f'arrivals = tasks x hops per lane, expected {arrivals}'
    fraction = served / arrivals
    assert fraction >= 0.95, \
        f'fleet served fraction {fraction:.3f} < 0.95; refusals by class: {refusals}'
    assert sum(refusals.values()) == 0, \
        f'zero refusals for fleet traffic under the derived shared bound: {refusals}'
    stats = shared.stats()
    assert stats['peak_inflight'] <= shared.max_inflight, 'the shared width held'
    print(f'fleet tick vs shared Admission: served {served}/{arrivals} ({fraction:.1%}), '
          f'refusals by class: {refusals or "none"}, derived width={shared.max_inflight} '
          f'queue={shared.queue_max}')


def test_the_pre_tr239_shared_bound_cannot_hold_one_whole_tick(monkeypatch):
    """The red arm for the fleet check: the SAME tick against the pre-TR-239
    constants genuinely refused fleet traffic. The derived bound is what
    changed the outcome, so this arm stays falsifiable after we leave.

    A slice of the tick (12 lanes x 2 tasks x 3 hops = 72 arrivals) hits a
    shared pool pinned to the old constants (4 slots + 16 waiting) whose slots
    are held the way real model calls hold them — for the duration. Nothing
    under that bound may be served, and every refusal must land in a named
    class with a count.
    """
    old = ri.Admission(max_inflight=4, queue_max=16, wait_s=0.2)   # the pre-TR-239 constants
    holders = [old.hold(f'held-{i}') for i in range(old.max_inflight)]
    try:
        served, refusals, first = _run_tick(old, 12, 2, 3, retries=0)
    finally:
        for h in holders:
            h.settle('ok', ok=True)
    arrivals = 12 * 2 * 3
    assert first == arrivals, f'one tick slice = lanes x tasks x hops = {arrivals}'
    fraction = served / arrivals
    assert fraction < 0.95, \
        f'the pre-TR-239 bound served {fraction:.3f} of the tick — the red arm rotted'
    assert refusals, 'the old bound must refuse the excess, and name the class'
    assert all(k in ('shared_queue_full', 'shared_no_slot_within_wait_s')
               for k in refusals), f'unexpected refusal classes: {refusals}'
    print(f'pre-TR-239 bound vs the same tick slice: served {served}/{arrivals} '
          f'({fraction:.1%}), refusals by class: {refusals}')


def test_the_derivation_floor_ceiling_override_and_sources(monkeypatch, tmp_path):
    """The derivation contract itself: resolved at call time from the
    enabled-lane count, floored at the pre-TR-239 constants, hard-ceilinged,
    cached per process, and the operator's env pins always win."""
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    # (a) the explicit override wins and is then cached for the process
    monkeypatch.setenv('ROUTER_INGRESS_LANE_COUNT', '96')
    assert ri._enabled_lane_count() == 96
    monkeypatch.setenv('ROUTER_INGRESS_LANE_COUNT', '7')
    assert ri._enabled_lane_count() == 96, 'resolved lazily once, cached per process'

    # the bounds GROW with the count — they are a function of it, not constants
    assert ri._derived_lane_queue_max(16) >= ri.LANE_QUEUE_MAX_FLOOR
    assert ri._derived_lane_queue_max(96) > ri._derived_lane_queue_max(16)
    assert ri._derived_lane_queue_max(ri.LANE_COUNT_INPUT_CEILING) == ri.LANE_QUEUE_MAX_CEILING, \
        'never unbounded: the per-lane queue has a hard ceiling'
    width_small, queue_small = ri._derived_shared_bounds(16)
    width_big, queue_big = ri._derived_shared_bounds(96)
    assert (width_big, queue_big) > (width_small, queue_small)
    width_min, queue_min = ri._derived_shared_bounds(1)
    assert (width_min, queue_min) == (ri.SHARED_MAX_INFLIGHT_FLOOR,
                                      ri.SHARED_QUEUE_MAX_FLOOR), \
        'the shared floors are the pre-TR-239 constants'
    width_max, queue_max = ri._derived_shared_bounds(ri.LANE_COUNT_INPUT_CEILING)
    assert width_max == ri.SHARED_MAX_INFLIGHT_CEILING
    assert queue_max == ri.SHARED_QUEUE_MAX_CEILING

    # a governor built under the derivation carries it; an env pin beats it
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    monkeypatch.setenv('ROUTER_INGRESS_LANE_COUNT', '96')
    guard = ri.LaneGuard()
    assert guard.per_lane_queue == ri._derived_lane_queue_max(96)
    monkeypatch.setenv('ROUTER_INGRESS_LANE_QUEUE_MAX', '3')
    assert ri.LaneGuard().per_lane_queue == 3, 'the operator pin wins'
    monkeypatch.delenv('ROUTER_INGRESS_LANE_QUEUE_MAX')
    shared = ri.Admission()
    assert (shared.max_inflight, shared.queue_max) == ri._derived_shared_bounds(96)
    monkeypatch.setenv('ROUTER_INGRESS_QUEUE_MAX', '5')
    assert ri.Admission().queue_max == 5, 'the operator pin wins'

    # (b) registry surface: the routing registry's projects table counts lanes
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 1, 'tables': {'projects': [
        {'id': 'lane-a'}, {'id': 'lane-b'}, {'id': ''}, 'junk-row']}}))
    monkeypatch.setenv('ROUTING_REGISTRY', str(reg))
    monkeypatch.delenv('ROUTER_INGRESS_LANE_COUNT')
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    assert ri._registry_lane_count() == 2, 'rows without an id are not lanes'
    assert ri._enabled_lane_count() == 2

    # (c) no surface at all -> the documented conservative fallback
    monkeypatch.setenv('ROUTING_REGISTRY', str(tmp_path / 'absent.json'))
    monkeypatch.setenv('ROUTER_INGRESS_ENDPOINTS', str(tmp_path / 'absent-endpoints.jsonl'))
    monkeypatch.setattr(ri, '_LANE_COUNT_CACHE', None)
    assert ri._enabled_lane_count() == ri.LANE_COUNT_FALLBACK

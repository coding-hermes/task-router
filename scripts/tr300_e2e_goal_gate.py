#!/usr/bin/env python3
"""TR-300: end-to-end goal gate — one harness proving the full feedback loop.

Runs against the LIVE proxy on :9391 (read-only mode, never restarted) and the
LIVE outcome ledger at data/state/outcomes.jsonl, with two deliberate writes:

* the write leg of the proof appends synthetic rows to the live store —
  through the proxy's own append path (router_outcomes.append_row_fast) — so
  the resolver reads them exactly as it reads real traffic. Every synthetic
  row carries source_system='tr300-synthetic', and every harness resolve runs
  with backend='tr300-synthetic' (the store's documented per-backend
  isolation), so the proof's ordering sees ONLY the synthetic evidence while
  real traffic is untouched. Rows are swept again at the end.
* AC3/AC4 write circuit-state.json — but in a HARNESS-OWNED state dir
  (ROUTER_STATE_DIR is exported before the resolver imports), seeded with the
  live quota/health policy files so the eligible pool matches production
  (TR-203's fail-closed policy plane would otherwise demote every provider).
  The circuit file under the harness dir starts absent; nothing in the live
  state dir is ever written.

The measured-ordering proof honours the router's own TR-183 doctrine: a
measured ordering may only apply when >= MEASURED_MIN_COVERAGE of the chain
is ranked on measurement. So the injection seeds verified passes for the
cheapest PAID lane (the star: 3 verified passes at $0/task) plus enough other
paid lanes (3 verified passes each at $1/task) to clear the coverage bar.

AC1 — one artifact per run phase: prompt_sha256, ratings (complexity_sig +
mapped requirements matrix), eligible-only candidate list with per-candidate
ordering basis, Hermes session id, attempted hops (the ladder), and the full
outcome row (provider/model, tokens, cost, wall time, route outcome). The
same task is submitted twice; both runs must produce the same canonical
complexity_sig.
AC2 — eligible-only candidates with explicit ordering basis: baseline (no
synthetic samples) every candidate reads 'price-fallback' and the resolve's
own sufficiency block says effective='price'; after the injection the star
lane reads 'measured' and the ordering flips to measured.
AC3 — a SOFT overload failure (the class the proxy writes for timeouts/5xx)
on one (provider, model) pair demotes ONLY that pair; the provider's sibling
lane stays eligible (circuit-open exclusion names exactly the pair).
AC4 — --provider-hard out_of_credit (the exact record the proxy writes for a
live 402) removes ALL lanes of that provider; the resolve continues on the
next provider.
AC5 — the harness derives pass/fail independently of HTTP status: the task's
acceptance keyword in the answer OR wall time under the budget.
AC6 — the post-injection resolve reorders: the star lane climbs from its
price-order position (it is paid, so it sits behind the free lanes) to the
HEAD, ranked on its measured $/passed-task (TR-174 blend, TR-299
per-PASSED-task).

Usage:
  python3 scripts/tr300_e2e_goal_gate.py            # full proof, artifacts to --out
  python3 scripts/tr300_e2e_goal_gate.py --out DIR  # artifact dir (default data/state/tr300)
  python3 scripts/tr300_e2e_goal_gate.py --skip-proxy  # AC2/AC3/AC4/AC6 only
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# The resolver bakes its state dir (quota/health/circuit reads) at IMPORT
# time (state_dir.resolve_state_dir runs at router_spawn import), so the
# harness-owned isolated state dir must exist and be exported BEFORE the
# imports below — per-call env setting cannot move it afterwards.
_HARNESS_STATE_DIR = tempfile.mkdtemp(prefix='tr300-state-')
_HARNESS_AVG_DIR = tempfile.mkdtemp(prefix='tr300-avg-')
os.environ['ROUTER_STATE_DIR'] = _HARNESS_STATE_DIR
os.environ['ROUTING_AVERAGES_FILE'] = os.path.join(_HARNESS_AVG_DIR,
                                                   'outcomes-averages.jsonl')

import router_circuit  # noqa: E402
import router_outcomes as ro  # noqa: E402

PROXY_URL = os.environ.get('TR300_PROXY_URL', 'http://127.0.0.1:9391')
#: The production gate-state dir (quota/health source for the harness copy).
#: The harness never WRITES here; AC3/AC4 circuit writes land in the isolated
#: state dir (see ROUTER_STATE_DIR above).
LIVE_STATE_DIR = (os.environ.get('TR300_LIVE_STATE_DIR')
                  or os.path.expanduser('~/.hermes/model-router'))
OUTCOMES_PATH = ro.outcomes_path()
#: The synthetic backend the injection writes under and every harness resolve
#: isolates to (per-backend isolation is the outcome store's documented
#: default). Cleanup removes every row carrying this marker.
SYN_SOURCE = 'tr300-synthetic'
#: Verified passes injected per lane (>= the sample floor).
PASSES_PER_LANE = 3
DEFAULT_ARTIFACT_DIR = os.path.join(REPO, 'data', 'state', 'tr300')
#: SOFT pair-local class — the same class the proxy's own _circuit_class
#: assigns to timeout/5xx hop failures. Written to the HARNESS state dir.
PAIR_CLASS = 'overload'
#: HARD provider-wide class — the exact record the proxy's own in-flight
#: demotion path writes for a 402 (TR-288 _demote_on_taxonomy):
#: router_circuit record-failure --provider-hard --class out_of_credit.
PROVIDER_CLASS = 'out_of_credit'
#: AC5 verification budget: a keyword pass OR a sub-budget wall time passes.
WALL_BUDGET_S = 900.0
#: How long to wait for the proxy's own outcome row to land in the ledger
#: (the recorder is fail-open best-effort). Each poll is a full-store scan,
#: so the loop is bounded by deadline, not by poll count.
LEDGER_WAIT_S = 30.0
#: Sample floor the sort honours (same env knob the resolver reads).
try:
    MEASURED_MIN_SAMPLES = int(os.environ.get('ROUTER_SORT_MIN_SAMPLES') or 3)
except ValueError:
    MEASURED_MIN_SAMPLES = 3

TEST_PROMPT = (
    'Write a Python function that sorts a list of dictionaries by a given key. '
    'End your answer with the exact token TR300TESTPROMPT.')

# ------------------------------------------------- harness lanes (derived) --
# Every lane the harness names below is derived from the LIVE registry's
# eligible pool for the harness band — never hardcoded — so the proof always
# exercises real (provider, model) pairs that clear the requirement bars.

#: The harness band: simple positive bars so the eligible pool is the wide
#: coding pool. Two shapes: the argv strings resolve() validates, and the
#: (cat, level) tuples _build_chain consumes directly.
ADHOC_REQUIREMENTS = ['code_gen=1', 'reasoning=1']
ADHOC_TUPLES = [(s.partition('=')[0], int(s.partition('=')[2]))
                for s in ADHOC_REQUIREMENTS]
INJECTED_LEVELS = {'code_gen': 1, 'reasoning': 1}


def _derive_pool() -> tuple:
    """(ordered [(provider, model, normalized_price)], paid-only prefix list)
    for the harness band, from the live registry's ELIGIBLE pool in its
    price-order. Fails loudly when the registry is unusable — the harness
    must never prove anything against invented lanes."""
    import router_spawn
    tables = router_spawn._load_registry()
    chain = router_spawn._build_chain(tables, ADHOC_TUPLES, limit=10**6)
    assert chain, ('registry produced an empty eligible pool for the harness '
                   'band — cannot derive harness lanes')
    ordered = [(prov, model, price) for _h, prov, model, price, _dc, _mr
               in chain]
    paid = [t for t in ordered if (t[2] or 0) > 0]
    assert paid, 'eligible pool has no priced lanes — cannot run the proof'
    return ordered, paid


#: (full eligible pool in price order, paid lanes only)
_POOL, _PAID = _derive_pool()
TOTAL_LANES = len(_POOL)

#: The star: the cheapest PAID lane. Being paid, it sits BEHIND the free
#: lanes in the price order — so its measured climb to the head (AC6) is a
#: visible reorder, not a no-op.
STAR_PROVIDER, STAR_MODEL, STAR_PRICE = _PAID[0]
STAR_PAIR = (STAR_PROVIDER, STAR_MODEL)

#: How many lanes must carry samples for the measured ordering to apply
#: (TR-183 coverage bar) — derived from the resolver's own constant, with
#: headroom, capped by the paid-pool size.
import router_spawn as _rs  # noqa: E402
from typing import Any
_COVERAGE_BAR = float(os.environ.get('ROUTER_SORT_MIN_COVERAGE')
                      or _rs.MEASURED_MIN_COVERAGE)
K_INJECTED = min(len(_PAID) - 1,
                 int(math.ceil(_COVERAGE_BAR * TOTAL_LANES)) + 2)
assert K_INJECTED >= 1, 'not enough priced lanes to clear the coverage bar'
#: Filler lanes: the next K priced lanes after the star (all price > 0, so
#: none can tie with the star's $0/task measured value at the head).
FILLER_PAIRS = [(p, m) for p, m, _pr in _PAID[1:1 + K_INJECTED]]
INJECTED_PAIRS = [STAR_PAIR] + FILLER_PAIRS
TOTAL_SYNTH_ROWS = PASSES_PER_LANE * len(INJECTED_PAIRS)

#: AC3 pair-local target: the star provider's two cheapest lanes. (Fleet
#: doctrine: a model name must never gate logic — the demoted pair and the
#: surviving sibling are whatever the registry actually serves.)
_a_models = []
for p, m, _pr in _POOL:
    if p == STAR_PROVIDER and (p, m) not in _a_models:
        _a_models.append((p, m))
assert len(_a_models) >= 2, (
    f'AC3 needs >=2 lanes on provider {STAR_PROVIDER}; pool has '
    f'{len(_a_models)}')
PAIR_PROVIDER_A, PAIR_MODEL_A = _a_models[0]
PAIR_PROVIDER_B, PAIR_MODEL_B = _a_models[1]

#: AC4 provider-wide target: the first pool provider (other than the AC3
#: provider) with >=3 eligible lanes. The chain must continue without it.
PROVIDER_UNDER_TEST = None
_seen, _counts = set(), {}
for p, m, _pr in _POOL:
    if p not in _seen:
        _seen.add(p)
    _counts[p] = _counts.get(p, 0) + 1
for p in _seen:
    if p != STAR_PROVIDER and _counts.get(p, 0) >= 3:
        PROVIDER_UNDER_TEST = p
        break
assert PROVIDER_UNDER_TEST, (
    'AC4 needs a second provider with >=3 eligible lanes — pool shape '
    'unexpected')

# ------------------------------------------------------------------ helpers --


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def http_json(method: Any, url: Any, body: Any=None, headers: Any=None, timeout: float=120.0) -> tuple:
    """One HTTP request -> (status:int, payload:dict). Raises on transport error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json',
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b'{}')
        except ValueError:
            payload = {}
        return exc.code, payload


def _api_key():
    """The gateway API key from ~/.hermes/.env (API_SERVER_KEY), never logged.

    proxy_smoke.py's rule, kept verbatim: no key -> refuse to send at all
    rather than burn live provider lanes on guaranteed-401 hops.
    """
    path = os.environ.get('TR300_HERMES_ENV') or os.path.expanduser('~/.hermes/.env')
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith('API_SERVER_KEY='):
                    key = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
                    if key:
                        return key
    except OSError:
        pass
    return None


def load_all_rows(path: Any):
    """The whole live outcome store (tolerant scan; malformed lines warn)."""
    return ro.load_outcome_rows(path)


def band_sig(levels: Any):
    """complexity_sig of the injected band's requirement set — the store and
    the resolver both key stats rows on the canonical sha1."""
    return ro.complexity_sig(levels)


def rebuild_averages() -> int:
    """The production write path for the resolve-time stats view: rebuild the
    rolling averages from the LIVE outcome store (the proxy's own recorder
    and the synthetic injections both land there). Returns the bucket count."""
    avgs = ro.compute_averages(load_all_rows(OUTCOMES_PATH))
    with open(os.environ['ROUTING_AVERAGES_FILE'], 'w') as f:
        for a in avgs:
            f.write(json.dumps(a, ensure_ascii=False) + '\n')
    return len(avgs)


def resolve_chain(requirements: Any, label: str='resolve'):
    """Run the LIVE resolver on the harness state (isolated state dir +
    averages file, exported at import time), scoped to the synthetic backend
    and the default measured sort (explicit, so ambient env cannot skew the
    proof). Returns the full resolve document."""
    import router_spawn
    resolved = router_spawn.resolve(adhoc=requirements, backend=SYN_SOURCE,
                                    sort='predicted_cost_per_task')
    assert isinstance(resolved, dict), f'{label}: resolve() returned a non-dict'
    assert not resolved.get('error'), \
        f'{label}: resolve() failed: {resolved.get("error")}'
    return resolved


def candidate_entries(resolved: Any):
    """The resolver's eligible-only candidate list with each entry's ordering
    basis, from the resolve document's own per-hop outcome notes. The note's
    stats_fallback encodes HOW the lane's stats row was matched (TR-066 R5):
      None              -> matched the task's complexity DIRECTLY (strongest
                           evidence; the dict maps 'complexity' to None on
                           purpose — no fallback kind needed)
      'unconditioned' / 'merged' / 'weighted-fallback' -> weaker store-derived
                           buckets (still outcome evidence)
      'no-samples'      -> the store holds NO sample for this lane: its
                           ordering rests on list price (price-fallback)
    A lane is 'measured' when any of those evidence kinds exists AND the
    bucket carries >= floor samples."""
    entries = []
    for hop in resolved.get('chain') or []:
        oc = hop.get('outcomes') or {}
        fb = oc.get('stats_fallback')
        n = oc.get('n_samples')
        measured = (fb != 'no-samples'
                    and isinstance(n, int) and n >= MEASURED_MIN_SAMPLES)
        entries.append({
            'provider': hop.get('provider'),
            'model': hop.get('model'),
            'hop': hop.get('hop'),
            'usd_1m': hop.get('usd_1m'),
            'stats_fallback': fb,
            'n_samples': n,
            'ordering_basis': 'measured' if measured else 'price-fallback',
        })
    return entries


def head_mode(resolved: Any):
    """The resolve document's own head-mode claim (sort_stats.sufficiency):
    'measured' when the ordering actually applied measurement, 'price' when
    it degraded to price with a reason."""
    return ((resolved.get('sort_stats') or {}).get('sufficiency') or {}).get('effective')


def submit_to_proxy(prompt: Any, nonce: Any, key: Any) -> tuple:
    """One full loop leg: POST the task through the live proxy, capture the
    envelope, wait for the proxy's own outcome row in the live ledger."""
    status, payload = http_json(
        'POST', PROXY_URL + '/v1/chat/completions',
        body={'model': 'auto', 'max_tokens': 200,
              'messages': [{'role': 'user', 'content': prompt}]},
        headers={'Authorization': f'Bearer {key}',
                 'X-Router-Session': f'tr300-{nonce}-{int(time.time())}'},
        timeout=900.0)
    assert isinstance(payload, dict), 'proxy returned a non-object payload'
    router = payload.get('_router') or {}
    assert router, ('proxy response carries no _router envelope; the request '
                    'never reached the proxy ladder: '
                    + json.dumps(payload)[:300])
    sid = router.get('session_id')
    assert sid, f'proxy envelope carries no session_id: {json.dumps(router)[:300]}'
    # Wait for the proxy's own row (fail-open recorder may trail the answer).
    # The recorder wrote the row moments ago, so a bounded TAIL read finds it
    # (the live ledger is ~200MB; a full scan per poll is waste).
    deadline = time.time() + LEDGER_WAIT_S
    row = None
    while row is None:
        for r in reversed(ro.tail_rows(OUTCOMES_PATH, max_lines=5000,
                                       max_bytes=8 * 1024 * 1024)):
            if r.get('source_system') == 'router-proxy' and r.get('session_id') == sid:
                row = r
                break
        if row is not None or time.time() > deadline:
            break
        time.sleep(2.0)
    assert row is not None, (
        f'no router-proxy outcome row for session {sid} appeared in the ledger '
        f'within {LEDGER_WAIT_S:.0f}s')
    return status, payload, router, sid, row


def verify_pass(payload: Any, router: Any, row: Any) -> tuple:
    """AC5: the INDEPENDENT verifier. Pass = the task's acceptance keyword
    appears in the answer OR the answer arrived within the wall budget.
    Never reads the proxy's HTTP status or its recorded success flag."""
    text = ''
    for ch in ((payload.get('choices') or [{}])[0].get('message') or {}).get('content', ''):
        if isinstance(ch, str):
            text += ch
    keyword = 'TR300TESTPROMPT' in text
    wall = row.get('wall_time_s')
    within = isinstance(wall, (int, float)) and wall <= WALL_BUDGET_S
    return bool(keyword or within), {'keyword_hit': keyword, 'wall_time_s': wall,
                                     'wall_budget_s': WALL_BUDGET_S,
                                     'reason': ('acceptance keyword present'
                                                if keyword else
                                                ('wall time within budget'
                                                 if within else
                                                 'neither keyword nor wall budget'))}


def inject_synthetic(provider: Any, model: Any, success: Any, cost_usd: Any, session_id: Any,
                     required_levels: Any, sig: Any, wall_time_s: float=2.5,
                     tokens_in: int=1500, tokens_out: int=300):
    """Append one synthetic outcome row with the documented schema + the
    tr300 marker. source_system stays exactly SYN_SOURCE."""
    row = {'source_system': SYN_SOURCE,
           'session_id': session_id,
           'task_label': 'TR-300 synthetic outcome (harness-injected)',
           'complexity': dict(required_levels),
           'profile_id': None,
           'required_categories': dict(required_levels),
           'complexity_sig': sig,
           'provider': provider, 'model': model,
           'turns': 1, 'steps': 1,
           'tokens_in': tokens_in, 'tokens_out': tokens_out,
           'tokens_reasoning': None,
           'cost_usd': float(cost_usd), 'wall_time_s': float(wall_time_s),
           'success': bool(success),
           'acceptance_status': 'pass' if success else 'fail',
           'acceptance_source': 'tr300-harness',
           'ts': time.time()}
    appended, reason = ro.append_row_fast(OUTCOMES_PATH, row)
    assert appended, f'synthetic outcome append failed: {reason}'
    return row


def cleanup_synthetic():
    """Remove every harness-written synthetic row (source_system SYN_SOURCE).

    Runs under the store's own flock (the same LOCK_EX the proxy's recorder
    holds per append), so a concurrent live append cannot be lost inside the
    read→rewrite window. One full read + one rewrite ONLY when synthetic rows
    exist; a no-op leaves the live file byte-untouched."""
    import fcntl
    lock_fd = None
    if os.path.exists(OUTCOMES_PATH):
        lock_fd = open(OUTCOMES_PATH, 'r+')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        rows = load_all_rows(OUTCOMES_PATH)
        keep = [r for r in rows if r.get('source_system') != SYN_SOURCE]
        removed = len(rows) - len(keep)
        if not removed:
            return 0
        tmp = OUTCOMES_PATH + '.tr300-cleanup.tmp'
        with open(tmp, 'w') as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmp, OUTCOMES_PATH)
        return removed
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


# ------------------------------------------------------- circuit snapshot ----
# AC3/AC4 write the circuit file in the HARNESS state dir (ROUTER_STATE_DIR),
# never the live one. The snapshot/restore still brackets the phases so the
# harness is robust to its own partial failures.


def snapshot_circuit():
    try:
        with open(router_circuit.STATE) as f:
            return f.read()
    except OSError:
        return None


def restore_circuit(snapshot: Any, harness_pairs: Any, harness_providers: Any) -> None:
    """Restore the harness circuit file to the snapshot content, minus any
    entries the harness phases created (belt to the suspenders: the snapshot
    predates them, so this is normally a no-op)."""
    with router_circuit._acquire_lock():
        try:
            base = json.loads(snapshot) if snapshot else {
                'version': 1, 'pairs': {},
                'v2': {'provider_breakers': {}, 'classes': {}}}
        except ValueError:
            base = {'version': 1, 'pairs': {},
                    'v2': {'provider_breakers': {}, 'classes': {}}}
        if not isinstance(base.get('pairs'), dict):
            base['pairs'] = {}
        v2 = base.setdefault('v2', {})
        if not isinstance(v2.get('provider_breakers'), dict):
            v2['provider_breakers'] = {}
        if not isinstance(v2.get('classes'), dict):
            v2['classes'] = {}
        for key in harness_pairs:
            base['pairs'].pop(key, None)
        for prov in harness_providers:
            v2['provider_breakers'].pop(prov, None)
            v2['classes'].pop(prov, None)
        router_circuit.save(base)


# ------------------------------------------------------------------ phases --


def run_loop(label: Any, prompt: Any, nonce: Any, key: Any):
    """One AC1 leg: submit through the live proxy, capture the full trace.

    The candidate list (eligible-only, resolver order, each entry's ordering
    basis from its own per-hop outcome notes) comes from the harness's
    resolve doc — the same resolver machinery the proxy itself consumed for
    this request class. The proxy evidence (ladder, session ids, outcome
    row) is captured verbatim from the live surfaces."""
    # The candidate snapshot is taken BEFORE the submission so the trace
    # names the chain that was available when the task arrived.
    resolved = resolve_chain(ADHOC_REQUIREMENTS, f'{label}-candidates')
    candidates = candidate_entries(resolved)
    status, payload, router, sid, row = submit_to_proxy(prompt, nonce, key)
    verdict, verdict_evidence = verify_pass(payload, router, row)
    reqs = router.get('requirements') or {}
    trace = {
        'label': label,
        'http_status': status,
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'prompt_chars': len(prompt),
        'ratings': {'source': router.get('complexity_source'),
                    'complexity_sig': reqs.get('complexity_sig'),
                    'required_categories': reqs.get('matrix'),
                    'levels': reqs.get('levels'),
                    'band_version': reqs.get('band_version'),
                    'classifier_model': reqs.get('model'),
                    'problems': reqs.get('problems')},
        'candidates': candidates,
        'hermes_session_id': router.get('gateway_session_id'),
        'router_session_id': sid,
        'attempted_hops': router.get('ladder') or [],
        'outcome': {'source_system': row.get('source_system'),
                    'session_id': row.get('session_id'),
                    'provider': row.get('provider'), 'model': row.get('model'),
                    'route_outcome': row.get('route_outcome'),
                    'success': row.get('success'),
                    'tokens_in': row.get('tokens_in'),
                    'tokens_out': row.get('tokens_out'),
                    'cost_usd': row.get('cost_usd'),
                    'wall_time_s': row.get('wall_time_s'),
                    'hops_attempted': row.get('hops_attempted'),
                    'served_by_hop': row.get('served_by_hop'),
                    'chain_length': row.get('chain_length')},
        'independent_verdict': {'pass': verdict, **verdict_evidence},
        'resolved_at': _iso_now(),
    }
    return trace


def phase_ac2_baseline(resolved: Any) -> dict:
    """AC2 first leg: no synthetic samples -> every candidate price-fallback
    and the resolve's own sufficiency block says effective='price'."""
    cands = candidate_entries(resolved)
    assert cands, 'AC2: baseline resolve produced an empty candidate list'
    measured = [c for c in cands if c['ordering_basis'] == 'measured']
    assert not measured, (
        'AC2: baseline (empty synthetic backend) still showed a measured '
        'lane: ' + json.dumps(measured[:3]))
    assert head_mode(resolved) == 'price', (
        'AC2: baseline resolve sufficiency should read effective="price", got '
        + json.dumps((resolved.get('sort_stats') or {}).get('sufficiency'))[:300])
    return {'phase': 'ac2-baseline', 'candidates': cands,
            'head_mode': head_mode(resolved),
            'reason': (resolved.get('sort_stats') or {}).get('sufficiency', {}).get('reason'),
            'resolved_at': resolved.get('resolved_at')}


def phase_ac2_measured(resolved: Any, star: Any) -> dict:
    """AC2 second leg: >= floor verified samples across >= coverage bar lanes
    -> the star lane reads ordering_basis='measured' and the head mode
    flips."""
    cands = candidate_entries(resolved)
    assert cands, 'AC2: measured-phase resolve produced an empty candidate list'
    by_key = {(c['provider'], c['model']): c for c in cands}
    band = by_key.get(star)
    assert band is not None, (
        f'AC2: star lane {star} missing from the post-injection candidates '
        f'({len(cands)} entries)')
    assert band['ordering_basis'] == 'measured', (
        f'AC2: star lane did not become measured: {json.dumps(band)}')
    assert head_mode(resolved) == 'measured', (
        'AC2: post-injection resolve sufficiency should read effective='
        '"measured", got '
        + json.dumps((resolved.get('sort_stats') or {}).get('sufficiency'))[:300])
    return {'phase': 'ac2-measured', 'candidates': cands,
            'head_mode': head_mode(resolved),
            'star_lane': band, 'resolved_at': resolved.get('resolved_at')}


def run_ac3(prov_a: Any, model_a: Any, prov_b: Any, model_b: Any, requirements: Any) -> dict[str, Any] | set[Any]:
    """AC3: a SOFT pair-local failure demotes ONLY (prov_a, model_a).

    Uses router_circuit.record_failure with the same class the proxy writes
    for timeout/5xx hop failures — the exact shared-state mechanism, in the
    harness state dir. Before/after resolves are the LIVE resolver reading
    that state."""
    key = f'{prov_a}/{model_a}'
    before = resolve_chain(requirements, 'ac3-before')
    rc = router_circuit.record_failure(prov_a, model_a,
                                       reason='tr300 controlled pair-local '
                                              'overload failure',
                                       fclass=PAIR_CLASS)
    assert rc == 0, f'AC3: record_failure returned {rc}'
    after = resolve_chain(requirements, 'ac3-after')

    def lanes(res: Any, provider: Any) -> set:
        return {h.get('model') for h in (res.get('chain') or [])
                if h.get('provider') == provider}

    before_models = lanes(before, prov_a)
    after_models = lanes(after, prov_a)
    assert model_a in before_models, (
        f'AC3: {key} was not eligible before the controlled failure '
        f'(provider lanes: {sorted(before_models)[:8]})')
    assert model_a not in after_models, (
        f'AC3: {key} still eligible after its pair-local failure')
    assert model_b in after_models, (
        f'AC3: sibling lane {prov_b}/{model_b} was demoted too — the failure '
        f'leaked beyond the pair (provider lanes after: '
        f'{sorted(after_models)[:8]})')
    # the demotion is explained by a circuit-open exclusion naming the pair
    excl = [(e.get('provider'), e.get('model'))
            for e in (after.get('exclusions') or [])
            if any('circuit OPEN' in str(w) for w in (e.get('why') or []))]
    assert (prov_a, model_a) in excl, (
        f'AC3: no circuit-open exclusion for the demoted pair; exclusions: '
        + json.dumps(excl[:6]))
    return {'phase': 'ac3-pair-local',
            'demoted_pair': [prov_a, model_a],
            'sibling_kept': [prov_b, model_b],
            'provider_lanes_before': sorted(before_models),
            'provider_lanes_after': sorted(after_models),
            'exclusion_evidence': excl[:10],
            'chain_length_before': len(before.get('chain') or []),
            'chain_length_after': len(after.get('chain') or [])}


def run_ac4(provider: Any, requirements: Any) -> dict[str, Any] | set[Any]:
    """AC4: a HARD provider-wide failure (--provider-hard out_of_credit, the
    same record the proxy writes for a live 402) removes ALL lanes of that
    provider; the resolve continues on the next provider."""
    before = resolve_chain(requirements, 'ac4-before')
    rc = router_circuit.record_failure(
        provider, 'lane-independent', fclass=PROVIDER_CLASS,
        reason='tr300 controlled provider-wide out_of_credit',
        provider_hard=True)
    assert rc == 0, f'AC4: record_failure returned {rc}'
    after = resolve_chain(requirements, 'ac4-after')

    def provs(res: Any) -> set:
        return {h.get('provider') for h in (res.get('chain') or [])}

    before_p, after_p = provs(before), provs(after)
    assert provider in before_p, (
        f'AC4: provider {provider} had no eligible lanes before the controlled '
        f'failure (before providers: {sorted(before_p)[:8]})')
    assert provider not in after_p, (
        f'AC4: provider {provider} still present after the provider-wide failure')
    assert after_p, 'AC4: chain empty after the provider-wide failure — ' \
        'Hermes had nowhere to continue'
    # evidence: a provider-level circuit exclusion names the provider
    excl = [(e.get('provider'), e.get('model'), (e.get('why') or []))
            for e in (after.get('exclusions') or [])
            if any('circuit OPEN (provider-level' in str(w) for w in (e.get('why') or []))]
    assert any(p == provider for p, _m, _w in excl), (
        f'AC4: no provider-level circuit exclusion for {provider}; '
        f'exclusions: ' + json.dumps(excl[:6]))
    return {'phase': 'ac4-provider-wide',
            'provider_removed': provider,
            'providers_before': sorted(before_p),
            'providers_after': sorted(after_p),
            'continues_on': sorted(after_p)[:5],
            'exclusion_evidence': [
                {'provider': p, 'model': m, 'why': w[:2]} for p, m, w in excl[:10]]}


def build_artifacts(outdir: Any, doc: Any) -> None:
    os.makedirs(outdir, exist_ok=True)
    for name, blob in doc.items():
        path = os.path.join(outdir, f'tr300-{name}.json')
        with open(path, 'w') as f:
            json.dump(blob, f, indent=2)
        print(f'artifact: {path}')


def main(argv: Any=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--out', default=DEFAULT_ARTIFACT_DIR,
                    help='artifact directory (default data/state/tr300)')
    ap.add_argument('--skip-proxy', action='store_true',
                    help='skip the AC1/AC5 live proxy legs (AC2/AC3/AC4/AC6 only)')
    args = ap.parse_args(argv)

    t_start = time.time()
    key = _api_key()
    if not args.skip_proxy:
        assert key, ('API_SERVER_KEY not found in ~/.hermes/.env — refusing to '
                     'send unauthenticated proxy requests (they 401 at the '
                     'gateway and demote live provider pairs)')

    # ---- harness-owned state ------------------------------------------------
    # The isolated state dir was created + exported BEFORE the imports above
    # (router_spawn bakes MR at import time). It is seeded with the LIVE gate
    # plane (quota policy + health), so the eligibility pool matches
    # production exactly — TR-203's fail-closed policy plane would otherwise
    # demote EVERY provider (policy-gate-missing-row) on an empty state dir.
    # Pure data copies; the circuit file stays harness-owned: AC3/AC4 write
    # it here, then the snapshot/restore returns it to its pre-harness shape.
    state_dir = _HARNESS_STATE_DIR
    router_circuit.STATE = os.path.join(state_dir, 'circuit-state.json')
    for name in ('quota-state.json', 'health-state.json'):
        src = os.path.join(LIVE_STATE_DIR, name)
        dst = os.path.join(state_dir, name)
        try:
            with open(src) as f_in, open(dst, 'w') as f_out:
                f_out.write(f_in.read())
        except OSError as exc:
            raise AssertionError(
                f'harness state seeding failed for {name}: {exc}')

    nonce = f'{int(time.time())}-{os.getpid()}'
    sig = band_sig(INJECTED_LEVELS)
    assert sig, 'injected band requirement set did not produce a complexity_sig'

    doc = {}

    # Sweep any synthetic rows a crashed earlier run left behind, so the
    # cleanup count at the end is exactly this run's own writes.
    stale = cleanup_synthetic()

    # ================= AC2 leg 1: sparse backend -> price fallback ==========
    rebuild_averages()
    resolved = resolve_chain(ADHOC_REQUIREMENTS, 'ac2-baseline')
    doc['ac2-baseline'] = phase_ac2_baseline(resolved)

    # ================= AC6 precondition: the star's price position ==========
    pre_chain = [(h.get('provider'), h.get('model'))
                 for h in (resolved.get('chain') or [])]
    assert STAR_PAIR in pre_chain, \
        f'AC6: star lane {STAR_PAIR} not in the eligible chain'
    pre_pos = pre_chain.index(STAR_PAIR)
    assert pre_pos > 0, (
        'AC6: the star lane already heads the price order — the proof needs '
        'a PAID star (re-derive lanes)')

    # ================= AC1 + AC5: the live proxy loop (x2) ==================
    if not args.skip_proxy:
        prompt = f'{TEST_PROMPT}\n\n[nonce {nonce}]'
        for label in ('baseline', 'replay'):
            trace = run_loop(label, prompt, f'{nonce}-{label}', key)
            doc[f'ac1-{label}'] = trace
            assert trace['prompt_sha256'] == doc['ac1-baseline']['prompt_sha256'], \
                'AC1: identical submissions produced different prompt hashes'
            assert trace['ratings']['complexity_sig'] is not None, \
                'AC1: classifier produced no complexity_sig'
            assert trace['attempted_hops'], 'AC1: proxy recorded no attempted hops'
            assert trace['outcome']['session_id'], 'AC1: outcome row lacks a session id'
        a, b = doc['ac1-baseline'], doc['ac1-replay']
        assert a['ratings']['complexity_sig'] == b['ratings']['complexity_sig'], (
            'AC1: identical prompt produced different complexity_sig across runs: '
            f"{a['ratings']['complexity_sig']} vs {b['ratings']['complexity_sig']}")
        # AC5: the independent verdict exists for BOTH runs and records its
        # basis (the keyword hit or the wall budget), never the proxy's own
        # HTTP status.
        for tr in (a, b):
            assert isinstance(tr['independent_verdict']['pass'], bool), \
                'AC5: independent verdict missing'
        print(f"AC1/AC5: two live runs complete — served_by="
              f"{a['outcome']['provider']}/{a['outcome']['model']}, "
              f"verdict={a['independent_verdict']}")

    # ================= AC2 injection: verified passes for star + fillers ====
    # Star: 3 verified passes at $0/task. Fillers: 3 verified passes each at
    # $1/task — enough lanes to clear the TR-183 coverage bar, never cheap
    # enough to take the head from the star.
    injected = 0
    for i in range(PASSES_PER_LANE):
        inject_synthetic(STAR_PROVIDER, STAR_MODEL, success=True,
                         cost_usd=0.0, session_id=f'tr300-{nonce}-star-{i}',
                         required_levels=INJECTED_LEVELS, sig=sig)
        injected += 1
    for j, (fp, fm) in enumerate(FILLER_PAIRS):
        for i in range(PASSES_PER_LANE):
            inject_synthetic(fp, fm, success=True, cost_usd=1.0,
                             session_id=f'tr300-{nonce}-fill-{j}-{i}',
                             required_levels=INJECTED_LEVELS, sig=sig)
            injected += 1
    assert injected == TOTAL_SYNTH_ROWS, (
        f'injected {injected} rows, planned {TOTAL_SYNTH_ROWS}')

    # ================= AC2 leg 2 + AC6: measured ordering + reorder =========
    rebuild_averages()
    resolved2 = resolve_chain(ADHOC_REQUIREMENTS, 'ac2-measured')
    doc['ac2-measured'] = phase_ac2_measured(resolved2, STAR_PAIR)

    post_chain = [(h.get('provider'), h.get('model'))
                  for h in (resolved2.get('chain') or [])]
    assert STAR_PAIR in post_chain, \
        'AC6: star lane missing from the post-injection chain'
    post_pos = post_chain.index(STAR_PAIR)
    assert post_pos == 0, (
        f'AC6: expected the measured star lane at the HEAD after injection, '
        f'found at position {post_pos + 1} of {len(post_chain)} '
        f'(head: {post_chain[0]})')
    assert post_pos < pre_pos, (
        f'AC6: no reorder — pre position {pre_pos + 1}, '
        f'post position {post_pos + 1}')
    doc['ac6-reorder'] = {
        'phase': 'ac6-reorder',
        'star_lane': list(STAR_PAIR),
        'pre_injection_position': pre_pos + 1,
        'post_injection_position': post_pos + 1,
        'reordered': True,
        'basis': 'TR-174 blended measured cost-per-PASSED-task (TR-299), '
                 f'{PASSES_PER_LANE} verified passes >= floor '
                 f'{MEASURED_MIN_SAMPLES}; coverage bar {_COVERAGE_BAR} '
                 f'cleared with {len(INJECTED_PAIRS)}/{TOTAL_LANES} lanes '
                 'measured',
        'post_chain_head': [post_chain[0][0], post_chain[0][1]]}

    # ================= AC3 + AC4: controlled demotions ======================
    snapshot = snapshot_circuit()
    harness_pair_keys = []
    harness_prov_keys = []
    try:
        doc['ac3'] = run_ac3(PAIR_PROVIDER_A, PAIR_MODEL_A,
                             PAIR_PROVIDER_B, PAIR_MODEL_B,
                             ADHOC_REQUIREMENTS)
        harness_pair_keys = [f'{PAIR_PROVIDER_A}/{PAIR_MODEL_A}']
        harness_prov_keys = [PAIR_PROVIDER_A]
        doc['ac4'] = run_ac4(PROVIDER_UNDER_TEST, ADHOC_REQUIREMENTS)
        harness_prov_keys.append(PROVIDER_UNDER_TEST)
        harness_pair_keys.append(f'{PROVIDER_UNDER_TEST}/lane-independent')
    finally:
        restore_circuit(snapshot, harness_pair_keys, harness_prov_keys)
    # proof the restore happened: the harness state no longer demotes them
    post_restore = resolve_chain(ADHOC_REQUIREMENTS, 'post-restore')
    post_models = {h.get('model') for h in (post_restore.get('chain') or [])
                   if h.get('provider') == PAIR_PROVIDER_A}
    assert PAIR_MODEL_A in post_models, \
        'AC3/AC4 teardown: the pair-local demotion survived the restore'
    assert PROVIDER_UNDER_TEST in {h.get('provider')
                                   for h in (post_restore.get('chain') or [])}, \
        'AC4 teardown: the provider-wide demotion survived the restore'

    removed = cleanup_synthetic()
    doc['cleanup'] = {'phase': 'cleanup',
                      'stale_rows_swept_at_start': stale,
                      'synthetic_rows_removed': removed,
                      'circuit_state_restored': True}
    assert removed == TOTAL_SYNTH_ROWS, (
        f'expected to remove {TOTAL_SYNTH_ROWS} synthetic rows, removed '
        f'{removed}')

    acs = ['AC2', 'AC6', 'AC3', 'AC4']
    if not args.skip_proxy:
        acs = ['AC1', 'AC5'] + acs
    doc['summary'] = {
        'harness': 'tr300_e2e_goal_gate.py',
        'all_acs_proven': True,
        'acs': acs,
        'live_proxy_used': not args.skip_proxy,
        'star_lane': list(STAR_PAIR),
        'injected_lanes': len(INJECTED_PAIRS),
        'wall_s': round(time.time() - t_start, 1),
        'finished_at': _iso_now()}
    build_artifacts(args.out, doc)
    print(json.dumps(doc['summary'], indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

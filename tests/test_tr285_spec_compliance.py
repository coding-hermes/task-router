"""TR-285: spec-compliance tests for docs/complexity-model.md (design authority).

The spec (docs/complexity-model.md, sections 2-5) states rules this suite pins
against the shipped router code. Each test cites its rule id in the name and
docstring. Hermetic throughout: fixture registries in tmp_path, the rolling
averages file pointed at a scratch path (ROUTING_AVERAGES_FILE), no network,
no live services, no ambient ~/.hermes state.

  R2.1  profile_id never overrides precedence sources 1 (declared-raw) or 2
        (prompt classification). A profile may only apply as policy.
  R2.2  a missing input must never silently become the cheapest lane: the
        floor path carries degrade_reason, and the resolve records it.
  R3.1  the band key (complexity_sig) is a canonical function of the
        requirement matrix, stable across runs, and derived by the SAME
        function on the write side (averages) and the read side (resolve).
  R4.3  every hop carries its basis: measured (with n_samples, window,
        success rate) or an explicitly named price fallback. Unmeasured is
        never reported as measured-cheap.
  R5.7  a field that cannot be measured is null with a stated reason, never
        a fake zero; a genuine measured zero stays distinct from no data.
        (Data-side: the reason vocabulary itself is pinned in
        tests/test_null_census.py AC2; the staleness/basis shapes follow
        tests/test_tr283_staleness.py.)
"""
import hashlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_spawn as rs      # noqa: E402

NOW = 1_800_000_000.0
MATRIX = {'code_gen': 4, 'debug': 3}
SIG = ro.complexity_sig(MATRIX)


# ---------------------------------------------------------------------------
# fixtures: the same hermetic shape as test_row_raw_levels.py, with
# per-category tiers chosen so the DECLARED levels and the PROFILE levels
# resolve to DIFFERENT chains (that difference is what makes R2.1 testable).
# ---------------------------------------------------------------------------

def _fixture_registry(tmp_path):
    """3 models / 2 providers. a1: tier 5 everywhere, a $0 promo lane.
    a2: tier 3 code_gen (cannot clear level 4), plan-priced. b1: tier 1,
    list-priced. Profile P0_FORE requires agent_tick=1, debug=1 -> all three
    eligible; code_gen=4 debug=3 -> only a1."""
    cats = ('agent_tick', 'debug', 'code_gen')
    tiers = {'a1': 5, 'a2': 3, 'b1': 1, 'u1': 5}
    prices = {'a1': (0.0, 0.0),     # (normalized, public): promo
              'a2': (2.0, 5.0),     # plan-effective
              'b1': (3.0, 3.0),     # list
              'u1': (None, None)}   # declares NO price (R5.7 pin)
    reg = {"version": 3, "generated_at": "fixture",
           "tables": {"models": [], "model_tier": [], "task_profiles": [],
                      "task_profile_requirements": [], "projects": [],
                      "category_levels": [], "level_defs": []}}
    for model in ('a1', 'a2', 'b1', 'u1'):
        reg["tables"]["models"].append({
            "provider": 'prov-b' if model == 'b1' else 'prov-a',
            "model": model, "normalized_price": prices[model][0],
            "public_price": prices[model][1],
            "token_factor": 1.0, "plan_tier": 0, "data_class": "zdr",
            "valid_to": None, "archive": False})
        for cat in cats:
            reg["tables"]["model_tier"].append(
                {"model": model, "category": cat, "tier": tiers[model]})
    for cat in cats:
        for lvl in range(-5, 6):
            reg["tables"]["category_levels"].append({"category": cat, "level": lvl})
        reg["tables"]["level_defs"].extend({"level": lvl} for lvl in range(-5, 6))
    reg["tables"]["task_profiles"].append({
        "id": 'P0_FORE', "title": "fixture", "created_at": None,
        "max_consecutive_per_provider": None, "max_total_per_provider": None})
    for cat in ('agent_tick', 'debug'):
        reg["tables"]["task_profile_requirements"].append(
            {"task_id": 'P0_FORE', "category": cat, "level": 1})
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(reg))
    return str(path)


def _open_state(tmp_path):
    """Every provider open (the resolver's policy plane is fail-closed on a
    missing quota-state row)."""
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "quota-state.json").write_text(json.dumps(
        {"updated": "test",
         "providers": {"prov-a": {"status": "open"},
                       "prov-b": {"status": "open"}}}))
    (d / "health-state.json").write_text(json.dumps(
        {"providers": {}}))
    (d / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    return str(d)


def _write_board(tmp_path, rows):
    p = tmp_path / '.coding-hermes' / 'board'
    p.mkdir(parents=True)
    f = p / 'tasks.jsonl'
    f.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(f)


@pytest.fixture()
def router_env(tmp_path, monkeypatch):
    """Fixture registry + open gates + scratch averages path (never the
    ambient rolling stats)."""
    monkeypatch.setattr(rs, 'REGISTRY', _fixture_registry(tmp_path))
    monkeypatch.setattr(rs, 'MR', _open_state(tmp_path))
    monkeypatch.setenv('ROUTING_AVERAGES_FILE', str(tmp_path / 'no-averages.jsonl'))


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py'] + argv)
    rs.main()
    return json.loads(capsys.readouterr().out)


def _chain_models(out):
    return [h['model'] for h in out.get('chain', [])]


# ---------------------------------------------------------------------------
# R2.1 — profile_id never overrides sources 1 (declared-raw) or 2 (classifier)
# ---------------------------------------------------------------------------

def test_r2_1_declared_raw_levels_beat_the_rows_profile_id(monkeypatch, capsys,
                                                           router_env, tmp_path):
    """R2.1: a board row carrying BOTH raw levels and a profile id resolves
    from the LEVELS. The declared profile would admit all three models
    (agent_tick=1, debug=1); the raw levels admit only a1 — and a1 is what
    the chain serves, so the profile_id did not override source 1."""
    board = _write_board(tmp_path, [
        {'id': 'TR-9001', 'title': 'raw levels row',
         'required_categories': MATRIX, 'profile': 'P0_FORE'}])
    out = _run_main(monkeypatch, capsys,
                    ['--profile-from-board', 'TR-9001', '--board', board,
                     '--profile', 'P0_FORE', '--no-health', '--format', 'json'])
    assert out['complexity']['source'] == 'declared-raw'
    assert out['complexity']['matrix'] == MATRIX
    assert out['complexity']['degraded'] is False
    assert out['resolved_as'] == 'adhoc', 'the raw levels ARE the requirements'
    assert _chain_models(out) == ['a1']
    # the counterfactual: the profile's own levels produce a DIFFERENT chain
    prof = _run_main(monkeypatch, capsys,
                     ['--profile', 'P0_FORE', '--no-health', '--format', 'json'])
    assert _chain_models(prof) == ['a1', 'a2', 'b1']
    assert _chain_models(out) != _chain_models(prof)


def test_r2_1_a_classifier_result_beats_the_profile_argument(monkeypatch, capsys,
                                                             router_env):
    """R2.1: prompt classification (source 2) replaces the --profile levels.
    The classifier's code_gen=4 admits only a1; the profile argument would
    have admitted all three."""
    monkeypatch.setattr(
        rs, 'complexity_requirements',
        lambda text, scorer='auto': (['code_gen=4'],
                                     {'source': 'classifier', 'degraded': False,
                                      'matrix': {'code_gen': 4},
                                      'adhoc': ['code_gen=4'],
                                      'problems': [], 'confidence': 0.9}))
    out = _run_main(monkeypatch, capsys,
                    ['--prompt', 'a hard coding task', '--profile', 'P0_FORE',
                     '--no-health', '--format', 'json'])
    assert out['complexity']['source'] == 'classifier'
    assert out['resolved_as'] == 'adhoc'
    assert _chain_models(out) == ['a1'], 'classifier levels held, not P0_FORE'


def test_r2_1_caller_cli_levels_beat_the_profile_argument(monkeypatch, capsys,
                                                          router_env):
    """R2.1: the caller's explicit raw levels (--profile-req) are the
    requirement list; naming a profile alongside them cannot loosen the
    chain. code_gen=5 excludes a2 (tier 3) and b1 (tier 1) even though the
    named P0_FORE profile would admit them."""
    out = _run_main(monkeypatch, capsys,
                    ['--profile-req', 'code_gen=5', '--profile', 'P0_FORE',
                     '--no-health', '--format', 'json'])
    assert out['resolved_as'] == 'adhoc'
    assert (out.get('complexity') or {}).get('source') != 'declared-raw'
    assert _chain_models(out) == ['a1']


def test_r2_1_the_profile_id_alone_cannot_admit_an_ineligible_lane(router_env):
    """R2.1 policy half: inside resolve(), an adhoc level list clears the
    PROFILE slot entirely (pid=None) — the profile_id is never merged into
    the requirements, so a lane below the required tier stays out no matter
    which profile is named."""
    out = rs.resolve(profile_id='P0_FORE', adhoc=['code_gen=5'],
                     use_health=False)
    assert out['resolved_as'] == 'adhoc'
    assert _chain_models(out) == ['a1']


# ---------------------------------------------------------------------------
# R2.2 — a missing input degrades with a named reason, never a silent cheapest
# ---------------------------------------------------------------------------

def test_r2_2_empty_task_text_degrades_with_a_named_reason():
    """R2.2: empty task text is a MISSING input: the floor path returns no
    matrix and carries degrade_reason naming the failure. The scorer is
    never called (nothing is invented from nothing)."""
    called = []
    adhoc, meta = rs.complexity_requirements(
        '   ', classify_fn=lambda t: called.append(t))
    assert adhoc is None
    assert meta['degraded'] is True
    assert meta['degrade_reason'] == 'empty task text'
    assert called == []


def test_r2_2_no_scorer_available_degrades_with_a_named_reason(monkeypatch):
    """R2.2: with every scorer unavailable the floor is reached and the
    reason is stated ('no scorer available'), not silently swallowed into a
    default chain."""
    monkeypatch.setitem(sys.modules, 'router_classify', None)
    monkeypatch.setitem(sys.modules, 'router_jev', None)
    adhoc, meta = rs.complexity_requirements('a real task', scorer='auto')
    assert adhoc is None
    assert meta['degraded'] is True
    assert meta['degrade_reason'] == 'no scorer available'
    assert meta['problems'], 'the import failures are recorded too'


def test_r2_2_a_degraded_resolve_records_the_reason_and_never_claims_levels(
        monkeypatch, capsys, router_env):
    """R2.2 end to end, on the REAL path (no stub): with every scorer
    unavailable the resolve still returns a chain (fail-open), the row
    RECORDS the degrade reason naming the failure, and it does NOT claim
    classified levels (resolved_as stays off 'adhoc') — the fallback to the
    profile is named, never silent."""
    monkeypatch.setitem(sys.modules, 'router_classify', None)
    monkeypatch.setitem(sys.modules, 'router_jev', None)
    out = _run_main(monkeypatch, capsys,
                    ['--prompt', 'a real task', '--profile', 'P0_FORE',
                     '--no-health', '--format', 'json'])
    assert out['chain'], 'fail-open: the caller still gets a chain'
    assert out['complexity']['degraded'] is True
    assert out['complexity']['degrade_reason'] == 'no scorer available'
    assert out['resolved_as'] != 'adhoc', 'the row must not claim classified levels'


# ---------------------------------------------------------------------------
# R3.1 — the band key: canonical, stable, one function on write and read
# ---------------------------------------------------------------------------

def test_r3_1_the_sig_is_the_documented_canonical_sha1():
    """R3.1: complexity_sig is sha1 over the canonical JSON of the
    {category: level} map — dict-order independent, level-sensitive, and
    reproducible from the documented definition alone (re-derived here with
    hashlib, not by calling the router)."""
    canon = json.dumps({'code_gen': 4, 'debug': 3},
                       separators=(',', ':'), sort_keys=True)
    assert SIG == hashlib.sha1(canon.encode()).hexdigest()
    assert ro.complexity_sig({'debug': 3, 'code_gen': 4}) == SIG
    assert ro.complexity_sig({'code_gen': 4.0, 'debug': 3}) == SIG  # int canonical
    assert ro.complexity_sig(MATRIX) == SIG          # stable across runs
    assert ro.complexity_sig({'code_gen': 3, 'debug': 3}) != SIG
    assert ro.complexity_sig({'code_gen': 4}) != SIG
    assert ro.complexity_sig({}) is None and ro.complexity_sig(None) is None


def test_r3_1_same_derivation_on_write_and_read():
    """R3.1 round trip: the WRITE side buckets an outcome under
    complexity_sig(matrix) and stores the canonical map; the READ side
    derives its reference from the same matrix (the profile-requirement
    channel) and matches the bucket — one derivation, two surfaces."""
    row = {'ts': NOW, 'source_system': 'router-proxy', 'provider': 'p',
           'model': 'm', 'complexity_sig': SIG, 'required_categories': MATRIX,
           'tokens_in': 1000, 'tokens_out': 100, 'cost_usd': 0.5, 'success': True}
    avg = ro.compute_averages([row], scales_h=[24], now_s=NOW)
    assert avg[0]['complexity_sig'] == SIG
    assert avg[0]['required_categories'] == MATRIX
    index = {('p', 'm'): avg}
    tables = {'task_profile_requirements': [
        {'task_id': 'P0_FORE', 'category': 'code_gen', 'level': 4},
        {'task_id': 'P0_FORE', 'category': 'debug', 'level': 3}]}
    keys = rs._complexity_keys('P0_FORE', tables)
    matched, how = rs.lane_stats(index, 'p', 'm', keys)
    assert how == 'complexity' and matched['complexity_sig'] == SIG
    value, prov = rs.lane_metric({'provider': 'p', 'model': 'm'},
                                 {'index': index, 'keys': keys, 'window_h': 24},
                                 'cost')
    assert value == 0.5 and prov['match'] == 'complexity'


def test_r3_1_different_levels_never_share_a_band():
    """R3.1: the band boundary is real. A row written under a DIFFERENT level
    map is not matched by this task's band reference (no cross-band
    contamination), and the readable band form is deterministic and
    level-sensitive too."""
    other = {'code_gen': 3}
    avg = ro.compute_averages(
        [{'ts': NOW, 'source_system': 'router-proxy', 'provider': 'p',
          'model': 'm', 'complexity_sig': ro.complexity_sig(other),
          'required_categories': other, 'cost_usd': 0.5, 'success': True}],
        scales_h=[24], now_s=NOW)
    keys = rs._complexity_keys('P0_FORE', {'task_profile_requirements': [
        {'task_id': 'P0_FORE', 'category': 'code_gen', 'level': 4},
        {'task_id': 'P0_FORE', 'category': 'debug', 'level': 3}]})
    _row, how = rs.lane_stats({('p', 'm'): avg}, 'p', 'm', keys)
    assert how != 'complexity', 'a different level map must not match this band'
    # readable form (shown in 'why this lane'): stable + level-sensitive
    assert ro.band_key(MATRIX) == 'b1:hard:code_gen+debug'
    assert ro.band_key(MATRIX) == ro.band_key(dict(reversed(list(MATRIX.items()))))
    assert ro.band_key({'code_gen': 2, 'debug': 3}) != ro.band_key(MATRIX)


# ---------------------------------------------------------------------------
# R4.3 — every hop carries its basis
# ---------------------------------------------------------------------------

def _stats_index(*triples):
    """,(provider, model, n_samples, avg_cost, success_rate) -> lane_stats index."""
    index = {}
    for prov, model, n, cost, rate in triples:
        index[(prov, model)] = [{'n_samples': n, 'avg_cost_task_24h': cost,
                                 'success_rate': rate, 'complexity': None}]
    return index


LANES = [{'provider': 'p', 'model': 'm', 'normalized_price': 0.10},
         {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}]


def test_r4_3_a_measured_head_carries_n_samples_window_and_success_rate():
    """R4.3: when the head wins on measurement its record names the basis and
    the measured triple — n_samples, window, success rate — and the hop's
    outcome note exposes the same numbers. Nothing is 'measured-cheap'
    without them."""
    ctx = {'index': _stats_index(('p', 'm', 9, 0.50, 0.9),
                                 ('q', 'n', 9, 0.02, 1.0)), 'window_h': 24}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    basis = ctx['_sort_basis']
    assert basis['effective'] == 'measured'
    assert basis['floor_samples'] == rs.MEASURED_MIN_SAMPLES
    assert basis['coverage'] == 1.0
    win = basis['win_reason']
    assert win['basis'].startswith('measured'), win
    assert win['samples'] == 9
    assert win['expected_cost'] is not None
    head_note = rs.outcome_note(LANES[1], ctx)   # q/n: 0.02 beats 0.50/0.9
    assert head_note['n_samples'] == 9
    assert head_note['window_h'] == 24
    assert head_note['success_rate'] == 1.0
    assert head_note['predicted_cost_per_task'] == 0.02
    assert head_note['selection']['basis'] == win['basis']
    assert rs.outcome_note(LANES[0], ctx).get('selection') is None


def test_r4_3_thin_lanes_fall_back_to_price_and_name_it():
    """R4.3: below the sample floor a lane returns (None, basis) — 'below
    floor' and 'no sample' are named, never folded into a fake measured
    value — the ordering says it fell back to price, and the head claims
    nothing (no fabricated win record)."""
    ctx = {'index': _stats_index(('p', 'm', 2, 0.0001, None)), 'window_h': 24}
    value, b = rs.measured_basis(LANES[0], ctx, None)
    assert value is None and b['basis'] == 'below-floor'
    assert b['n_samples'] == 2 and b['floor_samples'] == 3
    assert b['window_h'] == 24
    value2, b2 = rs.measured_basis(LANES[1], ctx, None)
    assert value2 is None and b2['basis'] == 'no-sample' and b2['n_samples'] is None
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    basis = ctx['_sort_basis']
    assert basis['effective'] == 'price'
    assert basis['fell_back_to_price'] == 2
    assert basis['reason'] == 'below-coverage-floor'
    assert 'win_reason' not in basis, 'a price-fallback head claims nothing'
    assert key(LANES[1])[0] == 1 and key(LANES[0])[0] == 1  # fallback bucket


def test_r4_3_every_priced_hop_states_its_price_basis(monkeypatch, capsys,
                                                      router_env):
    """R4.3: where measurement is thin the chain ranks on price — and each
    hop then STATES why it sits where it sits: free-by-promo,
    plan-effective (with the list price), or plain list."""
    out = _run_main(monkeypatch, capsys,
                    ['--profile-req', 'agent_tick=1', 'debug=1',
                     '--no-health', '--format', 'json'])
    assert _chain_models(out) == ['a1', 'a2', 'b1']
    bases = {h['model']: h['price_basis'] for h in out['chain']}
    assert bases['a1'] == 'free-by-promo'
    assert bases['a2'] == 'plan-effective (list 5/M)'
    assert bases['b1'] == 'list'


# ---------------------------------------------------------------------------
# R5.7 — unmeasurable is null with a reason; never a fake zero
# ---------------------------------------------------------------------------

def test_r5_7_unmeasured_bucket_metrics_are_null_not_zero():
    """R5.7: an averages bucket whose samples never carried a metric reports
    None for it — never 0, which would read as 'measured at zero'."""
    rows = [{'ts': NOW, 'source_system': 'router-proxy', 'provider': 'p',
             'model': 'm', 'complexity_sig': SIG, 'cost_usd': 0.4,
             'success': None}]
    entry = ro.compute_averages(rows, scales_h=[24], now_s=NOW)[0]
    assert entry['avg_wall_time_24h'] is None
    assert entry['avg_turns_24h'] is None
    assert entry['avg_tokens_in_24h'] is None
    assert entry['avg_tokens_out_24h'] is None
    assert entry['avg_tokens_total_24h'] is None
    assert entry['success_rate'] is None
    assert entry['n_success_known'] == 0
    # a bucket with no cost samples at all: the average is None, not 0.0
    no_cost = ro.compute_averages(
        [{'ts': NOW, 'source_system': 'x', 'provider': 'p', 'model': 'm',
          'complexity_sig': SIG, 'success': True}],
        scales_h=[24], now_s=NOW)[0]
    assert no_cost['avg_cost_task_24h'] is None


def test_r5_7_genuine_zero_stays_distinct_from_no_measurement():
    """R5.7 core distinction: a lane with a REAL 0.0 measured cost keeps
    value 0.0 with basis 'measured'; a lane with no sample gets (None,
    'no-sample'). A fake zero would collapse the two. Absent completion data
    is likewise reported ('no-completion-term'), never guessed as 1.0."""
    ctx = {'index': _stats_index(('p', 'm', 3, 0.0, None)), 'window_h': 24}
    value, b = rs.measured_basis(LANES[0], ctx, None)
    assert value == 0.0 and b['basis'] == 'measured'
    ctx_empty = {'index': {}, 'window_h': 24}
    value2, prov = rs.lane_metric(LANES[1], ctx_empty, 'cost')
    assert value2 is None and prov is None
    _v, b3 = rs.measured_basis(LANES[1], ctx_empty, None)
    assert b3['basis'] == 'no-sample'
    rate, why = rs.completion_term({'n_samples': 3, 'avg_cost_task_24h': 0.1})
    assert rate is None and why == 'no-completion-term'


def test_r5_7_an_unknown_price_never_reaches_a_hop(router_env):
    """R5.7: a lane that declares NO price must not appear in the chain
    dressed as free. Two layers pin it: (a) eligibility — _build_chain
    filters unpriced models BEFORE ordering, so the hop row (and its
    price_basis) simply never exists for them; (b) the sort key — an
    unknown price sinks to its own bucket, never tying with the genuine
    $0.0 promo lane at the head. If either layer regressed to a fake zero,
    u1 would surface free at the head of the chain."""
    out = rs.resolve(profile_id='P0_FORE', use_health=False)
    assert _chain_models(out) == ['a1', 'a2', 'b1'], 'unpriced u1 stays out'
    assert all(h['model'] != 'u1' for h in out['chain'])
    assert all(h.get('price') is not None for h in out['chain'])
    # the sort layer: unknown sinks, never the cheapest
    unpriced = {'provider': 'prov-a', 'model': 'u1'}
    free = {'provider': 'prov-a', 'model': 'a1', 'normalized_price': 0.0,
            'plan_tier': 0}
    assert rs._legacy_sort_key(unpriced) > rs._legacy_sort_key(free)
    assert rs._legacy_sort_key(unpriced)[2] == 1  # the priced/unknown separator


def test_r5_7_every_live_unpriced_lane_carries_a_stated_reason():
    """R5.7 data side: where the registry genuinely cannot measure a price,
    the null is stamped with a stated reason (finite vocabulary, pinned in
    test_null_census.py AC2) — the field stays null, the reason travels with
    it. Never an invented zero in the data either."""
    tables = os.path.join(REPO, 'data', 'tables')
    models = []
    with open(os.path.join(tables, 'models.jsonl')) as fh:
        for line in fh:
            if line.strip():
                models.append(json.loads(line))
    stamps = set()
    stamps_path = os.path.join(REPO, 'data', 'null_reasons.jsonl')
    with open(stamps_path) as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            assert r.get('reason'), 'a stamp without a reason is not a stamp'
            if r.get('field') == 'normalized_price':
                stamps.add((r.get('provider'), r.get('model')))
    for m in models:
        live = not m.get('archive') and not m.get('disabled') and not m.get('valid_to')
        if live and m.get('normalized_price') is None:
            assert (m.get('provider'), m.get('model')) in stamps, (
                'live unpriced lane without a stated reason: '
                f'{m.get("provider")}/{m.get("model")}')

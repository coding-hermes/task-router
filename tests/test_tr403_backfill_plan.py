"""TR-403 — the effort backfill plan.

The row has three acceptance criteria and each one is a property this suite
pins, on a synthetic registry so the assertions are about the RULE and not
about today's data:

1. the plan NAMES (model, effort, category) cells per day          -> test_order_*,
                                                                     test_per_day_*
2. no cell is done on estimate-only data                          -> test_done_*
3. the usability ratchet decreases as cells land                  -> test_ratchet_*,
                                                                     test_check_*,
                                                                     test_committed_baseline_*

`test_committed_baseline_is_not_stale` runs against the REAL tables and is the
permanent AC3 wire: it fails the moment the defect count drops below the
committed baseline without the ratchet being tightened.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
BASELINE = ROOT / 'data' / 'state' / 'usability-baseline.json'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS))
import backfill_plan as bp  # noqa: E402  (after sys.path surgery)

# ---------------------------------------------------------------------------
# a synthetic registry: two live models with traffic, one defect lane, one
# retired lane, and every evidence source the classifier knows about
# ---------------------------------------------------------------------------

def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r) + '\n')


@pytest.fixture()
def registry(tmp_path):
    data = tmp_path / 'tables'
    _write(data / 'models.jsonl', [
        {'provider': 'p1', 'model': 'm-busy', 'normalized_price': 1.0,
         'disabled': None, 'archive': False, 'valid_to': None},
        {'provider': 'p1', 'model': 'm-quiet', 'normalized_price': 5.0,
         'disabled': None, 'archive': False, 'valid_to': None},
        {'provider': 'p1', 'model': 'm-defect', 'normalized_price': 2.0,
         'disabled': None, 'archive': False, 'valid_to': None},
        {'provider': 'p1', 'model': 'm-retired', 'normalized_price': 1.0,
         'disabled': True, 'archive': False, 'valid_to': None},
        {'provider': 'p2', 'model': 'm-nodecl', 'normalized_price': 1.0,
         'disabled': None, 'archive': False, 'valid_to': None},
    ])
    _write(data / 'model_tier.jsonl', [
        {'model': 'm-busy', 'category': 'agent_tick', 'tier': 2,
         'tier_source': 'measured', 'reasoning_effort': 'default'},
        {'model': 'm-quiet', 'category': 'agent_tick', 'tier': 1,
         'tier_source': 'estimate', 'reasoning_effort': 'default'},
    ])
    _write(data / 'model_perf.jsonl', [
        # the DEFAULT pointer: measured once
        {'model': 'm-busy', 'category': 'agent_tick', 'perf': 0.8,
         'source': 'measured', 'reasoning_effort': 'default'},
        # ranked efforts carrying every NON-measured source
        {'model': 'm-busy', 'category': 'agent_tick', 'perf': 0.6,
         'source': 'estimate', 'reasoning_effort': 'high'},
        {'model': 'm-busy', 'category': 'agent_tick', 'perf': 0.55,
         'source': 'bench', 'reasoning_effort': 'low'},
        {'model': 'm-busy', 'category': 'agent_tick', 'perf': 0.5,
         'source': 'family', 'reasoning_effort': 'max'},
        # exactly one genuinely measured RANKED cell
        {'model': 'm-busy', 'category': 'terminal', 'perf': 0.9,
         'source': 'measured', 'reasoning_effort': 'high'},
    ])
    _write(data / 'model_aliases.jsonl', [])
    _write(data / 'model_catalog.jsonl', [
        {'model': 'm-busy', 'provider': 'p1', 'reasoning': True},
        {'model': 'm-quiet', 'provider': 'p1', 'reasoning': True},
        {'model': 'm-defect', 'provider': 'p1', 'reasoning': True},
        {'model': 'm-retired', 'provider': 'p1', 'reasoning': True},
        # m-nodecl deliberately has NO catalog row
    ])
    outcomes = tmp_path / 'outcomes.jsonl'
    rows = []
    for i in range(100):
        rows.append({'model': 'm-busy', 'provider': 'p1',
                     'required_categories': {'agent_tick': 2}, 'ts': i})
    for i in range(50):
        rows.append({'model': 'm-quiet', 'provider': 'p1',
                     'required_categories': {'terminal': 1}, 'ts': i})
    for i in range(5):
        rows.append({'model': 'm-defect', 'provider': 'p1',
                     'required_categories': {'review': 1}, 'ts': i})
    for i in range(20):
        rows.append({'model': 'm-retired', 'provider': 'p1', 'ts': i})
    _write(outcomes, rows)
    return {'data': data, 'outcomes': outcomes}


@pytest.fixture()
def stubbed_gate(monkeypatch):
    """A deterministic usability gate: one defect lane on m-defect."""
    class _Gate:
        @staticmethod
        def classify():
            return ([], [], [{'provider': 'p1', 'model': 'm-defect',
                              'why': 'never_measured'}],
                    {'never_measured': 1})
    monkeypatch.setitem(sys.modules, 'router_usability_gate', _Gate)
    return _Gate


def _plan(registry, monkeypatch, top=20, per_day=40, baseline=None):
    monkeypatch.setattr(bp, 'DATA_DIR', str(registry['data']))
    if baseline is None:
        monkeypatch.setattr(bp, 'BASELINE', '/nonexistent/baseline.json')
    else:
        p = registry['data'].parent / 'baseline.json'
        p.write_text(json.dumps({'defects': baseline}))
        monkeypatch.setattr(bp, 'BASELINE', str(p))
    return bp.build_plan(top=top, per_day=per_day,
                         traffic_file=str(registry['outcomes']))


def _cell(plan, model, effort, category):
    for c in plan['cells']:
        if (c['model'], c['effort'], c['category']) == (model, effort, category):
            return c
    raise AssertionError(f'no cell {model}@{effort}:{category}')


# ---------------------------------------------------------------------------
# AC2 — no cell is done on estimate-only data
# ---------------------------------------------------------------------------

def test_done_requires_measured_evidence(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    assert _cell(plan, 'm-busy', 'high', 'agent_tick')['state'] == 'estimate_only'
    assert _cell(plan, 'm-busy', 'low', 'agent_tick')['state'] == 'bench_only'
    assert _cell(plan, 'm-busy', 'max', 'agent_tick')['state'] == 'family_only'
    measured = _cell(plan, 'm-busy', 'high', 'terminal')
    assert measured['state'] == 'measured' and measured['done'] is True
    for c in plan['cells']:
        assert c['done'] is (c['state'] == 'measured'), c


def test_a_measured_sibling_does_not_close_a_cell(registry, monkeypatch, stubbed_gate):
    """Done is per CELL, never per model or per category."""
    plan = _plan(registry, monkeypatch)
    assert _cell(plan, 'm-busy', 'high', 'terminal')['done'] is True
    assert _cell(plan, 'm-busy', 'max', 'terminal')['done'] is False
    assert _cell(plan, 'm-busy', 'high', 'agent_tick')['done'] is False


def test_cell_state_labels_every_source():
    assert bp.cell_state(None) == ('missing', False)
    assert bp.cell_state('measured') == ('measured', True)
    for src, state in (('bench', 'bench_only'), ('family', 'family_only'),
                       ('estimate', 'estimate_only')):
        assert bp.cell_state(src) == (state, False)
    assert bp.cell_state('mystery') == ('other:mystery', False)


# ---------------------------------------------------------------------------
# AC1 — the plan NAMES (model, effort, category) cells per day
# ---------------------------------------------------------------------------

def test_every_planned_cell_names_all_three_keys(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    assert plan['cells'], 'the fixture must produce cells'
    for c in plan['todo']:
        assert c['model'] and c['effort'] and c['category'], c
        assert c['day'] >= 1
    for day in plan['days']:
        assert day['named'], f'day {day["day"]} names no cells'


def test_no_ranked_cell_uses_the_default_pointer(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    assert all(c['effort'] != 'default' for c in plan['cells'])
    assert all(c['effort'] in bp.REASONING_EFFORTS for c in plan['cells'])


def test_per_day_budget_and_contiguous_days(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch, per_day=7)
    for day in plan['days']:
        assert day['cells'] <= 7, day
    assert [d['day'] for d in plan['days']] == list(range(1, len(plan['days']) + 1))


def test_order_is_category_traffic_then_model_traffic_then_price(registry,
                                                                 monkeypatch,
                                                                 stubbed_gate):
    plan = _plan(registry, monkeypatch, per_day=1000)
    todo = plan['todo']
    effort = [c for c in todo if c['arm'] == 'effort']
    cats = [c['category'] for c in effort]
    # one category at a time: a category is never re-entered once left
    seen = []
    for cat in cats:
        if not seen or seen[-1] != cat:
            assert cat not in seen, f'category {cat} re-entered — not one at a time'
            seen.append(cat)
    # agent_tick has the most traffic (100 rows) so it must be the first pass
    assert seen[0] == 'agent_tick', seen[:3]
    # within the first category: m-busy (100 rows) before m-quiet (50 rows)
    first_models = [c['model'] for c in effort if c['category'] == 'agent_tick']
    assert first_models[0] == 'm-busy'
    assert first_models.index('m-busy') < first_models.index('m-quiet')


def test_efforts_follow_rank_order_within_a_model(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch, per_day=1000)
    busy = [c['effort'] for c in plan['todo']
            if c['arm'] == 'effort' and c['category'] == 'agent_tick'
            and c['model'] == 'm-busy']
    ranks = [bp.EFFORT_RANK[e] for e in busy]
    assert ranks == sorted(ranks), busy


# ---------------------------------------------------------------------------
# effort support: never invented
# ---------------------------------------------------------------------------

def test_reasoning_false_plans_only_off(registry, monkeypatch, stubbed_gate):
    cat = registry['data'] / 'model_catalog.jsonl'
    rows = [json.loads(x) for x in cat.read_text().splitlines()]
    rows.append({'model': 'm-quiet', 'provider': 'p1', 'reasoning': False})
    rows = [r for r in rows if r['model'] != 'm-quiet' or r['provider'] != 'p1'
            or r.get('reasoning') is False]
    _write(cat, rows)
    plan = _plan(registry, monkeypatch)
    efforts = {s['model']: s['efforts'] for s in plan['models']}
    assert efforts['m-quiet'] == ['off'], efforts['m-quiet']


def test_unknown_support_is_named_not_planned(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    # m-nodecl: no catalog row, no tier/unstamped evidence -> no ranked cells
    assert 'm-nodecl' not in {c['model'] for c in plan['cells']}


def test_evidence_beats_the_catalog_declaration():
    support, basis, _note = bp.effort_support(
        'm', {'high'}, set(), {'m': True})
    assert support == ['high'] and basis == 'evidence'
    support, basis, _ = bp.effort_support('m', set(), {'low'}, {'m': True})
    assert support == ['low'] and basis == 'evidence'
    support, basis, _ = bp.effort_support('m', set(), set(), {'m': True})
    assert support == list(bp.RANKED_EFFORTS) and basis == 'candidate'
    support, basis, _ = bp.effort_support('m', set(), set(), {'m': False})
    assert support == ['off'] and basis == 'catalog:reasoning=false'
    support, basis, _ = bp.effort_support('m', set(), set(), {})
    assert support == [] and basis == 'unknown'


# ---------------------------------------------------------------------------
# gaps: traffic that cannot be planned is named, never dropped
# ---------------------------------------------------------------------------

def test_retired_lane_traffic_is_a_named_gap(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    gaps = {g['model']: g for g in plan['gaps']['no_live_lane']}
    assert 'm-retired' in gaps
    assert gaps['m-retired']['reason'] == 'retired_lane'
    assert gaps['m-retired']['rows'] == 20
    assert 'm-retired' not in {c['model'] for c in plan['cells']}


# ---------------------------------------------------------------------------
# AC3 — the ratchet decreases as cells land
# ---------------------------------------------------------------------------

def test_ratchet_arm_covers_every_defect_model(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    assert [c['model'] for c in plan['ratchet_cells']] == ['m-defect']


def test_ratchet_projection_decreases_and_reaches_zero(registry, monkeypatch,
                                                       stubbed_gate):
    plan = _plan(registry, monkeypatch)
    proj = plan['ratchet']['projection']
    assert plan['ratchet']['defects_today'] == 1
    assert proj[0]['defects_after'] == 0, 'one defect model, cleared on its first day'
    after = [p['defects_after'] for p in proj]
    assert after == sorted(after, reverse=True), after
    assert after[-1] == 0


def test_ratchet_arm_gets_a_share_of_every_day(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch, per_day=40)
    assert plan['summary']['ratchet_share'] > 0
    assert plan['days'][0]['ratchet_cells'] >= 1
    assert plan['days'][0]['effort_cells'] >= 1


def test_check_fails_on_a_stale_baseline(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch, baseline=99)
    ok, problems = bp.check(plan['cells'], plan['todo'], plan['stats'],
                            plan['ratchet']['baseline'],
                            plan['ratchet']['defects_today'],
                            ratchet_cells=plan['ratchet_cells'],
                            defects_by_model={'m-defect': 1})
    assert not ok
    assert any('ratchet is stale' in p for p in problems), problems


def test_check_fails_when_a_defect_model_has_no_ratchet_cell(registry, monkeypatch,
                                                             stubbed_gate):
    plan = _plan(registry, monkeypatch)
    ok, problems = bp.check(plan['cells'], plan['todo'], plan['stats'],
                            plan['ratchet']['defects_today'],
                            plan['ratchet']['defects_today'],
                            ratchet_cells=[],
                            defects_by_model={'m-defect': 1})
    assert not ok
    assert any('no ratchet cell' in p for p in problems), problems


def test_check_passes_on_a_clean_plan(registry, monkeypatch, stubbed_gate):
    plan = _plan(registry, monkeypatch)
    ok, problems = bp.check(plan['cells'], plan['todo'], plan['stats'],
                            plan['ratchet']['defects_today'],
                            plan['ratchet']['defects_today'],
                            ratchet_cells=plan['ratchet_cells'],
                            defects_by_model={'m-defect': 1})
    assert ok, problems


def test_check_catches_a_done_cell_without_measurement():
    cells = [{'model': 'm', 'effort': 'high', 'category': 'agent_tick',
              'done': True, 'evidence_source': 'estimate'}]
    ok, problems = bp.check(cells, [], {'cells': 1}, None, 0)
    assert not ok
    assert any('non-measured evidence' in p for p in problems), problems


def test_committed_baseline_is_not_stale():
    """AC3's permanent wire, on the REAL tables: the baseline may never sit above
    the defects that actually exist, because a looser wire licenses a regression.
    """
    sys.path.insert(0, str(SCRIPTS))
    gate = _load('router_usability_gate_tr403', SCRIPTS / 'router_usability_gate.py')
    _usable, _explained, defects, _reasons = gate.classify()
    allowed = json.loads(BASELINE.read_text())['defects']
    assert allowed <= len(defects), (
        f'the usability baseline allows {allowed} defects but only {len(defects)} '
        f'exist: run scripts/router_usability_gate.py --ratchet and commit the '
        f'baseline (it may only decrease)')


def test_plan_is_deterministic(registry, monkeypatch, stubbed_gate):
    a = _plan(registry, monkeypatch)
    b = _plan(registry, monkeypatch)
    assert [(c['model'], c['effort'], c['category'], c['day']) for c in a['todo']] == \
           [(c['model'], c['effort'], c['category'], c['day']) for c in b['todo']]

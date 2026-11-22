"""TR-390 — capability-matrix backfill: the measured-vs-estimate report, the
lane-spelling derivation fix, and tier-row provenance.

Pins four properties:

1. `router_usability_gate.py --json` reports, for ALL 24 categories, the count
   of MEASURED (tier_source bench|measured) vs ESTIMATE (estimate|family) tier
   rows — classification derived from the rows, never asserted.
2. Benchmark evidence arrives under whichever casing the probe wrote, while
   the registry serves the same weights id under several casings: a synthetic
   benchmark row for a model with no tier rows must yield model_perf + tier
   rows under EVERY live lane spelling of that id, each keeping tier_source
   'bench' and a source_ref naming the evidence row.
3. No tier row may carry source_ref-less provenance.
4. AC2: every lane among the 20 cheapest live priced lanes of a task profile
   that lacks evidence in a required category carries an explicit
   unknown-by-design model_notes.jsonl row (TR-390 source marker).
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
GATE = REPO / 'scripts' / 'router_usability_gate.py'
VOCAB = REPO / 'scripts' / 'capability_vocab.py'
DATA_DIR = REPO / 'data' / 'tables'
SEED = REPO / 'scripts' / 'router_seed.py'
PY = sys.executable


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gate():
    return _load('router_usability_gate_under_test', GATE)


def _vocab():
    return _load('capability_vocab_under_test', VOCAB)


def _jsonl(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 1. the per-category measured-vs-estimate report (AC1)
# ---------------------------------------------------------------------------

def test_categories_block_covers_all_24_categories():
    cats = _gate().category_census()['categories']
    assert len(cats) == 24, f'expected 24 categories, got {len(cats)}'
    assert set(cats) == set(_vocab().PERF_CATEGORIES), (
        'category census drifted from the one capability vocabulary')


def test_per_category_counts_reconcile_with_model_tier_rows():
    """measured + estimate + other == the raw tier rows of that category, and
    the class split itself must be re-derivable from tier_source."""
    gate = _gate()
    rep = gate.category_census()
    raw = {c: {'measured': 0, 'estimate': 0, 'other': 0, 'total': 0}
           for c in rep['categories']}
    for t in _jsonl(DATA_DIR / 'model_tier.jsonl'):
        block = raw.get(t.get('category'))
        if block is None:
            continue
        block['total'] += 1
        if t.get('tier_source') in gate.MEASURED_TIER_SOURCES:
            block['measured'] += 1
        elif t.get('tier_source') in gate.ESTIMATE_TIER_SOURCES:
            block['estimate'] += 1
        else:
            block['other'] += 1
    for cat, block in rep['categories'].items():
        assert block['measured'] == raw[cat]['measured'], cat
        assert block['estimate'] == raw[cat]['estimate'], cat
        assert block['other'] == raw[cat]['other'], cat
        assert block['total'] == raw[cat]['total'], (
            f'{cat}: reported total {block["total"]} != tier rows {raw[cat]["total"]}')


def test_category_totals_match_the_sum_of_the_blocks():
    rep = _gate().category_census()
    totals = rep['totals']
    for key in ('measured', 'estimate', 'other'):
        assert totals[key] == sum(c[key] for c in rep['categories'].values())
    assert totals['total'] == sum(c['total'] for c in rep['categories'].values())


# ---------------------------------------------------------------------------
# 2. provenance: no tier row without a source_ref (AC: no invented numbers)
# ---------------------------------------------------------------------------

def test_no_tier_row_lacks_source_ref_provenance():
    rows = _jsonl(DATA_DIR / 'model_tier.jsonl')
    assert rows, 'model_tier.jsonl is empty — seed never ran?'
    missing = [r for r in rows
               if not str(r.get('source_ref') or '').strip()
               or not str(r.get('tier_source') or '').strip()]
    assert not missing, (
        f'{len(missing)} tier rows carry no source_ref/tier_source provenance; '
        f'sample: {missing[:3]}')


# ---------------------------------------------------------------------------
# 3. the derivation: case-variant evidence reaches every lane spelling (AC2/AC3)
# ---------------------------------------------------------------------------

def _hermetic_env(tmp_path):
    data = tmp_path / 'data'
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    return {'ROUTING_DATA_DIR': str(data), 'ROUTER_STATE_DIR': str(state),
            'ROUTING_REGISTRY': str(tmp_path / 'registry.json'),
            'ROUTER_SEED_PYTHON': PY, 'ROUTING_BOARD_PY': PY}


def _seed(env):
    p = subprocess.run([PY, str(SEED)], capture_output=True, text=True,
                       timeout=600, env=dict(os.environ, **env))
    assert p.returncode == 0, p.stderr[-800:]
    with open(env['ROUTING_REGISTRY']) as f:
        return json.load(f)


#: a THIRD casing of the qwen3.8-max-prime weights id. The registry serves the
#: id under two live lane spellings ('Qwen/Qwen3.8-Max-Prime' on commandcode,
#: 'qwen/qwen3.8-max-prime' on openrouter); the probe wrote this one; neither
#: lane has any tier row today (the commandcode lane is a committed TR-390
#: defect: live + priced + never measured).
PROBE_SPELLING = 'qwen/QWEN3.8-MAX-PRIME'
LANE_SPELLINGS = ('Qwen/Qwen3.8-Max-Prime', 'qwen/qwen3.8-max-prime')


def test_case_variant_benchmark_yields_tier_rows_for_every_lane_spelling(tmp_path):
    env = _hermetic_env(tmp_path)
    bench_path = os.path.join(env['ROUTING_DATA_DIR'], 'benchmarks.jsonl')
    with open(bench_path, 'a') as f:
        f.write(json.dumps({
            'model': PROBE_SPELLING, 'category': 'code_gen',
            'score': 0.9, 'max_score': 1.0,
            'source': 'tr390-derivation-probe',
            'valid_from': '2026-11-22', 'reasoning_effort': 'default'}) + '\n')
    reg = _seed(env)
    tiers = {(r['model'], r['category']): r for r in reg['tables']['model_tier']}
    for spelling in LANE_SPELLINGS:
        row = tiers.get((spelling, 'code_gen'))
        assert row is not None, (
            f'benchmark evidence for {PROBE_SPELLING} never reached lane spelling '
            f'{spelling!r}: the seed stored it under the probe\'s own casing')
        assert row['tier_source'] == 'bench', row
        assert 'tr390-derivation-probe' in str(row.get('source_ref')), (
            f'tier row for {spelling} lost the evidence-row provenance: {row}')
    # the perf path of the same derivation: one measured perf row per spelling
    perf = {(r['model'], r['category']): r for r in reg['tables']['model_perf']}
    seen = {r['perf'] for r in perf.values()
            if r['model'].lower() == PROBE_SPELLING.lower()
            and r['category'] == 'code_gen'}
    assert len(seen) == 1, f'lane spellings disagree on the measured value: {seen}'


# ---------------------------------------------------------------------------
# 4. AC2: cheapest-20-per-profile gaps carry an explicit unknown-by-design note
# ---------------------------------------------------------------------------

def test_profile_census_shape_matches_the_profile_table():
    census = _gate().profile_gap_census()
    reqs = _jsonl(DATA_DIR / 'task_profile_requirements.jsonl')
    profiles = {r['task_id'] for r in reqs}
    assert set(census) == profiles, 'census drifted from the task profiles'
    for profile, block in census.items():
        assert len(block['lanes']) == 20, (
            f'{profile}: expected the 20 cheapest priced lanes, got '
            f'{len(block["lanes"])}')
        required = {r['category'] for r in reqs if r['task_id'] == profile}
        assert set(block['required']) == required, profile


def test_ac2_gap_lanes_carry_unknown_by_design_notes():
    census = _gate().profile_gap_census()
    unexplained = [(pid, lane) for pid, block in census.items()
                   for lane in block['lanes']
                   if lane['missing'] and not lane['has_tr390_note']]
    assert not unexplained, (
        f'{len(unexplained)} cheapest-lane gaps lack an explicit unknown-by-design '
        f'note: {[(p, l["provider"], l["model"], l["missing"]) for p, l in unexplained[:5]]}. '
        f'Inspect the lane (benchmarks/model_perf/alias lookups for its exact id) and '
        f'add a model_notes.jsonl row with source "TR-390 ..." — never a tier.')

"""TR-295 tests: the A/B harness offline behavior against temp fixtures.

No live model calls anywhere; executor and verifier are fakes/injected."""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import router_ab  # noqa: E402
from router_ab import attempt_id  # noqa: E402,F401


@pytest.fixture
def price_file(tmp_path):
    p = tmp_path / 'prices.jsonl'
    rows = [
        {'lane': 'fixture/model-a', 'in_per_m': 0.5, 'out_per_m': 1.5,
         'cache_read_per_m': 0.05, 'source': 'test-fixture'},
        {'lane': 'fixture/model-b', 'in_per_m': 0.1, 'out_per_m': 0.3,
         'cache_read_per_m': 0.01, 'source': 'test-fixture'},
        {'lane': 'fixture/model-c', 'in_per_m': 2.0, 'out_per_m': 6.0,
         'cache_read_per_m': 0.2, 'source': 'test-fixture'},
        {'lane': 'xkiro/openai/gpt-6-luna', 'in_per_m': 0.9, 'out_per_m': 3.6,
         'cache_read_per_m': 0.09, 'source': 'test-fixture'},
    ]
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(p)


FAKE_PAIRS = [('fixture', 'model-a'), ('fixture', 'model-b'),
              ('fixture', 'model-c'), ('xkiro', 'openai/gpt-6-luna')]


def _run(price_file, tmp_path, pairs=FAKE_PAIRS, **kw):
    out = str(tmp_path / 'ab_rows.jsonl')
    report = str(tmp_path / 'ab_report.json')
    args = ['--out', out, '--report', report, '--price-source', price_file,
            '--run-id', 'test-run-1'] + \
           [x for p in pairs for x in ('--lane', f'{p[0]}/{p[1]}')] + \
           sum([[k, v] for k, v in kw.items()], [])
    rc = router_ab.main(args)
    assert rc == 0
    rows = [json.loads(l) for l in open(out) if l.strip()]
    rep = json.load(open(report))
    return rows, rep


def test_dry_run_no_production_write(tmp_path, price_file):
    """Default mode writes only to the explicit scratch paths."""
    rows, rep = _run(price_file, tmp_path)
    assert rep['mode'].startswith('dry-run')
    assert all(r['source_system'] == 'ab_harness' for r in rows)


def test_same_task_set_all_lanes(tmp_path, price_file):
    rows, _ = _run(price_file, tmp_path)
    tasks = {r['task_id'] for r in rows}
    assert tasks == {t['task_id'] for t in router_ab.frozen_tasks()}
    for t in {t['task_id'] for t in router_ab.frozen_tasks()}:
        lanes = {f"{r['requested_provider']}/{r['requested_model']}"
                 for r in rows if r['task_id'] == t}
        assert lanes == {'fixture/model-a', 'fixture/model-b',
                         'fixture/model-c', 'xkiro/openai/gpt-6-luna'}


def test_row_persistence_shape(tmp_path, price_file):
    rows, _ = _run(price_file, tmp_path)
    need = {'task_id', 'attempt_id', 'run_id', 'requested_provider',
            'requested_model', 'actual_provider', 'actual_model',
            'billing_base_url', 'complexity_levels', 'complexity_sig',
            'complexity_band', 'band_version', 'tokens_in',
            'tokens_cache_read', 'tokens_out', 'api_calls', 'wall_time_s',
            'acceptance_cmd', 'acceptance_result', 'passed', 'cost_usd',
            'cost_basis', 'price_source', 'requested_served_mismatch',
            'source_system', 'session_id'}
    missing = need - set(rows[0])
    assert not missing, missing


def test_band_key_versioned_and_matches_router_outcomes(tmp_path, price_file):
    from router_outcomes import band_key, BAND_VERSION
    rows, _ = _run(price_file, tmp_path)
    for r in rows:
        assert r['complexity_band'] == band_key(r['complexity_levels'])
        assert r['complexity_band'].startswith(BAND_VERSION + ':')
        assert r['complexity_sig']  # exact per-task signature retained


def test_mismatch_excluded_from_requested_bucket(tmp_path, price_file):
    """The xkiro Luna request was served GLM: flagged, and the sample does not
    count toward the Luna lane's denominator or passes."""
    rows, rep = _run(price_file, tmp_path)
    mism = [r for r in rows if r['requested_served_mismatch']]
    assert len(mism) == len(router_ab.frozen_tasks())
    assert all(r['excluded'] and r['exclusion_reason'] for r in mism)
    assert all(r['actual_model'] == 'z-ai/glm-5.3-flash' for r in mism)
    lane = [l for l in rep['lanes'] if l['lane'] == 'xkiro/openai/gpt-6-luna'][0]
    assert lane['excluded_mismatch'] == len(router_ab.frozen_tasks())
    assert lane['usable_attempts'] == 0
    assert lane['measured'] is False
    assert 'mismatch' in lane['unmeasured_reason']


def test_no_glml_run_counted_as_luna(tmp_path, price_file):
    """A GLM-served sample never lands in the Luna lane's passes or cost."""
    rows, _ = _run(price_file, tmp_path)
    for r in rows:
        if r['requested_model'] == 'openai/gpt-6-luna' and r['actual_model'] != 'openai/gpt-6-luna':
            assert r['passed'] is False or r['excluded']
            assert r['cost_usd'] is None


def test_pass_fail_from_verifier_not_self_report(tmp_path, price_file):
    """The executor's self_reported_success=True is never trusted: only the
    acceptance command's exit code decides."""
    rows, _ = _run(price_file, tmp_path)
    failing = [r for r in rows
               if f"{r['requested_provider']}/{r['requested_model']}"
               == 'fixture/model-c']
    assert failing, 'model-c exists'
    assert all(r['passed'] is False for r in failing)
    assert all(r['acceptance_result'].startswith('rc=') for r in failing)


def test_injected_verifier(tmp_path, price_file):
    calls = []

    def v(task, ex, row):
        calls.append(task['task_id'])
        return True, 'injected'

    attempts, lanes = router_ab.run_tasks(
        router_ab.frozen_tasks(), [('fixture', 'model-a')],
        router_ab.fake_executor, router_ab.load_price_source(price_file),
        'rid', str(tmp_path), verifier=v)
    assert len(calls) == len(router_ab.frozen_tasks())
    assert all(a['acceptance_result'] == 'injected' and a['passed'] for a in attempts)


def test_cost_per_passed_task_arithmetic(tmp_path, price_file):
    rows, rep = _run(price_file, tmp_path)
    lane = [l for l in rep['lanes'] if l['lane'] == 'fixture/model-a'][0]
    assert lane['measured'] is True
    row_cost = sum(r['cost_usd'] for r in rows
                   if r['requested_model'] == 'model-a'
                   and r['requested_provider'] == 'fixture' and not r['excluded'])
    assert lane['passed'] == len(router_ab.frozen_tasks())
    assert lane['cost_per_passed_task'] == round(row_cost / lane['passed'], 6)
    # both rates named separately
    assert lane['cost_per_attempt'] is not None
    assert 0 < lane['success_rate'] <= 1.0
    # ranking: cheapest passing lane first
    measured = [l for l in rep['lanes'] if l['measured']]
    assert measured[0]['lane'] == 'fixture/model-b'  # cheapest of the two passers


def test_failed_acceptance_checks_exclude_lane_from_ranking(tmp_path, price_file):
    """A lane passing nothing is unmeasured, never ranked by cheap cost."""
    rows, rep = _run(price_file, tmp_path)
    lane = [l for l in rep['lanes'] if l['lane'] == 'fixture/model-c'][0]
    assert lane['passed'] == 0
    assert lane['measured'] is False
    assert 'zero independently passed tasks' in lane['unmeasured_reason']


def test_missing_price_never_zero_cost(tmp_path):
    """A lane with no price entry is unmeasured with a reason; cost is never 0."""
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('')
    rows, rep = _run(str(empty), tmp_path)
    lane = [l for l in rep['lanes'] if l['lane'] == 'fixture/model-a'][0]
    assert lane['measured'] is False
    assert 'no verified price source' in lane['unmeasured_reason']
    assert all(r['cost_usd'] is None for r in rows
               if r['requested_provider'] == 'fixture')


def test_missing_token_meter_reason(tmp_path, price_file, monkeypatch):
    orig = router_ab.fake_executor

    def no_tokens(provider, model, task, scratch):
        ex = orig(provider, model, task, scratch)
        ex['tokens_out'] = None
        return ex

    monkeypatch.setattr(router_ab, 'fake_executor', no_tokens)
    rows, rep = _run(price_file, tmp_path)
    lane = [l for l in rep['lanes'] if l['lane'] == 'fixture/model-a'][0]
    assert lane['measured'] is False
    assert 'missing token meter' in lane['unmeasured_reason']


def test_glm_price_anomaly_excluded_unless_verified(tmp_path, price_file):
    """z-ai/glm-5.3-flash carries the $1344.97 anomaly: excluded without a
    price file; a lane file entry WITH basis+source un-excludes it."""
    lanes = router_ab.price_for_pair('z-ai', 'glm-5.3-flash', {})
    assert lanes is None
    with_basis = {'z-ai/glm-5.3-flash': {'in_per_m': 0.2, 'out_per_m': 0.6,
                                         'source': 'vendor rate card 2026-10',
                                         'basis': 'official per-1M pricing'}}
    assert router_ab.price_for_pair('z-ai', 'glm-5.3-flash', with_basis) is not None


def test_sample_floor(tmp_path, price_file):
    """A lane with fewer usable attempts than the floor is unmeasured."""
    attempts, lanes = router_ab.run_tasks(
        router_ab.frozen_tasks()[:2], [('fixture', 'model-a')],
        router_ab.fake_executor, router_ab.load_price_source(price_file),
        'rid', str(tmp_path), sample_floor=3)
    lane = lanes[0]
    assert lane['usable_attempts'] == 2 < lane['sample_floor']
    assert lane['measured'] is False
    assert 'below sample floor' in lane['unmeasured_reason']


def test_attempt_id_replay_deterministic():
    a1 = attempt_id('t1', 'p', 'm', 'run', 1)
    a2 = attempt_id('t1', 'p', 'm', 'run', 1)
    a3 = attempt_id('t1', 'p', 'm', 'run', 2)
    assert a1 == a2 and a1 != a3


def test_replay_idempotent_append(tmp_path, price_file):
    """Running the same run-id twice appends nothing new (append_rows dedupe)."""
    out = str(tmp_path / 'rows.jsonl')
    report = str(tmp_path / 'rep.json')
    args = ['--out', out, '--report', report, '--price-source', price_file,
            '--run-id', 'same-run'] + \
           [x for p in FAKE_PAIRS for x in ('--lane', f'{p[0]}/{p[1]}')]
    assert router_ab.main(args) == 0
    first = sum(1 for _ in open(out))
    assert router_ab.main(args) == 0
    second = sum(1 for _ in open(out))
    assert first == second == len(router_ab.frozen_tasks()) * len(FAKE_PAIRS)


def test_provider_separation_in_rows(tmp_path, price_file):
    rows, _ = _run(price_file, tmp_path)
    by_lane = {}
    for r in rows:
        by_lane.setdefault((r['requested_provider'], r['requested_model']), []).append(r)
    assert len(by_lane) == 4
    # every lane's rows keep their own served identity
    for (prov, model), rs in by_lane.items():
        assert all(r['requested_provider'] == prov for r in rs)


def test_output_guard_refuses_production_paths(tmp_path):
    for bad in ('~/task-router/data/state/outcomes.jsonl',
                '~/task-router/data/state/foo.jsonl'):
        with pytest.raises(SystemExit, match='REFUSED'):
            router_ab.guard_output(bad)
    assert router_ab.guard_output(str(tmp_path / 'scratch.jsonl')) == \
        str(tmp_path / 'scratch.jsonl')


def test_execute_refuses_without_four_lanes():
    with pytest.raises(SystemExit):
        router_ab.main(['--execute', '--out', '/tmp/x.jsonl',
                        '--report', '/tmp/x.json', '--lane', 'a/b',
                        '--price-source', '/dev/null'])


def test_execute_refuses_without_executor_script():
    lanes = [f'--lane p/m{i}' for i in range(4)]
    argv = ['--execute', '--out', '/tmp/x.jsonl', '--report', '/tmp/x.json']
    for l in ['p/m1', 'p/m2', 'p/m3', 'p/m4']:
        argv += ['--lane', l]
    with pytest.raises(SystemExit):
        router_ab.main(argv)


def test_live_executor_disabled_by_default(tmp_path):
    """No real API call can happen accidentally: live_executor hard-stops."""
    with pytest.raises(SystemExit, match='not configured'):
        router_ab.live_executor('p', 'm', {'task_id': 'x', 'acceptance_cmd': ':',
                                           'levels': {}}, str(tmp_path))

"""Unit tests for router_outcomes (TR-049 phase 1): decay math, bucketing,
rolling averages, idempotent import. No network, no live DB."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro

NOW = 1_800_000_000.0


def _row(model='m1', provider='p1', complexity=None, cost=1.0, age_s=0, ts=None,
         success=True, source='test'):
    return {'source_system': source, 'session_id': f'{source}-{model}-{age_s}-{cost}',
            'complexity': complexity, 'provider': provider, 'model': model,
            'turns': 1, 'tokens_in': 100, 'tokens_out': 10, 'tokens_reasoning': 0,
            'cost_usd': cost, 'wall_time_s': 5.0, 'success': success,
            'ts': ts if ts is not None else NOW - age_s}


def test_decay_weight_half_life():
    assert ro.decay_weight(0, 24) == 1.0
    assert ro.decay_weight(24 * 3600, 24) == pytest.approx(0.5)
    assert ro.decay_weight(48 * 3600, 24) == pytest.approx(0.25)
    assert ro.decay_weight(24 * 3600, 72) == pytest.approx(0.7937, rel=1e-3)  # 1d old on 3d scale


def test_bucket_avg_weights_recent_samples():
    rows = [_row(cost=1.0, age_s=0), _row(cost=3.0, age_s=24 * 3600)]  # 1d scale
    avg = ro.bucket_avg(rows, 24, now_s=NOW)
    assert avg == pytest.approx((1.0 + 0.5 * 3.0) / 1.5)  # ~1.667, recent dominates


def test_bucket_avg_no_costs_returns_none():
    rows = [{**_row(), 'cost_usd': None}]
    assert ro.bucket_avg(rows, 24, now_s=NOW) is None  # never fabricate


def test_compute_averages_per_backend_isolation():
    rows = [_row(source='hermes', provider='p1', model='m1', complexity='P1', cost=2.0),
            _row(source='opencode', provider='p1', model='m1', complexity='P1', cost=8.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(out) == 2  # NOT merged: two buckets
    by_src = {e['source_system']: e for e in out}
    assert by_src['hermes']['avg_cost_task_24h'] == pytest.approx(2.0)
    assert by_src['opencode']['avg_cost_task_24h'] == pytest.approx(8.0)
    # field-position guard: labels must not shift (regression 2026-09-17)
    assert by_src['hermes']['provider'] == 'p1' and by_src['hermes']['model'] == 'm1'


def test_compute_averages_merge_opt_in():
    rows = [_row(source='hermes', provider='p1', model='m1', complexity='P1', cost=2.0),
            _row(source='opencode', provider='p1', model='m1', complexity='P1', cost=8.0)]
    out = ro.compute_averages(rows, scales_h=[24], merge_backends=True, now_s=NOW)
    assert len(out) == 1
    assert out[0]['avg_cost_task_24h'] == pytest.approx(5.0)
    assert 'source_system' not in out[0]
    assert out[0]['provider'] == 'p1' and out[0]['model'] == 'm1' and out[0]['complexity'] == 'P1'


def test_complexity_is_part_of_the_bucket():
    rows = [_row(complexity='P1', cost=1.0), _row(complexity='P4', cost=9.0)]
    out = ro.compute_averages(rows, scales_h=[24], merge_backends=True, now_s=NOW)
    assert len(out) == 2
    costs = {e['complexity']: e['avg_cost_task_24h'] for e in out}
    assert costs['P1'] == pytest.approx(1.0) and costs['P4'] == pytest.approx(9.0)
    assert all(e['provider'] == 'p1' and e['model'] == 'm1' for e in out)


def test_append_rows_idempotent(tmp_path):
    p = str(tmp_path / 'outcomes.jsonl')
    rows = [_row(), _row(model='m2')]
    assert ro.append_rows(p, rows) == 2
    assert ro.append_rows(p, rows) == 0  # same (source, session, model) skipped
    assert ro.append_rows(p, [_row(model='m3')]) == 1
    assert len([l for l in open(p) if l.strip()]) == 3


def test_outcomes_and_averages_gitignored():
    gi = os.path.join(ro.REPO, '.gitignore')
    text = open(gi).read() if os.path.exists(gi) else ''
    assert 'data/state/outcomes.jsonl' in text
    assert 'data/state/outcomes-averages.jsonl' in text


def test_profile_signature_is_declared_category_levels():
    """Bane: complexity = the task's CATEGORIES (per-category required levels),
    not a scalar. The signature must come from the registry's
    task_profile_requirements — never invented."""
    sig = ro.profile_signature('P4_SECURITY')
    assert sig is not None
    assert sig == {'guard': 0, 'review': 0, 'security': 2}
    p1 = ro.profile_signature('P1_CODING')
    assert 'code_gen' in p1 and 'refactor' in p1
    assert ro.profile_signature('P_DOES_NOT_EXIST') is None  # unknown -> None, not fake


# ---------------------------------------------------------------------------
# TR-049 component 1 — ingest payload normalization + fail-open append
# ---------------------------------------------------------------------------

def _wire(**over):
    body = {'source_system': 'hermes', 'session_id': 's-1', 'task_label': 'tick',
            'complexity': {'code_gen': 2, 'guard': 0}, 'provider': 'deepseek',
            'model': 'deepseek-v4-flash', 'turns': 4, 'tokens_in': 10,
            'tokens_out': 20, 'tokens_reasoning': 5, 'cost': 0.25,
            'wall_time': 61.5, 'success': True}
    body.update(over)
    return body


def test_normalize_row_accepts_the_wire_shape():
    row = ro.normalize_row(_wire(), now_s=NOW)
    # wire alias `cost`/`wall_time` land on the store's own names
    assert row['cost_usd'] == 0.25 and row['wall_time_s'] == 61.5
    assert row['ts'] == NOW
    assert row['complexity'] == {'code_gen': 2, 'guard': 0}
    assert row['success'] is True and row['turns'] == 4
    # every store field is present (docs/outcomes-schema.md contract)
    assert set(ro.STORE_FIELDS) <= set(row)


def test_normalize_row_round_trips_store_names():
    """A row read back from the store re-posts unchanged (cost_usd/wall_time_s)."""
    row = ro.normalize_row(_wire(), now_s=NOW)
    again = ro.normalize_row(row, now_s=NOW)
    assert again == row


def test_normalize_row_rejects_every_problem_at_once():
    with pytest.raises(ValueError) as exc:
        ro.normalize_row({'source_system': '', 'session_id': 's', 'provider': 'p',
                          'model': 'm', 'turns': 1.5, 'success': 'yes',
                          'complexity': 7})
    msg = str(exc.value)
    for fragment in ('source_system', 'turns', 'success', 'complexity'):
        assert fragment in msg, msg


def test_normalize_row_rejects_conflicting_cost_aliases():
    with pytest.raises(ValueError) as exc:
        ro.normalize_row(_wire(cost=1.0, cost_usd=2.0))
    assert 'disagree' in str(exc.value)


def test_normalize_row_allows_null_metrics():
    row = ro.normalize_row({'source_system': 'hermes', 'session_id': 's2',
                            'provider': 'p', 'model': 'm'}, now_s=NOW)
    assert row['cost_usd'] is None and row['wall_time_s'] is None
    assert row['success'] is None and row['complexity'] is None
    assert row['turns'] is None


def test_append_row_fast_dedupes_inside_the_tail(tmp_path):
    p = str(tmp_path / 'store.jsonl')
    row = ro.normalize_row(_wire(session_id='dup'), now_s=NOW)
    assert ro.append_row_fast(p, row) == (True, 'appended')
    ok, reason = ro.append_row_fast(p, row)
    assert ok is False and 'duplicate' in reason
    assert len([l for l in open(p) if l.strip()]) == 1
    # a different session still lands
    assert ro.append_row_fast(p, ro.normalize_row(_wire(session_id='other'), now_s=NOW))[0]


def test_append_row_fast_is_fail_open_on_write_error(tmp_path):
    """A store problem returns (False, reason) — never raises: the reporter
    must not be blocked by our disk."""
    blocked = tmp_path / 'not-a-dir'
    blocked.write_text('x')
    ok, reason = ro.append_row_fast(str(blocked / 'store.jsonl'),
                                    ro.normalize_row(_wire(), now_s=NOW))
    assert ok is False and 'write failed' in reason


def test_ingest_honours_routing_outcomes_file(tmp_path, monkeypatch):
    store = tmp_path / 'elsewhere' / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    assert ro.outcomes_path() == str(store)
    out = ro.ingest(_wire(session_id='env-1'), now_s=NOW)
    assert out['appended'] is True and out['store'] == str(store)
    written = json.loads(open(store).read().strip())
    assert written['session_id'] == 'env-1' and written['cost_usd'] == 0.25
    # ingest is the only writer needed by the HTTP layer
    assert ro.outcomes_path() == str(store)


def test_ingest_validation_error_raises_for_the_400_path(monkeypatch, tmp_path):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'o.jsonl'))
    with pytest.raises(ValueError):
        ro.ingest({'source_system': 'hermes'})

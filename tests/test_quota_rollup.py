"""TR-415 — the persisted per-provider window rollup (resolve hot path).

What is proven here, one test per acceptance criterion that can be proven
hermetically:

  equivalence   a rollup-backed accounting block equals the ledger-walk block
                field for field (spend, tokens, unpriced debt, resets, headroom)
  AC4           appending a row to the ledger INVALIDATES the rollup (delta) and
                the next read reflects the new spend; a rewrite rebuilds; the
                test fails if invalidation is dropped (the entry count / spend
                assertions move with the ledger)
  AC3           with no rollup the FIRST read is ONE pass over the store
                (bytes_read == store size, never 2x — no double pay)
  fail-open     corrupt / partial / foreign sidecar, missing ledger and the
                kill switch all degrade to "no rollup", never to an exception
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import router_outcomes as ro            # noqa: E402
import router_quota_accounting as rqa   # noqa: E402
import router_quota_rollup as qr        # noqa: E402

NOW = 1795736000.0                      # 2026-11-26T18:13:20Z
LIMITS = {'default_window': 'rolling_5h',
          'providers': {'morph': {'window': 'monthly', 'limit_usd': 25},
                        'xkiro': {'window': 'weekly', 'limit_usd': 400},
                        'deepseek': {'window': 'rolling_5h',
                                     'limit_usd': 12, 'window_hours': 5}}}


def _row(provider, ts, tin, tout, cost=None, model='m-1'):
    row = {'source_system': 'test', 'session_id': f's-{provider}-{ts}-{tin}',
           'provider': provider, 'model': model, 'ts': ts,
           'tokens_in': tin, 'tokens_out': tout}
    if cost is not None:
        row['cost_usd'] = cost
    return row


def _write_ledger(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    return path


@pytest.fixture()
def ledger(tmp_path):
    """A store with rows in two windows: monthly (morph) and weekly (xkiro)."""
    rows = [
        _row('morph', NOW - 60, 1000, 100, cost=0.5),
        _row('morph', NOW - 3600, 2000, 200, cost=1.25),
        _row('morph', NOW - 20 * 86400, 500, 50, cost=9.0),   # older month
        _row('xkiro', NOW - 120, 30000, 400, cost=0.75),
        _row('xkiro', NOW - 300, 100, 10, cost=0.25),
        _row('deepseek', NOW - 600, 100, 10, cost=0.125),
        # tokens but no declared price: the window SUM must go NULL, not short
        {'provider': 'unpriced', 'ts': NOW - 30, 'tokens_in': 5, 'model': 'm-1'},
        {'provider': 'orphan'},                                # no ts
    ]
    return _write_ledger(tmp_path / 'outcomes.jsonl', rows)


def _blocks(ledger, limits=LIMITS, now=NOW, persist=True):
    cut = rqa.widest_window_cutoff(limits, now)
    state, stats = qr.refresh(ledger, cut, persist=persist)
    assert state is not None, stats
    totals = qr.totals_by_provider(state, limits, now)
    rows = ro.load_outcome_rows(ledger)
    by = {}
    for r in rows:
        p = r.get('provider')
        if p is not None:
            by.setdefault(str(p), []).append(r)
    out = {}
    for prov in sorted(set(list(by) + list(totals))):
        cfg = (limits.get('providers') or {}).get(prov) or {}
        walk = rqa.account_provider(prov, by.get(prov, []), cfg, now,
                                    default_window=limits['default_window'])
        roll = rqa.account_provider(prov, [], cfg, now,
                                    default_window=limits['default_window'],
                                    totals=totals.get(prov))
        out[prov] = (walk, roll)
    return out, state, stats


FIELDS = ('window', 'window_start', 'n_rows', 'tokens_in', 'tokens_out',
          'tokens_reasoning', 'tokens_total', 'spent_usd', 'unpriced_rows',
          'unpriced_reason', 'limit_usd', 'remaining_usd', 'remaining_reason',
          'resets_at', 'headroom_usd_per_s')


def test_rollup_totals_equal_ledger_walk(ledger):
    out, _state, stats = _blocks(ledger)
    assert stats['mode'] == 'rebuild'
    assert stats['single_pass'] is True
    assert out, 'no providers compared'
    for prov, (walk, roll) in out.items():
        diff = {k: (walk.get(k), roll.get(k)) for k in FIELDS
                if walk.get(k) != roll.get(k)}
        assert not diff, f'{prov}: {diff}'
    # the fixture actually exercises a non-zero window, or this test is vacuous
    assert out['xkiro'][1]['n_rows'] == 2
    assert out['xkiro'][1]['spent_usd'] == pytest.approx(1.0)
    assert out['xkiro'][1]['headroom_usd_per_s'] is not None
    # an unpriceable row makes the window SUM null on BOTH sides (never short)
    assert out['unpriced'][1]['unpriced_rows'] == 1
    assert out['unpriced'][1]['spent_usd'] is None


def test_append_invalidates_and_reflects_new_spend(ledger):
    """AC4: an append invalidates the rollup — the next read sees the new row.

    Dropping invalidation (e.g. returning a cached state without checking the
    source identity) leaves the spend at 0.75 and the entry count at 2 here, so
    this test fails."""
    out, state, stats = _blocks(ledger)
    before = out['xkiro'][1]['spent_usd']
    entries_before = len(state['providers']['xkiro'])
    assert (before, entries_before) == (pytest.approx(1.0), 2)

    with open(ledger, 'a', encoding='utf-8') as f:
        f.write(json.dumps(_row('xkiro', NOW - 10, 100, 10, cost=2.0)) + '\n')

    cut = rqa.widest_window_cutoff(LIMITS, NOW)
    state2, stats2 = qr.refresh(ledger, cut)
    assert stats2['mode'] == 'delta', stats2          # incremental, not rebuild
    assert stats2['rebuilt'] is False
    assert stats2['bytes_read'] < os.path.getsize(ledger)
    assert len(state2['providers']['xkiro']) == 3

    totals = qr.totals_by_provider(state2, LIMITS, NOW)
    block = rqa.account_provider('xkiro', [], LIMITS['providers']['xkiro'], NOW,
                                default_window=LIMITS['default_window'],
                                totals=totals['xkiro'])
    assert block['spent_usd'] == pytest.approx(3.0)
    assert block['n_rows'] == 3

    # the no-rollup walk agrees — the new row is a fact about the store
    rows = [r for r in ro.load_outcome_rows(ledger) if r.get('provider') == 'xkiro']
    walk = rqa.account_provider('xkiro', rows, LIMITS['providers']['xkiro'], NOW,
                                default_window=LIMITS['default_window'])
    assert walk['spent_usd'] == pytest.approx(3.0)


def test_rewrite_invalidates_with_a_rebuild(ledger):
    """A non-append change (truncate/rewrite) can never be read as a delta."""
    _out, _state, stats = _blocks(ledger)
    assert stats['mode'] == 'rebuild'
    rows = [_row('xkiro', NOW - 40, 10, 1, cost=0.25),
            _row('morph', NOW - 30, 10, 1, cost=0.125)]
    _write_ledger(ledger, rows)                      # same inode, new prefix
    state2, stats2 = qr.refresh(
        ledger, rqa.widest_window_cutoff(LIMITS, NOW))
    assert stats2['mode'] == 'rebuild', stats2
    assert stats2['reason'] == 'ledger-rewritten', stats2
    assert len(state2['providers']['xkiro']) == 1
    totals = qr.totals_by_provider(state2, LIMITS, NOW)
    assert totals['xkiro']['usd'] == pytest.approx(0.25)
    assert 'morph' in totals and totals['morph']['usd'] == pytest.approx(0.125)


def test_price_table_change_forces_a_rebuild(ledger, monkeypatch):
    """A row priced from the price table is only as current as that table."""
    _out, _state, stats = _blocks(ledger)
    assert stats['mode'] == 'rebuild'
    real = qr._prices_stat()
    monkeypatch.setattr(qr, '_prices_stat',
                        lambda: dict(real or {}, mtime_ns=(real or {}).get('mtime_ns', 0) + 1))
    state2, stats2 = qr.refresh(ledger, rqa.widest_window_cutoff(LIMITS, NOW))
    assert state2 is not None
    assert stats2['mode'] == 'rebuild'
    assert stats2['reason'] == 'price-table-changed'


def test_no_rollup_reads_the_store_once(ledger):
    """AC3 (no double pay): with no rollup the first read is ONE pass."""
    size = os.path.getsize(ledger)
    state, stats = qr.refresh(ledger, rqa.widest_window_cutoff(LIMITS, NOW))
    assert state is not None
    assert stats['mode'] == 'rebuild' and stats['single_pass'] is True
    assert stats['bytes_read'] == size            # exactly one pass, not two
    assert stats['rows_parsed'] == 8
    # the built state answers today's rows without a second read
    rows = qr.rows_from_state(state)
    assert len(rows) == 7          # every row WITH a ts is kept; the no-ts row is not
    assert {r['provider'] for r in rows} == {'morph', 'xkiro', 'deepseek',
                                             'unpriced'}


@pytest.mark.parametrize('payload', [
    b'{"not": "a rollup"}',
    b'{"version": 1, "providers": {"x": "nope"}}',
    b'\x00\x01garbage',
    b'',
])
def test_corrupt_rollup_falls_back_and_never_raises(ledger, payload):
    side = qr.rollup_path(ledger)
    with open(side, 'wb') as f:
        f.write(payload)
    cut = rqa.widest_window_cutoff(LIMITS, NOW)
    assert qr.load(side) is None
    state, stats = qr.refresh(ledger, cut)
    assert state is not None, stats               # rebuilt from the ledger
    assert stats['mode'] == 'rebuild'
    with open(side, encoding='utf-8') as f:
        assert json.load(f)['version'] == qr.VERSION


def test_missing_ledger_is_not_a_failure(tmp_path):
    state, stats = qr.refresh(tmp_path / 'nope.jsonl', NOW - 86400)
    assert state is None
    assert stats['reason'] == 'no ledger'


def test_kill_switch_disables_the_leg(ledger, monkeypatch):
    monkeypatch.setenv(qr.ENV_SWITCH, '0')
    state, stats = qr.refresh(ledger, rqa.widest_window_cutoff(LIMITS, NOW))
    assert state is None
    assert 'disabled' in (stats['reason'] or '')
    assert not os.path.exists(qr.rollup_path(ledger))


def test_widened_window_forces_a_rebuild(ledger):
    """The rollup keeps only the widest window's rows: a WIDER window later on
    cannot be answered from it, so it must rebuild instead of under-reporting."""
    narrow = {'default_window': 'rolling_5h',
              'providers': {'xkiro': {'window': 'rolling_daily',
                                      'limit_usd': 400}}}
    _out, _state, stats = _blocks(ledger, limits=narrow)
    assert stats['mode'] == 'rebuild'
    wider = {'default_window': 'rolling_5h',
             'providers': {'morph': {'window': 'monthly', 'limit_usd': 25},
                           'xkiro': {'window': 'weekly', 'limit_usd': 400}}}
    _state2, stats2 = qr.refresh(ledger, rqa.widest_window_cutoff(wider, NOW))
    assert stats2['mode'] == 'rebuild', stats2
    assert stats2['reason'] == 'window-widened'

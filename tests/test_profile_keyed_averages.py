"""TR-115 retry — declared-profile keys in the rolling averages.

The defect (measured 2026-10-22): import_hermes wrote rows with
profile_id=NULL and the AVERAGES carried only classifier complexity_sig
hashes, so the scheduler's resolve asked lane_stats for keys=['P1_CODING'],
found nothing complexity-matched, and every lane read 'unconditioned'/None —
coverage 0.0 and both sorts producing identical heads.

The fix under test:
  1. source data: the coding-hermes scheduler DB's ticks table names the
     gateway session key each spawn created + the project; the registry's
     projects table declares <project>.profile. import_hermes combines both
     and stamps the DECLARED profile on the emitted rows.
  2. the averages then carry 'profile:<id>' buckets next to the classifier
     hashes (row_complexity_sig's existing profile_id path — no math change).
  3. the read side (router_spawn.lane_stats) matches the plain profile id
     against the 'profile:<id>' bucket key, so a resolve's
     keys=['P1_CODING'] finds a measured cost.

Fail-open is pinned too: an unreadable scheduler DB / unknown project leaves
the row's profile_id NULL (honest unknown) and the import still succeeds.
"""
import json
import os
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import router_outcomes as ro  # noqa: E402


# --------------------------------------------------------------- fixtures

def _make_hermes_db(path, sessions):
    """sessions: [(session_id, model, provider, base_url, cost, t0, t1)]"""
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE session_model_usage ('
               'session_id TEXT, model TEXT, billing_provider TEXT,'
               ' billing_base_url TEXT, task TEXT, api_call_count INTEGER,'
               ' input_tokens INTEGER, output_tokens INTEGER,'
               ' reasoning_tokens INTEGER, estimated_cost_usd REAL,'
               ' first_seen REAL, last_seen REAL)')
    for (sid, model, prov, base, cost, t0, t1) in sessions:
        db.execute('INSERT INTO session_model_usage VALUES '
                   '(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid, model, prov, base, '', 3, 1000, 500, 0, cost,
                    t0, t1))
    db.commit()
    db.close()
    return path


def _make_scheduler_db(path, ticks):
    """ticks: [(tick_id, project, session_id, spawned_at)]"""
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE ticks (id TEXT PRIMARY KEY, project_name TEXT,'
               ' session_id TEXT, spawned_at TEXT)')
    for (tid, project, sid, spawned) in ticks:
        db.execute('INSERT INTO ticks VALUES (?,?,?,?)',
                   (tid, project, sid, spawned))
    db.commit()
    db.close()
    return path


def _make_registry(path, projects):
    with open(path, 'w') as f:
        json.dump({'tables': {'projects': projects}}, f)
    return path


@pytest.fixture
def attribution_env(tmp_path, monkeypatch):
    """Three separate stores + the env overrides pointing at them."""
    NOW = time.time()
    sched = _make_scheduler_db(tmp_path / 'sched.db', [
        ('tick-1', 'lore-foreman', 'sess-coding', '2026-10-01T00:00:00'),
        ('tick-2', 'lore-docs', 'sess-docs', '2026-10-02T00:00:00'),
        ('tick-3', 'unknown-project', 'sess-unknown', '2026-10-03T00:00:00'),
        ('tick-4', 'lore-docs', 'sess-older', '2025-01-01T00:00:00'),
    ])
    reg = _make_registry(tmp_path / 'registry.json', [
        {'id': 'lore-foreman', 'profile': 'P1_CODING'},
        {'id': 'lore-docs', 'profile': 'P3_DOCS'},
        # unknown-project deliberately absent
    ])
    hermes = _make_hermes_db(tmp_path / 'state.db', [
        ('sess-coding', 'kimi-k3', 'kimi-for-coding',
         'https://api.xkiro.com/v1', 0.10, NOW - 3600, NOW),
        ('sess-docs', 'deepseek-flash', 'deepseek', 'https://api.deepseek.com',
         0.05, NOW - 3600, NOW),
        ('sess-unknown', 'mimo-v2.5', 'opencode-go', 'https://localhost:9999',
         0.02, NOW - 3600, NOW),
        ('sess-older', 'deepseek-flash', 'deepseek', 'https://api.deepseek.com',
         0.20, NOW - 1800, NOW),
    ])
    store = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('HERMES_SCHEDULER_DB', str(sched))
    monkeypatch.setenv('ROUTER_PROFILE_REGISTRY', str(reg))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    monkeypatch.setenv('ROUTING_AVERAGES_FILE',
                       str(tmp_path / 'averages.jsonl'))
    return {'sched': sched, 'reg': reg, 'hermes': hermes, 'store': store}


# --------------------------------------------------- source-side attribution

def test_import_hermes_stamps_the_declared_profile(attribution_env):
    rows = ro.import_hermes(str(attribution_env['hermes']))
    by_sid = {r['session_id']: r for r in rows}
    assert by_sid['sess-coding']['profile_id'] == 'P1_CODING'
    assert by_sid['sess-docs']['profile_id'] == 'P3_DOCS'


def test_an_unknown_project_leaves_the_profile_null_and_the_row_intact(
        attribution_env):
    """Honest unknown: no registry declaration -> NULL profile, still a row."""
    rows = ro.import_hermes(str(attribution_env['hermes']))
    by_sid = {r['session_id']: r for r in rows}
    assert by_sid['sess-unknown']['profile_id'] is None
    assert by_sid['sess-unknown']['cost_usd'] is not None


def test_the_last_tick_for_a_session_wins(attribution_env):
    """A session spawned twice belongs to the tick driving it now."""
    rows = ro.import_hermes(str(attribution_env['hermes']))
    assert rows, 'fixture must emit rows'
    # sess-older is only ever attributed to lore-docs, on any tick order
    by_sid = {r['session_id']: r for r in rows}
    assert by_sid['sess-older']['profile_id'] == 'P3_DOCS'


def test_hermes_config_profile_is_never_mapped(attribution_env):
    """sessions.profile_name='default' is a HERMES config profile, not the
    router capability profile — P3_DOCS traffic must not read as P0_FORE."""
    # the fixture never relies on sessions.profile_name; pin the shape:
    for r in ro.import_hermes(str(attribution_env['hermes'])):
        assert not r.get('task_label') or isinstance(r['task_label'], str)


def test_fail_open_when_the_scheduler_db_is_missing(tmp_path, monkeypatch):
    hermes = _make_hermes_db(tmp_path / 'state.db', [
        ('sess-x', 'kimi-k3', 'kimi-for-coding',
         'https://api.xkiro.com/v1', 0.5, 0, 1)])
    monkeypatch.setenv('HERMES_SCHEDULER_DB',
                       str(tmp_path / 'does-not-exist.db'))
    monkeypatch.setenv('ROUTER_PROFILE_REGISTRY',
                       str(tmp_path / 'no-registry.json'))
    rows = ro.import_hermes(str(hermes))
    assert len(rows) == 1
    assert rows[0]['profile_id'] is None
    assert rows[0]['cost_usd'] == 0.5      # the row itself is unchanged


# ------------------------------------------------- averages carrying profiles

def test_averages_carry_a_profile_keyed_bucket_with_nonzero_cost(
        attribution_env):
    """ACCEPTANCE for the averages: keys=['P1_CODING'] finds a measured cost."""
    rows = ro.import_hermes(str(attribution_env['hermes']))
    n = ro.append_rows(str(attribution_env['store']), rows)
    assert n == 4
    avgs = ro.compute_averages(ro.load_outcome_rows(str(
        attribution_env['store'])))
    coding = [a for a in avgs if a['complexity_sig'] == 'profile:P1_CODING']
    assert coding, 'the averages must hold a profile:P1_CODING bucket'
    assert coding[0]['avg_cost_task_24h'] is not None
    assert coding[0]['avg_cost_task_24h'] > 0.0
    assert coding[0]['model'] == 'kimi-k3'
    # classifier-hash buckets still exist alongside (additive, no regression)
    docs = [a for a in avgs if a['complexity_sig'] == 'profile:P3_DOCS']
    assert docs and docs[0]['avg_cost_task_24h'] > 0.0


def test_row_complexity_sig_precedence_unchanged(attribution_env):
    """A row with an explicit complexity_sig is NOT re-keyed by the profile —
    the classifier hash wins, the profile stamp is additive only."""
    rows = ro.import_hermes(str(attribution_env['hermes']))
    assert ro.row_complexity_sig(rows[0]) == 'profile:P1_CODING'
    explicit = dict(rows[0])
    explicit['complexity_sig'] = 'abc123'
    assert ro.row_complexity_sig(explicit) == 'abc123'


# ------------------------------------------------------------- the read side

def _stats_index(avgs_rows):
    """The resolve-time index shape router_spawn.load_outcome_stats builds
    (--merge-backends equivalent, stdlib-only): collapse per-backend rows."""
    return {(r.get('provider'), r.get('model')): [r] for r in avgs_rows}


def test_lane_stats_matches_a_plain_profile_key_against_profile_buckets(
        attribution_env):
    import router_spawn as rs
    rows = ro.import_hermes(str(attribution_env['hermes']))
    avgs = ro.compute_averages(rows)
    index = _stats_index(avgs)
    row, match = rs.lane_stats(index, 'xkiro', 'kimi-k3',
                               {'P1_CODING'})
    assert row is not None
    assert row['complexity_sig'] == 'profile:P1_CODING'
    assert row['avg_cost_task_24h'] > 0.0
    assert match == 'complexity'


def test_lane_stats_still_matches_the_legacy_complexity_hash():
    """Regression arm: the exact-band path is untouched."""
    import router_spawn as rs
    levels = {'reasoning': 2}
    want = ro.complexity_sig(levels)
    row = {'provider': 'p', 'model': 'm', 'complexity_sig': want,
           'avg_cost_task_24h': 0.33, 'n_samples': 7}
    got, match = rs.lane_stats({('p', 'm'): [row]}, 'p', 'm', [levels])
    assert match == 'complexity' and got['avg_cost_task_24h'] == 0.33


# ------------------------------------------------- sort sufficiency end to end

def test_the_measured_sort_ranks_on_the_profile_bucket(attribution_env):
    """The sufficiency census flips: with a profile-keyed bucket carrying
    enough samples, ranked_on_measurement > 0 and coverage > 0."""
    import router_spawn as rs
    rows = ro.import_hermes(str(attribution_env['hermes']))
    # deepen the P1_CODING bucket past the floor (3) and the coverage bar
    deep = []
    for i in range(5):
        d = dict(rows[0])
        d['session_id'] = f'sess-coding-{i}'
        deep.append(d)
    avgs = ro.compute_averages(deep)
    for a in avgs:
        a['n_samples'] = 6      # above floor and blend ceiling
    lanes = [{'provider': 'xkiro', 'model': 'kimi-k3', 'usd_1m': 0.6}]
    ctx = {'index': _stats_index(avgs), 'keys': {'P1_CODING'},
           'window_h': 24, 'task_key': 'P1_CODING'}
    _key = rs._sort_predicted_cost_per_task('predicted_cost_per_task',
                                            lanes, ctx)
    basis = ctx.get('_sort_basis') or {}
    assert basis.get('coverage', 0.0) > 0.0
    assert (basis.get('ranked_on_measurement') or 0) > 0
    assert basis.get('effective') == 'measured'


# --------------------------------------------------- the historical backfill

def test_backfill_stamps_existing_rows_and_is_idempotent(attribution_env,
                                                         tmp_path):
    store = str(attribution_env['store'])
    rows = ro.import_hermes(str(attribution_env['hermes']))
    ro.append_rows(store, rows)
    summary = ro.backfill_hermes_profiles(str(store),
                                          str(attribution_env['hermes']))
    assert summary.get('changed') == 0   # fresh rows already carry profiles
    # build a pre-attribution store (the historical shape) and backfill it
    old_store = tmp_path / 'old-store.jsonl'
    with open(old_store, 'w') as f:
        for r in rows:
            r2 = dict(r)
            r2['profile_id'] = None
            f.write(json.dumps(r2, ensure_ascii=False) + '\n')
    summary = ro.backfill_hermes_profiles(str(old_store),
                                          str(attribution_env['hermes']))
    # 3 attributed rows (unknown-project's row stays NULL — honest unknown)
    assert summary.get('changed') == 3
    after = [json.loads(l) for l in open(old_store) if l.strip()]
    by_sid = {r['session_id']: r for r in after}
    assert by_sid['sess-coding']['profile_id'] == 'P1_CODING'
    assert by_sid['sess-unknown']['profile_id'] is None
    # idempotent re-run: nothing left to change
    again = ro.backfill_hermes_profiles(str(old_store),
                                        str(attribution_env['hermes']))
    assert again.get('changed') == 0
    assert again.get('corrected') == 0


def test_backfill_corrects_the_undeclared_inference_default(attribution_env,
                                                            tmp_path):
    """The proxy stamps ROUTER_INFER_PROFILE ('P0_FORE') as a FALLBACK on
    proxied spawn traffic. The tick's DECLARED project profile is
    authoritative: a P0_FORE stamp on a session attributed to a P8_SYNC
    tick is corrected; a P0_FORE stamp on a genuinely-P0_FORE session is
    left alone; a stamp on an unattributed session is left alone."""
    store = tmp_path / 'infer-default.jsonl'
    rows = ro.import_hermes(str(attribution_env['hermes']))
    with open(store, 'w') as f:
        for sid in ('sess-coding', 'sess-docs', 'sess-unknown'):
            r = dict(next(x for x in rows if x['session_id'] == sid))
            r['profile_id'] = 'P0_FORE'     # the proxy fallback stamp
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    summary = ro.backfill_hermes_profiles(str(store),
                                          str(attribution_env['hermes']))
    after = [json.loads(l) for l in open(store) if l.strip()]
    by_sid = {r['session_id']: r for r in after}
    assert by_sid['sess-coding']['profile_id'] == 'P1_CODING'
    assert by_sid['sess-docs']['profile_id'] == 'P3_DOCS'
    assert by_sid['sess-unknown']['profile_id'] == 'P0_FORE'  # untouched
    assert summary.get('corrected', 0) >= 2


def test_backfill_correction_matches_a_true_p0_session_only_when_equal(
        attribution_env, tmp_path):
    """The lore-foreman tick IS P0_FORE: its stamped rows are a no-op, not
    a correction — pinned so the correction arm cannot drift into always
    rewriting."""
    rows = ro.import_hermes(str(attribution_env['hermes']))
    assert rows[0]['profile_id'] == 'P1_CODING'   # fixture sanity


def test_sessions_profile_name_is_never_a_source(attribution_env, tmp_path):
    """sessions.profile_name='default' is the HERMES config profile — using
    it as the router profile would stamp P3_DOCS/P8_SYNC traffic as
    P0_FORE. The attribution map must carry ONLY tick-declared sessions."""
    import sqlite3
    hermes = attribution_env['hermes']
    db = sqlite3.connect(hermes)
    db.execute('CREATE TABLE IF NOT EXISTS sessions ('
               'id TEXT PRIMARY KEY, session_key TEXT, profile_name TEXT)')
    # every fixture session gets profile_name='default' (hermes reality),
    # including sess-unknown (which has NO tick declaration)
    for sid in ('sess-coding', 'sess-docs', 'sess-unknown', 'sess-older'):
        db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?)',
                   (sid + '-uuid', sid, 'default'))
    db.commit()
    db.close()
    mp = ro._session_profiles(hermes)
    assert mp.get('sess-coding') == 'P1_CODING'
    assert mp.get('sess-docs') == 'P3_DOCS'
    assert mp.get('sess-unknown') is None, (
        "profile_name='default' must not invent P0_FORE for a session no "
        "tick declared")


def test_backfill_never_touches_a_signed_row(attribution_env, tmp_path):
    """A row that already carries a complexity_sig (classifier output) keeps
    its key untouched — the profile stamp is additive, never overriding."""
    store = tmp_path / 'mixed.jsonl'
    rows = ro.import_hermes(str(attribution_env['hermes']))
    with open(store, 'w') as f:
        r = dict(rows[0])
        r['session_id'] = 'sess-coding'
        r['complexity_sig'] = 'classifier-hash'
        r['profile_id'] = None
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
    ro.backfill_hermes_profiles(str(store), str(attribution_env['hermes']))
    after = [json.loads(l) for l in open(store) if l.strip()]
    assert after[0]['complexity_sig'] == 'classifier-hash'
    assert after[0]['profile_id'] is None
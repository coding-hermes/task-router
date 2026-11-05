"""TR-388 — the Redis read plane's hardening battery (SPEC-REDIS-READ-PLANE v0.5).

Coverage map (R49): every test names a normative statement. The live-instance
tests skip with an explicit reason when no Redis answers on the configured port
(R64: a skip is never counted as a pass).
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import router_redis as rr  # noqa: E402

PY = sys.executable
SECRET = Path(os.path.expanduser('~/.hermes/secrets/router-redis.pass'))
PORT = int(os.environ.get('ROUTING_REDIS_PORT', '6391'))


# --------------------------------------------------------------------- helpers
def _redis_reachable() -> tuple:
    """(ok, reason) — never silently treat 'could not connect' as a pass."""
    if not SECRET.exists():
        return False, 'no AUTH secret file (~/.hermes/secrets/router-redis.pass)'
    try:
        s = socket.create_connection(('127.0.0.1', PORT), timeout=2)
    except OSError as exc:
        return False, f'no listener on 127.0.0.1:{PORT}: {exc}'
    s.close()
    return True, ''


LIVE, LIVE_REASON = _redis_reachable()
live = pytest.mark.skipif(not LIVE, reason=f'live Redis unavailable: {LIVE_REASON}')


@pytest.fixture()
def scratch_home(tmp_path, monkeypatch):
    """An isolated data dir = an isolated scope (R8), so tests never touch prod."""
    for name in rr.CACHED_TABLES:
        src = REPO / 'data' / 'tables' / f'{name}.jsonl'
        if src.exists():
            shutil.copy(src, tmp_path / f'{name}.jsonl')
    monkeypatch.setenv('ROUTING_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('ROUTING_REDIS_ALLOW_SCRATCH', '1')
    monkeypatch.setenv('ROUTING_REDIS', '0')  # the code-level default (R78)
    yield tmp_path
    # best-effort scope cleanup so repeated runs do not accumulate generations
    try:
        scope = rr.scope_id(str(tmp_path), rr.REGISTRY)
        conn = rr.RedisConn(timeout_ms=2000).connect()
        rr._drop_prefix(conn, f'{scope}:g')
        rr._drop_prefix(conn, f'{scope}:meta:')
        conn.close()
    except Exception:  # noqa: BLE001 - cleanup must never fail a test
        pass


# ------------------------------------------------------- offline: identity (R9)
def test_hash_preserves_multiplicity(tmp_path):
    path = tmp_path / 'models.jsonl'
    path.write_text('{"a":1}\n{"a":2}\n')
    first, count = rr.table_fingerprint(str(path))
    assert count == 2
    # identical duplicate: a sorted SET would ignore it; the hash must not
    path.write_text('{"a":1}\n{"a":2}\n{"a":2}\n')
    second, count2 = rr.table_fingerprint(str(path))
    assert count2 == 3 and second != first


def test_hash_is_order_insensitive(tmp_path):
    a = tmp_path / 'a.jsonl'
    b = tmp_path / 'b.jsonl'
    a.write_text('{"x":1}\n{"x":2}\n')
    b.write_text('{"x":2}\n\n{"x":1}\n')  # reordered + blank line
    assert rr.table_fingerprint(str(a)) == rr.table_fingerprint(str(b))


def test_table_set_is_exactly_the_twelve_and_not_cached_is_computed(tmp_path):
    for name in rr.CACHED_TABLES:
        (tmp_path / f'{name}.jsonl').write_text('{"k":1}\n')
    (tmp_path / 'model_notes.jsonl').write_text('{"n":1}\n')       # not cached
    (tmp_path / 'probe_gaps.jsonl').write_text('{"g":1}\n')        # not cached
    stamp = rr.jsonl_stamp(str(tmp_path), str(tmp_path / 'registry.json'))
    assert len(rr.CACHED_TABLES) == 12
    assert set(stamp['tables']) == set(rr.CACHED_TABLES)
    assert stamp['not_cached'] == ['model_notes', 'probe_gaps']


def test_stamp_equality_ignores_not_cached(tmp_path):
    for name in rr.CACHED_TABLES:
        (tmp_path / f'{name}.jsonl').write_text('{"k":1}\n')
    a = rr.jsonl_stamp(str(tmp_path), str(tmp_path / 'registry.json'))
    (tmp_path / 'brand_new_sidecar.jsonl').write_text('{"z":1}\n')
    b = rr.jsonl_stamp(str(tmp_path), str(tmp_path / 'registry.json'))
    assert a['not_cached'] != b['not_cached']
    same, drift = rr.stamp_equality(a, b)
    assert same and drift == []


def test_scope_hashes_both_resolved_paths(tmp_path):
    d1 = tmp_path / 'd1'
    d1.mkdir()
    reg_a = tmp_path / 'a.json'
    reg_b = tmp_path / 'b.json'
    same_dir_diff_registry = (rr.scope_id(str(d1), str(reg_a)), rr.scope_id(str(d1), str(reg_b)))
    assert same_dir_diff_registry[0] != same_dir_diff_registry[1], \
        'R8: a run that overrides only ROUTING_REGISTRY must not share the plane'


def test_no_third_party_imports():
    """R39/R40: scripts stay stdlib-only so the bare board venv can run them."""
    tree = ast.parse((SCRIPTS / 'router_redis.py').read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    third_party = {n for n in names
                   if n not in sys.stdlib_module_names and n not in ('registry_path',)}
    assert not third_party, f'non-stdlib imports in router_redis.py: {sorted(third_party)}'


def test_registered_in_the_copy_loop_not_the_symlink_loop():
    """R41: imported modules must ride the byte-copy list (TR-288)."""
    text = (SCRIPTS / 'sync_runtime.sh').read_text()
    copy_loop = text.split('for f in provider_health_probe.py', 1)[1].split('; do', 1)[0]
    assert 'router_redis.py' in copy_loop


def test_port_denylist_refuses_product_instances():
    for port in (6379, 6380, 16379):
        conn = rr.RedisConn(url=f'redis://127.0.0.1:{port}/0')
        with pytest.raises(rr.RedisError):
            conn.assert_allowed()


def test_auth_is_required(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_REDIS_PASSWORD_FILE', str(tmp_path / 'absent.pass'))
    conn = rr.RedisConn(url='redis://127.0.0.1:6391/0')
    with pytest.raises(rr.RedisError, match='AUTH'):
        conn.connect()


# ------------------------------------------------------ offline: row identity
def test_row_keys_cover_every_cached_table():
    assert set(rr.ROW_KEYS) == set(rr.CACHED_TABLES)


def test_category_values_carry_the_chain_sort_columns():
    tables = rr.load_tables(str(REPO / 'data' / 'tables'))
    values = rr.build_category_values(tables)
    needed = {'level', 'plan_tier', 'normalized_price', 'token_factor',
              'context_limit', 'archive', 'valid_to'}
    sample = next(iter(values.values()))
    payload = next(iter(sample.values()))
    assert needed <= set(payload), f'R34: missing {needed - set(payload)}'


# ------------------------------------------------------------- live: behaviour
@live
def test_publish_then_status_matches(scratch_home):
    assert rr.verb_load() == 0
    result = rr.status()
    assert result['state'] in ('UNSTAMPED', 'MATCH'), result
    assert result['exit'] == 0
    assert result['cache']['tables'] == result['jsonl']['tables']


@live
def test_one_field_change_is_stale_and_the_diff_names_it(scratch_home):
    assert rr.verb_load() == 0
    path = scratch_home / 'models.jsonl'
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for row in rows:
        if row.get('provider') == 'clinepass' and row.get('model') == 'glm-5.3':
            row['normalized_price'] = round((row.get('normalized_price') or 0) + 1, 4)
            break
    else:
        pytest.skip('fixture lane missing')
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
    result = rr.status(diff=True)
    assert result['exit'] == 1 and result['drift_class'] == 'cache-behind'
    assert 'models' in result['drifted_tables']
    diff = result['diff']
    assert diff['models']['changed'] == 1
    assert diff['models']['added'] == 0 and diff['models']['removed'] == 0
    others = {k: v for k, v in diff.items() if k != 'models'}
    assert all(v['changed'] == 0 and v['added'] == 0 and v['removed'] == 0
               for v in others.values()), f'false diff on untouched tables: {others}'


@live
def test_pointer_never_regresses_under_two_publishers(scratch_home):
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    assert rr.verb_load() == 0
    conn = rr.RedisConn(timeout_ms=5000).connect()
    before = json.loads(conn.cmd('GET', f'{scope}:meta:version'))
    # simulate the older publisher finishing late: its CAS must abort
    stale = dict(before, generation=before['generation'] - 5, created_at=rr._now())
    cur_gen = int(before['generation'])
    conn.cmd('WATCH', f'{scope}:meta:version')
    conn.cmd('UNWATCH')
    conn.run_multi([['SET', f'{scope}:meta:version', json.dumps(stale)]])
    after = json.loads(conn.cmd('GET', f'{scope}:meta:version'))
    conn.close()
    assert int(after['generation']) == int(stale['generation'])  # raw SET took effect
    # …so the guard has to live in the publisher: a lower generation must abort
    receipt = {'published': False}
    conn = rr.RedisConn(timeout_ms=5000).connect()
    tables = rr.load_tables(str(scratch_home))
    ref = rr.jsonl_stamp(str(scratch_home), rr.REGISTRY)
    receipt = rr.publish(tables, ref, scope, conn)
    conn.close()
    assert receipt['published'] is True, 'a higher generation must publish'
    assert receipt['gen'] > cur_gen


@live
def test_crashed_publish_orphan_is_reaped(scratch_home):
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    assert rr.verb_load() == 0
    conn = rr.RedisConn(timeout_ms=5000).connect()
    cur = json.loads(conn.cmd('GET', f'{scope}:meta:version'))
    orphan_gen = int(cur['generation']) + 9
    conn.cmd('SET', f'{scope}:g{orphan_gen}:__ts', rr._now() - 3600)  # old, unpublished
    conn.cmd('SET', f'{scope}:g{orphan_gen}:junk', 'x')
    conn.close()
    assert rr.verb_load() == 0  # any load reaps
    conn = rr.RedisConn(timeout_ms=5000).connect()
    assert conn.cmd('EXISTS', f'{scope}:g{orphan_gen}:junk') == 0
    conn.close()


@live
def test_ghost_state_when_pointer_names_a_missing_generation(scratch_home):
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    assert rr.verb_load() == 0
    conn = rr.RedisConn(timeout_ms=5000).connect()
    stamp = json.loads(conn.cmd('GET', f'{scope}:meta:version'))
    conn.cmd('DEL', f"{scope}:g{stamp['generation']}:__ts")
    conn.close()
    result = rr.status()
    assert result['state'] == 'GHOST' and result['exit'] == 4


@live
def test_foreign_instance_is_refused(scratch_home):
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    os.makedirs(rr.STATE_DIR, exist_ok=True)
    backup = None
    if os.path.exists(rr.INSTANCE_FILE):
        backup = Path(rr.INSTANCE_FILE).read_text()
    try:
        Path(rr.INSTANCE_FILE).write_text(json.dumps({'run_id': 'not-the-live-one'}))
        conn = rr.RedisConn(timeout_ms=5000).connect()
        conn.cmd('SET', f'{scope}:stranger', '1')  # keys exist, no owner key
        conn.close()
        rr.RedisConn(timeout_ms=5000).connect().cmd('DEL', f'{scope}:meta:owner')
        result = rr.status()
        assert result['state'] == 'FOREIGN' and result['exit'] == 5
    finally:
        if backup is not None:
            Path(rr.INSTANCE_FILE).write_text(backup)
        else:
            try:
                os.unlink(rr.INSTANCE_FILE)
            except OSError:
                pass


@live
def test_round_trip_budget_for_a_category_scan(scratch_home):
    assert rr.verb_load() == 0
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    conn = rr.RedisConn(timeout_ms=5000).connect()
    gen = int(json.loads(conn.cmd('GET', f'{scope}:meta:version'))['generation'])
    before = conn.commands
    payload = conn.cmd('HGETALL', f'{scope}:g{gen}:cat:code_gen')
    after = conn.commands
    conn.close()
    assert after - before == 1, 'R36/R37: a category scan is ONE round trip'
    assert len(rr._hash_pairs(payload)) > 100


@live
def test_full_row_parity_for_every_cached_table(scratch_home):
    assert rr.verb_load() == 0
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    conn = rr.RedisConn(timeout_ms=5000).connect()
    gen = int(json.loads(conn.cmd('GET', f'{scope}:meta:version'))['generation'])
    mismatches = {}
    for name in rr.CACHED_TABLES:
        local = rr.read_table(name, str(scratch_home))
        remote = rr.rows_from_cache(conn, scope, gen, name)
        lmap = {rr.row_pk(name, r): r for r in local}
        rmap = {rr.row_pk(name, r): r for r in remote}
        if set(lmap) != set(rmap):
            mismatches[name] = ('keys', len(lmap), len(rmap))
            continue
        bad = [k for k in lmap if lmap[k] != rmap[k]]
        if bad:
            mismatches[name] = ('values', len(bad), bad[0])
    conn.close()
    assert not mismatches, f'R31 parity broken: {mismatches}'


@live
def test_load_is_idempotent_when_fresh(scratch_home, capsys):
    assert rr.verb_load() == 0
    out = capsys.readouterr().out
    assert 'fresh' in out or 'cache:' in out
    assert rr.verb_load(if_stale=True) == 0
    second = capsys.readouterr().out
    assert 'nothing to publish' in second, second


@live
def test_cli_verbs_dispatch(capsys):
    rc = rr.main(['status', '--state-only'])
    assert rc in (0, 1, 2, 3, 4, 5, 6)


@live
def test_get_miss_exits_non_zero(scratch_home, capsys):
    """A lookup that finds nothing must not exit 0 (unrepresentable-failure class).

    `get` is the verb a cron/CI reads to prove the plane answers. Printing `null`
    with rc=0 makes a miss indistinguishable from a hit.
    """
    assert rr.verb_load() == 0
    assert rr.main(['get', 'model', 'nope/nope']) == 1
    out = capsys.readouterr().out
    assert '"found": false' in out, out


def test_cli_exposes_the_redis_verb():
    """R74: `router redis ...` must dispatch through the real CLI entry point."""
    from task_router import cli
    assert cli.COMMANDS.get('redis') == 'router_redis.py'
    exports = cli._home_env_exports()
    assert 'ROUTING_DATA_DIR' in (exports.get('redis') or {})
    # ROUTING_REGISTRY deliberately not exported (TR-REV-1012-1, same as siblings)
    assert 'ROUTING_REGISTRY' not in (exports.get('redis') or {})


@live
def test_publish_arms_the_scope_heartbeat(scratch_home):
    """R8: the heartbeat must be CREATED (SET ... EX), not just EXPIREd.

    The stage-1 code issued EXPIRE on a key that was never set, so scope expiry
    was inert and dead scopes were immortal (measured: 3 scopes / 120,939 keys /
    116 MiB with the 256 MiB cap already touched).
    """
    assert rr.verb_load() == 0
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    conn = rr.RedisConn(timeout_ms=5000).connect()
    try:
        ttl = conn.cmd('TTL', f'{scope}:meta:heartbeat')
        assert isinstance(ttl, int) and ttl > 0, f'heartbeat TTL must be positive, got {ttl}'
    finally:
        conn.close()


@live
def test_sweep_drops_a_dead_scope_and_spares_the_current_one(scratch_home):
    """R8: a scope with no heartbeat and an old pointer is collected; ours is not."""
    import time
    assert rr.verb_load() == 0
    scope = rr.scope_id(str(scratch_home), rr.REGISTRY)
    conn = rr.RedisConn(timeout_ms=5000).connect()
    try:
        dead = 'deadbeef0001'
        conn.cmd('SET', f'{dead}:meta:owner', rr._j({'owner': rr.OWNER_ID, 'schema': rr.SCHEMA}))
        conn.cmd('SET', f'{dead}:meta:version',
                 rr._j({'generation': 1, 'created_at': int(time.time()) - 86400}))
        conn.cmd('HSET', f'{dead}:g1:model:x/y', 'provider', 'x')
        dropped, keys = rr.sweep_scopes(conn, keep_scope=scope)
        assert dropped >= 1, 'the dead scope must be collected'
        assert keys >= 1
        assert conn.cmd('EXISTS', f'{dead}:meta:owner') == 0
        assert conn.cmd('EXISTS', f'{scope}:meta:version') == 1, 'never sweep the current scope'
    finally:
        conn.close()

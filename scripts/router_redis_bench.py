#!/usr/bin/env python3
"""router_redis_bench.py — R50/R51/R52 measurements for the Redis read plane.

The spec's whole value claim is bounded by one question: how much of a cold
`router_spawn` actually is the registry read, and how much would a Redis arm
remove? This harness answers it with measurements instead of assertions, and emits
the numbers the row's evidence needs.

It measures, in one process (so comparisons are like-for-like):
  1. interpreter + import cost      — the floor nobody can remove
  2. file path                      — _load_registry_with_meta() (TR-277 cache off)
  3. redis path                     — connect + pointer + profile requirement scan
                                      + one pipelined category batch (the R36 shape)
  4. resolve() end to end           — with each arm, same project
  5. the file path's share          — (2)/(4), the ceiling a read plane can remove

R52's stop rule: if (cold resolve - floor) < 10 %, a shipped `redis` default is not
justified and the row says so. `--json` prints everything for the board evidence.

Usage:
  python3 scripts/router_redis_bench.py
  python3 scripts/router_redis_bench.py --project coding-hermes-scheduler --json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

try:
    import router_redis as rr
except Exception as exc:  # pragma: no cover
    print(f'bench: cannot import router_redis: {type(exc).__name__}: {exc}', file=sys.stderr)
    raise SystemExit(2)


def _ms(fn, runs: int = 5) -> dict:
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {'runs': runs, 'median_ms': round(statistics.median(samples), 2),
            'min_ms': round(min(samples), 2), 'max_ms': round(max(samples), 2)}


def _cold_cli(script: str, args: list, env_extra: dict, runs: int = 3) -> dict:
    """A true cold process: new interpreter, new module imports, real CLI."""
    samples = []
    for _ in range(runs):
        env = dict(os.environ, **env_extra)
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, os.path.join(_HERE, script), *args],
                              capture_output=True, env=env, cwd=_REPO)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if proc.returncode != 0:
            raise SystemExit(f'cold run failed ({script} {" ".join(args)}): '
                             f'{proc.stderr.decode()[:300]}')
        samples.append(elapsed)
    return {'runs': runs, 'median_ms': round(statistics.median(samples), 2),
            'min_ms': round(min(samples), 2), 'max_ms': round(max(samples), 2)}


def _internal(which: str, profile: str) -> int:
    """One arm per process: the FIRST call is cold, the rest are warm.

    Comparing a warm in-process cache against a network round trip is how a
    benchmark lies. Each arm is therefore measured in its own interpreter, and the
    cold number is reported beside the warm one.
    """
    import time as _t
    if which == 'file':
        import router_spawn as rs
        samples = []
        for _ in range(6):
            t0 = _t.perf_counter()
            rs._load_registry_with_meta()
            samples.append((_t.perf_counter() - t0) * 1000.0)
        out = {'arm': 'file', 'cold_ms': round(samples[0], 2),
               'warm_ms': round(statistics.median(samples[1:]), 2),
               'warm_all_ms': [round(x, 2) for x in samples[1:]]}
    elif which == 'redis':
        samples = []
        detail = {}
        for i in range(6):
            t0 = _t.perf_counter()
            detail = redis_arm_cost(profile)
            samples.append((_t.perf_counter() - t0) * 1000.0)
        out = {'arm': 'redis', 'cold_ms': round(samples[0], 2),
               'warm_ms': round(statistics.median(samples[1:]), 2),
               'warm_all_ms': [round(x, 2) for x in samples[1:]], 'detail': detail}
    else:
        raise SystemExit(f'unknown internal arm {which}')
    print(json.dumps(out, sort_keys=True))
    return 0


def redis_arm_cost(profile: str = 'P0_FORE') -> dict:
    """The R36 read shape: pointer, project/profile, one pipelined category batch."""
    data_dir = rr.current_data_dir()
    reqs = []
    for row in rr.read_table('task_profile_requirements', data_dir):
        if row.get('task_id') == profile and row.get('category'):
            reqs.append(row.get('category'))
    scope = rr.scope_id()
    conn = rr.RedisConn(timeout_ms=2000).connect()
    try:
        t0 = time.perf_counter()
        raw = conn.cmd('GET', f'{scope}:meta:version')
        if not raw:
            raise SystemExit('bench: no cache published (run router_redis.py load)')
        gen = int((json.loads(raw) or {}).get('generation') or 0)
        t_pointer = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        conn.cmd('HGETALL', f'{scope}:g{gen}:profile:{profile}:req')
        t_profile = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        payloads = conn.pipeline([['HGETALL', f'{scope}:g{gen}:cat:{cat}'] for cat in reqs])
        t_batch = (time.perf_counter() - t0) * 1000.0
        lanes = sum(len(rr._hash_pairs(p)) for p in payloads)

        t0 = time.perf_counter()
        _ = [rr._un(p) for p in payloads]
        t_parse = (time.perf_counter() - t0) * 1000.0
        info = dict(line.split(':', 1) for line in str(conn.cmd('INFO', 'memory')).splitlines()
                    if ':' in line)
    finally:
        conn.close()
    return {'profile': profile, 'categories': len(reqs), 'lanes': lanes,
            'round_trips': 1 + 1 + 1,
            'pointer_ms': round(t_pointer, 2), 'profile_ms': round(t_profile, 2),
            'category_batch_ms': round(t_batch, 2), 'parse_ms': round(t_parse, 2),
            'total_ms': round(t_pointer + t_profile + t_batch + t_parse, 2),
            'generation': gen,
            'used_memory_human': info.get('used_memory_human'),
            'maxmemory_human': info.get('maxmemory_human')}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description='R50/R51/R52 measurements for the Redis read plane')
    ap.add_argument('--project', default='coding-hermes-scheduler')
    ap.add_argument('--profile', default='P0_FORE')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--internal', default=None, choices=['file', 'redis'],
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.internal:
        return _internal(args.internal, args.profile)

    import router_spawn as rs  # imported inside main so the import cost is measurable separately

    def _arm(which: str, env_extra: dict | None = None) -> dict:
        env = dict(os.environ, **(env_extra or {}))
        out = subprocess.run([sys.executable, os.path.abspath(__file__),
                              '--internal', which, '--profile', args.profile],
                             capture_output=True, env=env, cwd=_REPO)
        if out.returncode != 0:
            raise SystemExit(f'arm {which} failed: {out.stderr.decode()[:300]}')
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    file_arm_raw = _arm('file', {'ROUTER_CACHE': '0'})
    redis_arm_raw = _arm('redis')
    file_arm = {'cold_ms': file_arm_raw['cold_ms'], 'warm_ms': file_arm_raw['warm_ms']}
    redis_arm = dict(redis_arm_raw['detail'])
    redis_arm['cold_ms'] = redis_arm_raw['cold_ms']
    redis_arm['warm_ms'] = redis_arm_raw['warm_ms']

    resolve_file = _ms(lambda: rs.resolve(project=args.project), runs=3)
    cold_file = _cold_cli('router_spawn.py', [args.project, '--format', 'json'],
                          {'ROUTER_CACHE': '0'})
    cold_parse_only = _cold_cli('router_spawn.py', ['--help'], {})
    cold_get = _cold_cli('router_redis.py', ['get', 'model', 'zai-glm/glm-5.3'], {})

    share = round(file_arm['cold_ms'] / resolve_file['median_ms'] * 100.0, 1)
    # The like-for-like comparison is COLD vs COLD: both arms pay their own setup.
    ceiling = round(file_arm['cold_ms'] - redis_arm['cold_ms'], 2)
    ceiling_pct = round(ceiling / resolve_file['median_ms'] * 100.0, 1)
    report = {
        'file_arm_load_registry': file_arm,
        'redis_arm': redis_arm,
        'resolve_in_process_file_arm': resolve_file,
        'cold_cli': {'resolve_ROUTER_CACHE_0': cold_file,
                     'argparse_only_no_registry': cold_parse_only,
                     'redis_get_cold_process': cold_get},
        'file_share_of_resolve_pct': share,
        'best_case_saving_ms': ceiling,
        'best_case_saving_pct': ceiling_pct,
        'r52_stop_rule': ('file path is fast enough — ship file as default'
                          if ceiling_pct < 10.0 else 'wiring justified'),
        'project': args.project,
    }
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0

    print(f"file arm   cold {file_arm['cold_ms']} ms | warm {file_arm['warm_ms']} ms (TR-277)")
    print(f"redis arm   cold {redis_arm['cold_ms']} ms | warm {redis_arm['warm_ms']} ms "
          f"(pointer {redis_arm['pointer_ms']}, profile {redis_arm['profile_ms']}, "
          f"{redis_arm['categories']} category scans {redis_arm['category_batch_ms']}, "
          f"parse {redis_arm['parse_ms']}; round_trips={redis_arm['round_trips']}, "
          f"lanes={redis_arm['lanes']})")
    print(f"redis arm ({redis_arm['profile']}): {redis_arm['total_ms']} ms "
          f"[pointer {redis_arm['pointer_ms']} + profile {redis_arm['profile_ms']} + "
          f"{redis_arm['categories']} category scans {redis_arm['category_batch_ms']} + "
          f"parse {redis_arm['parse_ms']}] round_trips={redis_arm['round_trips']} "
          f"lanes={redis_arm['lanes']}")
    print(f"resolve() in process, file arm: {resolve_file['median_ms']} ms "
          f"-> the file read is {share}% of it")
    print(f"cold CLI resolve (ROUTER_CACHE=0): {cold_file['median_ms']} ms | "
          f"argparse-only floor: {cold_parse_only['median_ms']} ms | "
          f"cold redis get: {cold_get['median_ms']} ms")
    print(f"best case saving: {ceiling} ms ({ceiling_pct}%) "
          f"-> R52: {report['r52_stop_rule']}")
    print(f"memory: {redis_arm['used_memory_human']} used of {redis_arm['maxmemory_human']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

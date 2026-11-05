#!/usr/bin/env python3
"""router_redis_parity.py — prove the Redis category scans say what the file path says.

TR-388 stage-2 gate (spec R31/R34/R48): before any resolver reads from the cache,
the cache's decision tuples must equal the tuples an independent derivation from
`data/tables/*.jsonl` produces. This tool derives them from the raw tables WITHOUT
calling the publish helper (so it cannot agree with a bug by construction), then
compares against the live `<scope>:g<N>:cat:<category>` hashes.

Why it exists: the naive wiring — reconstruct all twelve tables from Redis and hand
them to the resolver — measured ~2.35 s against ~45 ms to parse registry.json. The
only shape that can win is the category scan, which makes `cat:*` the load-bearing
key family. If those values are wrong or provider-less, the resolver would route on
garbage while every "cache is fresh" check still passed.

Exit codes: 0 parity, 1 mismatches, 2 cannot run (no cache, no tables), 3 unreachable.

Usage:
  python3 scripts/router_redis_parity.py            # all 24 categories
  python3 scripts/router_redis_parity.py --json     # machine-readable for the row
  python3 scripts/router_redis_parity.py --profile P0_FORE
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)

try:
    import router_redis as rr  # noqa: E402
except Exception as exc:  # pragma: no cover - a broken import must be visible, not silent
    print(f'parity: cannot import router_redis: {type(exc).__name__}: {exc}', file=sys.stderr)
    raise SystemExit(2)

TUPLE_FIELDS = ('level', 'plan_tier', 'normalized_price', 'token_factor',
                'context_limit', 'archive', 'valid_to', 'disabled')


def _norm(value: object) -> object:
    """Compare like with like: None stays None, bools stay bools, numbers as floats."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    try:
        return float(text)
    except ValueError:
        return text


def file_side_tuples(data_dir: str) -> dict:
    """Independent derivation: (category -> {lane -> tuple}) from the raw tables.

    A lane is `provider/model`, because a price/plan/context is a property of the
    LANE, not of the model name: the same model id can be served by several
    providers at different prices, and the chain build sorts those lanes against
    each other.
    """
    models: list = []
    tiers: list = []
    for row in rr.read_table('models', data_dir):
        if row.get('provider') and row.get('model'):
            models.append(row)
    for row in rr.read_table('model_tier', data_dir):
        if row.get('model') and row.get('category'):
            tiers.append(row)

    by_model: dict = {}
    for row in models:
        by_model.setdefault(str(row['model']), []).append(row)

    out: dict = {}
    for tier in tiers:
        cat, model = tier.get('category'), str(tier.get('model'))
        for mrow in by_model.get(model, []):
            lane = f"{mrow.get('provider')}/{mrow.get('model')}"
            out.setdefault(cat, {})[lane] = {
                'level': tier.get('tier'),
                'plan_tier': mrow.get('plan_tier'),
                'normalized_price': mrow.get('normalized_price'),
                'token_factor': mrow.get('token_factor'),
                'context_limit': mrow.get('context_limit'),
                'archive': mrow.get('archive'),
                'valid_to': mrow.get('valid_to'),
                'disabled': mrow.get('disabled'),
            }
    return out


def redis_side_tuples(conn: 'rr.RedisConn', scope: str, gen: int, categories) -> tuple:
    """One HGETALL per category — the exact round-trip shape R36 asks for."""
    prefix = f'{scope}:g{gen}'
    out: dict = {}
    round_trips = 0
    for cat in categories:
        flat = conn.cmd('HGETALL', f'{prefix}:cat:{cat}')
        round_trips += 1
        pairs = rr._hash_pairs(flat)
        out[cat] = {lane: rr._un(payload) for lane, payload in pairs.items()}
    return out, round_trips


def compare(file_side: dict, cache_side: dict, categories) -> dict:
    report: dict = {'categories': {}, 'totals': {
        'missing_lanes': 0, 'extra_lanes': 0, 'field_mismatches': 0, 'lanes_compared': 0}}
    for cat in categories:
        want = file_side.get(cat) or {}
        got = cache_side.get(cat) or {}
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        field_bad = []
        for lane in sorted(set(want) & set(got)):
            wrow, grow = want[lane], got[lane]
            if not isinstance(grow, dict):
                field_bad.append({'lane': lane, 'field': '*', 'why': 'not a hash payload'})
                continue
            for field in TUPLE_FIELDS:
                if _norm(wrow.get(field)) != _norm(grow.get(field)):
                    field_bad.append({'lane': lane, 'field': field,
                                      'file': wrow.get(field), 'cache': grow.get(field)})
        report['categories'][cat] = {
            'lanes_file': len(want), 'lanes_cache': len(got),
            'missing': missing[:5], 'missing_count': len(missing),
            'extra': extra[:5], 'extra_count': len(extra),
            'field_mismatches': field_bad[:5], 'field_mismatch_count': len(field_bad),
        }
        report['totals']['missing_lanes'] += len(missing)
        report['totals']['extra_lanes'] += len(extra)
        report['totals']['field_mismatches'] += len(field_bad)
        report['totals']['lanes_compared'] += len(set(want) & set(got))
    return report


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description='Redis category-scan parity vs the JSONL tables')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--profile', default=None,
                    help='only the categories that profile requires (default: all in model_tier)')
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--registry', default=None)
    args = ap.parse_args(argv)

    data_dir = args.data_dir or rr.current_data_dir()
    registry = args.registry or rr.current_registry()
    scope = rr.scope_id(data_dir, registry)

    file_side = file_side_tuples(data_dir)
    if not file_side:
        print(f'parity: no model_tier rows under {data_dir}', file=sys.stderr)
        return 2

    if args.profile:
        reqs = {r.get('category') for r in rr.read_table('task_profile_requirements', data_dir)
                if r.get('task_id') == args.profile}
        categories = sorted(c for c in reqs if c)
        if not categories:
            print(f'parity: profile {args.profile} declares no requirements', file=sys.stderr)
            return 2
    else:
        categories = sorted(file_side)

    try:
        conn = rr.RedisConn(timeout_ms=5000).connect()
    except Exception as exc:  # noqa: BLE001
        print(f'parity: UNREACHABLE: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 3
    try:
        raw = conn.cmd('GET', f'{scope}:meta:version')
        if not raw:
            print(f'parity: no cache for scope {scope} (run load first)', file=sys.stderr)
            return 2
        gen = int((json.loads(raw) or {}).get('generation') or 0)
        cache_side, round_trips = redis_side_tuples(conn, scope, gen, categories)
    finally:
        conn.close()

    report = compare(file_side, cache_side, categories)
    report['scope'] = scope
    report['generation'] = gen
    report['round_trips'] = round_trips
    report['categories_checked'] = len(categories)
    bad = (report['totals']['missing_lanes'] + report['totals']['extra_lanes']
           + report['totals']['field_mismatches'])

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True, default=str))
        return 0 if bad == 0 else 1

    print(f"parity: scope={scope} g{gen} categories={len(categories)} "
          f"lanes={report['totals']['lanes_compared']} round_trips={round_trips} "
          f"missing={report['totals']['missing_lanes']} "
          f"extra={report['totals']['extra_lanes']} "
          f"fields={report['totals']['field_mismatches']}")
    if bad:
        for cat, entry in sorted(report['categories'].items()):
            if entry['missing_count'] or entry['extra_count'] or entry['field_mismatch_count']:
                bits = []
                if entry['missing_count']:
                    bits.append(f"missing {entry['missing_count']} (e.g. {entry['missing'][:2]})")
                if entry['extra_count']:
                    bits.append(f"extra {entry['extra_count']} (e.g. {entry['extra'][:2]})")
                if entry['field_mismatch_count']:
                    first = entry['field_mismatches'][0]
                    bits.append(f"fields {entry['field_mismatch_count']} "
                                f"(e.g. {first.get('lane')}.{first.get('field')}: "
                                f"file={first.get('file')!r} cache={first.get('cache')!r})")
                print(f'  MISMATCH {cat}: ' + ' | '.join(bits))
        return 1
    print('parity: OK — the cache and the file path agree on every compared lane')
    return 0


if __name__ == '__main__':
    sys.exit(main())

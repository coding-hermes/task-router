#!/usr/bin/env python3
"""Proof table: does the live registry promote the newer model of a family?

Loads the real data/tables/models.jsonl, builds lanes exactly as the resolver
sees them, and prints the winner for each known same-price sibling pair —
sorted with the REAL _legacy_sort_key. Run with --baseline to sort with the OLD
key (no version vector) and diff the two.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rs', ROOT / 'scripts' / 'router_spawn.py')
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

PAIRS = [
    ('deepseek-v4.1-flash', 'deepseek-v4.1-flash-1017'),
    ('deepseek/deepseek-v4.1-flash', 'deepseek/deepseek-v4.1-flash-1017'),
    ('deepseek-v4.1-flash', 'deepseek-v4p1-flash-1017'),
    ('minimax-m2', 'minimax-m2.1'),
    ('gpt-6-sol', 'gpt-6.1-sol'),
    ('claude-sonnet-5', 'claude-sonnet-5.5'),
]

rows = []
with open(ROOT / 'data' / 'tables' / 'models.jsonl') as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass


def live(r):
    return (not r.get('disabled')) and not r.get('archived') and not r.get('valid_to')


def as_lane(r):
    return {
        'model': r.get('model'),
        'provider': r.get('provider'),
        'normalized_price': r.get('normalized_price'),
        'public_in_per_m': r.get('public_in_per_m'),
        'context_limit': r.get('context_limit'),
        'plan_tier': -3,
        '_payg': False,
        '_probe_unprobed': False,
        '_quota_headroom': None,
    }


baseline = '--baseline' in sys.argv
key = rs._legacy_sort_key
if baseline:
    def key(m):
        # the pre-TR-369 ordering: identical minus the version vector
        t = rs._legacy_sort_key(m)
        return t[:-3] + t[-2:]


print(f'sort key: {"BASELINE (pre-TR-369)" if baseline else "CURRENT (TR-369 version vector)"}')
print(f'live lanes in registry: {sum(1 for r in rows if live(r))}')
print()
wins = {}
for old, new in PAIRS:
    lanes = [as_lane(r) for r in rows if live(r) and r.get('model') in (old, new)]
    if len(lanes) < 2:
        print(f'{old!r} vs {new!r}: pair not both live ({len(lanes)} lane(s)) — skipped')
        continue
    lanes.sort(key=key)
    top = lanes[0]
    same_price = {round(float(x['normalized_price']), 4) for x in lanes
                  if x['normalized_price'] is not None}
    tag = 'TIE' if len(same_price) <= 1 else 'prices differ'
    verdict = 'NEWER WINS' if top['model'] == new else ('older wins' if top['model'] == old else top['model'])
    wins[f'{old}|{new}'] = verdict
    print(f'{verdict:<14} [{tag}]  {top["provider"]}:{top["model"]}  '
          f'(prices={sorted(same_price)})')

json.dump(wins, open(f'/tmp/tiebreak_{"baseline" if baseline else "current"}.json', 'w'), indent=1)

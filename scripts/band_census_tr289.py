#!/usr/bin/env python3
"""Offline TR-289 census over an explicit copy of the outcome store.

Refuses the configured live-store path and writes only to stdout. The demand
census uses the latest 400 non-empty router-proxy chains; lane sample counts
use all copied router-proxy outcome rows and resolve's provider/model/band key.
"""
import json
import os
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_outcomes as ro  # noqa: E402

RECENT_CHAIN_LIMIT = 400
SAMPLE_FLOOR = 3


def census(rows, keyfn, label):
    buckets = Counter(key for row in rows if (key := keyfn(row)))
    sizes = list(buckets.values())
    singletons = sum(size == 1 for size in sizes)
    print(f'--- {label}')
    print(f'  rated chains: {sum(sizes)}')
    print(f'  task classes: {len(sizes)}')
    if sizes:
        print(f'  rows per class: median={statistics.median(sizes):.1f} '
              f'mean={sum(sizes) / len(sizes):.2f} max={max(sizes)}')
        print(f'  singleton share: {singletons}/{len(sizes)} = '
              f'{100.0 * singletons / len(sizes):.1f}%')


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: band_census_tr289.py <outcomes-copy.jsonl>')
    src = os.path.realpath(sys.argv[1])
    live = os.path.realpath(ro.outcomes_path())
    if src == live:
        raise SystemExit('refusing the configured live outcome store; pass a copy')

    proxy_rows = []
    lane_samples = Counter()
    store_rows = 0
    proxy_outcomes = 0
    with open(src, encoding='utf-8') as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            store_rows += 1
            band = ro.row_band_key(row)
            provider, model = row.get('provider'), row.get('model')
            if band and provider and model and row.get('cost_usd') is not None:
                lane_samples[(provider, model, band)] += 1
            if row.get('source_system') != 'router-proxy':
                continue
            proxy_outcomes += 1
            chain = row.get('chain')
            if isinstance(chain, list) and chain:
                try:
                    row['_census_ts'] = float(row.get('ts') or 0)
                except (TypeError, ValueError):
                    row['_census_ts'] = 0.0
                proxy_rows.append(row)

    proxy_rows.sort(key=lambda row: row['_census_ts'])
    recent = proxy_rows[-RECENT_CHAIN_LIMIT:]
    print(f'copy: {src}')
    print(f'copied outcome rows: {store_rows}; historical same-band cost samples '
          f'across merged backends: {sum(lane_samples.values())}')
    print(f'router-proxy outcome rows: {proxy_outcomes}; non-empty chains: '
          f'{len(proxy_rows)}; latest window: {len(recent)} chains')
    census(recent, ro.row_complexity_sig, 'BEFORE — exact complexity signature')
    census(recent, ro.row_band_key, f'AFTER — {ro.BAND_VERSION} dominant-category band')

    banded_chains = measured_candidates = passing_chains = 0
    for row in recent:
        band = ro.row_band_key(row)
        if not band:
            continue
        candidates = {(candidate.get('provider'), candidate.get('model'))
                      for candidate in row.get('chain', [])
                      if isinstance(candidate, dict)
                      and candidate.get('provider') and candidate.get('model')}
        if not candidates:
            continue
        banded_chains += 1
        measured = sum(lane_samples[(provider, model, band)] >= SAMPLE_FLOOR
                       for provider, model in candidates)
        measured_candidates += measured
        if measured / len(candidates) >= 0.5:
            passing_chains += 1
    print(f'candidate lanes with >= {SAMPLE_FLOOR} same-band cost samples: '
          f'{measured_candidates}')
    print(f'banded chains with >=50% candidates clearing the sample floor: '
          f'{passing_chains}/{banded_chains}')
    print('Offline sample-gate replay only; it does not prove live ordering changed.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Offline TR-289 census over an explicit copy of the outcome store.

The helper refuses the configured live-store path and writes only to stdout.
Counts mirror resolve-time joins: provider + model + exact signature/band;
source_system is collapsed because the resolver merges backend averages.
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_outcomes as ro  # noqa: E402


def census(rows, keyfn, label):
    buckets = {}
    for r in rows:
        k = (r.get('provider'), r.get('model'), keyfn(r))
        buckets.setdefault(k, []).append(r)
    sizes = [len(v) for v in buckets.values()]
    singletons = sum(1 for s in sizes if s == 1)
    print(f'--- {label}')
    print(f'  buckets: {len(buckets)}')
    if sizes:
        print(f'  rows per bucket: median={statistics.median(sizes):.1f} '
              f'mean={sum(sizes)/len(sizes):.2f} max={max(sizes)}')
        print(f'  singleton share: {singletons}/{len(sizes)} = '
              f'{100.0*singletons/len(sizes):.1f}%')
    return buckets


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: band_census_tr289.py <outcomes-copy.jsonl>')
    src = os.path.realpath(sys.argv[1])
    live = os.path.realpath(ro.outcomes_path())
    if src == live:
        raise SystemExit('refusing the configured live outcome store; pass a copy')
    rows = []
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    print(f'store copy: {src}  rows={len(rows)}')

    # BEFORE: the exact-sig key compute_averages used pre-TR-289
    census(rows, ro.row_complexity_sig, 'BEFORE — exact complexity_sig key')
    # AFTER: the versioned coarse band
    census(rows, ro.row_band_key, 'AFTER — b1 coarse band key (complexity_band)')

    # binding rehearsal: of the task classes seen, how many reach >=3 samples
    # (the MEASURED_MIN_SAMPLES floor) per lane — the offline proxy for
    # 'a repeated task shape finds >=3 samples on >=50% of its chain'.
    for label, keyfn in (('BEFORE', ro.row_complexity_sig), ('AFTER', ro.row_band_key)):
        buckets = {}
        for r in rows:
            k = (r.get('provider'), r.get('model'), keyfn(r))
            buckets[k] = buckets.get(k, 0) + 1
        lanes = {}
        for key4, n in buckets.items():
            provider, model = key4[0], key4[1]
            lanes.setdefault((provider, model), []).append(n)
        with3 = sum(1 for counts in lanes.values()
                    if any(c >= 3 for c in counts))
        print(f'{label}: lanes with >=1 band holding >=3 samples: '
              f'{with3}/{len(lanes)} = {100.0*with3/max(1,len(lanes)):.1f}%')


if __name__ == '__main__':
    main()

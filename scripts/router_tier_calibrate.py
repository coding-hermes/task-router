#!/usr/bin/env python3
"""Recalibrate the FROZEN tier ladder (data/tables/tier_ladder.jsonl).

Why this tool exists: `router_seed.py` derives `model_tier` from ABSOLUTE score
bands read out of `data/tables/tier_ladder.jsonl` (section 1c of the seed), not
from live pool quantiles. That is what makes a new benchmark row for model A
unable to re-tier model B. The price of a frozen ladder is that it does not
follow the pool by itself — so moving it is a DELIBERATE, reviewed act, and this
is the only supported way to move it.

What it does:
  1. copies the committed data/tables into a scratch dir;
  2. runs the seed there with ROUTING_TIER_LADDER_IGNORE=1, i.e. the pure
     pool-quantile path the seed used before the ladder existed;
  3. keeps the quantile bands for every (category, reasoning_effort) pair whose
     population clears the seed's own MIN_LADDER_FAMILIES floor (a thin
     population collapses every quantile onto one value — freezing that would
     bake a sample artefact into the scale forever);
  4. writes data/tables/tier_ladder.jsonl and prints the band diff plus how many
     models would change tier, so the commit message can carry the blast radius.

Usage:
  python3 scripts/router_tier_calibrate.py            # dry run: print the diff
  python3 scripts/router_tier_calibrate.py --write    # rewrite the ladder file
  python3 scripts/router_tier_calibrate.py --write --include-efforts
        # also freeze per-effort ladders (default: only pairs that clear the
        # population floor are frozen; thin efforts keep the quantile fallback
        # so they can still switch on as the backfill grows)

Exit codes: 0 ok (including "no change"), 1 the seed failed, 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, 'data', 'tables')
LADDER = os.path.join(DATA_DIR, 'tier_ladder.jsonl')
SEED = os.path.join(REPO, 'scripts', 'router_seed.py')

#: must match router_seed.MIN_LADDER_FAMILIES — a pair below this floor has no
#: population to calibrate against.
MIN_LADDER_FAMILIES = 10


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def _quantile_ladder():
    """Run the seed on a scratch copy with the ladder ignored; return its bands."""
    scratch = tempfile.mkdtemp(prefix='tier-calibrate-')
    try:
        tables = os.path.join(scratch, 'tables')
        shutil.copytree(DATA_DIR, tables)
        env = dict(os.environ,
                   ROUTING_DATA_DIR=tables,
                   ROUTING_REGISTRY=os.path.join(scratch, 'registry.json'),
                   ROUTING_NS=os.path.join(scratch, 'ns'),
                   ROUTING_TIER_LADDER_IGNORE='1',
                   # the satisfiability gate is about routable lanes, not about
                   # the ladder; a calibration run must not be refused for it.
                   ROUTING_ALLOW_UNSATISFIABLE='1')
        p = subprocess.run([sys.executable, SEED], capture_output=True, text=True,
                           env=env, timeout=900)
        if p.returncode != 0:
            sys.stderr.write(p.stdout[-2000:] + '\n' + p.stderr[-2000:] + '\n')
            raise SystemExit(1)
        levels = _read_jsonl(os.path.join(tables, 'category_levels.jsonl'))
        perf = _read_jsonl(os.path.join(tables, 'model_perf.jsonl'))
        return levels, perf
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _populations(perf):
    """{(category, effort): (families, distinct_perfs)} — the seed's own rule."""
    fam = {}
    for r in perf:
        key = (r.get('category'), r.get('reasoning_effort') or 'default')
        fam.setdefault(key, {}).setdefault(r.get('model'), set()).add(r.get('perf'))
    out = {}
    for key, models in fam.items():
        perfs = set()
        for s in models.values():
            perfs |= s
        out[key] = (len(models), len(perfs))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--write', action='store_true',
                    help='rewrite data/tables/tier_ladder.jsonl (default: dry run)')
    ap.add_argument('--include-efforts', action='store_true',
                    help='freeze per-effort ladders too (default: only pairs that '
                         'clear the population floor)')
    ap.add_argument('--json', action='store_true', help='machine-readable summary')
    args = ap.parse_args(argv)

    levels, perf = _quantile_ladder()
    pops = _populations(perf)

    new_rows, skipped = [], []
    _seen_skip = set()
    for r in levels:
        cat = r.get('category')
        eff = r.get('reasoning_effort') or 'default'
        fams, dperfs = pops.get((cat, eff), (0, 0))
        if eff != 'default' and not args.include_efforts:
            if (cat, eff) not in _seen_skip:
                _seen_skip.add((cat, eff))
                skipped.append((cat, eff, fams, dperfs, 'effort ladder not frozen'))
            continue
        if fams < MIN_LADDER_FAMILIES or dperfs < 2:
            if (cat, eff) not in _seen_skip:
                _seen_skip.add((cat, eff))
                skipped.append((cat, eff, fams, dperfs,
                                f'population {fams} families/{dperfs} distinct perfs '
                                f'< floor {MIN_LADDER_FAMILIES}'))
            continue
        new_rows.append({'category': cat, 'reasoning_effort': eff,
                         'level': int(r['level']), 'label': r.get('label') or '',
                         'min_perf': float(r['min_perf'])})
    new_rows.sort(key=lambda r: (r['category'], r['reasoning_effort'], r['level']))

    old_rows = _read_jsonl(LADDER)
    old = {(r['category'], r.get('reasoning_effort') or 'default', int(r['level'])):
           float(r['min_perf']) for r in old_rows}
    new = {(r['category'], r['reasoning_effort'], r['level']): r['min_perf']
           for r in new_rows}

    moved = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a is None or b is None or abs(a - b) > 1e-12:
            moved.append({'category': key[0], 'reasoning_effort': key[1],
                          'level': key[2], 'old': a, 'new': b})

    # blast radius: how many (model, category, effort) tier rows the new bands
    # would move, computed against the CURRENT model_perf pool.
    def _tier(perf_val, bands):
        best = None
        for lvl, mp in bands:
            if mp <= perf_val and (best is None or lvl > best):
                best = lvl
        return best

    old_bands, new_bands = {}, {}
    for (cat, eff, lvl), mp in old.items():
        old_bands.setdefault((cat, eff), []).append((lvl, mp))
    for (cat, eff, lvl), mp in new.items():
        new_bands.setdefault((cat, eff), []).append((lvl, mp))
    changed = 0
    for r in perf:
        key = (r.get('category'), r.get('reasoning_effort') or 'default')
        if key not in new_bands or key not in old_bands:
            continue
        if _tier(float(r['perf']), old_bands[key]) != _tier(float(r['perf']), new_bands[key]):
            changed += 1

    summary = {'ladder_rows_before': len(old_rows), 'ladder_rows_after': len(new_rows),
               'bands_moved': len(moved), 'tier_rows_that_would_move': changed,
               'pairs_skipped': len({(c, e) for c, e, *_ in skipped}),
               'written': False}
    if args.json:
        print(json.dumps({'summary': summary, 'moved': moved[:200],
                          'skipped': [{'category': c, 'reasoning_effort': e,
                                       'families': f, 'distinct_perfs': d,
                                       'why': w} for c, e, f, d, w in skipped[:200]]},
                         indent=1))
    else:
        print(f'ladder rows: {len(old_rows)} -> {len(new_rows)}; '
              f'{len(moved)} band(s) moved; {changed} tier row(s) would move')
        for m in moved[:40]:
            print(f"  {m['category']}@{m['reasoning_effort']} L{m['level']:+d}: "
                  f"{m['old']} -> {m['new']}")
        if len(moved) > 40:
            print(f'  ... {len(moved) - 40} more')
        for c, e, f, d, w in skipped[:10]:
            print(f'  skipped {c}@{e}: {w}')
        if len(skipped) > 10:
            print(f'  ... {len(skipped) - 10} more skipped pair(s)')

    if args.write:
        if not moved and len(old_rows) == len(new_rows):
            print('no change — ladder file left untouched')
        else:
            tmp = LADDER + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                for r in new_rows:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
            os.replace(tmp, LADDER)
            print(f'wrote {LADDER} ({len(new_rows)} rows)')
            summary['written'] = True
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

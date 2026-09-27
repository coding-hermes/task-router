#!/usr/bin/env python3
"""router_rank_audit.py — find forgotten / superseded / unranked models.

Written after the 2026-09-27 audit (Bane): a live, healthy, fully wired model
(muse-spark-1.3) received ZERO fleet sessions because it carried almost no tier
rows, and newer generations across 19 families were ranked WORSE than the older
versions they supersede — so the router kept choosing last generation's ids.

Why this exists as a script and not a habit: the registry is a RELATIVE scoring
system. A model's rank is only correct while it is re-measured against its
current peers, so "keep ranks up to date" is a standing job with two halves —
promote what is new, and demote what has been superseded. Both are invisible
unless someone looks.

Sections
  1 UNRANKED   active, priced lanes with no tier rows (the muse class: routable
               in principle, selectable in practice by nothing)
  2 INVERSIONS the newer version of a family carries fewer tier rows than an
               older ACTIVE sibling — the fleet then routes the OLD model
  3 BIRTHDAYS  rows with no release_date. valid_from is NOT a launch date (this
               database is new, so every row looks recent); without real launch
               days generations cannot be ordered or reasoned about
  4 DECAY      ranked but superseded: the older version is still fully ranked
               while its successor exists on the same provider AND is ranked
               (safe to retire) — or is unranked (rank first, then retire)

Read-only. Exit 0 always (fail-open, like the rest of the router).

Usage:
  ~/.hermes/venvs/board/bin/python3 scripts/router_rank_audit.py [--json]
"""
import argparse
import collections
import json
import os
import re
import sys

DATA_DIR = os.environ.get(
    'ROUTING_DATA_DIR',
    os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'data', 'tables'))


def load(name):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    with open(path, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def state(m):
    if m.get('archive'):
        return 'archived'
    if m.get('valid_to'):
        return 'retired'
    if m.get('disabled'):
        return 'disabled'
    if m.get('normalized_price') is None:
        return 'unpriced'
    return 'active'


def norm_model(name):
    n = str(name).lower().strip().lstrip('~')
    n = re.sub(r'^accounts/fireworks/(models|routers)/', '', n)
    n = re.sub(r'^[a-z0-9_.\-]+/', '', n)
    return re.sub(r':(free|batch|latest)$', '', n)


def version(name):
    """Order key from a version-ish token; snapshot dates fold in so 0731 < 0902.
    None when the id carries no version at all."""
    n = str(name).lower()
    nums = []
    m = re.search(r'v(\d+)(?:[.\-](\d+))?(?:[.\-](\d+))?', n)
    if m:
        nums = [int(x) for x in m.groups() if x]
    else:
        m = re.search(r'(?<![\d.])(\d+)\.(\d+)(?:\.(\d+))?', n)
        if m:
            nums = [int(x) for x in m.groups() if x]
        else:
            # Last resort: BARE MAJOR(S) between separators — gpt-6-luna,
            # minimax-m3, claude-opus-5-5 (collect them all, so a trailing
            # segment still orders 5 < 5-5). Guarded so parameter counts and
            # years cannot masquerade as versions: single digit 1-9, not
            # preceded by a digit/dot, not followed by another digit or a 'b'
            # (8b / 27b / 70b), and not part of a 4-digit year.
            nums = [int(x) for x in re.findall(r'(?<![0-9.])([1-9])(?![0-9bB])', n)[:3]]
    snap = re.search(r'[-_]?(0[1-9]|1[0-2])(\d{2})(?!\d)', n)
    if snap:
        nums += [int(snap.group(1)), int(snap.group(2))]
    return tuple(nums) if nums else None


def family(name):
    n = norm_model(name)
    n = re.sub(r'[-_]?(20\d{2}[-_]?\d{2}[-_]?\d{2})', '', n)
    n = re.sub(r'[-_]?(0[1-9]|1[0-2])\d{2}\b', '', n)
    n = re.sub(r'[-_]?v?\d+(\.\d+)*', '', n)
    n = re.sub(r'[-_]?\d+(\.\d+)?b\b', '', n)
    return re.sub(r'[-_]{2,}', '-', n).strip('-')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    args = ap.parse_args()

    models = load('models')
    tiers = collections.defaultdict(set)
    for r in load('model_tier'):
        tiers[r['model']].add(r['category'])
    total_cats = len({r['category'] for r in load('category_levels')}) or 24

    active = [m for m in models if state(m) == 'active']
    unranked = [m for m in active if not tiers.get(m['model'])]
    thin = [m for m in active if 0 < len(tiers.get(m['model'], set())) < 6]

    # ---- 2 + 4: inversions and decay
    groups = collections.defaultdict(list)
    for m in active:
        groups[(m['provider'], family(m['model']))].append(m)

    inversions, decay_safe, decay_blocked, unorderable = [], [], [], []
    for (prov, fam), ms in groups.items():
        if len(ms) < 2:
            continue
        rows = []
        for m in ms:
            v = version(m['model'])
            rd = m.get('release_date')
            if v is None and not rd:
                continue
            rows.append({'model': m['model'], 'v': v, 'rd': rd,
                         'tiers': len(tiers.get(m['model'], set())),
                         'norm': m.get('normalized_price')})
        if len(rows) < 2:
            continue
        # Ordering basis (Bane 2026-09-27 accuracy rule): compare launch dates
        # when EVERY sibling has one, else version tokens when every sibling has
        # them. MIXING the two is not a comparison — an undated row would sort as
        # '' and read as the oldest/newest at random, which is how this tool
        # mislabelled muse-spark-1.3 as the older sibling of 1.2. When neither
        # basis is complete the family is UNORDERABLE and is reported, not
        # guessed: that is precisely the cost of a missing release_date.
        if all(r['rd'] for r in rows):
            for r in rows:
                r['key'] = (r['rd'],)
        elif all(r['v'] for r in rows):
            for r in rows:
                r['key'] = (r['v'],)
        else:
            unorderable.append({'provider': prov, 'family': fam,
                                'models': [(r['model'], r['tiers']) for r in rows]})
            continue
        rows.sort(key=lambda r: r['key'])
        newest, oldest = rows[-1], rows[0]
        if newest['key'] == oldest['key']:
            continue
        entry = {'provider': prov, 'family': fam,
                 'basis': 'release_date' if all(r['rd'] for r in rows) else 'version',
                 'old': {'model': oldest['model'], 'tiers': oldest['tiers'], 'release_date': oldest['rd']},
                 'new': {'model': newest['model'], 'tiers': newest['tiers'], 'release_date': newest['rd']}}
        if newest['tiers'] < oldest['tiers']:
            inversions.append(entry)
        elif newest['tiers'] >= 6:
            decay_safe.append(entry)      # successor ranked -> retire the old one
        else:
            decay_blocked.append(entry)   # successor unranked -> rank first

    birthdays = [m for m in models if not m.get('release_date')]

    if args.json:
        print(json.dumps({
            'active': len(active), 'unranked': len(unranked), 'thin': len(thin),
            'inversions': len(inversions), 'decay_safe': len(decay_safe),
            'decay_blocked': len(decay_blocked), 'unorderable': len(unorderable),
            'no_release_date': len(birthdays),
        }, indent=1))
        return 0

    print('=' * 74)
    print('1) UNRANKED — active, priced lanes with no tier rows')
    print('=' * 74)
    print(f'   active/priced: {len(active)}   unranked: {len(unranked)}   thin(<6 of {total_cats}): {len(thin)}')
    print('   by provider:', dict(collections.Counter(m['provider'] for m in unranked).most_common(12)))
    print_ = unranked[:12]
    for m in print_:
        print(f"      {m['provider']:14s} {m['model'][:44]:44s} price={m.get('normalized_price')}")

    print()
    print('=' * 74)
    print('2) INVERSIONS — newer version ranked worse than the older it replaces')
    print('=' * 74)
    for e in inversions[:14]:
        print(f"   {e['provider']:14s} {e['family'][:26]:26s}")
        print(f"        OLD {e['old']['model'][:44]:44s} tiers={e['old']['tiers']:3d} rel={e['old']['release_date']}")
        print(f"        NEW {e['new']['model'][:44]:44s} tiers={e['new']['tiers']:3d} rel={e['new']['release_date']}  <-- under-ranked")
    print(f'   total inversion families: {len(inversions)}')

    print()
    print('=' * 74)
    print('3) BIRTHDAYS — rows with no release_date (valid_from is NOT a launch date)')
    print('=' * 74)
    print(f'   rows missing release_date: {len(birthdays)} of {len(models)}')
    print('   by provider:', dict(collections.Counter(m['provider'] for m in birthdays).most_common(10)))

    print()
    print('=' * 74)
    print('4) DECAY — older version still fully ranked')
    print('=' * 74)
    print(f"   successor ranked  -> retire the old one: {len(decay_safe)} families")
    for e in decay_safe[:8]:
        print(f"      {e['provider']:14s} {e['old']['model'][:40]:40s} ({e['old']['tiers']} tiers)"
              f" -> {e['new']['model'][:34]} ({e['new']['tiers']} tiers)")
    print(f"   successor unranked -> RANK FIRST, then retire: {len(decay_blocked)} families")
    for e in decay_blocked[:8]:
        print(f"      {e['provider']:14s} {e['old']['model'][:40]:40s} ({e['old']['tiers']} tiers)"
              f" -> {e['new']['model'][:34]} ({e['new']['tiers']} tiers)")
    print()
    print(f'   UNORDERABLE families (no complete launch-date or version basis — the'
          f' direct cost of a missing release_date): {len(unorderable)}')
    for e in unorderable[:6]:
        print(f"      {e['provider']:14s} {e['family'][:26]:26s} "
              + ', '.join(f'{n}({t}t)' for n, t in e['models'][:3]))
    return 0


if __name__ == '__main__':
    sys.exit(main())

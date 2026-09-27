#!/usr/bin/env python3
"""router_release_backfill.py — turn vendor release rosters into release_date.

Companion to router_rank_audit.py. The audit reports which families cannot be
ordered because the launch dates are missing; this consumes the researched
rosters (one JSON per vendor family) and stamps `models.release_date`, then
proposes — never performs — supersession retirements.

Roster shape (one file per vendor, any filename under the roster dir):
  {"models": [{"vendor": "DeepSeek", "model": "deepseek-v4.1-flash",
              "release_date": "2026-08-14", "supersedes": "deepseek-v4-flash",
              "source": "https://api-docs.deepseek.com/...", "confidence": "high"}],
   "gaps": ["..."]}

Rules (from the model-rank-maintenance skill):
  - a launch date is per MODEL, so one roster entry stamps every provider lane
    carrying those weights
  - never guess: an entry without a parseable ISO date is skipped and reported
  - never silently overwrite: an existing release_date that disagrees is a
    CONFLICT and is reported, not replaced (use --force to override)
  - retirement is PROPOSED only, and only where the successor exists on the SAME
    provider and is itself ranked (rank first, retire second)

Usage:
  ~/.hermes/venvs/board/bin/python3 scripts/router_release_backfill.py \
      [--rosters DIR] [--dry-run|--commit] [--force] [--report PATH]
"""
import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))
DEFAULT_ROSTERS = os.path.expanduser('~/model_bench/rosters')


def load(name):
    with open(os.path.join(DATA_DIR, f'{name}.jsonl'), encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def norm(name):
    n = str(name).lower().strip().lstrip('~')
    n = re.sub(r'^accounts/fireworks/(models|routers)/', '', n)
    n = re.sub(r'^[a-z0-9_.\-]+/', '', n)
    return re.sub(r':(free|batch|latest)$', '', n)


def plausible(d):
    try:
        dt = datetime.date.fromisoformat(d)
    except (TypeError, ValueError):
        return False
    return datetime.date(2025, 1, 1) <= dt <= datetime.date.today() + datetime.timedelta(days=7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rosters', default=DEFAULT_ROSTERS)
    ap.add_argument('--commit', action='store_true', help='write models.jsonl (default: dry run)')
    ap.add_argument('--force', action='store_true', help='override an existing release_date on conflict')
    ap.add_argument('--report', default=os.path.expanduser('~/model_bench/roster_backfill_report.json'))
    args = ap.parse_args()

    if not os.path.isdir(args.rosters):
        print(f'no roster dir at {args.rosters} — nothing to do')
        return 0

    entries, gaps, files = [], [], []
    for fn in sorted(os.listdir(args.rosters)):
        if not fn.endswith('.json'):
            continue
        files.append(fn)
        try:
            doc = json.load(open(os.path.join(args.rosters, fn), encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'WARN unreadable roster {fn}: {exc}')
            continue
        for m in doc.get('models') or []:
            if m.get('model'):
                entries.append(m)
        gaps.extend(doc.get('gaps') or [])

    print(f'rosters read: {len(files)} file(s), {len(entries)} model entries, {len(gaps)} stated gaps')
    if not entries:
        print('no entries — nothing to do')
        return 0

    by_model, bad, supersedes = {}, [], {}
    for e in entries:
        d, name = e.get('release_date'), e['model']
        if not plausible(d):
            bad.append({'model': name, 'release_date': d, 'reason': 'missing or implausible date'})
            continue
        k = norm(name)
        if k in by_model and by_model[k] != d:
            bad.append({'model': name, 'release_date': d, 'reason': f'roster disagrees with {by_model[k]}'})
            continue
        by_model[k] = d
        if e.get('supersedes'):
            supersedes.setdefault(norm(e['supersedes']), set()).add(k)

    models = load('models')
    tiers = collections.defaultdict(set)
    for r in load('model_tier'):
        tiers[r['model']].add(r['category'])

    def active(m):
        return not (m.get('archive') or m.get('valid_to') or m.get('disabled')) and m.get('normalized_price') is not None

    stamped, conflicts, missing = 0, [], collections.Counter()
    for m in models:
        k = norm(m['model'])
        d = by_model.get(k)
        if not d:
            missing[m['provider']] += 1
            continue
        cur = m.get('release_date')
        if cur and cur != d and not args.force:
            conflicts.append({'provider': m['provider'], 'model': m['model'], 'have': cur, 'roster': d})
            continue
        if cur != d:
            stamped += 1
        m['release_date'] = d

    # supersession PROPOSALS (never executed here)
    proposals, blocked = [], []
    seen = set()
    for old_key, new_keys in supersedes.items():
        for m in models:
            if norm(m['model']) != old_key or not active(m):
                continue
            for nk in new_keys:
                # every provider carrying the old model, not just the first one
                # found (an early break here silently produced zero proposals)
                sibs = [s for s in models if norm(s['model']) == nk and s['provider'] == m['provider']]
                for s in sibs:
                    if not active(s):
                        continue
                    key = (m['provider'], m['model'], s['model'])
                    if key in seen:
                        continue
                    seen.add(key)
                    rec = {'provider': m['provider'], 'old': m['model'], 'new': s['model'],
                           'new_ranked': len(tiers.get(s['model'], ())), 'old_ranked': len(tiers.get(m['model'], ()))}
                    (proposals if len(tiers.get(s['model'], ())) >= 6 else blocked).append(rec)

    print(f'\nrows stamped: {stamped}   rows already correct/unchanged: '
          f'{sum(1 for m in models if by_model.get(norm(m["model"])))}')
    print(f'conflicts (existing date disagrees): {len(conflicts)}')
    print(f'roster names matched nothing in the registry: '
          f'{len([1 for k in by_model if not any(norm(m["model"]) == k for m in models)])}')
    print(f'proposed retirements (successor ranked): {len(proposals)}')
    print(f'BLOCKED retirements (rank the successor first): {len(blocked)}')
    for p in proposals[:10]:
        print(f"   {p['provider']:14s} {p['old'][:34]:34s} ({p['old_ranked']}t) -> {p['new'][:30]:30s} ({p['new_ranked']}t)")

    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
              'roster_files': files, 'stamped': stamped, 'conflicts': conflicts,
              'unparseable': bad, 'retire_proposals': proposals, 'retire_blocked': blocked,
              'roster_gaps': gaps, 'rows_without_roster_date_by_provider': dict(missing.most_common(15))}
    with open(args.report, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)
    print('report:', args.report)

    if not args.commit:
        print('\nDRY RUN — nothing written. Re-run with --commit to stamp models.jsonl.')
        return 0

    path = os.path.join(DATA_DIR, 'models.jsonl')
    shutil.copy(path, f'/tmp/models.jsonl.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    with open(path, 'w', encoding='utf-8') as fh:
        for m in models:
            fh.write(json.dumps(m, ensure_ascii=False) + '\n')
    print(f'\nCOMMITTED to working tree: {path} ({len(models)} rows). Re-seed next.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

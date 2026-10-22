#!/usr/bin/env python3
"""router_mirror_account.py — mirror a provider's registry rows onto a second account.

Precedent: xkiro/xkiro-2 is a verbatim copy with only `provider` renamed;
opencode-go-2 drifted (some rows kept first-account prices/evidence). This script
does the xkiro pattern and is idempotent: it only adds models the destination is
missing, so re-running after a catalog refresh is safe.

Usage:
    router_mirror_account.py <src_provider> <dst_provider> [--dry-run|--commit]
    router_mirror_account.py commandcode commandcode-2 --commit

Why: a second paid seat whose key is wired in Hermes config but which has NO
registry rows can never be selected by the router — the seat sits idle while the
fleet hammers lane 1. That was the state of commandcode seat 2 on 2026-09-26.

Writes models.jsonl in the repo's canonical JSON spacing (compact separators
reformat every other row — see the 2026-09-26 muse-code churn incident).
"""
from __future__ import annotations
import argparse
import json
import pathlib
import shutil
import sys
import time

TABLES = pathlib.Path.home() / 'task-router' / 'data' / 'tables'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if not (args.commit ^ args.dry_run):
        ap.error('pass exactly one of --dry-run / --commit')

    p = TABLES / 'models.jsonl'
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    src = [r for r in rows if r['provider'] == args.src]
    have = {r['model'] for r in rows if r['provider'] == args.dst}
    missing = [r for r in src if r['model'] not in have]
    if not src:
        print(f'ERROR: no rows for {args.src}', file=sys.stderr)
        return 2
    if not missing:
        print(f'{args.dst} already mirrors all {len(src)} {args.src} models — nothing to do')
        return 0

    stamp = time.strftime('%Y-%m-%d')
    new = []
    for r in missing:
        c = dict(r)
        c['provider'] = args.dst
        c['price_evidence'] = f'mirror:{r.get("price_evidence") or r.get("provider")}'
        c['lifecycle_source'] = f'mirror-account of {args.src} ({stamp})'
        c['lifecycle_checked_at'] = stamp
        new.append(c)

    print(f'{args.src}: {len(src)} rows | {args.dst}: {len(have)} existing | adding {len(new)}')
    for r in new[:5]:
        print(f"   + {r['model'][:44]:44s} norm={r.get('normalized_price')} disabled={bool(r.get('disabled'))}")
    if args.dry_run:
        print('dry-run: nothing written')
        return 0

    # TR-342: never write to /tmp; use data/state/ with retention
    _STATE_DIR = os.path.join(TABLES.parent, 'state')
    os.makedirs(_STATE_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    _bak = os.path.join(_STATE_DIR, f'models.jsonl.bak-mirror-{ts}')
    shutil.copy(p, _bak)
    # Retention: keep only the 3 newest mirror backups
    max_keep = 3
    all_baks = sorted(
        [f for f in os.listdir(_STATE_DIR) if f.startswith('models.jsonl.bak-mirror-')],
        reverse=True,
    )
    for old in all_baks[max_keep:]:
        os.remove(os.path.join(_STATE_DIR, old))
    p.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows + new) + '\n')
    print(f'wrote {len(new)} rows to {p}')
    print('next: add a plan_terms row for the new account, then router_maintain.py all')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

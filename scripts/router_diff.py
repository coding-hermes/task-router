#!/usr/bin/env python3
"""router_diff.py — chain-snapshot diff between two dates (TR-030).

Answers "what changed in routing since yesterday": head moves per profile,
lanes that appeared/disappeared, and per-lane price deltas, parsed from the
docs/chains-<date>.md snapshots written by router_maintain.py snapshot.

Snapshot format (router_maintain.build_snapshot_text):
  ## <PROFILE> — <title>
  profile: <cat>=<sym> ...
    <hop>. $ <price>/M  <provider>/<model>
Only the `##` and hop lines carry signal here; the header prose and profile
lines are ignored. Duplicate (provider, model) lanes inside one profile
(e.g. tagged variants) are matched positionally: the first occurrence pairs
with the first occurrence.

CLI:
  router_diff.py <date-from> <date-to> [--json]        (dates YYYY-MM-DD)
    --json   pure machine-parseable JSON (default output IS json; the flag
             exists for contract symmetry with the other tools)
    --format json|text (text = compact human summary)
Exit: 0 on success, 2 on missing/unparseable snapshot file (clean error on
stderr, nothing on stdout — a missing file is a usage-level failure, unlike
the fail-open status tools: a diff over absent data would be a fabrication).
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
DOCS_DIR = os.environ.get('ROUTING_DOCS_DIR', os.path.join(_REPO, 'docs'))

_HEAD_RE = re.compile(r'^##\s+(\S+)\b')
# "  12. $ 0.284/M  opencode-go/kimi-k2.7-code"
_HOP_RE = re.compile(
    r'^\s+(\d+)\.\s+\$\s*([0-9]+(?:\.[0-9]+)?)\s*/M\s+(\S+?)/(\S+)\s*$')


def snapshot_path(date):
    return os.path.join(DOCS_DIR, f'chains-{date}.md')


def parse_snapshot(path, date):
    """chains-<date>.md → {profile: [(price, provider, model), ...] (hop order).

    Raises ValueError on unreadable file or zero parsed hops (a snapshot with
    no hop lines is corrupt — the caller reports it as a clean error).
    """
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        raise ValueError(f'snapshot not found: {path}')
    except Exception as e:  # noqa: BLE001 — clean error, not a traceback
        raise ValueError(f'snapshot unreadable: {type(e).__name__}: {e}')
    profiles = {}
    current = None
    n_hops = 0
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            current = m.group(1)
            profiles.setdefault(current, [])
            continue
        m = _HOP_RE.match(line)
        if m and current:
            profiles[current].append(
                (float(m.group(2)), m.group(3), m.group(4)))
            n_hops += 1
    if n_hops == 0:
        raise ValueError(f'snapshot has no parseable chain hops: {path}')
    return profiles


def _pair(price, provider, model):
    return f'{provider}/{model}'


def diff(old, new, d_old, d_new):
    profiles = sorted(set(old) | set(new))
    per_profile = []
    totals = {'profiles_compared': 0, 'head_moves': 0,
              'new_lanes': 0, 'dropped_lanes': 0, 'price_changes': 0}
    for pid in profiles:
        old_lanes = old.get(pid) or []
        new_lanes = new.get(pid) or []
        head_move = None
        if (old_lanes and new_lanes and
                _pair(*old_lanes[0]) != _pair(*new_lanes[0])):
            head_move = {'from': _pair(*old_lanes[0]), 'to': _pair(*new_lanes[0]),
                         'from_price': old_lanes[0][0],
                         'to_price': new_lanes[0][0]}
        if old_lanes and not new_lanes:
            note = f'profile absent from {d_new} snapshot'
        elif new_lanes and not old_lanes:
            note = f'profile absent from {d_old} snapshot'
        else:
            note = None
        old_map, new_map = {}, {}
        for price, prov, model in old_lanes:
            old_map.setdefault(_pair(price, prov, model), []).append(price)
        for price, prov, model in new_lanes:
            new_map.setdefault(_pair(price, prov, model), []).append(price)
        new_names = []
        for key in sorted(new_map):
            if key not in old_map:
                new_names.append(key)
        dropped_names = []
        for key in sorted(old_map):
            if key not in new_map:
                dropped_names.append(key)
        price_changes = []
        for key in sorted(set(old_map) & set(new_map)):
            o, n = old_map[key][0], new_map[key][0]
            if o != n:
                price_changes.append({'lane': key, 'from': o, 'to': n,
                                      'delta': round(n - o, 6)})
        if not note:
            totals['profiles_compared'] += 1
        totals['head_moves'] += 1 if head_move else 0
        totals['new_lanes'] += len(new_names)
        totals['dropped_lanes'] += len(dropped_names)
        totals['price_changes'] += len(price_changes)
        per_profile.append({
            'profile': pid,
            'note': note,
            'head': {'from': _pair(*old_lanes[0]) if old_lanes else None,
                     'to': _pair(*new_lanes[0]) if new_lanes else None},
            'head_move': head_move,
            'new_lanes': new_names,
            'dropped_lanes': dropped_names,
            'price_changes': price_changes,
            'hops': {'from': len(old_lanes), 'to': len(new_lanes)},
        })
    return {'from': d_old, 'to': d_new, 'profiles': per_profile, 'totals': totals}


def build(d_old, d_new):
    old = parse_snapshot(snapshot_path(d_old), d_old)
    new = parse_snapshot(snapshot_path(d_new), d_new)
    doc = diff(old, new, d_old, d_new)
    doc['generated_at_source'] = 'docs/chains-<date>.md snapshots'
    return doc


def render_text(doc):
    lines = [f"chains diff {doc['from']} → {doc['to']}"]
    t = doc['totals']
    lines.append(f"profiles {t['profiles_compared']} compared · "
                 f"{t['head_moves']} head moves · {t['new_lanes']} new · "
                 f"{t['dropped_lanes']} dropped · {t['price_changes']} price changes")
    for p in doc['profiles']:
        head = f"{p['head']['from']} -> {p['head']['to']}"
        lines.append(f"  {p['profile']}: head {head}")
        if p['note']:
            lines.append(f"    ({p['note']})")
        if p['head_move']:
            hm = p['head_move']
            lines.append(f"    MOVED: {hm['from']} (${hm['from_price']:.3f}/M) -> "
                         f"{hm['to']} (${hm['to_price']:.3f}/M)")
        for lane in p['new_lanes']:
            lines.append(f"    + {lane}")
        for lane in p['dropped_lanes']:
            lines.append(f"    - {lane}")
        for ch in p['price_changes']:
            sign = '+' if ch['delta'] >= 0 else ''
            lines.append(f"    $ {ch['lane']}: {ch['from']:.3f} -> {ch['to']:.3f} "
                         f"({sign}{ch['delta']:.3f})")
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Diff two docs/chains-<date>.md snapshots (head moves, '
                    'lane changes, price deltas)')
    ap.add_argument('date_from', help='YYYY-MM-DD (older snapshot)')
    ap.add_argument('date_to', help='YYYY-MM-DD (newer snapshot)')
    ap.add_argument('--json', action='store_true',
                    help='pure JSON on stdout (output is JSON by default; '
                         'flag kept for --json contract symmetry)')
    ap.add_argument('--format', choices=['json', 'text'], default='json')
    args = ap.parse_args(argv)
    try:
        doc = build(args.date_from, args.date_to)
    except ValueError as e:
        print(f'router_diff: {e}', file=sys.stderr)
        return 2
    if args.json or args.format == 'json':
        print(json.dumps(doc, indent=1))
    else:
        print(render_text(doc))
    return 0


if __name__ == '__main__':
    sys.exit(main())

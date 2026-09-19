#!/usr/bin/env python3
"""outcomes_averages.py — rolling cost-per-task averages (TR-049 component 3).

Reads the outcome store (append-only JSONL, one row per completed task) and
writes the rolling-averages table that resolve-time sorting consumes.

The average works like the Linux load average: every sample contributes with
an exponential decay whose HALF-LIFE is the window, so a sample exactly one
window old weighs 0.5.  Windows default to 1d/3d/7d and are configurable
(`--windows 12,48` or `--windows 1d,3d,7d,30d`) — never hardcoded beyond the
default set.

Buckets are per (source_system, provider, model, complexity): one row per
backing system (per-backend isolation) AND one row per (model × requested
complexity reference) so a lane's cost is comparable at the task profile it
will be asked to serve.  `--merge-backends` collapses the source dimension
(sample-count weighted) into (provider, model, complexity).

Paths (TR-049 component 2): `--input` > $ROUTING_OUTCOMES_FILE > the
repo-relative gitignored default; `--output` > $ROUTING_AVERAGES_FILE > the
repo-relative default.

Usage:
  python3 scripts/outcomes_averages.py --dry-run
  python3 scripts/outcomes_averages.py --windows 1d,3d,7d,30d
  python3 scripts/outcomes_averages.py --merge-backends --output /tmp/avg.jsonl

Exit codes: 0 ok (fail-open — a missing/empty store prints a JSON summary with
0 buckets and still exits 0), 2 on a malformed CLI value, 1 only if a write to
--output fails.
"""
import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import router_outcomes as ro  # noqa: E402  (single source of truth for the math)


def parse_windows(spec):
    """'24,72,168' | '1d,3d,7d' | '12h,1d' -> sorted unique hour list."""
    if spec is None or str(spec).strip() == '':
        return list(ro.DEFAULT_SCALES_H)
    hours = []
    for token in str(spec).split(','):
        t = token.strip().lower()
        if not t:
            continue
        try:
            if t.endswith('h'):
                hours.append(int(float(t[:-1])))
            elif t.endswith('d'):
                hours.append(int(float(t[:-1]) * 24))
            else:
                hours.append(int(float(t)))
        except ValueError:
            raise ValueError(f'bad window {token!r} (use 24 / 24h / 1d)')
    if not hours:
        raise ValueError('no windows given')
    if any(h <= 0 for h in hours):
        raise ValueError('windows must be positive')
    return sorted(set(hours))


def read_rows(path):
    """Store rows in file order; unparseable lines are skipped (never fatal —
    a torn tail must not take the averages down)."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def build(rows, windows, merge_backends=False, now_s=None):
    """Averages for the given rows. `merge_backends` = one bucket per
    (provider, model, complexity), otherwise one per backend."""
    return ro.compute_averages(rows, scales_h=windows,
                               merge_backends=merge_backends, now_s=now_s)


def write_rows(path, averages):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        for a in averages:
            f.write(json.dumps(a, ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    return path


def summary(rows, averages, windows, merge_backends, input_path, output_path,
            dry_run):
    return {
        'input': input_path,
        'output': output_path,
        'rows': len(rows),
        'buckets': len(averages),
        'windows_h': windows,
        'merge_backends': bool(merge_backends),
        'dry_run': bool(dry_run),
        'computed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'averages': averages,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='outcomes_averages.py',
        description='Rolling (exponential-decay) cost-per-task averages from '
                    'the outcome store.')
    ap.add_argument('--input', help='outcome store JSONL '
                                    '(default $ROUTING_OUTCOMES_FILE)')
    ap.add_argument('--output', help='averages JSONL to write '
                                     '(default $ROUTING_AVERAGES_FILE)')
    ap.add_argument('--windows', default=None,
                    help='comma list of windows: 24,72,168 / 1d,3d,7d '
                         f'(default {",".join(str(h) for h in ro.DEFAULT_SCALES_H)})')
    ap.add_argument('--merge-backends', action='store_true',
                    help='collapse the source_system dimension (default: one '
                         'bucket per backend — isolation)')
    ap.add_argument('--dry-run', action='store_true',
                    help='compute and print the averages; write nothing')
    ap.add_argument('--now', type=float, default=None,
                    help='epoch seconds to evaluate the decay against '
                         '(default: now)')
    args = ap.parse_args(argv)

    try:
        windows = parse_windows(args.windows)
    except ValueError as exc:
        print(f'outcomes_averages: {exc}', file=sys.stderr)
        return 2

    input_path = args.input or ro.outcomes_path()
    output_path = args.output or ro.averages_path()
    rows = read_rows(input_path)
    averages = build(rows, windows, merge_backends=args.merge_backends,
                     now_s=args.now)
    if not args.dry_run and averages:
        try:
            write_rows(output_path, averages)
        except OSError as exc:
            print(json.dumps({'error': f'write failed: {exc}',
                              'output': output_path}))
            return 1
    print(json.dumps(summary(rows, averages, windows, args.merge_backends,
                             input_path, output_path, args.dry_run),
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

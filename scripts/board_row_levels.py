#!/usr/bin/env python3
"""TR-292 (complexity-model R6.1/R6.2): the board's side of the complexity contract.

The owner asked twice (2026-10-03): pass the RAW complexities per task, not the
profile that was assigned. Measured drift on the live board: `complexity` held
ints (3 x43, 2 x31) AND strings ("moderate" x13, plus low/medium/large/small/
mechanical) — a scalar in a field the resolver can only use as a profile id or
drop, and nothing on a row able to express the per-category level map the
resolver already reads (`required_categories`, router_outcomes.row_complexity_sig).

This tool is the WRITE side, so the field cannot drift by hand again:

  canonical vocabulary
  --------------------
  - `complexity`     : int 0..5 or null. ORDERING ONLY (R6.2) — never a routing
                       input. Legacy scalar words map: mechanical=1, small=1,
                       low=2, medium=3, moderate=3, large=4.
  - `required_categories`: {category: level}, level int -5..+5 — the RAW levels
                       of THIS task, in the router's canonical vocabulary (the
                       same signed level the registry tiers and the ad-hoc
                       `--profile-req cat=level` channel use; the registry is
                       the authority for the category list). Written by board
                       tooling, read by router_spawn (declared-raw) — a row that
                       carries it resolves with NO classifier call.

  subcommands
  -----------
  normalize  Scan a board, emit (or apply) superseding rows that carry the WHOLE
             original row with: a string `complexity` replaced by its int value
             (vocabulary above) and a `complexity_note` naming the drift and
             that raw levels are NOT derivable from a scalar (a null must carry
             a reason — author `required_categories` to declare them).
  validate   Exit 1 when any row's `complexity` is neither int nor null, or any
             `required_categories` value is not an int in -5..+5.

  apply mechanics
  ---------------
  The board is APPEND-ONLY JSONL, last entry wins. A superseding append must
  carry the WHOLE row (title, detail, acceptance_criteria, ...) or the fields
  not repeated are silently blanked — this already ate a P0 row once. This
  script copies the parsed original row VERBATIM and changes only the fields it
  owns, then appends through ~/.hermes/scripts/board_append.py (the append-only
  writer that owns the one-JSON-per-line invariant). Never hand-edit the file.

Fail-open/quiet by contract: `validate` is the guard; `normalize` never writes
without --apply (dry run is the default).
"""
import argparse
import json
import os
import subprocess
import sys

#: legacy scalar word -> ordering int (R6.2). Absent word = no mapping; the
#: row is reported, never guessed.
SCALAR_VOCAB = {'mechanical': 1, 'small': 1, 'low': 2, 'medium': 3,
                'moderate': 3, 'large': 4}

BOARD_APPEND = os.path.join(os.path.expanduser('~'),
                            '.hermes', 'scripts', 'board_append.py')


def iter_rows(path):
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield n, row


def latest_by_id(path):
    """id -> (line_no, row) for the LAST occurrence of each id."""
    latest = {}
    for n, row in iter_rows(path):
        rid = row.get('id')
        if rid is not None:
            latest[rid] = (n, row)
    return latest


def complexity_drift(row):
    """The normalised complexity for a row, or None when it needs none.
    Returns (new_value, was) — was is the offending scalar string."""
    v = row.get('complexity')
    if v is None or (isinstance(v, int) and not isinstance(v, bool)):
        return None
    if isinstance(v, str) and v.strip().lower() in SCALAR_VOCAB:
        return SCALAR_VOCAB[v.strip().lower()], v
    return None


def level_drift(row):
    """Bad required_categories entries: [(category, value)] non-int/out-of-range."""
    raw = row.get('required_categories')
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [('__row__', raw)]
    bad = []
    for cat, lvl in raw.items():
        if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) \
                or not -5 <= int(lvl) <= 5:
            bad.append((cat, lvl))
    return bad


def cmd_normalize(args):
    latest = latest_by_id(args.board)
    superseding = []
    for rid, (n, row) in sorted(latest.items(), key=lambda kv: kv[1][0]):
        fix = complexity_drift(row)
        if fix is None:
            continue
        new_v, was = fix
        out = dict(row)  # WHOLE row verbatim — a partial append blanks fields
        out['complexity'] = new_v
        out['complexity_note'] = (
            f'TR-292: scalar "{was}" normalised to {new_v} (ordering only, '
            f'complexity-model R6.2); raw per-category levels are NOT '
            f'derivable from a scalar — author required_categories to declare '
            f'them (R6.1). Normalised by board_row_levels.py.')
        out.setdefault('updated_note', []).append(
            'TR-292 complexity scalar normalised by board_row_levels.py') \
            if isinstance(out.get('updated_note'), list) else \
            out.__setitem__('updated_note',
                            ['TR-292 complexity scalar normalised '
                             'by board_row_levels.py'])
        superseding.append((n, rid, was, new_v, out))

    if not superseding:
        print('NORMALIZE: no drift — every latest row\'s complexity is int|null')
        return 0
    for n, rid, was, new_v, _out in superseding:
        print(f'line {n}: {rid}  "{was}" -> {new_v}')
    if not args.apply:
        print(f'\nDRY RUN: {len(superseding)} superseding row(s) would be '
              f'appended (whole-row copies; use --apply)')
        return 0
    appender = os.path.abspath(BOARD_APPEND)
    if not os.path.exists(appender):
        print(f'ERROR: board appender not found at {appender}', file=sys.stderr)
        return 1
    argv = [sys.executable, appender, os.path.abspath(args.board)] + \
        [json.dumps(out) for _n, _rid, _w, _v, out in superseding]
    if args.dry_commit_note:
        print('(dry-commit-note: skipping the actual append)')
        return 0
    r = subprocess.run(argv, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        print(f'ERROR: appender exit {r.returncode}', file=sys.stderr)
        return r.returncode
    # read-back verification: the drift must be gone
    after = latest_by_id(args.board)
    residual = [rid for rid, (n, row) in after.items()
                if complexity_drift(row) is not None]
    if residual:
        print(f'ERROR: after append, rows still drift: {residual}',
              file=sys.stderr)
        return 1
    print(f'APPLIED: {len(superseding)} superseding row(s); re-read clean')
    return 0


def cmd_validate(args):
    problems = []
    seen_latest = latest_by_id(args.board)
    for rid, (n, row) in sorted(seen_latest.items(), key=lambda kv: kv[1][0]):
        v = row.get('complexity')
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            problems.append(f'{rid} (line {n}): complexity is '
                            f'{type(v).__name__} {v!r} — must be int|null')
        for cat, lvl in level_drift(row):
            problems.append(f'{rid} (line {n}): required_categories[{cat!r}] '
                            f'= {lvl!r} — must be int in -5..+5')
    if problems:
        print(f'VALIDATE: {len(problems)} problem(s):')
        for p in problems:
            print(f'  - {p}')
        return 1
    print(f'VALIDATE: {len(seen_latest)} latest row(s) conform '
          f'(complexity int|null; required_categories int -5..+5)')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='TR-292: board complexity contract — normalise scalar '
                    'drift, enforce the field types, document the raw-level '
                    'vocabulary')
    ap.add_argument('command', choices=['normalize', 'validate'])
    ap.add_argument('--board', default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '.coding-hermes', 'board', 'tasks.jsonl'))
    ap.add_argument('--apply', action='store_true',
                    help='normalize: actually append the superseding rows '
                         '(default: dry run)')
    ap.add_argument('--dry-commit-note', action='store_true')
    args = ap.parse_args()
    if args.command == 'normalize':
        return cmd_normalize(args)
    return cmd_validate(args)


if __name__ == '__main__':
    sys.exit(main())

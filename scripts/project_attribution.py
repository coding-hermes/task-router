#!/usr/bin/env python3
"""project_attribution.py — TR-250: per-project attribution + completeness readout.

Every board row belongs to a PROJECT (1 primary foreman lane + its satellite
lanes: -qa, -pm, -sync, -dogfood, -perf, -releng, -review, -docs), but this
board's rows never said so: nothing on a row names its project or lane, so
per-project questions ("how open is task-router's -qa lane?") were answered by
eyeballing prefixes. This script makes attribution DERIVED and the derivation
HONEST: a row it cannot attribute is reported as underivable, never guessed.

Attribution derivation (first match wins; the winning basis is reported):

  1. explicit   — the row already carries `project` (passthrough, never
                  overwritten);
  2. workdir    — basename of the row's `workdir` (fleet broadcast rows carry
                  the project workdir they were filed against);
  3. prefix     — the id's first token in PREFIX_PROJECT_MAP (this repo's own
                  board: `TR` -> task-router) and its lane role from the
                  documented lane vocabulary: a satellite prefix in
                  PREFIX_LANE_MAP (measured on this board: QA/REVIEW/DOC/
                  README/RELEASE) or a lane-suffix token on the id
                  (`<id>-qa`, `<id>-sync`, ...). No suffix anywhere = the
                  PRIMARY lane by the documented convention.
  4. underivable — no basis applied: project and/or lane_role stay None and
                  the row lands in the report's `underivable_rows` with a
                  reason (unmapped-id-prefix | row-has-no-id | ...). An
                  unmapped prefix like INT/PYTYP is REPORTED, not mapped to a
                  plausible lane.

STAMPING (the board row-writing path). This repo's board appends happen
through the append-only writers (gitreins task tooling, foreman waves —
read-modify-append, never wholesale rewrite, per AGENTS.md). Writers that
create NEW rows stamp them at append time:

    from project_attribution import stamp_row        # scripts/ on sys.path
    row = stamp_row(row)                             # sets project+lane_role
                                                       when derivable

or pipe-shaped on the CLI:

    python3 scripts/project_attribution.py stamp --row '{"id": "TR-9", ...}'

`stamp_row` never edits the board itself and never stamps an underivable row
(the fields simply stay absent — absence IS the reportable state).

REPORT (per-project completeness readout; stable schema, stdlib only):

    python3 scripts/project_attribution.py --report

emits one JSON object, schema `task-router.project_attribution/v1`:

    attribution           how project/lane were derived (sources, maps,
                          status vocabularies — the documentation travels
                          inside the payload)
    projects.<name>       lanes_source + one entry per lane:
                            open_rows                        count
                            complete_rows                    count
                            complete_rows_lacking_evidence   count (per lane)
                          and per project:
                            complete_rows_lacking_evidence   count + row ids
                            oldest open row per lane         {id, age_days,
                                                              created_at} or
                                                              null WITH a
                                                              reason (Bane's
                                                              null doctrine:
                                                              every null
                                                              carries why)
                            underivable_rows                 [{id, reason}]

    Vocabulary (documented, mirrors board_stall_check.py):
      open     = status not in TERMINAL_STATUSES
      terminal = complete | done | failed | superseded
      evidence on a complete row = any of commit_hash, guard_result,
      ci_result, worker_summary, foreman_note — a complete row with NONE of
      them is counted and listed as lacking evidence.

BACKFILL (named migration, dry-run only by construction):

    python3 scripts/project_attribution.py backfill --dry-run

lists every row's derivable attribution ({id, project, lane_role, basis})
plus the underivable rows. There is no --apply: the flag is refused loudly.
The live board is NEVER edited by this script — a backfill that wrote rows
would race the append-only writers and the merge driver; proposing is the
whole job. `--out FILE` writes the proposal to a sidecar JSON instead of
stdout.

Stdlib only (like every script in this repo — it must run in the bare board
venv). Read-only against the board in every mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')

#: Report schema version — bump on any shape change.
SCHEMA = 'task-router.project_attribution/v1'

#: The documented fleet lane vocabulary (foreman doctrine): a project is one
#: primary lane plus these satellites. Stored on rows WITH the leading dash.
LANE_ROLES = ('primary', '-qa', '-pm', '-sync', '-dogfood',
              '-perf', '-releng', '-review', '-docs')
_ROLE_TOKENS = tuple(role.lstrip('-') for role in LANE_ROLES if role != 'primary')

#: This repo's primary lane prefix (AGENTS.md: task ids `TR-00N`).
PRIMARY_PREFIX = 'TR'
DEFAULT_PROJECT = 'task-router'

#: id first token -> project (rows that name no project themselves).
PREFIX_PROJECT_MAP = {PRIMARY_PREFIX: DEFAULT_PROJECT}

#: id first token -> lane role, for the satellite lanes that file rows under
#: their own prefix on this board (every entry measured in tasks.jsonl;
#: anything NOT in this map is reported underivable, never guessed).
PREFIX_LANE_MAP = {
    'QA': '-qa',
    'REVIEW': '-review',
    'DOC': '-docs',
    'README': '-docs',
    'RELEASE': '-releng',
}

#: Status vocabularies (terminal set mirrors board_stall_check.py's lifecycle
#: terminals plus the row-superseding/failed outcomes this board actually
#: writes; everything else counts as open).
TERMINAL_STATUSES = ('complete', 'done', 'failed', 'superseded')

#: A complete row shows its work through at least one of these fields.
EVIDENCE_FIELDS = ('commit_hash', 'guard_result', 'ci_result',
                   'worker_summary', 'foreman_note')


# --------------------------------------------------------------------------- 
# timestamps
# ---------------------------------------------------------------------------

def parse_ts(value: object) -> datetime | None:
    """Parse a board timestamp; None when absent or unparseable.

    The board carries several spellings (measured in tasks.jsonl):
    `2026-09-26T12:33:49Z`, `2026-09-23T12:46:26.126701+00:00`,
    `2026-08-27 05:45:00.000000` (space separator + microseconds), naive and
    offset forms. Z-suffixed and naive strings are read as UTC.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    text = text.replace(' ', 'T', 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_days(ts: datetime, now: datetime) -> float:
    """Age of a parsed timestamp in days (1 decimal) against `now`."""
    return round((now - ts).total_seconds() / 86400.0, 1)


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------

def _lane_role_from_id(tid: str) -> str:
    """Lane role from the id alone, or None.

    Prefix first (satellite lanes file rows under their own prefix token on
    this board), then a lane-suffix token (`<base>-qa`); no marker anywhere
    means the PRIMARY lane by the documented convention.
    """
    head = tid.split('-', 1)[0]
    if head in PREFIX_LANE_MAP:
        return PREFIX_LANE_MAP[head]
    tail = tid.rsplit('-', 1)[-1]
    if tail in _ROLE_TOKENS:
        return '-' + tail
    return 'primary'


def _satellite_marker(tid: str) -> str | None:
    """Satellite lane marker in the id (prefix token or lane suffix), or None.

    Unlike _lane_role_from_id this does NOT default to primary — it answers
    only "does the id NAME a satellite lane".
    """
    head = tid.split('-', 1)[0]
    if head in PREFIX_LANE_MAP:
        return PREFIX_LANE_MAP[head]
    tail = tid.rsplit('-', 1)[-1]
    if tail in _ROLE_TOKENS:
        return '-' + tail
    return None


def derive_attribution(row: dict, default_project: str | None = None,
                       prefix_map: dict | None = None) -> dict:
    """Derive (project, lane_role, basis, reason) for one board row.

    Never raises, never guesses: a field stays None when nothing derives it
    and `reason` says why. `default_project` models board ownership (rows on
    task-router's board belong to task-router even when the id carries an
    unmapped prefix or no id at all) — pass None to keep the derivation
    strictly id/workdir-based.

    Basis order: explicit project field > workdir basename (the row names
    the project it was filed against) > id prefix map > board ownership.
    Lanes: a satellite marker in the id (QA-/REVIEW-/DOC-/... or <id>-qa)
    names the lane under ANY basis; "primary" is claimed only when the id
    prefix itself was the project basis (the marker-free id IS the primary
    lane's naming convention) — a workdir/explicit-project basis says
    nothing about which primary lane the row belongs to.
    """
    prefixes = PREFIX_PROJECT_MAP if prefix_map is None else prefix_map
    tid = row.get('id')
    tid = tid.strip() if isinstance(tid, str) and tid.strip() else None
    marker = _satellite_marker(tid) if tid else None

    project = row.get('project')
    if isinstance(project, str) and project.strip():
        return {'project': project.strip(), 'lane_role': marker,
                'basis': 'explicit', 'reason': None}
    workdir = row.get('workdir')
    if isinstance(workdir, str) and workdir.strip():
        name = os.path.basename(workdir.strip().rstrip('/'))
        if name:
            return {'project': name, 'lane_role': marker,
                    'basis': 'workdir', 'reason': None}
    if tid is not None:
        head = tid.split('-', 1)[0]
        if head in prefixes:
            return {'project': prefixes[head],
                    'lane_role': _lane_role_from_id(tid), 'basis': 'prefix',
                    'reason': None}
        if marker is not None:
            # A satellite marker names the LANE; board ownership names the
            # project when it is known.
            if default_project:
                return {'project': default_project, 'lane_role': marker,
                        'basis': 'board-ownership', 'reason': None}
            return {'project': None, 'lane_role': None, 'basis': None,
                    'reason': 'unmapped-id-prefix'}
        if default_project:
            # Board ownership names the project; the LANE stays underivable —
            # an unmapped prefix is reported, never mapped to a plausible role.
            return {'project': default_project, 'lane_role': None,
                    'basis': 'board-ownership',
                    'reason': 'unmapped-id-prefix'}
        return {'project': None, 'lane_role': None, 'basis': None,
                'reason': 'unmapped-id-prefix'}
    if default_project:
        # No id at all: the board still names the project, nothing names the
        # lane (reported, never guessed).
        return {'project': default_project, 'lane_role': None,
                'basis': 'board-ownership', 'reason': 'row-has-no-id'}
    return {'project': None, 'lane_role': None, 'basis': None,
            'reason': 'row-has-no-id'}


def stamp_row(row: dict, default_project: str | None = DEFAULT_PROJECT,
              prefix_map: dict | None = None) -> dict:
    """Stamp `project` + `lane_role` onto a NEW board row (in place, returned).

    The integration point for the board row-writing path: writers call this
    right before the append. An explicit `project` is never overwritten; each
    field is stamped only when DERIVABLE — an underivable project or lane
    stays absent (absence is the reportable state — the backfill/report
    surfaces it), so no row ever carries a guessed value.
    """
    att = derive_attribution(row, default_project=default_project,
                             prefix_map=prefix_map)
    if att['project'] is not None and att['basis'] != 'explicit':
        row['project'] = att['project']
    if att['lane_role'] is not None:
        row['lane_role'] = att['lane_role']
    return row


# ---------------------------------------------------------------------------
# board reading (tolerant, like every reader in scripts/)
# ---------------------------------------------------------------------------

def read_board(path: str) -> tuple[list[dict], int]:
    """-> (rows, malformed_line_count). Rows without an id are kept (they are
    reported underivable); a torn line is skipped and counted, never fatal."""
    rows, bad = [], 0
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def _is_open(row: dict) -> bool:
    return row.get('status') not in TERMINAL_STATUSES


def _is_complete(row: dict) -> bool:
    return row.get('status') in ('complete', 'done')


def _has_evidence(row: dict) -> bool:
    return any(row.get(f) for f in EVIDENCE_FIELDS)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

_LANE_ORDER = {role: i for i, role in enumerate(LANE_ROLES)}


def _lane_sort_key(name: str) -> tuple:
    return (_LANE_ORDER.get(name, len(_LANE_ORDER)), name)


def build_report(board: str, now: datetime,
                 default_project: str | None = DEFAULT_PROJECT,
                 prefix_map: dict | None = None) -> dict:
    """Per-project completeness readout (stable schema, stdlib only)."""
    rows, bad = read_board(board)
    prefixes = PREFIX_PROJECT_MAP if prefix_map is None else prefix_map
    projects = {}
    totals = {'rows': len(rows), 'unique_ids': 0, 'malformed_lines': bad,
              'rows_without_id': 0, 'duplicate_id_rows': 0}

    seen_ids = {}
    for row in rows:
        att = derive_attribution(row, default_project=default_project,
                                 prefix_map=prefixes)
        tid = row.get('id')
        tid = tid.strip() if isinstance(tid, str) else None
        if not tid:
            totals['rows_without_id'] += 1
        elif tid in seen_ids:
            totals['duplicate_id_rows'] += 1
        else:
            seen_ids[tid] = True
        name = att['project']
        if not name:
            # Nothing names this row's project: board-ownership fallback is
            # OFF in the pure report only when no default was resolved.
            name = f"<underivable:{att['reason']}>"
        proj = projects.setdefault(name, {
            'lanes_source': (
                'row attribution over %s (explicit project field > id prefix '
                'map > workdir basename); lane roles from the documented '
                'fleet vocabulary %s matched by satellite id prefix or lane '
                'suffix; rows whose lane was underivable count under the '
                '"unknown-lane" bucket and are listed in underivable_rows'
                % (board, ', '.join(LANE_ROLES))),
            'lanes': {},
            'complete_rows_lacking_evidence': 0,
            'complete_rows_lacking_evidence_ids': [],
            'underivable_rows': [],
            'open_rows_without_timestamp': [],
        })
        if att['reason'] and not att['lane_role']:
            proj['underivable_rows'].append({'id': tid, 'reason': att['reason']})

        lane_name = att['lane_role'] or 'unknown-lane'
        lane = proj['lanes'].setdefault(lane_name, {
            'open_rows': 0, 'complete_rows': 0,
            'complete_rows_lacking_evidence': 0,
            'oldest_open_row': None,
        })
        created = parse_ts(row.get('created_at'))
        if _is_open(row):
            lane['open_rows'] += 1
            if created is None:
                proj['open_rows_without_timestamp'].append(tid)
            else:
                cand = {'id': tid, 'age_days': age_days(created, now),
                        'created_at': row.get('created_at')}
                cur = lane['oldest_open_row']
                if cur is None or cand['age_days'] > cur['age_days']:
                    lane['oldest_open_row'] = cand
        elif _is_complete(row):
            lane['complete_rows'] += 1
            if not _has_evidence(row):
                lane['complete_rows_lacking_evidence'] += 1
                proj['complete_rows_lacking_evidence'] += 1
                if tid:
                    proj['complete_rows_lacking_evidence_ids'].append(tid)

    # Oldest-open nulls must carry their reason (Bane's null doctrine).
    for proj in projects.values():
        for lane in proj['lanes'].values():
            if lane['oldest_open_row'] is None:
                lane['oldest_open_row'] = {
                    'id': None, 'age_days': None,
                    'reason': ('no-parseable-created-at on any open row'
                               if lane['open_rows'] else 'no-open-rows'),
                }
        proj['underivable_rows'].sort(
            key=lambda r: (r['id'] is None, r['id'] or ''))
        proj['open_rows_without_timestamp'].sort(key=lambda t: (t is None, t or ''))
    totals['unique_ids'] = len(seen_ids)

    return {
        'schema': SCHEMA,
        'generated_at': now.isoformat(timespec='seconds'),
        'board': board,
        'attribution': {
            'project_source': (
                "row 'project' field when present, else basename(workdir), "
                'else the id-prefix map %s (board ownership default: %r); '
                'rows matching none are listed under underivable_rows, '
                'never guessed' % (prefixes, default_project)),
            'lane_source': (
                'documented fleet lane vocabulary (%s); satellite id '
                'prefixes %s; a lane-suffix token (<id>-qa) also maps; no '
                'marker = primary' % (', '.join(LANE_ROLES),
                                      sorted(PREFIX_LANE_MAP))),
            'prefix_map': dict(prefixes),
            'prefix_lane_map': dict(PREFIX_LANE_MAP),
            'open_statuses': 'any status outside %s' % (TERMINAL_STATUSES,),
            'terminal_statuses': list(TERMINAL_STATUSES),
            'evidence_fields': list(EVIDENCE_FIELDS),
        },
        'projects': {name: projects[name]
                     for name in sorted(projects)},
        'totals': totals,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_json(payload: dict, out_path: str | None = None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=False)
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
        print('wrote %s' % out_path, file=sys.stderr)
    else:
        print(text)


def cmd_report(args: argparse.Namespace) -> int:
    now = parse_ts(args.now) or datetime.now(timezone.utc)
    report = build_report(args.board, now,
                          default_project=(args.project
                                           if args.project else
                                           (None if args.no_default_project
                                            else DEFAULT_PROJECT)),
                          prefix_map=({'TR': args.project}
                                      if args.project else None))
    _emit_json(report, args.out)
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    if args.apply:
        print('refusing --apply: backfill is dry-run only by construction '
              '(TR-250) — the live board is never edited by this script',
              file=sys.stderr)
        return 2
    rows, bad = read_board(args.board)
    stamped, underivable = [], []
    for row in rows:
        att = derive_attribution(row, default_project=DEFAULT_PROJECT)
        tid = row.get('id')
        tid = tid.strip() if isinstance(tid, str) else None
        if att['reason'] and not att['lane_role']:
            underivable.append({'id': tid, 'reason': att['reason']})
        else:
            stamped.append({'id': tid, 'project': att['project'],
                            'lane_role': att['lane_role'],
                            'basis': att['basis']})
    proposal = {
        'schema': SCHEMA,
        'mode': 'dry-run (proposal only — the board is never edited)',
        'board': args.board,
        'malformed_lines': bad,
        'stamped': stamped,
        'underivable': underivable,
    }
    _emit_json(proposal, args.out)
    print('backfill proposal: %d derivable, %d underivable '
          '(no rows written)' % (len(stamped), len(underivable)),
          file=sys.stderr)
    return 0


def cmd_stamp(args: argparse.Namespace) -> int:
    def _stamp_one(row: dict) -> dict:
        return stamp_row(row, default_project=DEFAULT_PROJECT)

    if args.row:
        try:
            row = json.loads(args.row)
        except ValueError as e:
            print('--row is not valid JSON: %s' % e, file=sys.stderr)
            return 2
        if not isinstance(row, dict):
            print('--row must be a JSON object', file=sys.stderr)
            return 2
        print(json.dumps(_stamp_one(row), sort_keys=True))
        return 0
    if args.file:
        underivable = []
        with open(args.file, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    underivable.append({'id': None,
                                        'reason': 'malformed-line'})
                    continue
                out = _stamp_one(row)
                print(json.dumps(out, sort_keys=True))
                att = derive_attribution(row, default_project=DEFAULT_PROJECT)
                if att['reason'] and not att['lane_role']:
                    underivable.append({'id': row.get('id'),
                                        'reason': att['reason']})
        if underivable:
            print('underivable rows left unstamped: %s'
                  % json.dumps(underivable), file=sys.stderr)
        return 0
    print('stamp needs --row JSON or --file rows.jsonl', file=sys.stderr)
    return 2


def _add_common(p: argparse.ArgumentParser,
                suppress: bool = False) -> argparse.ArgumentParser:
    """Shared flags, accepted on the main parser AND after a subcommand.

    Subparser copies use `argparse.SUPPRESS` so an option given BEFORE the
    subcommand is not clobbered by the subparser's default (argparse resets
    the attribute when a subparser defines the same dest).
    """
    d = (lambda default: argparse.SUPPRESS) if suppress else (lambda d: d)
    p.add_argument('--board', default=d(DEFAULT_BOARD),
                   help='board tasks.jsonl path (default: the repo board)')
    p.add_argument('--out', default=d(None),
                   help='write the JSON payload to a file instead of stdout')
    p.add_argument('--now', default=d(None),
                   help='ISO-8601 instant for age math (default: wall clock)')
    p.add_argument('--project', default=d(None),
                   help='override the primary prefix->project mapping '
                        '(default: %s->%s)' % (PRIMARY_PREFIX, DEFAULT_PROJECT))
    p.add_argument('--no-default-project', action='store_true',
                   default=d(None),
                   help='disable the board-ownership project default '
                        '(strict id/workdir-only derivation)')
    return p


def main(argv: list[str] | None = None) -> int | None:
    ap = argparse.ArgumentParser(
        description='TR-250 per-project attribution + completeness readout '
                    '(read-only against the board in every mode)')
    _add_common(ap)
    ap.add_argument('--report', action='store_true',
                    help='emit the per-project completeness readout (JSON, '
                         'schema %s)' % SCHEMA)
    sub = ap.add_subparsers(dest='cmd')

    bf = sub.add_parser(
        'backfill',
        help='named migration: LIST derivable attributions '
             '(dry-run only; never edits the board)')
    _add_common(bf, suppress=True)
    bf.add_argument('--dry-run', action='store_true',
                    help='accepted and required-by-convention: the backfill '
                         'is ALWAYS dry-run')
    bf.add_argument('--apply', action='store_true',
                    help='refused loudly: no write mode exists')
    bf.set_defaults(func=cmd_backfill)

    st = sub.add_parser(
        'stamp',
        help='stamp project+lane_role onto new board rows '
             '(stdout; the caller owns the append)')
    _add_common(st, suppress=True)
    st.add_argument('--row', default=None, help='one row as JSON')
    st.add_argument('--file', default=None, help='a JSONL file of rows')
    st.set_defaults(func=cmd_stamp)

    args = ap.parse_args(argv)
    if args.cmd == 'backfill':
        return cmd_backfill(args)
    if args.cmd == 'stamp':
        return cmd_stamp(args)
    if args.report:
        return cmd_report(args)
    ap.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())

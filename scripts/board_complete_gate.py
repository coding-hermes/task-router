#!/usr/bin/env python3
"""board_complete_gate.py — TR-249: the completion gate for the board write path.

Doctrine: docs/traceability-doctrine.md ("Completion gate (TR-249)") — a board
can reach "complete" while the deliverable it names does not exist anywhere.
The 2026-10-01 scan behind that doctrine found 222 complete rows, 8 carrying
any evidence field, 0 carrying an evidence run. This module makes that state
unreachable for NEW completions, without mass-editing history.

Gate contract (board row TR-249)
--------------------------------
1. A transition into a terminal status (`complete`, `done`) is REFUSED when
   `acceptance_criteria` is absent/empty — the refusal NAMES the field.
2. REFUSED when no evidence is attached. Evidence must name a MEASURED NUMBER
   (contains a digit), an ARTIFACT PATH (`/` or a file extension), or an
   EXPLICIT STATED-REASON NULL (`none:<reason>`, or the `evidence_none`
   field, or `witness=none:<reason>` per doctrine hard rule 3). Prose like
   "GREEN" or "done" is not evidence. A non-empty `witness` (external
   locator) is the strongest form and is accepted.
3. The gate applies at THE single transition point that writes
   status=complete: `evaluate()` / `gate_row()` / `append_completion()`
   (CLI: `append-completion`). Writer audit (every in-repo tasks.jsonl
   writer, grepped 2026-10-07):
     - scripts/board_complete_gate.py append-completion — THE gated writer.
     - scripts/router_modelsdev.py _file_gap_rows — appends NEW rows only,
       status `todo` (never terminal) → gate not applicable.
     - scripts/board_stall_check.py — rewrites rows but only to
       REQUEUE_STATUS (`pending`), never terminal → not applicable.
     - scripts/board_task_intake.py — appends filed rows with status
       `pending`; completions are only READ → not applicable.
     - scripts/board-merge-driver.py — resolves conflicts over existing
       lines, synthesizes no status → not applicable.
     - Out-of-repo writers (~/.hermes/scripts/board_append.py, foreman tick
       tooling) are outside this repo's write path; they should route
       completion writes through this module's evaluate()/append_completion().
4. Existing rows are NOT mass-edited. `backlog` is a read-only scan that
   emits a NAMED report of complete-but-unproven rows with per-row status.
5. `check --dry-run` replays a completion transition (use `--prev-status
   pending` for a historical row) and prints the refusal without writing.

Idempotence: re-appending a row whose PREVIOUS state was already terminal is
not a new transition and is not gated (history is append-only; the gate never
re-judges the past).

ch:trace row=TR-249 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_board_complete_gate.py evidence=docs/completeness-backlog-2026-10-07.md
"""
import argparse
from collections.abc import Iterator
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')

#: Row statuses the gate treats as "completion" (board_stall_check.TERMINAL).
TERMINAL = ('complete', 'done')

ARTIFACT_EXT = re.compile(
    r'\.(md|json|jsonl|txt|log|csv|tsv|html?|xml|ya?ml|png|jpe?g|gif|svg|pdf'
    r'|py|sh|ts|tsx|js|go|rs|parquet|sqlite|db|ipynb)\b', re.I)


class CompletionGateError(ValueError):
    """A completion transition was refused (the message names the field)."""


# ----------------------------------------------------------------- criteria #

def criteria_state(row: dict) -> tuple[bool, str]:
    """(present, detail) for the acceptance_criteria field."""
    v = row.get('acceptance_criteria')
    if v is None:
        return False, 'missing field: acceptance_criteria'
    items = v if isinstance(v, list) else [v]
    if any(isinstance(i, str) and i.strip() for i in items):
        return True, 'acceptance_criteria present'
    return False, 'missing field: acceptance_criteria (present but empty)'


# ----------------------------------------------------------------- evidence #

def _evidence_item_ok(item: object) -> bool:
    """One evidence string: measured number | artifact path | stated NULL."""
    if not isinstance(item, str):
        return False
    t = item.strip()
    if not t:
        return False
    if t.lower().startswith('none:'):
        return bool(t[5:].strip())          # the reason must be stated
    if re.search(r'\d', t):                 # names a measured number
        return True
    if '/' in t or ARTIFACT_EXT.search(t):  # names an artifact path
        return True
    return False


def evidence_state(row: dict) -> tuple[bool, str]:
    """(present, detail) for the evidence requirement. Accepted fields, in
    order: `evidence` / `evidence_artifact` (str or list), `witness`
    (external locator; `none:<reason>` = stated-reason NULL), and
    `evidence_none` (explicit stated-reason NULL)."""
    for field in ('evidence', 'evidence_artifact'):
        v = row.get(field)
        items = v if isinstance(v, list) else [v]
        for it in items:
            if _evidence_item_ok(it):
                return True, f'{field}={it!r}'
        raw = row.get(field)
        if raw is not None and not any(_evidence_item_ok(i) for i in items):
            shown = raw if isinstance(raw, str) else json.dumps(raw)
            return False, (f'missing field: evidence ({field}={shown.strip()[:80]!r} '
                           'names no measured number, no artifact path, '
                           'and is not a stated-reason NULL)')
    w = row.get('witness')
    if isinstance(w, str) and w.strip():
        t = w.strip()
        if t.lower().startswith('none:'):
            if t[5:].strip():
                return True, f'witness={t!r} (stated-reason NULL)'
        else:
            return True, f'witness={t!r}'
    en = row.get('evidence_none')
    if isinstance(en, str) and en.strip():
        return True, f'evidence_none={en.strip()!r} (stated-reason NULL)'
    return False, 'missing field: evidence (no measured number, no artifact path, no stated-reason NULL)'


# --------------------------------------------------------------------- gate #

def evaluate(row: dict, prev_status: str | None = None) -> list[str]:
    """Problems [] for a row write; non-empty = the transition is refused.

    Only a transition INTO a terminal status is gated. `prev_status` is the
    row's previous terminal-ness (None = unknown/new → treated as a
    transition); an already-terminal previous state is an idempotent
    re-append and passes un-gated."""
    status = str(row.get('status') or '').strip().lower()
    if status not in TERMINAL:
        return []
    if prev_status is not None and str(prev_status).strip().lower() in TERMINAL:
        return []
    problems = []
    ok_ac, why_ac = criteria_state(row)
    if not ok_ac:
        problems.append(why_ac)
    ok_ev, why_ev = evidence_state(row)
    if not ok_ev:
        problems.append(why_ev)
    return problems


def refusal_message(row: dict, problems: list[str]) -> str:
    tid = row.get('id') or '<no-id>'
    return (f"{tid}: completion REFUSED by board_complete_gate (TR-249): "
            + '; '.join(problems))


def gate_row(row: dict, prev_status: str | None = None) -> dict:
    """Raise CompletionGateError if the completion transition is refused."""
    problems = evaluate(row, prev_status)
    if problems:
        raise CompletionGateError(refusal_message(row, problems))
    return row


# ------------------------------------------------------------ board helpers #

def iter_rows(path: str) -> Iterator[tuple[int, dict]]:
    """(line_no, row) for every parseable JSON line (tolerant, last wins)."""
    with open(path, encoding='utf-8') as fh:
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


def latest_by_id(path: str) -> dict[str, dict]:
    """id -> row (LAST occurrence wins — the board is append-only JSONL)."""
    latest = {}
    for _, row in iter_rows(path):
        tid = row.get('id')
        if tid:
            latest[str(tid)] = row
    return latest


def append_jsonl(path: str, row: dict) -> None:
    """Append ONE row, flock-serialized, fsync'd — same discipline as
    board_task_intake.append_jsonl (a crash must not tear a line)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + '\n'
    with open(path, 'a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------- transition #

def append_completion(board_path: str, task_id: str, sets: dict | None,
                      dry_run: bool = False,
                      prev_status: str | None = None) -> dict:
    """The ONE gated write of a completion transition. Returns a verdict dict;
    refuses (CompletionGateError) before any byte is written."""
    latest = latest_by_id(board_path)
    prev = latest.get(str(task_id))
    if prev is None:
        raise CompletionGateError(
            f'{task_id}: REFUSED — no row with this id on {board_path}')
    row = dict(prev)
    for k, v in (sets or {}).items():
        row[k] = v
    row['updated_at'] = _utcnow()
    effective_prev = (prev_status if prev_status is not None
                      else prev.get('status'))
    problems = evaluate(row, effective_prev)
    if problems:
        # Fail LOUD, before any byte is written — library callers get the
        # exception; the CLI catches it, prints the refusal, exits 2.
        raise CompletionGateError(refusal_message(row, problems))
    row['completion_gate'] = {'gate': 'board_complete_gate', 'row': 'TR-249',
                              'at': _utcnow(), 'verdict': 'pass'}
    verdict = {'task_id': str(task_id), 'dry_run': bool(dry_run),
               'verdict': 'APPEND', 'problems': [], 'written': False,
               'row': row}
    if dry_run:
        return verdict
    append_jsonl(board_path, row)
    verdict['written'] = True
    return verdict


# ------------------------------------------------------------------ backlog #

def classify_row(row: dict) -> tuple[str, str]:
    """Backlog class for one terminal row (+ per-field detail)."""
    ok_ac, _ = criteria_state(row)
    ok_ev, why_ev = evidence_state(row)
    if ok_ac and ok_ev:
        return 'proven', why_ev
    missing = []
    if not ok_ac:
        missing.append('acceptance_criteria')
    if not ok_ev:
        missing.append('evidence')
    return '+'.join(missing), why_ev


def build_backlog_report(board_path: str, date_stamp: str) -> tuple[str, dict]:
    """(markdown, stats) — the NAMED backlog of complete-but-unproven rows.
    Read-only: no board byte is touched."""
    latest = latest_by_id(board_path)
    terminal, unproven, proven_ids = [], [], []
    for tid in sorted(latest):
        row = latest[tid]
        status = str(row.get('status') or '').strip().lower()
        if status not in TERMINAL:
            continue
        terminal.append(tid)
        cls, detail = classify_row(row)
        if cls == 'proven':
            proven_ids.append(tid)
        else:
            unproven.append((tid, row, cls, detail))
    stats = {'terminal': len(terminal), 'proven': len(proven_ids),
             'unproven': len(unproven)}
    by_cls = {}
    for _, _, cls, _ in unproven:
        by_cls[cls] = by_cls.get(cls, 0) + 1
    lines = [
        f'# Completeness backlog — complete-but-unproven rows ({date_stamp})',
        '',
        f'Generated by `scripts/board_complete_gate.py backlog` (gate TR-249, '
        f'doctrine docs/traceability-doctrine.md). Read-only scan of '
        f'`.coding-hermes/board/tasks.jsonl` (last row per id wins); '
        f'**no row was edited** — this file is the named report the gate '
        f'requires before any remediation is scheduled.',
        '',
        '## Method',
        '',
        '- A row is *complete-but-unproven* when its latest status is '
        '`complete`/`done` AND the gate would refuse the transition today:',
        '  no non-empty `acceptance_criteria`, or no evidence naming a '
        'measured number, an artifact path, or an explicit stated-reason '
        'NULL (`evidence`/`evidence_artifact`/`witness`/`evidence_none`).',
        '- Historical rows are reported, never rewritten (TR-249 req 4).',
        '',
        '## Totals',
        '',
        f'- terminal rows: **{stats["terminal"]}**',
        f'- proven (would pass the gate): **{stats["proven"]}**',
        f'- complete-but-unproven: **{stats["unproven"]}** '
        f'({", ".join(f"{k}: {v}" for k, v in sorted(by_cls.items())) or "none"})',
        '',
        '## Unproven rows (per-row status)',
        '',
        '| id | status | worker_status | missing | evidence detail |',
        '|---|---|---|---|---|',
    ]
    for tid, row, cls, detail in unproven:
        title = str(row.get('title') or '').replace('|', '\\|')[:60]
        detail_md = str(detail).replace('|', '\\|')[:100]
        status = row.get('status')
        worker_status = row.get('worker_status')
        lines.append(
            f'| {tid} | {status} | {worker_status} | {cls} | {detail_md} |')
    lines += [
        '',
        '## Proven rows',
        '',
        f'{stats["proven"]} rows carry criteria + acceptable evidence: '
        + (', '.join(proven_ids) if proven_ids else '(none)') + '',
        '',
    ]
    return '\n'.join(lines), stats


# ---------------------------------------------------------------------- CLI #

def _parse_set(pairs: list[str] | None) -> dict:
    sets = {}
    for pair in pairs or []:
        if '=' not in pair:
            raise SystemExit(f'--set expects key=value, got {pair!r}')
        k, v = pair.split('=', 1)
        try:
            sets[k] = json.loads(v)      # lists/numbers/null pass through
        except ValueError:
            sets[k] = v
    return sets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='TR-249 completion gate for .coding-hermes/board/tasks.jsonl')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_chk = sub.add_parser(
        'check', help='evaluate one row as a completion transition (exit 2 = refused)')
    p_chk.add_argument('--board', default=DEFAULT_BOARD)
    p_chk.add_argument('--task', help='board row id (latest row wins)')
    p_chk.add_argument('--row-file', help='evaluate a single JSON row file instead')
    p_chk.add_argument('--prev-status', default=None,
                       help="previous status (e.g. pending) — replay a historical "
                            "row's transition; omit for a new/unknown prior state")
    p_chk.add_argument('--dry-run', action='store_true',
                       help='check never writes; the flag just stamps the output')

    p_app = sub.add_parser(
        'append-completion',
        help='THE gated write: merge --set fields into a row and append it')
    p_app.add_argument('--board', default=DEFAULT_BOARD)
    p_app.add_argument('--task', required=True)
    p_app.add_argument('--set', action='append', default=[],
                       help='key=value (value may be JSON); repeatable')
    p_app.add_argument('--prev-status', default=None)
    p_app.add_argument('--dry-run', action='store_true',
                       help='print the verdict, write nothing')

    p_blg = sub.add_parser(
        'backlog', help='read-only scan: complete-but-unproven rows report')
    p_blg.add_argument('--board', default=DEFAULT_BOARD)
    p_blg.add_argument('--out', default=None,
                       help='report path (default docs/completeness-backlog-<date>.md)')
    p_blg.add_argument('--dry-run', action='store_true',
                       help='print totals only, write no report file')

    args = ap.parse_args(argv)

    if args.cmd == 'check':
        if args.row_file:
            with open(args.row_file, encoding='utf-8') as fh:
                row = json.load(fh)
        else:
            if not args.task:
                ap.error('check needs --task or --row-file')
            row = latest_by_id(args.board).get(str(args.task))
            if row is None:
                print(f'{args.task}: no row on {args.board}')
                return 1
        problems = evaluate(row, args.prev_status)
        tag = 'DRY-RUN ' if args.dry_run else ''
        if problems:
            print(tag + refusal_message(row, problems))
            return 2
        print(tag + f"{row.get('id')}: completion would be ACCEPTED by "
                    f"board_complete_gate (TR-249)")
        return 0

    if args.cmd == 'append-completion':
        try:
            verdict = append_completion(args.board, args.task,
                                        _parse_set(args.set),
                                        dry_run=args.dry_run,
                                        prev_status=args.prev_status)
        except CompletionGateError as e:
            print(str(e))
            return 2
        what = 'WOULD APPEND (dry-run)' if args.dry_run else 'APPENDED'
        print(f"{args.task}: {what} — completion accepted by the TR-249 gate")
        return 0

    if args.cmd == 'backlog':
        date_stamp = _utcnow()[:10]
        report, stats = build_backlog_report(args.board, date_stamp)
        if args.dry_run:
            print(f"DRY-RUN backlog: terminal={stats['terminal']} "
                  f"proven={stats['proven']} unproven={stats['unproven']} "
                  '(no report written)')
            return 0
        out = args.out or os.path.join(
            REPO, 'docs', f'completeness-backlog-{date_stamp}.md')
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(report)
        print(f"backlog written: {out} — terminal={stats['terminal']} "
              f"proven={stats['proven']} unproven={stats['unproven']}")
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())

"""TR-249 — the completion gate (docs/traceability-doctrine.md "Completion gate").

Origin: the 2026-10-01 traceability scan found 222 rows marked complete, 8
carrying any evidence field, 0 carrying an evidence run — a board that says
"done" with nothing behind it. The gate makes that state unreachable for NEW
completion transitions without touching historical rows.

Pinned here:
  * acceptance_criteria absent/empty → REFUSED, field named;
  * no evidence (no measured number / artifact path / stated-reason NULL)
    → REFUSED, field named; prose like "GREEN" is not evidence;
  * evidence forms accepted: `evidence` list/str, `witness` (incl.
    `none:<reason>` per doctrine hard rule 3), `evidence_none`;
  * the gate fires ONLY on transitions into `complete`/`done` — non-terminal
    writes and idempotent re-appends of already-terminal rows pass;
  * the CLI's gated writer (`append-completion`) refuses BEFORE any byte is
    written and appends a gate-stamped row when it accepts;
  * the backlog is read-only: it never writes a board byte.

Every test drives tmp_path boards — the real board is never written.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import board_complete_gate as gate  # noqa: E402

PY = ("/home/kara/.hermes/venvs/board/bin/python3"
      if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
      else sys.executable)

GOOD_CRITERIA = ["the thing works", "the thing is verified"]


def write_board(tmp_path, rows):
    p = tmp_path / 'tasks.jsonl'
    p.write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows),
                 encoding='utf-8')
    return str(p)


def base_row(**over):
    row = {'id': 'TR-900', 'status': 'pending', 'title': 'gate fixture'}
    row.update(over)
    return row


# ----------------------------------------------------------------- criteria #

def test_complete_without_criteria_is_refused_and_names_the_field():
    row = base_row(status='complete', evidence='docs/evidence/tr900.md')
    ok, why = gate.criteria_state(row)
    assert ok is False
    assert 'acceptance_criteria' in why
    problems = gate.evaluate(row)
    assert problems and 'acceptance_criteria' in problems[0]


def test_complete_with_empty_criteria_list_is_refused():
    row = base_row(status='complete', acceptance_criteria=[],
                   evidence='docs/evidence/tr900.md')
    ok, _ = gate.criteria_state(row)
    assert ok is False
    assert gate.evaluate(row)


def test_complete_with_criteria_only_whitespace_is_refused():
    row = base_row(status='done', acceptance_criteria=['   ', ''],
                   evidence='docs/evidence/tr900.md')
    assert gate.evaluate(row)


# ----------------------------------------------------------------- evidence #

def test_complete_without_evidence_is_refused_and_names_the_field():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA)
    ok, why = gate.evidence_state(row)
    assert ok is False
    assert 'evidence' in why
    problems = gate.evaluate(row)
    assert len(problems) == 1 and 'evidence' in problems[0]


@pytest.mark.parametrize('bad', ['GREEN', 'done', 'looks good to me', 'ok'])
def test_prose_without_number_or_path_is_not_evidence(bad):
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   evidence=bad)
    ok, why = gate.evidence_state(row)
    assert ok is False
    assert 'evidence' in why
    assert bad in why          # the refusal shows what was rejected


def test_empty_evidence_list_is_no_evidence():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   evidence=[])
    assert gate.evaluate(row)


@pytest.mark.parametrize('good', [
    'docs/evidence/tr900.md',                        # artifact path
    '/home/x/reports/xray.html',                     # absolute path
    'CI run 36953390462: 1443 tests passed',         # measured number
    'latency p50 412ms p99 1100ms',                  # measured numbers
    'none:no external surface claimed; unit tests only',   # stated-reason NULL
])
def test_evidence_forms_accepted(good):
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   evidence=good)
    assert gate.evaluate(row) == []
    ok, why = gate.evidence_state(row)
    assert ok and 'evidence=' in why


def test_none_without_reason_is_refused():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   evidence='none:')
    assert gate.evaluate(row)


def test_witness_locator_is_evidence():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   witness='http:200+sha256:abc123')
    assert gate.evaluate(row) == []


def test_witness_none_with_reason_is_stated_null():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   witness='none:internal-only change, no external surface')
    assert gate.evaluate(row) == []


def test_witness_none_without_reason_is_refused():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   witness='none:')
    assert gate.evaluate(row)


def test_evidence_none_field_is_stated_null():
    row = base_row(status='complete', acceptance_criteria=GOOD_CRITERIA,
                   evidence_none='mechanical rename, nothing measurable')
    assert gate.evaluate(row) == []


def test_gate_row_raises_with_named_fields():
    with pytest.raises(gate.CompletionGateError) as ei:
        gate.gate_row(base_row(status='complete'))
    msg = str(ei.value)
    assert 'acceptance_criteria' in msg and 'evidence' in msg
    assert 'REFUSED' in msg


# ------------------------------------------------------- transition scoping #

def test_pending_write_is_not_gated():
    row = base_row(status='pending')
    assert gate.evaluate(row) == []


def test_in_progress_write_is_not_gated():
    row = base_row(status='in_progress')
    assert gate.evaluate(row) == []


def test_failed_write_is_not_gated():
    row = base_row(status='failed', blocked_reason='provider 402')
    assert gate.evaluate(row) == []


def test_done_is_a_terminal_status_too():
    row = base_row(status='done')
    assert gate.evaluate(row)


def test_idempotent_reappend_of_terminal_row_is_not_gated():
    row = base_row(status='complete', worker_status='complete')
    assert gate.evaluate(row, prev_status='complete') == []
    assert gate.evaluate(row, prev_status='done') == []


def test_transition_from_pending_is_gated():
    row = base_row(status='complete')
    assert gate.evaluate(row, prev_status='pending')


# ------------------------------------------------------- gated writer (CLI) #

def test_append_completion_refuses_before_writing(tmp_path):
    board = write_board(tmp_path, [base_row(acceptance_criteria=GOOD_CRITERIA)])
    before = open(board, encoding='utf-8').read()
    with pytest.raises(gate.CompletionGateError) as ei:
        gate.append_completion(board, 'TR-900', {'status': 'complete'})
    assert 'evidence' in str(ei.value)
    assert open(board, encoding='utf-8').read() == before


def test_append_completion_dry_run_writes_nothing(tmp_path):
    board = write_board(tmp_path, [base_row()])
    with pytest.raises(gate.CompletionGateError) as ei:
        gate.append_completion(board, 'TR-900', {'status': 'complete'},
                               dry_run=True)
    assert 'evidence' in str(ei.value)
    assert len(open(board, encoding='utf-8').readlines()) == 1


def test_append_completion_accepts_and_stamps(tmp_path):
    board = write_board(tmp_path, [base_row()])
    v = gate.append_completion(board, 'TR-900', {
        'status': 'complete', 'worker_status': 'complete',
        'acceptance_criteria': GOOD_CRITERIA,
        'evidence': ['tests/test_board_complete_gate.py::test_append_completion_accepts_and_stamps',
                     'pytest -q tests/: 1 passed']})
    assert v['verdict'] == 'APPEND' and v['written'] is True
    rows = [json.loads(l) for l in open(board, encoding='utf-8')]
    assert len(rows) == 2
    assert rows[-1]['id'] == 'TR-900'
    assert rows[-1]['status'] == 'complete'
    assert rows[-1]['completion_gate']['verdict'] == 'pass'


def test_append_completion_unknown_task_is_refused(tmp_path):
    board = write_board(tmp_path, [base_row()])
    with pytest.raises(gate.CompletionGateError) as ei:
        gate.append_completion(board, 'TR-404', {'status': 'complete',
                                                 'evidence': 'x.md'})
    assert 'TR-404' in str(ei.value)


def test_cli_append_completion_refusal_exit_code_and_output(tmp_path):
    board = write_board(tmp_path, [base_row(acceptance_criteria=GOOD_CRITERIA)])
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'),
         'append-completion', '--board', board, '--task', 'TR-900',
         '--set', 'status=complete', '--set', 'worker_status=complete'],
        capture_output=True, text=True)
    assert r.returncode == 2
    assert 'acceptance_criteria' not in r.stdout    # that field is fine
    assert 'evidence' in r.stdout                   # the missing one is named
    assert len(open(board, encoding='utf-8').readlines()) == 1


def test_cli_check_replays_historical_row(tmp_path):
    board = write_board(tmp_path, [base_row(status='complete')])
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'check',
         '--board', board, '--task', 'TR-900', '--prev-status', 'pending',
         '--dry-run'], capture_output=True, text=True)
    assert r.returncode == 2
    assert 'acceptance_criteria' in r.stdout and 'evidence' in r.stdout
    assert 'DRY-RUN' in r.stdout


# ------------------------------------------------------------------ backlog #

def test_backlog_is_read_only_and_classifies(tmp_path):
    proven = base_row(id='TR-901', status='complete',
                      acceptance_criteria=GOOD_CRITERIA,
                      evidence='docs/evidence/tr901.md')
    unproven = base_row(id='TR-902', status='complete')
    pending = base_row(id='TR-903', status='pending')
    board = write_board(tmp_path, [proven, unproven, pending])
    before = open(board, 'rb').read()
    report, stats = gate.build_backlog_report(board, '2026-10-07')
    assert open(board, 'rb').read() == before      # read-only, byte-identical
    assert stats['terminal'] == 2
    assert stats['proven'] == 1
    assert stats['unproven'] == 1
    assert 'TR-902' in report and 'TR-903' not in report
    assert 'acceptance_criteria+evidence' in report
    assert stats['terminal'] == stats['proven'] + stats['unproven']


def test_backlog_counts_are_derived_from_rows(tmp_path):
    rows = [base_row(id=f'TR-9{n:02d}', status='complete',
                     acceptance_criteria=GOOD_CRITERIA, evidence='r.md')
            for n in range(10, 13)]
    rows.append(base_row(id='TR-999', status='done'))
    board = write_board(tmp_path, rows)
    _, stats = gate.build_backlog_report(board, '2026-10-07')
    assert stats == {'terminal': 4, 'proven': 3, 'unproven': 1}


def test_backlog_cli_writes_named_report(tmp_path):
    board = write_board(tmp_path, [base_row(id='TR-905', status='complete')])
    out = tmp_path / 'backlog.md'
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'backlog',
         '--board', board, '--out', str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert 'TR-905' in out.read_text(encoding='utf-8')


# ------------------------------------------- writer audit (transition point) #

def test_no_inrepo_tasks_writer_bypasses_the_gate():
    """req 3: the gate sits at the single transition point. The audit greps
    every in-repo script that touches tasks.jsonl and fails if a new one
    writes a terminal status without going through the gate."""
    import re
    hits = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith('.py'):
            continue
        path = os.path.join(SCRIPTS, name)
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        if 'tasks.jsonl' not in src and 'tasks_jsonl' not in src:
            continue
        if 'board_complete_gate' in src:
            continue                                   # gated writer: fine
        for m in re.finditer(r"""['"]status['"]\s*[:\]]?\s*,?\s*['"](complete|done)['"]""", src):
            hits.append(f'{name}: {m.group(0)[:60]}')
    assert hits == [], (
        'a tasks.jsonl writer assigns a terminal status without the TR-249 '
        'gate — route it through board_complete_gate.evaluate/gate_row:\n'
        + '\n'.join(hits))


def test_gate_module_docstring_names_the_writers():
    """The gate documents which writers call it (req 3)."""
    src = open(os.path.join(SCRIPTS, 'board_complete_gate.py'),
               encoding='utf-8').read()
    for writer in ('router_modelsdev.py', 'board_stall_check.py',
                   'board_task_intake.py', 'board-merge-driver.py'):
        assert writer in src, f'writer audit missing: {writer}'

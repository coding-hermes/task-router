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
    # TR-252 added the witness axis; these unbound rows are all unwitnessed=0
    assert stats == {'terminal': 4, 'proven': 3, 'unproven': 1,
                     'unwitnessed': 0}


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


# =========================================================================== #
#                     TR-252 — THE SCALE (external witness)                   #
# =========================================================================== #

from datetime import datetime, timedelta, timezone  # noqa: E402

ENVELOPES = os.path.join(REPO, 'docs', 'envelopes')

HEADER = {'kind': 'envelope', 'project': 'task-router', 'version': 1,
          'author': 'Bane (outside the work loop)',
          'created_at': '2026-10-21T00:00:00Z'}


def write_envelope(tmp_path, header, items, name='task-router.jsonl'):
    d = tmp_path / 'envelopes'
    d.mkdir(exist_ok=True)
    p = d / name
    lines = [json.dumps(header, sort_keys=True)]
    lines += [json.dumps(it, sort_keys=True) for it in items]
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(d)


def rel_item(**over):
    it = {'id': 'release-0.2.0',
          'statement': 'v0.2.0 is published as a GitHub Release.',
          'owner': 'Bane',
          'witness': {'kind': 'github_release',
                      'locator': 'coding-hermes/task-router#v0.2.0',
                      'expect': 'a release object for v0.2.0 exists at origin'},
          'rows': ['RELEASE-004', 'RELEASE-005', 'RELEASE-006']}
    it.update(over)
    return it


def bound_row(**over):
    """A row bound to the envelope item via witness = envelope:<p>#<item>."""
    row = {'id': 'RELEASE-006', 'status': 'complete',
           'title': 'v0.2.0 release', 'acceptance_criteria': GOOD_CRITERIA,
           'evidence': 'docs/evidence/rel006.md',
           'witness': 'envelope:task-router#release-0.2.0',
           'author': 'worker-agent-1', 'claimed_at': '2026-10-01T00:00:00Z'}
    row.update(over)
    return row


def obs(item_id='release-0.2.0', observer='external-cron', at=None,
        locator='coding-hermes/task-router#v0.2.0'):
    return {'item_id': item_id, 'observer': observer,
            'locator': locator, 'observed': 'release present',
            'observed_at': at or '2026-10-02T12:00:00Z'}


# ------------------------------------------------------------- envelopes --- #

def test_envelope_parses_items_witnesses_and_version(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    env = gate.load_envelope('task-router', d)
    assert env.version == 1 and env.project == 'task-router'
    assert len(env.items) == 1
    it = env.items[0]
    assert it.id == 'release-0.2.0' and it.owner == 'Bane'
    assert it.witness.kind == 'github_release'
    assert it.witness.locator == 'coding-hermes/task-router#v0.2.0'
    assert it.rows == ['RELEASE-004', 'RELEASE-005', 'RELEASE-006']


def test_real_task_router_envelope_is_valid():
    """The committed envelope parses and pins the release item."""
    env = gate.load_envelope('task-router')
    assert env.project == 'task-router' and env.version >= 1
    ids = [it.id for it in env.items]
    assert 'release-0.2.0' in ids
    kinds = {it.witness.kind for it in env.items}
    assert kinds <= set(gate.Witness.KINDS)


def test_envelope_rejects_unknown_witness_kind(tmp_path):
    d = write_envelope(tmp_path, HEADER,
                       [rel_item(witness={'kind': 'trust_me_bro',
                                          'locator': 'x', 'expect': 'y'})])
    with pytest.raises(gate.ScaleError) as ei:
        gate.load_envelope('task-router', d)
    assert 'witness.kind' in str(ei.value)


def test_envelope_rejects_item_without_expect(tmp_path):
    d = write_envelope(tmp_path, HEADER,
                       [rel_item(witness={'kind': 'http_get',
                                          'locator': 'https://x', 'expect': ''})])
    with pytest.raises(gate.ScaleError) as ei:
        gate.load_envelope('task-router', d)
    assert 'witness.expect' in str(ei.value)


@pytest.mark.parametrize('kind', ['github_tag', 'github_release', 'http_get',
                                  'path_at_origin', 'live_ingress_resolves',
                                  'provider_usage_page', 'ledger_predicate'])
def test_all_seven_witness_kinds_accepted(tmp_path, kind):
    d = write_envelope(tmp_path, HEADER,
                       [rel_item(id='i1', witness={
                           'kind': kind, 'locator': 'some-locator',
                           'expect': 'some expect'})])
    env = gate.load_envelope('task-router', d)
    assert env.items[0].witness.kind == kind


# ---------------------------------------------------- anti-recursion rule --- #

def test_closure_by_row_reference_is_refused_and_names_witness():
    it = gate.EnvelopeItem(
        id='release-0.2.0', statement='s', owner='Bane',
        witness=gate.Witness('github_release', 'coding-hermes/task-router',
                             'release exists'))
    with pytest.raises(gate.ScaleError) as ei:
        gate.validate_closure(it, 'per TR-248 release row — see board')
    msg = str(ei.value)
    assert 'CLOSED BY REFERENCE' in msg and 'witness' in msg.lower()


def test_closure_by_verdict_path_is_refused():
    it = gate.EnvelopeItem(
        id='x', statement='s', owner='Bane',
        witness=gate.Witness('github_release', 'o/r', 'e'))
    with pytest.raises(gate.ScaleError) as ei:
        gate.validate_closure(it, '.gitreins/history/2026-10-19/abc/verdict.json')
    assert 'CLOSED BY REFERENCE' in str(ei.value)


def test_closure_by_report_is_refused():
    it = gate.EnvelopeItem(
        id='x', statement='s', owner='Bane',
        witness=gate.Witness('github_release', 'o/r', 'e'))
    with pytest.raises(gate.ScaleError):
        gate.validate_closure(it, 'as documented in docs/release-readiness.md')


def test_closure_by_observation_is_accepted():
    it = gate.EnvelopeItem(
        id='x', statement='s', owner='Bane',
        witness=gate.Witness('github_release', 'o/r', 'e'))
    gate.validate_closure(it, 'observed 1 release: v0.2.0 draft published')


def test_closure_none_is_refused_as_no_observation():
    it = gate.EnvelopeItem(
        id='x', statement='s', owner='Bane',
        witness=gate.Witness('github_release', 'o/r', 'e'))
    with pytest.raises(gate.ScaleError) as ei:
        gate.validate_closure(it, None)
    assert 'no observation' in str(ei.value)


# ----------------------------------------------------- observation checks --- #

def test_observer_is_author_is_refused_names_witness(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    row = bound_row(author='external-cron')  # the observer IS the author
    env = gate.load_envelope('task-router', d)
    it = env.item('release-0.2.0')
    ok, why = gate.check_observation(it, obs(observer='external-cron'), row)
    assert not ok
    assert 'witness' in why and 'author' in why


def test_observation_before_claim_is_refused(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    row = bound_row(claimed_at='2026-10-05T00:00:00Z')
    env = gate.load_envelope('task-router', d)
    it = env.item('release-0.2.0')
    ok, why = gate.check_observation(
        it, obs(at='2026-10-02T12:00:00Z'), row)
    assert not ok
    assert 'not later than the row claim' in why


def test_observation_after_claim_by_non_author_passes(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    row = bound_row(claimed_at='2026-10-01T00:00:00Z')
    env = gate.load_envelope('task-router', d)
    it = env.item('release-0.2.0')
    ok, why = gate.check_observation(
        it, obs(observer='external-cron', at='2026-10-02T12:00:00Z'), row)
    assert ok, why


def test_observation_without_observer_identity_is_refused(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    env = gate.load_envelope('task-router', d)
    ok, why = gate.check_observation(env.item('release-0.2.0'),
                                     obs(observer=''), None)
    assert not ok and 'observer' in why


def test_observation_locator_must_name_the_witness_surface(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    env = gate.load_envelope('task-router', d)
    ok, why = gate.check_observation(
        env.item('release-0.2.0'),
        obs(locator='https://somewhere-else.example'), None)
    assert not ok and 'does not name the witness surface' in why


def test_record_observation_appends_and_gate_reads_it_back(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    gate.record_observation('task-router', 'release-0.2.0', 'external-cron',
                            'coding-hermes/task-router#v0.2.0',
                            '1 release present', envelopes_dir=d,
                            observed_at='2026-10-02T12:00:00Z')
    obs_loaded = gate.load_observations('task-router', d)
    assert len(obs_loaded) == 1
    assert obs_loaded[0]['observer'] == 'external-cron'
    assert obs_loaded[0]['item_id'] == 'release-0.2.0'


def test_author_recorded_observation_does_not_witness(tmp_path):
    """The author cannot self-witness by recording its own observation."""
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    gate.record_observation('task-router', 'release-0.2.0', 'worker-agent-1',
                            'coding-hermes/task-router#v0.2.0',
                            'trust me', envelopes_dir=d)
    row = bound_row()  # author worker-agent-1
    env = gate.load_envelope('task-router', d)
    problems = gate.scale_state_for_row(row, envelopes_dir=d)
    assert problems and 'witness NOT satisfied' in problems[-1]
    assert 'author' in ' '.join(problems) or 'NOT WITNESSED' in problems[-1]


# ------------------------------------------------- bound row gate behavior -- #

def test_unbound_row_keeps_tr249_behavior(tmp_path):
    """A row with no envelope binding is judged ONLY by TR-249."""
    row = {'id': 'TR-900', 'status': 'complete',
           'acceptance_criteria': GOOD_CRITERIA,
           'evidence': 'run 123: 42 tests passed'}
    assert gate.evaluate(row) == []


def test_bound_row_without_witnessing_is_refused(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    problems = gate.scale_state_for_row(bound_row(), envelopes_dir=d)
    assert any('witness NOT satisfied' in p and 'NOT WITNESSED' in p
               for p in problems)


def test_bound_row_with_qualifying_observation_passes_scale(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    gate.record_observation('task-router', 'release-0.2.0', 'external-cron',
                            'coding-hermes/task-router#v0.2.0',
                            '1 release: v0.2.0', envelopes_dir=d,
                            observed_at='2026-10-02T12:00:00Z')
    problems = gate.scale_state_for_row(
        bound_row(), envelopes_dir=d,
        now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert problems == []


def test_stale_witness_stops_counting(tmp_path):
    d = write_envelope(tmp_path, HEADER,
                       [rel_item(window_days=30)])
    # observed AFTER the claim (2026-09-15 > claim 2026-09-01), but never
    # re-observed within the 30d window relative to now
    gate.record_observation('task-router', 'release-0.2.0', 'external-cron',
                            'coding-hermes/task-router#v0.2.0', 'present',
                            envelopes_dir=d,
                            observed_at='2026-09-15T00:00:00Z')
    problems = gate.scale_state_for_row(
        bound_row(claimed_at='2026-09-01T00:00:00Z'), envelopes_dir=d,
        now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert any('STALE' in p for p in problems)


def test_coverage_prints_fraction_with_envelope_version(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item(), rel_item(id='ci-green')])
    cov = gate.coverage(gate.load_envelope('task-router', d), [])
    assert cov['items'] == 2 and cov['witnessed'] == 0
    assert cov['version'] == 1


def test_coverage_counts_witnessed_within_window(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item(), rel_item(id='ci-green')])
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    gate.record_observation('task-router', 'release-0.2.0', 'external-cron',
                            'coding-hermes/task-router#v0.2.0', 'present',
                            envelopes_dir=d, observed_at=recent)
    cov = gate.coverage(gate.load_envelope('task-router', d),
                        gate.load_observations('task-router', d))
    assert cov['witnessed'] == 1 and cov['stale'] == 0


# --------------------------------------------- live probes (fail-closed) --- #

def test_live_probe_refuses_zero_tags_zero_releases(tmp_path):
    """THE demonstration: the real task-router release item reads
    NOT WITNESSED today — git ls-remote --tags origin = 0 tags and
    gh release list --repo coding-hermes/task-router = 0 releases."""
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    row = bound_row()
    problems = gate.scale_state_for_row(
        row, envelopes_dir=d, live=True,
        workdir=os.path.dirname(SCRIPTS))   # the real origin, live
    assert problems, 'zero tags + zero releases must refuse the close'
    joined = ' '.join(problems)
    assert 'NOT WITNESSED' in joined
    # the refusal carries the externally checkable stop conditions
    assert 'ZERO' in joined.upper()
    assert 'tags' in joined.lower() or 'releases' in joined.lower()


def test_release_item_may_not_close_while_origin_is_empty(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    env = gate.load_envelope('task-router', d)
    it = env.item('release-0.2.0')
    ok, why = gate.probe_witness(
        it, workdir=os.path.dirname(SCRIPTS))
    assert ok is False
    assert 'ZERO' in why.upper()


def test_live_ingress_and_provider_kinds_need_out_of_band_observer(tmp_path):
    d = write_envelope(tmp_path, HEADER, [
        rel_item(id='i1', witness={'kind': 'live_ingress_resolves',
                                   'locator': 'https://x', 'expect': '200'}),
        rel_item(id='i2', witness={'kind': 'provider_usage_page',
                                   'locator': 'usage-page', 'expect': '$'}),
        rel_item(id='i3', witness={'kind': 'ledger_predicate',
                                   'locator': 'ledger', 'expect': 'true'})])
    env = gate.load_envelope('task-router', d)
    for it in env.items:
        ok, why = gate.probe_witness(it)
        assert ok is False
        assert 'OUT-OF-BAND' in why


def test_http_probe_digest(tmp_path, monkeypatch):
    import io
    import urllib.request as ur

    class FakeResp(io.BytesIO):
        status = 200
        __enter__ = io.BytesIO.__enter__
        __exit__ = io.BytesIO.__exit__

    body = b'hello witness'
    import hashlib
    digest = hashlib.sha256(body).hexdigest()

    def fake_urlopen(req, timeout=0):
        return FakeResp(body)

    monkeypatch.setattr(ur, 'urlopen', fake_urlopen)
    it = gate.EnvelopeItem(id='x', statement='s', owner='B',
                           witness=gate.Witness(
                               'http_get', 'https://example.com/artifact',
                               f'sha256:{digest}'))
    ok, why = gate.probe_witness(it)
    assert ok, why
    it_bad = gate.EnvelopeItem(id='x', statement='s', owner='B',
                               witness=gate.Witness(
                                   'http_get', 'https://example.com/a',
                                   'sha256:' + '0' * 64))
    ok, why = gate.probe_witness(it_bad)
    assert not ok and 'MISMATCH' in why


def test_http_probe_fail_closed_on_network_error():
    it = gate.EnvelopeItem(id='x', statement='s', owner='B',
                           witness=gate.Witness(
                               'http_get', 'http://127.0.0.1:1/nope', 'x'))
    ok, why = gate.probe_witness(it)
    assert ok is False and 'failed' in why


# ------------------------------------------------------- self-refutation ---- #

def test_release005_shape_complete_row_stating_nogo_is_refused():
    row = {'id': 'RELEASE-005', 'status': 'complete',
           'acceptance_criteria': GOOD_CRITERIA,
           'evidence': 'CI unverified on HEAD, no tags/Release objects, '
                       'v0.2.0 NO-GO',
           'title': 'v0.2.0 readiness sweep'}
    refused, matched = gate.self_refutation_state(row)
    assert refused and matched
    problems = gate.evaluate(row, prev_status='pending')
    assert any('self-refuting' in p for p in problems)


def test_release006_shape_release_surface_absent_is_refused():
    row = {'id': 'RELEASE-006', 'status': 'done',
           'acceptance_criteria': GOOD_CRITERIA,
           'evidence': 'v0.2.0 CI now GREEN on HEAD, release surface '
                       'still absent',
           'title': 'ship v0.2.0'}
    refused, _ = gate.self_refutation_state(row)
    assert refused


def test_pending_row_with_same_text_is_not_refused():
    row = {'id': 'RELEASE-007', 'status': 'pending',
           'title': 'release surface still absent — next sweep',
           'acceptance_criteria': GOOD_CRITERIA}
    refused, _ = gate.self_refutation_state(row)
    assert refused is False


def test_normal_completion_row_is_not_self_refuting():
    row = {'id': 'TR-900', 'status': 'complete',
           'acceptance_criteria': GOOD_CRITERIA,
           'evidence': 'CI run 39378454973 GREEN: 2354 tests passed',
           'title': 'fix the flaky probe'}
    refused, _ = gate.self_refutation_state(row)
    assert refused is False


def test_self_refused_shape_appears_in_backlog(tmp_path):
    board = write_board(tmp_path, [
        {'id': 'RELEASE-005', 'status': 'complete',
         'acceptance_criteria': GOOD_CRITERIA,
         'evidence': 'v0.2.0 NO-GO: release surface still absent'}])
    report, stats = gate.build_backlog_report(board, '2026-10-21')
    assert stats['unwitnessed'] == 1
    assert 'RELEASE-005' in report


# ------------------------------------------------------- CLI (TR-252) ------ #

def test_cli_check_bound_row_refusal_exit_2_names_witness(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    board = write_board(tmp_path, [bound_row()])
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'check',
         '--board', board, '--task', 'RELEASE-006',
         '--prev-status', 'pending', '--dry-run',
         '--envelopes', d], capture_output=True, text=True)
    assert r.returncode == 2, r.stdout
    assert 'witness' in r.stdout.lower()
    assert 'NOT WITNESSED' in r.stdout


def test_cli_check_bound_row_with_observation_exit_0(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    gate.record_observation('task-router', 'release-0.2.0', 'external-cron',
                            'coding-hermes/task-router#v0.2.0', 'present',
                            envelopes_dir=d,
                            observed_at='2026-10-02T12:00:00Z')
    board = write_board(tmp_path, [bound_row()])
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'check',
         '--board', board, '--task', 'RELEASE-006',
         '--prev-status', 'pending', '--dry-run',
         '--envelopes', d], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_cli_backlog_dry_run_exit_0_prints_totals(tmp_path):
    board = write_board(tmp_path, [
        bound_row(),  # complete, unwitnessed
        {'id': 'TR-901', 'status': 'complete',
         'acceptance_criteria': GOOD_CRITERIA, 'evidence': 'e.md'}])
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'backlog',
         '--board', board, '--envelopes',
         str(write_envelope(tmp_path, HEADER, [rel_item()])),
         '--dry-run'], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert 'terminal=2' in r.stdout
    assert 'unwitnessed=1' in r.stdout
    assert 'unproven=1' in r.stdout


def test_cli_coverage_task_router_reads_not_witnessed_today():
    """The demonstration case, against the REAL committed envelope:
    zero observations recorded → the release item is NOT WITNESSED."""
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'coverage',
         'task-router'], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert 'task-router v1' in r.stdout
    assert 'witnessed 0/' in r.stdout
    assert 'NOT WITNESSED' in r.stdout


def test_cli_record_then_check_witness_flow(tmp_path):
    d = write_envelope(tmp_path, HEADER, [rel_item()])
    board = write_board(tmp_path, [bound_row()])
    r1 = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'record',
         'task-router', '--item', 'release-0.2.0',
         '--observer', 'external-cron',
         '--locator', 'coding-hermes/task-router#v0.2.0',
         '--observed', '1 release present',
         '--at', '2026-10-02T12:00:00Z',
         '--envelopes', d], capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'check',
         '--board', board, '--task', 'RELEASE-006',
         '--prev-status', 'pending', '--dry-run',
         '--envelopes', d], capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout


def test_envelope_shrink_requires_signoff(tmp_path):
    v1_path = os.path.join(write_envelope(tmp_path, HEADER, [rel_item()]),
                           'task-router.jsonl')
    old = gate.parse_envelope(v1_path)
    # v2 drops the item, no sign-off author
    h2 = dict(HEADER, version=2, author='')
    d2 = write_envelope(tmp_path, h2, [], name='v2.jsonl')
    new = gate.parse_envelope(os.path.join(d2, 'v2.jsonl'))
    with pytest.raises(gate.ScaleError) as ei:
        gate.envelope_shrink_signoff(old, new)
    assert 'SIGN-OFF' in str(ei.value)
    # same shrink WITH sign-off author passes
    h3 = dict(HEADER, version=2, author='Bane (shrink approved)')
    d3 = write_envelope(tmp_path, h3, [], name='v3.jsonl')
    new3 = gate.parse_envelope(os.path.join(d3, 'v3.jsonl'))
    gate.envelope_shrink_signoff(old, new3)


def test_envelope_version_must_increase(tmp_path):
    v1 = os.path.join(write_envelope(tmp_path, HEADER, [rel_item()]),
                      'task-router.jsonl')
    old = gate.parse_envelope(v1)
    d2 = write_envelope(tmp_path, dict(HEADER, version=1), [rel_item()],
                        name='same.jsonl')
    new = gate.parse_envelope(os.path.join(d2, 'same.jsonl'))
    with pytest.raises(gate.ScaleError) as ei:
        gate.envelope_shrink_signoff(old, new)
    assert 'INCREASE' in str(ei.value)


def test_cli_envelope_shrink_from_flag(tmp_path):
    d1 = write_envelope(tmp_path, HEADER, [rel_item()], name='v1.jsonl')
    d2 = write_envelope(tmp_path, dict(HEADER, version=2,
                                       author='Bane (ok)'),
                        [], name='task-router.jsonl')
    r = subprocess.run(
        [PY, os.path.join(SCRIPTS, 'board_complete_gate.py'), 'envelope',
         'task-router', '--envelopes', d2, '--shrink-from',
         os.path.join(d1, 'v1.jsonl')],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'shrink check' in r.stdout

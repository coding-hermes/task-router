"""TR-250 — per-project attribution + completeness readout.

The board never said which project or lane a row belongs to, so per-project
questions ("how open is task-router's -qa lane?") were answered by eyeballing
prefixes. Two things must hold now:

  * STAMPING derives project+lane_role from explicit field > workdir basename
    > id prefix, and an underivable row is LEFT UNSTAMPED (reported with a
    reason) — never guessed. An unmapped prefix (INT-, PYTYP- on the live
    board) must surface as underivable, not silently become `primary`.
  * REPORT counts per lane (open rows, oldest open row age, complete rows
    lacking evidence) with a stable JSON schema, and every null carries a
    reason (Bane's null doctrine: an unexplained null on a live row is junk).

The board is NEVER written: the backfill mode is dry-run by construction and
`--apply` is refused loudly.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import project_attribution as pa  # noqa: E402

SCRIPT = os.path.join(REPO, 'scripts', 'project_attribution.py')


def _write_board(path, rows, raw=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        if raw is not None:
            fh.write(raw)
            return
        for r in rows:
            fh.write(json.dumps(r) + '\n')


# ─────────────────────────── timestamp parsing ─────────────────────────────

def test_parse_ts_handles_every_board_spelling():
    # Z-suffixed (the common spelling), offset form, space-separated
    # microseconds (TR-001's completed_at), naive — all read as UTC.
    assert pa.parse_ts('2026-10-05T12:00:00Z').isoformat() == \
        '2026-10-05T12:00:00+00:00'
    assert pa.parse_ts('2026-09-23T12:46:26.126701+00:00') is not None
    assert pa.parse_ts('2026-08-27 05:45:00.000000') is not None
    assert pa.parse_ts('2026-08-27T05:45:00').tzinfo is not None


def test_parse_ts_absent_or_garbage_is_none():
    assert pa.parse_ts(None) is None
    assert pa.parse_ts('') is None
    assert pa.parse_ts('not-a-date') is None
    assert pa.parse_ts(20261007) is None


# ─────────────────────────── derivation + stamping ─────────────────────────

def test_stamp_primary_row_from_tr_prefix():
    row = pa.stamp_row({'id': 'TR-9', 'title': 'x', 'status': 'pending'})
    assert row['project'] == 'task-router'
    assert row['lane_role'] == 'primary'


def test_stamp_satellite_prefix_maps_to_documented_lane():
    measured = {'QA-TASK-ROUTER-1': '-qa',
                'REVIEW-TR-001': '-review',
                'DOC-2': '-docs',
                'README-1': '-docs',
                'RELEASE-002': '-releng'}
    for tid, lane in measured.items():
        row = pa.stamp_row({'id': tid})
        assert row['project'] == 'task-router', tid
        assert row['lane_role'] == lane, tid


def test_stamp_lane_suffix_token_on_tr_id():
    # A lane-suffix token maps too (<id>-sync); a base id stays primary.
    assert pa.stamp_row({'id': 'TR-123-sync'})['lane_role'] == '-sync'
    assert pa.stamp_row({'id': 'TR-123-dogfood'})['lane_role'] == '-dogfood'
    assert pa.stamp_row({'id': 'TR-187'})['lane_role'] == 'primary'
    # `-close` is not a lane role: still primary, never guessed into a lane.
    assert pa.stamp_row({'id': 'TR-132-close'})['lane_role'] == 'primary'


def test_stamp_never_overwrites_explicit_project():
    row = pa.stamp_row({'id': 'TR-9', 'project': 'other-project'})
    assert row['project'] == 'other-project'
    # An explicit foreign project breaks the TR-naming agreement, so the lane
    # is NOT claimed: marker-free id + foreign project -> lane stays absent.
    assert row.get('lane_role') is None
    # ...but an explicit satellite marker in the id still names the lane.
    assert pa.stamp_row({'id': 'QA-1', 'project': 'other-project'})[
        'lane_role'] == '-qa'


def test_stamp_workdir_basename_names_the_project():
    row = pa.stamp_row({'id': 'TR-9',
                        'workdir': '/home/kara/other-repo'})
    assert row['project'] == 'other-repo'
    # workdir names the project but says NOTHING about the lane — no guess.
    assert row.get('lane_role') is None
    att = pa.derive_attribution({'id': 'TR-9',
                                 'workdir': '/home/kara/other-repo'})
    assert att['basis'] == 'workdir'


def test_stamp_unmapped_prefix_is_reported_not_guessed():
    # INT-/PYTYP- prefixes exist on the live board. The PROJECT is derivable
    # (board ownership) and IS stamped; the LANE is not derivable from the
    # id, so it stays ABSENT and the reason is reportable — never guessed.
    row = pa.stamp_row({'id': 'INT-CI-001'})
    assert row['project'] == 'task-router'
    assert 'lane_role' not in row
    att = pa.derive_attribution({'id': 'INT-CI-001'},
                                default_project='task-router')
    assert att['basis'] == 'board-ownership'
    assert att['lane_role'] is None                 # lane: reported, not guessed
    assert att['reason'] == 'unmapped-id-prefix'


def test_stamp_row_without_id_gets_project_not_lane():
    # Board ownership still names the project; NOTHING names the lane, so
    # lane_role stays absent and the reason is reportable.
    row = pa.stamp_row({'title': 'orphan'})
    assert row['project'] == 'task-router'
    assert 'lane_role' not in row
    att = pa.derive_attribution({'title': 'orphan'},
                                default_project='task-router')
    assert att['reason'] == 'row-has-no-id'
    # Strict mode (no board default): fully underivable.
    strict = pa.derive_attribution({'title': 'orphan'})
    assert strict['project'] is None and strict['reason'] == 'row-has-no-id'


def test_derivation_strict_mode_without_board_default():
    att = pa.derive_attribution({'id': 'INT-CI-001'}, default_project=None)
    assert att['project'] is None and att['lane_role'] is None
    assert att['reason'] == 'unmapped-id-prefix'


# ────────────────────────────── the report ─────────────────────────────────

NOW = pa.parse_ts('2026-10-07T12:00:00Z')


def _report(rows, raw=None, default_project='task-router'):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), 'tasks.jsonl')
    _write_board(path, rows, raw=raw)
    return pa.build_report(path, NOW, default_project=default_project)


def test_report_counts_open_and_complete_rows_per_lane():
    rows = [
        {'id': 'TR-1', 'status': 'complete', 'commit_hash': 'abc1234',
         'created_at': '2026-09-01T00:00:00Z'},
        {'id': 'TR-2', 'status': 'pending',
         'created_at': '2026-10-05T12:00:00Z'},
        {'id': 'QA-1', 'status': 'review',
         'created_at': '2026-09-07T00:00:00Z'},
        {'id': 'QA-2', 'status': 'complete', 'worker_summary': 'done',
         'created_at': '2026-09-07T00:00:00Z'},
    ]
    rep = _report(rows)
    proj = rep['projects']['task-router']
    assert proj['lanes']['primary']['open_rows'] == 1
    assert proj['lanes']['primary']['complete_rows'] == 1
    assert proj['lanes']['-qa']['open_rows'] == 1
    assert proj['lanes']['-qa']['complete_rows'] == 1
    # failed/superseded are terminal: neither open nor complete.
    rep2 = _report(rows + [
        {'id': 'TR-3', 'status': 'failed', 'created_at': '2026-10-01T00:00:00Z'},
        {'id': 'TR-4', 'status': 'superseded',
         'created_at': '2026-10-01T00:00:00Z'},
    ])
    p2 = rep2['projects']['task-router']['lanes']['primary']
    assert p2['open_rows'] == 1 and p2['complete_rows'] == 1


def test_report_oldest_open_row_uses_created_at_age():
    rows = [
        {'id': 'TR-2', 'status': 'pending',
         'created_at': '2026-10-05T12:00:00Z'},
        {'id': 'TR-3', 'status': 'in_progress',
         'created_at': '2026-09-07T00:00:00Z'},
    ]
    lane = _report(rows)['projects']['task-router']['lanes']['primary']
    assert lane['oldest_open_row']['id'] == 'TR-3'
    assert lane['oldest_open_row']['age_days'] == 30.5


def test_report_null_oldest_row_carries_a_reason():
    # no-open-rows case
    lane = _report([{'id': 'TR-1', 'status': 'complete',
                     'commit_hash': 'abc'}])['projects']['task-router'][
        'lanes']['primary']
    assert lane['oldest_open_row'] == {'id': None, 'age_days': None,
                                       'reason': 'no-open-rows'}
    # open rows exist but none carries a parseable created_at
    lane2 = _report([{'id': 'TR-5', 'status': 'pending'}])[
        'projects']['task-router']['lanes']['primary']
    assert lane2['oldest_open_row']['reason'] == \
        'no-parseable-created-at on any open row'
    assert lane2['oldest_open_row']['id'] is None


def test_report_complete_row_without_any_evidence_is_counted():
    rows = [
        {'id': 'TR-1', 'status': 'complete', 'commit_hash': None,
         'guard_result': None, 'ci_result': None, 'worker_summary': None,
         'foreman_note': None, 'created_at': '2026-09-01T00:00:00Z'},
        {'id': 'TR-2', 'status': 'complete',
         'created_at': '2026-09-01T00:00:00Z'},
        {'id': 'TR-3', 'status': 'complete', 'guard_result': 'PASS',
         'created_at': '2026-09-01T00:00:00Z'},
        {'id': 'QA-1', 'status': 'complete', 'foreman_note': 'note',
         'created_at': '2026-09-01T00:00:00Z'},
    ]
    rep = _report(rows)
    proj = rep['projects']['task-router']
    assert proj['lanes']['primary']['complete_rows'] == 3
    assert proj['lanes']['primary']['complete_rows_lacking_evidence'] == 2
    assert proj['lanes']['-qa']['complete_rows_lacking_evidence'] == 0
    assert sorted(proj['complete_rows_lacking_evidence_ids']) == \
        ['TR-1', 'TR-2']
    assert proj['complete_rows_lacking_evidence'] == 2


def test_report_underivable_rows_are_listed_with_reasons():
    rows = [
        {'id': 'INT-CI-001', 'status': 'complete'},
        {'title': 'no id at all', 'status': 'pending'},
    ]
    rep = _report(rows)
    proj = rep['projects']['task-router']
    reasons = {r['id']: r['reason'] for r in proj['underivable_rows']}
    assert reasons.get('INT-CI-001') == 'unmapped-id-prefix'
    assert reasons.get(None) == 'row-has-no-id'
    assert rep['totals']['rows_without_id'] == 1


def test_report_schema_is_stable_and_documents_its_sources():
    rep = _report([{'id': 'TR-1', 'status': 'pending'}])
    assert rep['schema'] == 'task-router.project_attribution/v1'
    att = rep['attribution']
    assert att['prefix_map'] == {'TR': 'task-router'}
    assert sorted(att['prefix_lane_map'].items()) == sorted(
        pa.PREFIX_LANE_MAP.items())
    assert 'commit_hash' in att['evidence_fields']
    assert 'complete' in att['terminal_statuses']
    proj = rep['projects']['task-router']
    # lanes_source documents WHERE the lane set came from (AC2).
    assert 'tasks.jsonl' in proj['lanes_source']
    for role in pa.LANE_ROLES:
        assert role in proj['lanes_source'], role


def test_report_counts_malformed_lines_and_duplicate_ids():
    raw = (json.dumps({'id': 'TR-1', 'status': 'pending'}) + '\n'
           + '{"id": "TR-1", torn' + '\n'
           + json.dumps({'id': 'TR-1', 'status': 'complete',
                         'commit_hash': 'a'}) + '\n'
           + '42' + '\n')
    rep = _report(None, raw=raw)
    assert rep['totals']['malformed_lines'] == 2
    assert rep['totals']['duplicate_id_rows'] == 1
    assert rep['totals']['unique_ids'] == 1


# ─────────────────────────────── the CLI ───────────────────────────────────

def _run(argv):
    return subprocess.run([sys.executable, SCRIPT] + argv,
                          capture_output=True, text=True, timeout=120)


def test_cli_report_emits_the_readout(tmp_path):
    board = tmp_path / 'tasks.jsonl'
    _write_board(str(board), [
        {'id': 'TR-1', 'status': 'pending',
         'created_at': '2026-10-05T12:00:00Z'},
        {'id': 'TR-2', 'status': 'complete', 'guard_result': 'PASS'},
    ])
    p = _run(['--report', '--board', str(board),
              '--now', '2026-10-07T12:00:00Z'])
    assert p.returncode == 0, p.stderr
    rep = json.loads(p.stdout)
    assert rep['schema'] == 'task-router.project_attribution/v1'
    lanes = rep['projects']['task-router']['lanes']
    assert lanes['primary']['open_rows'] == 1
    assert lanes['primary']['complete_rows'] == 1


def test_cli_backfill_dry_run_lists_and_never_edits_the_board(tmp_path):
    board = tmp_path / 'tasks.jsonl'
    rows = [{'id': 'TR-1', 'status': 'pending'},
            {'id': 'INT-CI-001', 'status': 'pending'}]
    _write_board(str(board), rows)
    before = board.read_bytes()
    p = _run(['backfill', '--dry-run', '--board', str(board)])
    assert p.returncode == 0, p.stderr
    proposal = json.loads(p.stdout)
    stamped = {r['id']: r for r in proposal['stamped']}
    assert stamped['TR-1']['project'] == 'task-router'
    assert stamped['TR-1']['lane_role'] == 'primary'
    assert {'id': 'INT-CI-001',
            'reason': 'unmapped-id-prefix'} in proposal['underivable']
    assert board.read_bytes() == before  # the board was NOT edited


def test_cli_backfill_apply_is_refused(tmp_path):
    board = tmp_path / 'tasks.jsonl'
    _write_board(str(board), [{'id': 'TR-1', 'status': 'pending'}])
    before = board.read_bytes()
    p = _run(['backfill', '--apply', '--board', str(board)])
    assert p.returncode == 2
    assert 'refus' in p.stderr.lower()
    assert board.read_bytes() == before


def test_cli_stamp_row_prints_stamped_row(tmp_path):
    p = _run(['stamp', '--row', json.dumps({'id': 'TR-42'})])
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['project'] == 'task-router'
    assert out['lane_role'] == 'primary'


def test_cli_report_on_the_real_repo_board_is_live(tmp_path):
    """The readout must run against the repo's own board (read-only)."""
    out = str(tmp_path / 'report.json')
    p = _run(['--report', '--out', out])
    assert p.returncode == 0, p.stderr
    rep = json.load(open(out))
    assert rep['board'].endswith(os.path.join('.coding-hermes', 'board',
                                              'tasks.jsonl'))
    assert rep['totals']['rows'] > 0
    assert 'task-router' in rep['projects']

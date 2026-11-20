"""cos_view_freshness — the COS read-view check must fail on a stale view.

The defect this pins (measured 2026-11-20 on `cos/initiatives`): the table is
declared with a single-file storage path, DuckBrain's writer rotates to numeric
segments and never appends `current.jsonl` again, so the read view freezes at
the pre-rotation row count while the partitions keep growing. The API served
619 rows whose newest `created_at` was 2026-11-16T04:56:10Z while the directory
held 22 files / 12,044 lines / 969 distinct ids.

A check that cannot fail is worthless, so every arm is exercised in BOTH
directions here: the stale shape must FAIL, the fresh shape must PASS, and a
skipped arm must never be reported as PASS.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))
import cos_view_freshness as cvf  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _arm(arms, name):
    return next(a for a in arms if a["arm"] == name)


# --------------------------------------------------------------------------
# parsing / projection
# --------------------------------------------------------------------------
def test_parse_iso_accepts_the_spellings_duckbrain_writes():
    assert cvf.parse_iso("2026-11-16T04:56:10.000Z") == datetime(
        2026, 11, 16, 4, 56, 10, tzinfo=timezone.utc)
    assert cvf.parse_iso("2026-11-16T04:56:10+00:00") == datetime(
        2026, 11, 16, 4, 56, 10, tzinfo=timezone.utc)
    # naive stamps are UTC, not local — a local read would shift the age
    assert cvf.parse_iso("2026-11-16T04:56:10") == datetime(
        2026, 11, 16, 4, 56, 10, tzinfo=timezone.utc)
    assert cvf.parse_iso("") is None
    assert cvf.parse_iso("not-a-date") is None


def test_newest_created_at_ignores_unparseable_rows():
    rows = [{"created_at": "2026-11-16T04:56:10.000Z"},
            {"created_at": "garbage"},
            {"created_at": "2026-11-19T03:02:37.000Z"},
            {}]
    assert cvf.newest_created_at(rows) == datetime(
        2026, 11, 19, 3, 2, 37, tzinfo=timezone.utc)
    assert cvf.newest_created_at([]) is None


def test_project_from_title_reads_the_leading_tag_only():
    assert cvf.project_from_title("[task-router] Evidence: x") == "task-router"
    assert cvf.project_from_title("market-verify openai/gpt-6.1-terra") is None
    assert cvf.project_from_title("") is None
    assert cvf.project_from_title("[unclosed tag") is None


# --------------------------------------------------------------------------
# segment state — the rotation evidence the failure names
# --------------------------------------------------------------------------
def test_segment_state_counts_rotation_and_duplicate_versions(tmp_path):
    table = tmp_path / "tables" / "initiatives"
    table.mkdir(parents=True)
    (table / "current.jsonl").write_text(
        json.dumps({"initiative_id": "a", "created_at": "2026-11-16T04:56:10.000Z"}) + "\n"
        + json.dumps({"initiative_id": "b", "created_at": "2026-11-16T04:56:10.000Z"}) + "\n")
    (table / "0001.jsonl").write_text(
        json.dumps({"initiative_id": "a", "created_at": "2026-11-19T03:02:37.000Z"}) + "\n"
        + json.dumps({"initiative_id": "c", "created_at": "2026-11-19T03:02:37.000Z"}) + "\n")
    state = cvf.segment_state(table)
    assert state["files"] == 2
    assert state["numeric_segments"] == 1
    assert state["rotated"] is True
    assert state["lines"] == 4
    assert state["distinct_ids"] == 3
    # `a` exists twice: the upsert appended a new version rather than rewriting
    assert state["duplicate_versions"] == 1


def test_segment_state_reports_unrotated_table(tmp_path):
    table = tmp_path / "t"
    table.mkdir()
    (table / "current.jsonl").write_text(
        json.dumps({"initiative_id": "a", "created_at": "2026-11-20T00:00:00.000Z"}) + "\n")
    state = cvf.segment_state(table)
    assert state["rotated"] is False
    assert state["numeric_segments"] == 0


# --------------------------------------------------------------------------
# arms — both directions
# --------------------------------------------------------------------------
def test_stale_view_fails_freshness_and_coverage():
    """The measured 2026-11-20 shape: 4-day-old view, 619 rows vs 310+ cards."""
    arms = cvf.evaluate_arms(
        newest=datetime(2026, 11, 16, 4, 56, 10, tzinfo=timezone.utc),
        now=NOW, max_age_hours=24,
        visible_rows=[], expected_project="task-router",
        api_total=619, board_total=310,
    )
    assert _arm(arms, "freshness")["status"] == cvf.FAIL
    assert _arm(arms, "visibility")["status"] == cvf.FAIL
    assert _arm(arms, "coverage")["status"] == cvf.PASS  # 619 >= 310 is not proof
    assert cvf.overall(arms) == cvf.FAIL


def test_fresh_view_passes_every_arm():
    arms = cvf.evaluate_arms(
        newest=NOW - timedelta(hours=2), now=NOW, max_age_hours=24,
        visible_rows=[{"initiative_id": "t_x", "project": "task-router"}],
        expected_project="task-router",
        api_total=1200, board_total=1100,
    )
    assert [a["status"] for a in arms] == [cvf.PASS, cvf.PASS, cvf.PASS]
    assert cvf.overall(arms) == cvf.PASS


def test_visibility_arm_fails_on_duplicate_rows():
    """A glob-the-segments repair without dedupe returns N versions of one card."""
    arms = cvf.evaluate_arms(
        newest=NOW, now=NOW, max_age_hours=24,
        visible_rows=[{"project": "task-router"}, {"project": "task-router"}],
        expected_project="task-router", api_total=12044, board_total=1100,
    )
    assert _arm(arms, "visibility")["status"] == cvf.FAIL
    assert "exactly 1 row" in _arm(arms, "visibility")["detail"]


def test_visibility_arm_fails_on_wrong_project():
    arms = cvf.evaluate_arms(
        newest=NOW, now=NOW, max_age_hours=24,
        visible_rows=[{"project": None}], expected_project="task-router",
        api_total=1200, board_total=1100,
    )
    assert _arm(arms, "visibility")["status"] == cvf.FAIL


def test_skipped_arm_never_upgrades_to_pass():
    arms = cvf.evaluate_arms(
        newest=NOW, now=NOW, max_age_hours=24,
        visible_rows=None, expected_project=None,
        api_total=None, board_total=1100,
    )
    assert _arm(arms, "visibility")["status"] == cvf.SKIP
    assert _arm(arms, "coverage")["status"] == cvf.SKIP
    assert cvf.overall(arms) == cvf.SKIP


def test_unparseable_newest_fails_rather_than_passing():
    arms = cvf.evaluate_arms(
        newest=None, now=NOW, max_age_hours=24,
        visible_rows=[{"project": "task-router"}], expected_project="task-router",
        api_total=1200, board_total=1100,
    )
    assert _arm(arms, "freshness")["status"] == cvf.FAIL
    assert cvf.overall(arms) == cvf.FAIL


# --------------------------------------------------------------------------
# CLI — exit codes are the contract a cron reads
# --------------------------------------------------------------------------
def test_cli_exits_1_on_a_stale_fixture(tmp_path, capsys):
    ns = tmp_path / "cos"
    table = ns / "tables" / "initiatives"
    table.mkdir(parents=True)
    (table / "current.jsonl").write_text(
        json.dumps({"initiative_id": "a", "created_at": "2026-11-16T04:56:10.000Z"}) + "\n")
    (table / "0001.jsonl").write_text(
        json.dumps({"initiative_id": "b", "created_at": "2026-11-19T03:02:37.000Z"}) + "\n")
    boards = tmp_path / "boards"
    boards.mkdir()
    code = cvf.main([
        "--ns-dir", str(ns), "--boards-dir", str(boards),
        "--token-file", str(tmp_path / "absent.token"),
        "--now", "2026-11-20T12:00:00+00:00", "--json",
    ])
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "fail"
    assert report["segments"]["rotated"] is True
    assert report["current_rows"] == 1


def test_cli_exits_2_when_the_table_is_absent(tmp_path):
    code = cvf.main([
        "--ns-dir", str(tmp_path / "nope"), "--boards-dir", str(tmp_path),
        "--token-file", str(tmp_path / "absent.token"),
        "--now", "2026-11-20T12:00:00+00:00",
    ])
    assert code == 2


def test_cli_never_prints_the_token(tmp_path, capsys):
    ns = tmp_path / "cos"
    table = ns / "tables" / "initiatives"
    table.mkdir(parents=True)
    (table / "current.jsonl").write_text(
        json.dumps({"initiative_id": "a", "created_at": "2026-11-20T10:00:00.000Z"}) + "\n")
    token_file = tmp_path / "cos.token"
    token_file.write_text("SUPER-SECRET-TOKEN-VALUE\n")
    boards = tmp_path / "boards"
    boards.mkdir()
    cvf.main([
        "--ns-dir", str(ns), "--boards-dir", str(boards),
        "--token-file", str(token_file), "--base", "http://127.0.0.1:9/tables",
        "--now", "2026-11-20T12:00:00+00:00", "--json",
    ])
    captured = capsys.readouterr()
    assert "SUPER-SECRET-TOKEN-VALUE" not in captured.out
    assert "SUPER-SECRET-TOKEN-VALUE" not in captured.err

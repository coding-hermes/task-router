"""REVIEW-TR-002 — the fleet priority census counts boards once, and honestly.

AC3 of the row asks that "a fleet-wide priority census becomes meaningful". The
thing that makes a census UNmeaningful is not a missing script, it is counting
the same board several times (a symlinked satellite reads its primary's board; a
worktree carries its own byte-identical copy) and then reporting the inflated
histogram as the fleet's.

Every case below is a real shape measured on this host on 2026-09-23:
  * 42 duplicate board copies across 110 discovered paths;
  * a board whose priority value is a JSON fragment because a column splice
    swallowed a neighbouring key (``P0","source":"dogfood-dagger`` — consensus,
    asce, warpfs, dexdat-memory, inference-estimator, escalation-doctrine);
  * `high`/`medium`/`low` as a whole priority scheme (gitreins, 96 rows);
  * a mistyped value `PP3` (wojons-mythos);
  * absent priority (99 rows) vs an off-vocabulary value — different classes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))
import board_priority_census as bpc  # noqa: E402


def _write(path, rows, raw=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        if raw is not None:
            fh.write(raw)
            return
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _board(root, name, rows):
    path = os.path.join(root, name, ".coding-hermes", "board", "tasks.jsonl")
    _write(path, rows)
    return path


# ─────────────────────── value classification ──────────────────────────────

def test_canonical_values_classify_as_canonical():
    for v in bpc.VOCABULARY:
        assert bpc.classify(v) == "canonical", v


def test_bare_digits_are_off_vocabulary_not_absent():
    """`3` is a SECOND SPELLING of P3 — the defect class the row reported."""
    assert bpc.classify("3") == "off-vocabulary"
    assert bpc.classify("0") == "off-vocabulary"


def test_absent_priority_is_its_own_class():
    assert bpc.classify(None) == "absent"
    assert bpc.classify("") == "absent"


def test_a_json_fragment_is_called_corrupt_not_merely_off_vocabulary():
    """A splice that ate the next key means the whole ROW is suspect.

    Filing it only as "off-vocabulary" invites a repair that re-spells the
    priority and leaves the row broken.
    """
    assert bpc.classify('P0","source":"dogfood-dagger') == "corrupt-fragment"
    assert bpc.classify('P1","source":"qa-dagger') == "corrupt-fragment"


def test_prose_scheme_is_off_vocabulary():
    for v in ("high", "High", "medium", "Critical", "urgent", "PP3", "P4"):
        assert bpc.classify(v) == "off-vocabulary", v


def test_census_separates_absent_from_offending():
    """The histogram counts everything; `offending` is the actionable subset."""
    rows = [{"id": "A", "priority": "P1"},
            {"id": "B"},
            {"id": "C", "priority": "3"}]
    s = bpc.census_board(rows)
    assert s["histogram"] == {"P1": 1, "3": 1, "<absent>": 1}
    assert s["offending"] == {"3": ["C"]}
    assert s["classes"] == {"canonical": 1, "absent": 1, "off-vocabulary": 1}


def test_a_corrupt_fragment_is_offending_too():
    """A spliced value is actionable even though its class is not a spelling."""
    rows = [{"id": "A", "priority": 'P0","source":"dogfood-dagger'}]
    s = bpc.census_board(rows)
    assert s["classes"] == {"corrupt-fragment": 1}
    assert list(s["offending"]) == ['P0","source":"dogfood-dagger']


# ──────────────────────── dedupe (the count's integrity) ───────────────────

def test_identical_boards_are_counted_once(tmp_path, monkeypatch):
    """A worktree's byte-identical board copy must not double the histogram."""
    root = str(tmp_path)
    monkeypatch.setattr(bpc, "_TIER_WORKTREE", "/wt/")
    rows = [{"id": "TR-1", "priority": "P2"}, {"id": "TR-2", "priority": "3"}]
    _board(root, "primary", rows)
    _board(root, "wt/copy", rows)  # byte-identical copy
    report = bpc.run([root])
    assert report["boards_scanned"] == 1, report["duplicates"]
    assert report["boards_skipped_as_duplicates"] == 1
    assert report["task_rows_total"] == 2
    assert report["priority_histogram"] == {"P2": 1, "3": 1}


def test_a_symlinked_board_is_counted_once(tmp_path):
    """The -sync/-qa/-pm lanes read the primary's board through a symlink."""
    root = str(tmp_path)
    primary = _board(root, "primary", [{"id": "TR-1", "priority": "P1"}])
    # The satellite's own board DIR is the symlink (that is how the lanes mount
    # the primary's board) — mirroring the real topology, not a symlinked file.
    link_parent = os.path.join(root, "satellite", ".coding-hermes")
    os.makedirs(link_parent, exist_ok=True)
    os.symlink(os.path.dirname(primary), os.path.join(link_parent, "board"))
    report = bpc.run([root])
    assert report["boards_scanned"] == 1, report["duplicates"]
    assert {d["reason"] for d in report["duplicates"]} == {"symlink"}


def test_distinct_boards_are_both_counted(tmp_path):
    """Dedupe must not swallow a genuinely different board."""
    root = str(tmp_path)
    _board(root, "one", [{"id": "TR-1", "priority": "P1"}])
    _board(root, "two", [{"id": "TR-2", "priority": "3"}])
    report = bpc.run([root])
    assert report["boards_scanned"] == 2
    assert report["boards_with_off_vocabulary_priority"] == 1


# ────────────────────────── tiers and the verdict ──────────────────────────

def test_tier_classification():
    assert bpc.classify_path("/x/proj/.coding-hermes/board/tasks.jsonl") == "primary"
    assert bpc.classify_path("/x/worktrees/proj-TASK/.coding-hermes/board/tasks.jsonl") == "worktree"
    assert bpc.classify_path("/x/relocated-from-tmp/2026-09-21/y/.coding-hermes/board/tasks.jsonl") == "archive"
    assert bpc.classify_path("/x/.hermes/release-engineer/repo/.coding-hermes/board/tasks.jsonl") == "archive"


def test_a_dirty_worktree_alone_does_not_fail_the_fleet_verdict(tmp_path, monkeypatch):
    """A worktree snapshot is reported, but it is not the durable surface.

    Failing the fleet verdict on a reaped worktree would make the census
    permanently red for a defect that no longer exists on any primary board.
    """
    root = str(tmp_path)
    monkeypatch.setattr(bpc, "_TIER_WORKTREE", "/wt/")
    _board(root, "proj", [{"id": "TR-1", "priority": "P2"}])
    _board(root, "wt/proj-TASK", [{"id": "TR-1", "priority": "3"}])
    report = bpc.run([root])
    assert report["dirty_boards_by_tier"] == {"worktree": 1}
    assert report["single_vocabulary"] is True


def test_a_dirty_primary_board_fails_the_fleet_verdict(tmp_path):
    root = str(tmp_path)
    _board(root, "proj", [{"id": "TR-1", "priority": "3"}])
    report = bpc.run([root])
    assert report["dirty_boards_by_tier"] == {"primary": 1}
    assert report["single_vocabulary"] is False


# ─────────────────────────── tolerant reading ──────────────────────────────

def test_an_unparseable_line_does_not_kill_the_census(tmp_path):
    """DF-BOARDCTL-9's class: one bad line must not hide a whole board.

    The bad line is COUNTED and reported, so the histogram is never quietly
    presented as complete.
    """
    root = str(tmp_path)
    path = os.path.join(root, "proj", ".coding-hermes", "board", "tasks.jsonl")
    _write(path, None, raw=json.dumps({"id": "TR-1", "priority": "P2"}) + "\n"
                             + '{"id": "TR-2", "priority": broken\n'
                             + json.dumps({"id": "TR-3", "priority": "3"}) + "\n")
    report = bpc.run([root])
    assert report["task_rows_total"] == 2
    assert report["unreadable_boards"] == [{"path": path, "unparseable_lines": 1}]
    assert report["boards_with_off_vocabulary_priority"] == 1


def test_snapshots_are_excluded_from_the_scan(tmp_path):
    """An archived snapshot is not a board anyone routes work from."""
    root = str(tmp_path)
    snap = os.path.join(root, "snapshots", "old", ".coding-hermes", "board", "tasks.jsonl")
    _write(snap, [{"id": "TR-1", "priority": "3"}])
    _board(root, "proj", [{"id": "TR-1", "priority": "P2"}])
    report = bpc.run([root])
    assert report["boards_scanned"] == 1
    assert report["single_vocabulary"] is True


# ───────────────────────────── the CLI contract ────────────────────────────

def test_cli_exit_codes_and_json_shape(tmp_path, capsys):
    root = str(tmp_path)
    _board(root, "proj", [{"id": "TR-1", "priority": "P2"}])
    assert bpc.main(["--root", root, "--json"]) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["single_vocabulary"] is True

    _board(root, "proj2", [{"id": "TR-2", "priority": "3"}])
    assert bpc.main(["--root", root, "--json"]) == 1
    dirty = json.loads(capsys.readouterr().out)
    assert dirty["single_vocabulary"] is False


def test_quiet_prints_nothing(tmp_path, capsys):
    root = str(tmp_path)
    _board(root, "proj", [{"id": "TR-1", "priority": "P2"}])
    assert bpc.main(["--root", root, "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_render_names_the_offending_values_and_the_vocabulary(tmp_path):
    root = str(tmp_path)
    _board(root, "proj", [{"id": "TR-1", "priority": "3"}])
    text = bpc.render(bpc.run([root]))
    assert "RESULT:" in text
    assert "'3' x1" in text
    assert "P0,P1,P2,P3" in text


# ─────────────── the live repo's own board: the row's subject ──────────────

def test_the_task_router_board_itself_is_single_vocabulary():
    """REVIEW-TR-002 was filed about THIS board. Pin the outcome here too.

    The fleet census cannot express this (this repo's board is one primary among
    ~72), and the row's own acceptance criterion is about its own board, so the
    claim is asserted against the repo it belongs to.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board = os.path.join(repo, ".coding-hermes", "board", "tasks.jsonl")
    rows, bad = bpc.parse_board(board)
    assert rows, f"no rows parsed from {board}"
    assert bad == 0, f"{bad} unparseable line(s) in {board}"
    summary = bpc.census_board(rows)
    assert summary["offending"] == {}, (
        f"task-router board carries off-vocabulary priority: {summary['offending']}")

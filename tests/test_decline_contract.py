"""TR-393 decline contract tests.

The property under test is NOT "a function returns a list". It is: a board row
in blocked/failed/review that cannot state WHY it is not complete (structured
`decline`, closed vocabulary) is REFUSED at commit time — and an honest board
passes. The owner's one-command answer (`--report`) prints every non-complete
row with its code, `-` when it legitimately has none (pending rows).

Layers, matching tests/test_model_data_commit_gate.py:
  * CLI tests against fixture boards in tmp_path — exact exit codes and the
    row id / bad code named in the output;
  * a test over the REAL board — the backfill is guarded permanently;
  * real `git` end-to-end tests in a throwaway repo for --staged: the staged
    blob (not the working tree) is what gets judged, the fast path exits 0
    without output when the board is untouched, and the ALLOW_DECLINE_GAP
    escape hatch prints every violation but exits 0.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "decline_contract.py"
BOARD_REL = pathlib.Path(".coding-hermes") / "board" / "tasks.jsonl"

VALID_DECLINE = {
    "code": "DEPENDENCY_UNLANDED",
    "reason": "gated on another row",
    "evidence": "fixture@sha:1",
    "needs": "the dependency to land",
}


def run_cli(args, cwd=ROOT, env_extra=None):
    env = dict(os.environ)
    env.pop("ALLOW_DECLINE_GAP", None)  # never inherit an ambient hatch
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=120,
    )


def write_board(tmp_path, rows):
    path = tmp_path / "board.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return str(path)


def row(rid, status, **kw):
    out = {"id": rid, "status": status, "title": "fixture row"}
    out.update(kw)
    return out


# ---------------------------------------------------------------------------
# --validate over fixture boards
# ---------------------------------------------------------------------------
def test_blocked_row_without_decline_fails_and_names_the_row(tmp_path):
    board = write_board(tmp_path, [
        row("TR-9001", "blocked"),
        row("TR-9002", "pending"),  # pending MAY lack a decline
    ])
    p = run_cli(["--validate", "--board", board])
    assert p.returncode == 1
    out = p.stdout + p.stderr
    assert "TR-9001" in out, out
    assert "decline is missing" in out, out


def test_unknown_code_fails_and_names_the_bad_code(tmp_path):
    bad = dict(VALID_DECLINE, code="WHATEVER")
    board = write_board(tmp_path, [row("TR-9003", "failed", decline=bad)])
    p = run_cli(["--validate", "--board", board])
    assert p.returncode == 1
    out = p.stdout + p.stderr
    assert "TR-9003" in out, out
    assert "WHATEVER" in out, out
    assert "closed vocabulary" in out, out


def test_real_board_validates_clean():
    """Every blocked/failed/review row on the published board carries a valid
    decline (TR-266 et al. from ecbc472 + the TR-393 backfill)."""
    p = run_cli(["--validate"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert "OK" in p.stderr


def test_decline_missing_required_key_and_empty_value_fail(tmp_path):
    board = write_board(tmp_path, [
        row("TR-9004", "review", decline={"code": "CI_RED", "reason": "r"}),
        row("TR-9005", "blocked", decline=dict(VALID_DECLINE, evidence="   ")),
    ])
    p = run_cli(["--validate", "--board", board])
    assert p.returncode == 1
    out = p.stdout + p.stderr
    assert "missing required key(s)" in out and "TR-9004" in out, out
    assert "decline.evidence is empty" in out and "TR-9005" in out, out


def test_unreadable_board_is_an_internal_error(tmp_path):
    p = run_cli(["--validate", "--board", str(tmp_path / "absent.jsonl")])
    assert p.returncode == 2
    # A corrupt line must also be an internal error, never a silent skip.
    board = write_board(tmp_path, [row("TR-9006", "blocked")])
    with open(board, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    p = run_cli(["--validate", "--board", board])
    assert p.returncode == 2


# ---------------------------------------------------------------------------
# --report
# ---------------------------------------------------------------------------
def test_report_prints_pending_and_blocked_rows(tmp_path):
    declined = row("TR-9007", "blocked",
                   decline=dict(VALID_DECLINE, needs="owner authorises the restart"))
    pending = row("TR-9008", "pending")
    done = row("TR-9009", "complete")
    board = write_board(tmp_path, [declined, pending, done])
    p = run_cli(["--report", "--board", board])
    assert p.returncode == 0
    lines = {ln.split("\t")[0]: ln for ln in p.stdout.strip().splitlines()}
    assert set(lines) == {"TR-9007", "TR-9008"}  # complete rows are skipped
    assert lines["TR-9007"].split("\t")[1] == "blocked"
    assert lines["TR-9007"].split("\t")[2] == "DEPENDENCY_UNLANDED"
    assert "owner authorises" in lines["TR-9007"]
    assert lines["TR-9008"].split("\t")[1] == "pending"
    assert lines["TR-9008"].split("\t")[2] == "-"


# ---------------------------------------------------------------------------
# --staged against a real throwaway repo
# ---------------------------------------------------------------------------
def _git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                          capture_output=True, text=True)


@pytest.fixture
def scratch_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / BOARD_REL).parent.mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "fixture@example.invalid", cwd=repo)
    _git("config", "user.name", "fixture", cwd=repo)
    (repo / BOARD_REL).write_text(
        json.dumps(row("TR-9010", "blocked", decline=VALID_DECLINE)) + "\n",
        encoding="utf-8",
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed board", cwd=repo)
    return repo


def test_staged_bad_board_is_blocked_and_names_the_row(scratch_repo):
    (scratch_repo / BOARD_REL).write_text(
        json.dumps(row("TR-9011", "blocked")) + "\n", encoding="utf-8")
    _git("add", BOARD_REL.as_posix(), cwd=scratch_repo)
    p = run_cli(["--staged", "--repo", str(scratch_repo)])
    assert p.returncode == 1
    out = p.stdout + p.stderr
    assert "TR-9011" in out, out


def test_staged_judges_the_blob_not_the_working_tree(scratch_repo):
    """A working-tree fix does NOT hide a violation that is staged."""
    staged_bad = row("TR-9012", "blocked")  # no decline
    fixed_tree = row("TR-9012", "blocked", decline=VALID_DECLINE)
    (scratch_repo / BOARD_REL).write_text(
        json.dumps(staged_bad) + "\n", encoding="utf-8")
    _git("add", BOARD_REL.as_posix(), cwd=scratch_repo)
    (scratch_repo / BOARD_REL).write_text(
        json.dumps(fixed_tree) + "\n", encoding="utf-8")  # working tree is clean
    p = run_cli(["--staged", "--repo", str(scratch_repo)])
    assert p.returncode == 1
    assert "TR-9012" in p.stdout + p.stderr


def test_staged_fast_path_when_board_untouched(scratch_repo):
    (scratch_repo / "unrelated.txt").write_text("x", encoding="utf-8")
    _git("add", "unrelated.txt", cwd=scratch_repo)
    p = run_cli(["--staged", "--repo", str(scratch_repo)])
    assert p.returncode == 0
    assert p.stdout == "" and "VIOLATED" not in p.stderr


def test_staged_escape_hatch_names_violations_and_exits_zero(scratch_repo):
    (scratch_repo / BOARD_REL).write_text(
        json.dumps(row("TR-9013", "review")) + "\n", encoding="utf-8")
    _git("add", BOARD_REL.as_posix(), cwd=scratch_repo)
    p = run_cli(["--staged", "--repo", str(scratch_repo)],
                env_extra={"ALLOW_DECLINE_GAP": "1"})
    assert p.returncode == 0  # allowed, never silently
    out = p.stdout + p.stderr
    assert "ALLOW_DECLINE_GAP" in out, out
    assert "TR-9013" in out, out

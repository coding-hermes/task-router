"""TR-251 — the commit-boundary gate (mechanism 4) + replay gate (mechanism 2).

Origin: TR-011. A row reached status=complete with 4 acceptance criteria and a
PASS guard, and the deliverable it named did not exist — because the completing
agent graded its own homework and no boundary ever looked at the DIFF. The
commit-boundary gate makes that state unreachable at the merge/push boundary:
a diff that flips a row to complete with unbound evidence is REFUSED.

Pinned here (fixture = TR-011's exact shape):
  * a row flipping pending->complete with 4 criteria + guard PASS and NO
    evidence run is REFUSED (exit 1, refusal names the row id);
  * the same row WITH evidence_run_id passes;
  * the same row with a stated-reason null (witness=none:<reason>) passes;
  * an already-complete row re-written complete passes (history not re-judged);
  * a diff that touches nothing terminal passes;
  * the replay gate: a verdict-form run WITH a recorded command replays and
    must agree; a digest-only verdict (no command.json) FAILS — a stored
    digest alone never passes; an unresolvable run id FAILS naming the store;
  * the outcome-probe library: coverage derives from the projects table,
    template placeholders are REFUSED until authored, authorship is recorded.

Every test drives tmp_path boards — the real board is never written.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
sys.path.insert(0, SCRIPTS)

import board_commit_boundary_gate as gate  # noqa: E402
import evidence_replay as replay  # noqa: E402
import outcome_probes as probes  # noqa: E402


# --------------------------------------------------------------------------- #
# TR-011's exact shape: the false completion the fleet actually shipped.       #
# --------------------------------------------------------------------------- #
def tr011_row(task_id="TR-011", **overrides):
    row = {
        "id": task_id,
        "title": "Full CI pass + live E2E readiness — router cleared to move traffic",
        "status": "complete",
        "priority": "P0",
        "guard_result": "PASS",
        "ci_result": "GREEN",
        "commit_hash": "4257241",
        "acceptance_criteria": [
            "GitHub Actions CI green on main (tests + guard parity + smoke + integrity)",
            "live scheduler spawns resolve via the router (ROUTER lines + SPAWN head)",
            "all registry projects resolve with 0 errors / 0 empty chains",
            "breaker OPEN->exclusion->CLOSED cycle proven",
        ],
    }
    row.update(overrides)
    return row


def _lines(*rows):
    return [json.dumps(r) for r in rows]


def test_tr011_shape_is_refused():
    """THE regression fixture: TR-011's shape (4 criteria, guard PASS, false
    claim) must be REFUSED by the boundary gate."""
    old = _lines({"id": "TR-011", "title": "t", "status": "pending", "priority": "P0"})
    new = _lines(tr011_row())
    flipped = gate.rows_flipped_to_complete(old, new)
    assert len(flipped) == 1
    refusals = gate.check_flipped_rows(flipped)
    assert refusals, "TR-011's shape must be refused"
    assert "TR-011" in refusals[0]
    assert "unbound evidence" in refusals[0]


def test_tr011_shape_with_evidence_run_passes():
    old = _lines({"id": "TR-011", "title": "t", "status": "pending", "priority": "P0"})
    new = _lines(tr011_row(evidence_run_id="run-abc123"))
    assert gate.check_flipped_rows(gate.rows_flipped_to_complete(old, new)) == []


def test_tr011_shape_with_detail_evidence_envelope_passes():
    row = tr011_row(detail={"evidence": [{"run_id": "run-xyz", "ts": "2026-10-16"}]})
    old = _lines({"id": "TR-011", "title": "t", "status": "pending", "priority": "P0"})
    assert gate.check_flipped_rows(gate.rows_flipped_to_complete(old, _lines(row))) == []


def test_tr011_shape_with_stated_null_passes():
    old = _lines({"id": "TR-011", "title": "t", "status": "pending", "priority": "P0"})
    new = _lines(tr011_row(witness="none:no-routable-surface-yet"))
    assert gate.check_flipped_rows(gate.rows_flipped_to_complete(old, new)) == []


def test_already_complete_rewrite_is_not_a_transition():
    row = tr011_row()
    old = _lines(row)
    new = _lines(tr011_row(worker_summary="re-append"))
    assert gate.rows_flipped_to_complete(old, new) == []


def test_guard_pass_alone_is_not_evidence():
    """The gate's whole point: guard_result=PASS + criteria prove nothing."""
    row = tr011_row(guard_result="PASS", ci_result="GREEN")
    old = _lines({"id": "TR-011", "title": "t", "status": "in_progress", "priority": "P0"})
    refusals = gate.check_flipped_rows(gate.rows_flipped_to_complete(old, _lines(row)))
    assert refusals


def test_non_board_diff_passes():
    assert gate.rows_flipped_to_complete([], []) == []


def test_file_mode_cli(tmp_path):
    old_f = tmp_path / "old.jsonl"
    new_f = tmp_path / "new.jsonl"
    old_f.write_text(json.dumps({"id": "TR-011", "status": "pending", "priority": "P0"}) + "\n")
    new_f.write_text(json.dumps(tr011_row()) + "\n")
    rc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_commit_boundary_gate.py"),
         "--old-file", str(old_f), "--new-file", str(new_f)],
        capture_output=True, text=True)
    assert rc.returncode == 1
    assert "TR-011" in rc.stderr


# --------------------------------------------------------------------------- #
# Replay (mechanism 2): RE-EXECUTE the recorded command; digest never enough.  #
# --------------------------------------------------------------------------- #
def _make_verdict_run(repo, run_id, command=None, verdict_passed=True):
    """Build a .gitreins/history/<date>/<run-id>/ run dir with verdict.json and
    an optional command.json."""
    run_dir = repo / ".gitreins" / "history" / "2026-10-16" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "verdict.json").write_text(json.dumps({
        "task_id": "TR-011", "passed": verdict_passed, "commit": "abc"}))
    if command is not None:
        (run_dir / "command.json").write_text(json.dumps(command))
    return run_dir


def test_replay_agrees(tmp_path):
    _make_verdict_run(tmp_path, "run-ok", command={
        "command": ["echo", "hello"], "expect_exit": 0,
        "stdout_digest": "sha256:" + __import__("hashlib").sha256(b"hello\n").hexdigest()})
    row = {"id": "R1", "status": "complete", "evidence_run_id": "run-ok"}
    failures, replayed = replay.replay_diff(None, None, str(tmp_path)) if False else (None, None)
    # Direct: resolve + replay the single row.
    kind, payload = replay.resolve_run("run-ok", str(tmp_path))
    assert kind == "verdict"
    cmd = replay.load_command(payload["dir"], "run-ok")
    result = replay.replay_command(cmd, "run-ok")
    assert result["ok"], result["detail"]


def test_replay_disagreement_fails(tmp_path):
    _make_verdict_run(tmp_path, "run-bad", command={
        "command": ["false"], "expect_exit": 0})
    kind, payload = replay.resolve_run("run-bad", str(tmp_path))
    cmd = replay.load_command(payload["dir"], "run-bad")
    result = replay.replay_command(cmd, "run-bad")
    assert not result["ok"]
    assert "replay exit" in result["detail"]


def test_digest_alone_never_passes(tmp_path):
    """A verdict with NO command.json is digest-only evidence: FAIL."""
    _make_verdict_run(tmp_path, "run-digest")  # verdict only, no command
    with pytest.raises(replay.ReplayFailure) as exc:
        replay.load_command(
            tmp_path / ".gitreins/history/2026-10-16/run-digest", "run-digest")
    assert "digest alone never passes" in str(exc.value) or "no recorded command" in str(exc.value)


def test_unresolvable_run_fails_naming_store(tmp_path):
    with pytest.raises(replay.ReplayFailure) as exc:
        replay.resolve_run("run-nowhere", str(tmp_path))
    msg = str(exc.value)
    assert "does not resolve" in msg
    assert ".gitreins/history" in msg


def test_stated_null_is_not_replayable():
    with pytest.raises(replay.ReplayFailure):
        replay.resolve_run("none:no-routable-surface", "/tmp")


def test_replay_diff_end_to_end_refuses_unbound(tmp_path):
    """A staged diff flipping a row with an unresolvable run -> replay fails."""
    (tmp_path / ".git" ).mkdir()
    (tmp_path / ".coding-hermes" / "board").mkdir(parents=True)
    board = tmp_path / ".coding-hermes" / "board" / "tasks.jsonl"
    board.write_text(json.dumps(tr011_row(evidence_run_id="run-nowhere")) + "\n")
    failures, replayed = replay.replay_diff(None, None, str(tmp_path))
    assert failures
    assert any("run-nowhere" in f for f in failures)


# --------------------------------------------------------------------------- #
# Outcome-probe library (mechanism 3).                                         #
# --------------------------------------------------------------------------- #
def test_probe_projects_derive_families(tmp_path):
    table = tmp_path / "projects.jsonl"
    table.write_text(
        "\n".join([
            json.dumps({"id": "proj", "profile": "P0_FORE"}),
            json.dumps({"id": "proj-qa", "profile": "P9_REVIEW"}),
            json.dumps({"id": "proj-foreman", "profile": "P0_FORE"}),
            json.dumps({"id": "other", "profile": "P2_AGENTIC"}),
        ]) + "\n")
    assert probes.load_primary_projects(str(table)) == ["other", "proj"]


def test_probe_template_is_refused_until_authored(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "PROBES_DIR", str(tmp_path))
    path, created = probes.ensure_probe("probe-fixture", "coordinator")
    assert created
    payload = probes._load_probe(path)
    errors = probes.validate_probe(payload)
    assert any("template placeholder" in e for e in errors), errors
    # Author it: replace the placeholder with a real statement.
    payload["probes"][0]["statement"] = "a request through the live ingress resolves with at least one hop"
    assert probes.validate_probe(payload) == []


def test_probe_authorship_is_auditable(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "PROBES_DIR", str(tmp_path))
    probes.ensure_probe("probe-fixture2", "kimi-seat")
    payload = probes._load_probe(probes.probe_path("probe-fixture2"))
    assert payload["authored_by"] == "kimi-seat"
    assert payload["authored_at"]
    errors = probes.validate_probe(payload)
    # The template refusal is expected; authorship fields must NOT be flagged.
    assert not any("authored_by" in e or "authored_at" in e for e in errors)


def test_live_library_covers_every_active_project():
    """THE acceptance probe for the library itself: every active project in
    the committed registry table carries a probe file with authorship."""
    projects = probes.load_primary_projects()
    missing = [p for p in projects if not os.path.exists(probes.probe_path(p))]
    assert not missing, f"probe library missing: {missing[:5]}…"

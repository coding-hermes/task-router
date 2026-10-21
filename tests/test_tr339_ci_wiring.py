"""TR-339: verify the CI wiring of TR251_CI_MODE and reference-gate.

Closes the Tier-2 judge gap: the boundary gate logic itself is tested in
test_tr251_boundary_and_replay.py, but that suite never checks that the CI
workflow *wires* the environment variables and outcome_probes subcommands
that the dogfood findings surfaced.

This file reads the live .github/workflows/ci.yml and asserts the presence
of the three TR-339 sub-fixes that the Tier-2 verdict flagged as unmetered.

TR-343 adds the CI-mode evidence forms: a digit run id must VERIFY through
`gh run view` (workflow grants GH_TOKEN + actions:read), every
unverifiable arm (no gh, no auth, not completed, verdict-form with no
local history) refuses loudly, and a board-only flip bound to a
completed+successful run id goes green through replay_diff under
ci_mode with no .gitreins/history.
"""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YAML = os.path.join(REPO, ".github", "workflows", "ci.yml")
# TR-343: the CI-mode cases import the live evidence_replay module the
# same way tests/test_tr251_boundary_and_replay.py does.
sys.path.insert(0, os.path.join(REPO, "scripts"))


@pytest.fixture()
def ci_content():
    """Return the full text of ci.yml."""
    with open(CI_YAML) as f:
        return f.read()


def test_ci_wires_TR251_CI_MODE_for_boundary_gate(ci_content):
    """339-03: the boundary gate step must have env: TR251_CI_MODE: '1'."""
    # Match the step name for the boundary gate
    assert "TR-251 boundary gate" in ci_content
    # The step must carry the env block
    assert 'TR251_CI_MODE' in ci_content
    assert '"TR251_CI_MODE"' in ci_content or 'TR251_CI_MODE:' in ci_content
    # Verify the env is on the boundary gate step, not just present anywhere
    # Split into steps and check the boundary gate step has the env
    steps = ci_content.split("- name:")
    for step in steps:
        if "TR-251 boundary gate" in step:
            assert "TR251_CI_MODE" in step, (
                "TR251_CI_MODE must be set on the 'TR-251 boundary gate' step")
            break
    else:
        pytest.fail("Could not find 'TR-251 boundary gate' step in ci.yml")


def test_ci_wires_TR251_CI_MODE_for_evidence_replay(ci_content):
    """339-03: the evidence replay step must ALSO have env: TR251_CI_MODE: '1'."""
    steps = ci_content.split("- name:")
    for step in steps:
        if "TR-251 evidence replay" in step:
            assert "TR251_CI_MODE" in step, (
                "TR251_CI_MODE must be set on the 'TR-251 evidence replay' step")
            break
    else:
        pytest.fail("Could not find 'TR-251 evidence replay' step in ci.yml")


def test_ci_wires_reference_gate_step(ci_content):
    """339-05: outcome_probes.py reference-gate must be wired into CI."""
    assert "reference-gate" in ci_content, (
        "CI must invoke `python3 scripts/outcome_probes.py reference-gate`")
    # Verify it's actually called as a command, not just mentioned in a comment
    # Look for the run block that invokes it
    run_blocks = re.findall(r'- name:.*?\n\s+run: \|\n((?:\s+.+\n?)*)', ci_content)
    reference_gate_called = False
    for block in run_blocks:
        if "reference-gate" in block and "outcome_probes.py" in block:
            reference_gate_called = True
            break
    assert reference_gate_called, (
        "reference-gate must be invoked as a command in a CI run block, not just a comment")


def test_ci_wires_check_all_step(ci_content):
    """339-05: outcome_probes.py check-all must be wired into CI."""
    assert "check-all" in ci_content, (
        "CI must invoke `python3 scripts/outcome_probes.py check-all`")
    run_blocks = re.findall(r'- name:.*?\n\s+run: \|\n((?:\s+.+\n?)*)', ci_content)
    check_all_called = False
    for block in run_blocks:
        if "check-all" in block and "outcome_probes.py" in block:
            check_all_called = True
            break
    assert check_all_called, (
        "check-all must be invoked as a command in a CI run block, not just a comment")


# --------------------------------------------------------------------------- #
# TR-343 — the evidence-replay step must be able to VERIFY a bound GitHub
# Actions run id on a runner without local .gitreins/history, while every
# unverifiable form still refuses loudly. Two surfaces:
#   * the workflow wiring (GH_TOKEN + permissions.actions:read), and
#   * the replay module's ci_mode behaviour against a mocked gh.
# --------------------------------------------------------------------------- #


def test_ci_replay_step_carries_GH_TOKEN_and_actions_read(ci_content):
    """TR-343: the replay step's gh invocation must be authenticated and
    authorized. gh authenticates from GH_TOKEN; the default GITHUB_TOKEN
    only carries actions:read when a permissions block grants it — on a
    private repo neither defaults to readable, so assert both surfaces."""
    # Top-level (or job-level) permissions granting actions read.
    assert re.search(r'(?m)^permissions:\s*$', ci_content), (
        "ci.yml must declare a permissions block for TR-343")
    assert re.search(r'(?m)^\s{2}actions:\s*read\s*$', ci_content), (
        "permissions must grant `actions: read` for `gh run view`")
    # The replay step specifically exports GH_TOKEN (not just GITHUB_TOKEN):
    # split into steps and check the evidence replay step's env block.
    steps = ci_content.split("- name:")
    for step in steps:
        if "TR-251 evidence replay" in step:
            assert "GH_TOKEN:" in step, (
                "'TR-251 evidence replay' must export GH_TOKEN for gh run view")
            assert "TR251_CI_MODE" in step
            break
    else:
        pytest.fail("Could not find 'TR-251 evidence replay' step in ci.yml")


def test_resolve_run_ci_id_passes_completed_success_run(monkeypatch):
    """TR-343: a digit run id verified by gh as completed+success resolves
    to the 'ci' kind with the recorded conclusion. Mocked gh — no network,
    no auth. (Negative arm: mutation-test gh returning failure/rc!=0 and
    watch this test go red — the pass must come from the verification.)"""
    import evidence_replay as replay_mod

    def fake_which(name):
        return "/usr/bin/gh" if name == "gh" else None

    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        class P:  # noqa: E701
            returncode = 0
            stdout = '{"status": "completed", "conclusion": "success"}'
            stderr = ""
        return P()

    monkeypatch.setattr(replay_mod.shutil, "which", fake_which)
    monkeypatch.setattr(replay_mod.subprocess, "run", fake_run)
    kind, payload = replay_mod.resolve_run("12345678901", "/nonexistent/repo",
                                           ci_mode=True)
    assert kind == "ci"
    assert payload == {"run_id": "12345678901", "verified_via": "gh run view",
                       "conclusion": "success"}
    # The verification is real gh, not a shape check:
    assert calls["argv"][:4] == ["gh", "run", "view", "12345678901"]
    assert "--json" in calls["argv"]


def test_resolve_run_ci_id_refused_without_gh(monkeypatch):
    """TR-343: no gh binary -> loud refusal, never a pass. This is the
    arm a runner without gh (or a sabotaged PATH) would hit; a pass here
    would mean shape-only validation came back."""
    import evidence_replay as replay_mod
    monkeypatch.setattr(replay_mod.shutil, "which", lambda name: "" )
    with pytest.raises(replay_mod.ReplayFailure, match="gh.*not installed"):
        replay_mod.resolve_run("12345678901", "/nonexistent/repo", ci_mode=True)


def test_resolve_run_ci_id_refused_without_gh_auth(monkeypatch):
    """TR-343: gh present but unauthenticated/unauthorized (the pre-fix
    CI arm: GITHUB_TOKEN without actions:read on a private repo answers
    'Could not resolve to a Run') -> loud refusal quoting gh's refusal,
    never a pass. (gh's error cannot distinguish 'no such run' from
    'no permission to see it'; the gate refuses either way and names
    what gh said.)"""
    import evidence_replay as replay_mod

    def fake_which(name):
        return "/usr/bin/gh" if name == "gh" else None

    def fake_run(argv, **kwargs):
        class P:  # noqa: E701
            returncode = 1
            stdout = ""
            stderr = ("gh: Could not resolve to a Run with the number "
                      "12345678901")
        return P()

    monkeypatch.setattr(replay_mod.shutil, "which", fake_which)
    monkeypatch.setattr(replay_mod.subprocess, "run", fake_run)
    with pytest.raises(replay_mod.ReplayFailure,
                       match="does not exist as a GitHub Actions run"):
        replay_mod.resolve_run("12345678901", "/nonexistent/repo", ci_mode=True)


def test_resolve_run_ci_id_refused_when_not_completed(monkeypatch):
    """TR-343: gh verifies the id but the run is in_progress -> loud
    refusal (only a completed run resolves out-of-band)."""
    import evidence_replay as replay_mod

    def fake_which(name):
        return "/usr/bin/gh" if name == "gh" else None

    def fake_run(argv, **kwargs):
        class P:  # noqa: E701
            returncode = 0
            stdout = '{"status": "in_progress", "conclusion": null}'
            stderr = ""
        return P()

    monkeypatch.setattr(replay_mod.shutil, "which", fake_which)
    monkeypatch.setattr(replay_mod.subprocess, "run", fake_run)
    with pytest.raises(replay_mod.ReplayFailure, match="NOT complete"):
        replay_mod.resolve_run("12345678901", "/nonexistent/repo", ci_mode=True)


def test_ci_mode_verdict_form_refusal_names_command_json_writer(tmp_path):
    """TR-343: a verdict-form id in ci_mode with NO .gitreins/history
    refuses loudly, naming the missing command.json writer — the intended
    behaviour the row pins (pre-fix arm: this message must survive)."""
    import evidence_replay as replay_mod
    with pytest.raises(replay_mod.ReplayFailure,
                       match="command.json writer"):
        replay_mod.resolve_run("verdict-abc123", str(tmp_path), ci_mode=True)


def test_ci_run_id_verification_and_verdict_refusal(tmp_path, monkeypatch):
    """TR-343 acceptance: a board-only commit flipping a row to complete
    whose row carries a bound (verified) GitHub Actions run id goes GREEN
    through replay_diff on a runner with NO repo-local .gitreins/history —
    while a verdict-form id on the same runner refuses loudly. gh is
    mocked (completed + success) at the subprocess seam."""
    import evidence_replay as replay_mod

    def fake_which(name):
        return "/usr/bin/gh" if name == "gh" else None

    real_run = subprocess.run  # the gate's own `git show` must stay real

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "gh":
            class P:  # noqa: E701
                returncode = 0
                stdout = '{"status": "completed", "conclusion": "success"}'
                stderr = ""
            return P()
        return real_run(argv, **kwargs)

    monkeypatch.setattr(replay_mod.shutil, "which", fake_which)
    monkeypatch.setattr(replay_mod.subprocess, "run", fake_run)

    repo = tmp_path
    # A real tiny git repo: HEAD carries the board BEFORE the flip (the
    # "board-only commit" premise), the working file carries the flip.
    git_env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")

    def _git(*argv):
        subprocess.run(["git", *argv], cwd=repo, check=True,
                       capture_output=True, text=True, env=git_env)

    board = repo / ".coding-hermes" / "board"
    board.mkdir(parents=True)
    old_row = {"id": "TR-343-ROW", "status": "in_progress",
               "evidence_run_id": "98765432109"}
    (board / "tasks.jsonl").write_text(
        json.dumps(old_row) + "\n", encoding="utf-8")
    _git("init", "-q")
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "add", ".coding-hermes/board/tasks.jsonl")
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "board: pre-flip")

    # Flip the row to complete in the working tree (the commit under gate).
    new_row = dict(old_row, status="complete")
    (board / "tasks.jsonl").write_text(
        json.dumps(new_row) + "\n", encoding="utf-8")

    # NO .gitreins/history anywhere under repo — the CI condition.
    assert not (repo / ".gitreins" / "history").exists()

    failures, replayed = replay_mod.replay_diff(
        None, None, str(repo), ci_mode=True)
    assert failures == [], failures
    assert replayed == 1

    # Negative arm on the same runner: a verdict-form row refuses loudly.
    verdict_row = {"id": "TR-343-V", "status": "complete",
                   "evidence_run_id": "verdict-deadbeef"}
    (board / "tasks.jsonl").write_text(
        json.dumps(new_row) + "\n" + json.dumps(verdict_row) + "\n",
        encoding="utf-8")
    failures, replayed = replay_mod.replay_diff(
        None, None, str(repo), ci_mode=True)
    assert replayed == 1  # the verified CI-run row still goes green
    assert any("TR-343-V" in f and "command.json writer" in f
               for f in failures), failures

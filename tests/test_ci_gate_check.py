"""TR-240 — the CI gate must refuse to land on a RED main, not just a RED sha.

The failure this pins (2026-09-29): the pre-push gate resolved the sha being
pushed and asked gh for runs ON THAT SHA. A brand-new commit has no completed
run yet, so the gate printed "no completed run — proceeding" and 8f429506 went
RED after the gate said proceed. The law the gate was written for is "refuse
while main is red": when the pushed sha has no completed run of its own, the
gate must fall back to the BRANCH TIP's newest completed run and apply the same
refuse/override semantics.

Every test stubs `runs_for`/`tip` — no test here may call real gh or real git,
and an ambient CI_GATE_BYPASS/ALLOW_RED_PUSH must never leak into a verdict.
"""

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PATH = os.path.join(REPO, "scripts", "ci_gate_check.py")


def _load_gate():
    spec = importlib.util.spec_from_file_location("ci_gate_check", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def gate_module(monkeypatch):
    """Fresh module per test + a clean override environment."""
    monkeypatch.delenv("CI_GATE_BYPASS", raising=False)
    monkeypatch.delenv("ALLOW_RED_PUSH", raising=False)
    yield _load_gate()


def run_gate(monkeypatch, gate, *args):
    """Invoke the gate's main() with argv, returns (exit_code, stdout, stderr)."""
    import contextlib
    import io

    monkeypatch.setattr(sys, "argv", ["ci_gate_check.py", *args])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gate.main()
    return rc, out.getvalue(), err.getvalue()


def run(sha, status, conclusion=None, db=1, title="t", wf="ci"):
    r = {
        "status": status,
        "displayTitle": title,
        "databaseId": db,
        "workflowName": wf,
        "createdAt": "2026-09-29T00:00:00Z",
    }
    if conclusion is not None:
        r["conclusion"] = conclusion
    return r


NEW_SHA = "a" * 40  # the commit being pushed — no completed run of its own yet
TIP_SHA = "b" * 40  # where origin/main actually sits


def stub_tip(monkeypatch, gate, tip_sha=TIP_SHA):
    monkeypatch.setattr(gate, "tip", lambda branch: tip_sha)


# ---------------------------------------------------------------------------
# THE LAW (TR-240): a sha with no completed run must not read as green when the
# branch tip's newest completed run is red.
# ---------------------------------------------------------------------------


def test_unjudged_sha_red_tip_refuses(gate_module, monkeypatch, capsys):
    """RED on current code: gate printed 'proceeding' (exit 0) and 8f429506 went red."""
    gate = gate_module
    stub_tip(monkeypatch, gate)

    def fake_runs_for(sha, limit=10):
        if sha == NEW_SHA:
            return [run(sha, "in_progress", db=7)]  # CI still running on the new commit
        if sha == TIP_SHA:
            return [run(sha, "completed", "failure", db=42, title="broken build")]
        return []

    monkeypatch.setattr(gate, "runs_for", fake_runs_for)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 1
    assert "RED" in err
    assert TIP_SHA[:9] in err  # names the run an operator must inspect
    assert "42" in err


def test_unjudged_sha_red_tip_override_is_loud(gate_module, monkeypatch):
    """Same law, override honoured — but the override must print its warning."""
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setenv("CI_GATE_BYPASS", "1")

    def fake_runs_for(sha, limit=10):
        if sha == NEW_SHA:
            return []
        if sha == TIP_SHA:
            return [run(sha, "completed", "failure", db=42)]
        return []

    monkeypatch.setattr(gate, "runs_for", fake_runs_for)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "OVERRIDDEN by CI_GATE_BYPASS" in err


def test_unjudged_sha_allow_red_push_override_also_works(gate_module, monkeypatch):
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setenv("ALLOW_RED_PUSH", "1")

    def fake_runs_for(sha, limit=10):
        if sha == NEW_SHA:
            return []
        if sha == TIP_SHA:
            return [run(sha, "completed", "failure", db=42)]
        return []

    monkeypatch.setattr(gate, "runs_for", fake_runs_for)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "OVERRIDDEN by ALLOW_RED_PUSH" in err


def test_unjudged_sha_green_tip_proceeds(gate_module, monkeypatch):
    """The flip side: no completed run on the sha, but main's latest is green -> proceed."""
    gate = gate_module
    stub_tip(monkeypatch, gate)

    def fake_runs_for(sha, limit=10):
        if sha == NEW_SHA:
            return []
        if sha == TIP_SHA:
            return [run(sha, "completed", "success", db=41)]
        return []

    monkeypatch.setattr(gate, "runs_for", fake_runs_for)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "success" in out + err  # the OK/proceed line states the tip's verdict


# ---------------------------------------------------------------------------
# Pre-existing contracts, pinned so the fix cannot disturb them.
# ---------------------------------------------------------------------------


def test_red_sha_still_refuses(gate_module, monkeypatch):
    """The pushed sha itself red -> refuse (unchanged)."""
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setattr(
        gate, "runs_for", lambda sha, limit=10: [run(sha, "completed", "failure", db=9)]
    )
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 1
    assert NEW_SHA[:9] in err


def test_red_sha_refuse_override(gate_module, monkeypatch):
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setenv("CI_GATE_BYPASS", "1")
    monkeypatch.setattr(
        gate, "runs_for", lambda sha, limit=10: [run(sha, "completed", "failure", db=9)]
    )
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0


def test_green_sha_still_ok(gate_module, monkeypatch):
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setattr(
        gate, "runs_for", lambda sha, limit=10: [run(sha, "completed", "success", db=5)]
    )
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "OK" in out
    assert "5" in out


def test_sha_is_tip_no_completed_run_still_proceeds(gate_module, monkeypatch):
    """No --sha (tip == sha) and the tip itself has no completed run: state is
    genuinely unknown — old announced-proceed semantics hold (--require-green aside)."""
    gate = gate_module
    stub_tip(monkeypatch, gate, tip_sha=NEW_SHA)  # default mode: sha IS the tip
    monkeypatch.setattr(
        gate, "runs_for", lambda sha, limit=10: [run(sha, "in_progress", db=7)]
    )
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main")
    assert rc == 0
    assert "no completed run" in err
    assert "in flight" in err


def test_sha_is_tip_no_completed_run_require_green_fails(gate_module, monkeypatch):
    gate = gate_module
    stub_tip(monkeypatch, gate, tip_sha=NEW_SHA)
    monkeypatch.setattr(gate, "runs_for", lambda sha, limit=10: [])
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--require-green")
    assert rc == 1


def test_gh_unauthenticated_announced_skip(gate_module, monkeypatch):
    """runs_for raising = gh failed (unauthenticated/offline): SKIP, not fail."""
    gate = gate_module
    stub_tip(monkeypatch, gate)

    def boom(sha, limit=10):
        raise RuntimeError("gh: auth required")

    monkeypatch.setattr(gate, "runs_for", boom)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "SKIP" in err
    rc, out, err = run_gate(
        monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA, "--require-green"
    )
    assert rc == 1


def test_gh_missing_no_runs_at_all_proceeds(gate_module, monkeypatch):
    """gh missing => runs_for returns [] for every sha: skip-announce and proceed."""
    gate = gate_module
    stub_tip(monkeypatch, gate)
    monkeypatch.setattr(gate, "runs_for", lambda sha, limit=10: [])
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "no runs found" in err


def test_tip_lookup_failure_announces_skip_not_refuse(gate_module, monkeypatch):
    """The sha has no completed run and the tip lookup itself errors: the state is
    unknowable -> announced skip (same contract as a first-call failure), NOT a
    fabricated green and NOT a crash."""
    gate = gate_module
    stub_tip(monkeypatch, gate)
    calls = []

    def flaky(sha, limit=10):
        calls.append(sha)
        if sha == NEW_SHA:
            return []
        raise RuntimeError("network blip")

    monkeypatch.setattr(gate, "runs_for", flaky)
    rc, out, err = run_gate(monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA)
    assert rc == 0
    assert "SKIP" in err
    assert calls == [NEW_SHA, TIP_SHA]  # the fallback really consulted the tip
    rc, out, err = run_gate(
        monkeypatch, gate, "--branch", "main", "--sha", NEW_SHA, "--require-green"
    )
    assert rc == 1

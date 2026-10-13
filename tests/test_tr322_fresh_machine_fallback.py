"""TR-322 — INTAKE on a fresh machine with no scorer credentials (P1).

Dogfood run-33 (bunker-mvp, 2026-10-13): task_text_for() FOUND the board row
text, but complexity_requirements() degraded with BOTH scorers unavailable
(classifier: ROUTER_CLASSIFIER_BASE_URL not configured; jev: no OR_JEV in env
or ~/.hermes/.env). The row carried no declared profile, so there was no
profile to degrade to, and main() fell through to
NO_INPUT {error: no project/profile given} — intake recorded
"resolve FAILED — resolve reported: no project/profile given" with an EMPTY
stderr tail (the degrade WARNING existed but _err() suppressed it under the
TR-055 quiet default).

Contract pinned here:

- fresh machine (no scorer credentials, no ROUTER_CLASSIFIER_BASE_URL, no
  OR_JEV anywhere) + a one-row pending board with NO declared profile
  -> resolve returns a chain driven by the documented default profile
  (P0_FORE), NOT NO_INPUT — fail-open preserved (exit 0, no blocking);
- the degrade WARNING reaches stderr EVEN IN QUIET MODE (the intake ledger
  reads the stderr tail — a warning suppressed by default is a blind ledger);
- the payload stamps the cause: complexity.degraded/degrade_reason plus the
  profile_fallback provenance (which profile rescued the resolve, and why);
- a board row that DOES declare a profile keeps its declaration (the fallback
  must never outrank a real declaration — TR-124 precedence is untouched);
- board_task_intake mirrors the complexity provenance FLAT onto the
  intake-resolves.jsonl row (complexity_degraded / degrade_reason / problems /
  profile_fallback), so the ledger names the cause without opening the full
  payload.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

sys.path.insert(0, str(SCRIPTS))
import router_spawn as rs  # noqa: E402
import board_task_intake as intake  # noqa: E402


# ---------------------------------------------------------------------------
# Fresh-machine fixtures
# ---------------------------------------------------------------------------

def _scrub_scorer_env(monkeypatch, tmp_path):
    """The fresh-machine condition: NO scorer credentials anywhere.

    ROUTER_CLASSIFIER_BASE_URL unset kills the classifier lane; OR_JEV /
    JEV_API_KEY / OPENROUTER_API_KEY / OR_API_KEY unset kill the env arm of
    router_jev.key_candidates(), and HOME redirection kills its
    ~/.hermes/.env arm (router_jev reads that file via expanduser) — without
    this the in-process tests would make REAL OpenRouter calls on a control
    host that has the key, and the suite is hermetic on a fresh machine by
    construction, not by luck.
    """
    for name in ("ROUTER_CLASSIFIER_BASE_URL", "ROUTER_CLASSIFIER_FALLBACK_BASE_URL",
                 "OR_JEV", "JEV_API_KEY", "OPENROUTER_API_KEY", "OR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ROUTER_SPAWN_QUIET", raising=False)
    monkeypatch.delenv("ROUTER_MISS_VERBOSE", raising=False)
    home = tmp_path / "fresh-home"
    home.mkdir(exist_ok=True)
    (home / ".hermes").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


def _board_one_pending_row(tmp_path):
    """A one-row pending board with NO `profile` and NO `required_categories`
    — the dogfood run-33 shape."""
    d = tmp_path / ".coding-hermes" / "board"
    d.mkdir(parents=True)
    p = d / "tasks.jsonl"
    p.write_text(json.dumps({
        "id": "DFB-1", "status": "pending", "priority": "P1",
        "title": "Fix the flaky judge",
        "description": "It times out on big diffs."}) + "\n")
    return str(p)


def _fixture_registry_p0(tmp_path):
    """Mini registry with the DEFAULT FALLBACK PROFILE (P0_FORE) present —
    faithful to router_seed.py's output shape (same fixture pattern as
    tests/test_spawn_complexity.py). Three models across two providers, all
    tier-eligible for P0_FORE."""
    cats = ("agent_tick", "debug")
    reg = {"version": 3, "generated_at": "fixture",
           "tables": {"models": [], "model_tier": [], "task_profiles": [],
                      "task_profile_requirements": [], "projects": []}}
    for prov, model, price in (("prov-a", "a1", 1.0), ("prov-a", "a2", 2.0),
                               ("prov-b", "b1", 3.0)):
        reg["tables"]["models"].append({
            "provider": prov, "model": model, "normalized_price": price,
            "token_factor": 1.0, "plan_tier": 0, "data_class": "zdr",
            "valid_to": None, "archive": False})
        for cat in cats:
            reg["tables"]["model_tier"].append(
                {"model": model, "category": cat, "tier": 5})
    reg["tables"]["task_profiles"].append({
        "id": "P0_FORE", "title": "fixture default foreman",
        "created_at": None, "max_consecutive_per_provider": None,
        "max_total_per_provider": None})
    for cat in cats:
        reg["tables"]["task_profile_requirements"].append(
            {"task_id": "P0_FORE", "category": cat, "level": 1})
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(reg))
    return str(path)


def _open_state(tmp_path, providers):
    """Hermetic state dir with every provider open (production gates a
    provider absent from quota-state fail-CLOSED; see
    tests/test_spawn_complexity.py::_open_state)."""
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "quota-state.json").write_text(json.dumps(
        {"updated": "test",
         "providers": {p: {"status": "open"} for p in providers}}))
    (d / "health-state.json").write_text(json.dumps(
        {"providers": {p: {"status": "open"} for p in providers}}))
    (d / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    return str(d)


# ---------------------------------------------------------------------------
# (in-process) scoring degrade + fallback on a profile-less row
# ---------------------------------------------------------------------------

def test_both_scorers_unavailable_degrades_with_named_problems(monkeypatch,
                                                               tmp_path):
    """The exact run-33 scorer condition, in-process: real router_classify +
    router_jev imports, both dead -> meta.degraded with BOTH causes named."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    adhoc, meta = rs.complexity_requirements("fix the flaky judge")
    assert adhoc is None
    assert meta["degraded"] is True
    assert meta["degrade_reason"]
    joined = " ".join(meta["problems"]).lower()
    assert "classifier" in joined and "jev" in joined, \
        "both scorer outages must be named on the meta, not one"


def test_fresh_machine_profileless_board_resolves_default_profile(
        monkeypatch, tmp_path, capsys):
    """THE bug: fresh machine + one-row pending board without a profile
    -> a chain driven by the default profile, not NO_INPUT."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rs, "REGISTRY", _fixture_registry_p0(tmp_path))
    monkeypatch.setattr(rs, "MR", _open_state(tmp_path, ("prov-a", "prov-b")))
    board = _board_one_pending_row(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "router_spawn.py", "--from-task", "DFB-1", "--board", board,
        "--format", "json"])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert "error" not in out, out.get("error")
    assert out.get("head"), "fail-open: the fresh machine still gets a model"
    assert out["resolved_as"] == "profile-arg"
    assert out["profile"] == "P0_FORE"
    # the cause rides the payload, not just stderr
    cx = out["complexity"]
    assert cx["degraded"] is True and cx["degrade_reason"]
    fb = cx["profile_fallback"]
    assert fb["profile"] == "P0_FORE"
    assert "no profile" in fb["reason"]


def test_degrade_warning_fires_in_quiet_mode(monkeypatch, tmp_path, capsys):
    """The empty-stderr-tail half of the bug: the degrade WARNING must reach
    stderr even under the TR-055 quiet DEFAULT (intake records the stderr
    tail; a suppressed warning is a blind ledger row)."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rs, "REGISTRY", _fixture_registry_p0(tmp_path))
    monkeypatch.setattr(rs, "MR", _open_state(tmp_path, ("prov-a", "prov-b")))
    board = _board_one_pending_row(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "router_spawn.py", "--from-task", "DFB-1", "--board", board,
        "--format", "json"])
    rs.main()
    err = capsys.readouterr().err
    assert "complexity scoring degraded" in err, \
        "the degrade warning must survive the quiet default (TR-322)"
    assert "degrade_reason" in err or "scorer" in err.lower()
    assert "P0_FORE" in err, "the rescue itself must be named"


def test_quiet_default_still_holds_elsewhere(monkeypatch, tmp_path, capsys):
    """force=True is scoped: a healthy resolve on the same fixtures stays
    SILENT on stderr by default (the TR-055 contract is not globally undone)."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rs, "REGISTRY", _fixture_registry_p0(tmp_path))
    monkeypatch.setattr(rs, "MR", _open_state(tmp_path, ("prov-a", "prov-b")))
    monkeypatch.setattr(sys, "argv", [
        "router_spawn.py", "--from-task", "DFB-1", "--board",
        _board_one_pending_row(tmp_path),
        "--profile", "P0_FORE", "--format", "json"])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out.get("head")
    assert capsys.readouterr().err == ""


def test_declared_profile_still_outranks_the_fallback(monkeypatch, tmp_path,
                                                      capsys):
    """TR-124 precedence is untouched: a row that declares a profile uses it;
    the fallback must only fire when NOTHING names a profile."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    reg_path = tmp_path / "registry.json"
    reg = json.loads(Path(_fixture_registry_p0(tmp_path)).read_text())
    reg["tables"]["task_profiles"].append({
        "id": "P1_CODING", "title": "fixture coding",
        "created_at": None, "max_consecutive_per_provider": None,
        "max_total_per_provider": None})
    reqs = reg["tables"]["task_profile_requirements"]
    for r in list(reqs):
        reqs.append({"task_id": "P1_CODING", "category": r["category"],
                     "level": 2})
    reg_path.write_text(json.dumps(reg))
    monkeypatch.setattr(rs, "REGISTRY", str(reg_path))
    monkeypatch.setattr(rs, "MR", _open_state(tmp_path, ("prov-a", "prov-b")))
    d = tmp_path / ".coding-hermes" / "board"
    d.mkdir(parents=True)
    p = d / "tasks.jsonl"
    p.write_text(json.dumps({
        "id": "DFB-2", "status": "pending", "profile": "P1_CODING",
        "title": "hard refactor"}) + "\n")
    monkeypatch.setattr(sys, "argv", [
        "router_spawn.py", "--from-task", "DFB-2", "--board", str(p),
        "--profile-from-board", "DFB-2", "--format", "json"])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out["board_profile"]["matched"] is True
    assert out["profile"] == "P1_CODING"
    assert not (out.get("complexity") or {}).get("profile_fallback"), \
        "a matched declaration skips scoring entirely — no fallback stamp"


# ---------------------------------------------------------------------------
# (end-to-end) the REAL dogfood command line on a fresh-machine env
# ---------------------------------------------------------------------------

def test_real_spawn_subprocess_fresh_machine(monkeypatch, tmp_path):
    """The run-33 command line verbatim: subprocess with ROUTER_CLASSIFIER_*
    unset, the OR_* keys unset, and HOME redirected so ~/.hermes/.env cannot
    leak the control host's JEV key. Resolve must return a model.
    (No --no-health: gates only exclude, they cannot block the chain —
    quota-state marks the fixture providers open.)"""
    _scrub_scorer_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ROUTING_REGISTRY", _fixture_registry_p0(tmp_path))
    monkeypatch.setenv("ROUTER_STATE_DIR", _open_state(tmp_path, ("prov-a", "prov-b")))
    monkeypatch.setenv("ROUTER_INTAKE_STATE_DIR", str(tmp_path / "intake-state"))
    board = _board_one_pending_row(tmp_path)
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "router_spawn.py"), "--from-task",
         "DFB-1", "--board", board, "--format", "json"],
        capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-400:]
    out = json.loads(p.stdout)
    assert "error" not in out, out.get("error")
    assert out.get("head", {}).get("model"), "fresh machine resolves a model"
    assert out["profile"] == "P0_FORE"
    assert out["complexity"]["degraded"] is True
    assert out["complexity"]["profile_fallback"]["profile"] == "P0_FORE"
    # the intake ledger's stderr tail now has the cause in it
    assert "complexity scoring degraded" in p.stderr
    assert "P0_FORE" in p.stderr


# ---------------------------------------------------------------------------
# (b) intake-resolves.jsonl mirrors the complexity provenance flat
# ---------------------------------------------------------------------------

def _intake_spawn_cmd(registry, state_dir, home, extra=()):
    """The real spawn argv plus the pinned-env plumbing, as a spawn_cmd list
    the intake runner accepts (env is applied inside the child via the
    router_spawn argv contract: ROUTING_REGISTRY / ROUTER_STATE_DIR are read
    from os.environ — so the intake test launches the child through a tiny
    env-wrapper to keep board_task_intake itself unmodified)."""
    wrapper = home / "spawn_env.py"
    wrapper.write_text(
        "import os, sys\n"
        f"os.environ['ROUTING_REGISTRY'] = {json.dumps(str(registry))}\n"
        f"os.environ['ROUTER_STATE_DIR'] = {json.dumps(str(state_dir))}\n"
        f"os.environ['HOME'] = {json.dumps(str(home))}\n"
        "for k in ('ROUTER_CLASSIFIER_BASE_URL', 'OR_JEV', 'JEV_API_KEY',\n"
        "          'OPENROUTER_API_KEY', 'OR_API_KEY'):\n"
        "    os.environ.pop(k, None)\n"
        "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n")
    return [sys.executable, str(wrapper),
            str(SCRIPTS / "router_spawn.py"), *extra]


def test_intake_ledger_carries_degrade_reason_and_problems(
        monkeypatch, tmp_path, capsys):
    """Full intake pass on the fresh-machine board: the resolves row carries
    complexity_degraded / degrade_reason / problems / profile_fallback FLAT —
    the cause is visible in the ledger without opening the payload."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    registry = _fixture_registry_p0(tmp_path)
    state_dir = _open_state(tmp_path, ("prov-a", "prov-b"))
    board = _board_one_pending_row(tmp_path)
    obj = intake.BoardIntake(
        board=board, state_dir=str(tmp_path / "istate"),
        spawn_cmd=_intake_spawn_cmd(registry, state_dir, tmp_path),
        timeout_s=120, log=lambda msg: None)
    tally = obj.poll(backlog=True)
    assert tally["new"] == 1 and tally["resolved"] == 1 and tally["errors"] == 0
    rows = [json.loads(l) for l in
            (Path(tmp_path / "istate") / intake.RESOLVES_FILE)
            .read_text().splitlines() if l.strip()]
    rec = rows[0]
    assert rec["id"] == "DFB-1"
    assert not rec.get("error"), \
        f"a degraded-but-resolved intake is not an error: {rec.get('error')}"
    assert rec["chain_head"], "the ledger still has the model pick"
    assert rec["complexity_degraded"] is True
    assert rec["degrade_reason"], "the WHY is on the row itself"
    assert rec["problems"], "the per-scorer causes are mirrored flat"
    joined = " ".join(rec["problems"]).lower()
    assert "classifier" in joined and "jev" in joined
    assert rec["profile_fallback"]["profile"] == "P0_FORE"


def test_intake_ledger_healthy_resolve_has_no_degrade_fields(
        monkeypatch, tmp_path):
    """A NON-degraded resolve adds no noise keys: the mirror is additive and
    only appears when there is something to say (same always-additive
    discipline as board_profile/hint). A row that DECLARES its profile skips
    complexity scoring entirely (TR-124 precedence), so the payload carries
    no complexity block at all."""
    _scrub_scorer_env(monkeypatch, tmp_path)
    registry = _fixture_registry_p0(tmp_path)
    state_dir = _open_state(tmp_path, ("prov-a", "prov-b"))
    d = tmp_path / ".coding-hermes" / "board"
    d.mkdir(parents=True)
    board = d / "tasks.jsonl"
    board.write_text(json.dumps({
        "id": "DFB-1", "status": "pending", "profile": "P0_FORE",
        "title": "fix the flaky judge"}) + "\n")
    obj = intake.BoardIntake(
        board=str(board), state_dir=str(tmp_path / "istate"),
        spawn_cmd=_intake_spawn_cmd(registry, state_dir, tmp_path,
                                    extra=["--profile-from-board", "DFB-1"]),
        timeout_s=120, log=lambda msg: None)
    tally = obj.poll(backlog=True)
    assert tally["resolved"] == 1 and tally["errors"] == 0
    rows = [json.loads(l) for l in
            (Path(tmp_path / "istate") / intake.RESOLVES_FILE)
            .read_text().splitlines() if l.strip()]
    rec = rows[0]
    assert rec["chain_head"]
    assert "degrade_reason" not in rec and "problems" not in rec
    assert not rec.get("complexity_degraded")

"""TR-023 + TR-024 regression battery — CLI contract tests from the 3-judge
usability review (2026-08-28).

TR-023 (fail-open breach): --profile-req input validation honesty. Every
invalid input → {error, code: INVALID_REQUIREMENT, retryable: false} + exit 0
(never a traceback, never a silently-weakened requirement). Unknown profile →
'profile X not in registry'; --profile + project conflict reported via stderr;
text format prints the error field.

TR-024 (--json real everywhere): router_pricing --json, router_plan_sweep
--json, router_circuit status --json are PURE JSON on stdout (json.loads
succeeds); router-data-quality.sh never truncates the gap report and aborts on
required-step failure.

TR-058 (usage errors are not runtime failures): the CLI wrapper's fail-open
coercion covers RUNTIME failures only. `router plan-sweep --dry-run` (flag
does not exist) exits 2 with argparse's message and no "coerced to 0" line;
`router spawn --bogus` still exits 0 (spawn's contract is absolute, AGENTS.md);
non-fail-open commands keep their native codes.

All tests run the REAL scripts via subprocess with hermetic env (temp data dir
/ state dir) — no mocks, no import-time env games.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable


def run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _hermetic_env(tmp_path):
    """ROUTING_DATA_DIR → temp copy of data/tables; ROUTER_STATE_DIR → temp."""
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state)}


# ============================================================ TR-023: spawn ==

def _spawn_err(args, tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), *args, "--format", "json",
            env_extra=env)
    assert p.returncode == 0, f"fail-open: exit must be 0, got {p.returncode}: {p.stderr}"
    data = json.loads(p.stdout)
    assert "error" in data, f"expected error dict, got: {p.stdout}"
    return p, data


def test_profile_req_bare_word_fails_open(tmp_path):
    """'reasoning=high' (non-int level) → JSON error, exit 0, never traceback."""
    p, data = _spawn_err(["--profile-req", "reasoning=high"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert data["retryable"] is False


def test_profile_req_empty_arg_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", ""], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert data["retryable"] is False


def test_profile_req_missing_equals_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", "reasoning5"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"


def test_profile_req_out_of_range_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", "reasoning=9"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert "out of range" in data["error"]


def test_profile_req_typo_category_rejected(tmp_path):
    """A typo'd category must NOT be accepted (would silently weaken to -1)."""
    p, data = _spawn_err(["--profile-req", "typo_category=-5"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert "unknown category" in data["error"]
    assert "typo_category" in data["error"]


def test_bogus_profile_not_in_registry(tmp_path):
    p, data = _spawn_err(["--profile", "NOT_A_PROFILE"], tmp_path)
    assert "not in registry" in data["error"]
    assert data["code"] == "PROFILE_NOT_FOUND"
    assert data["retryable"] is False


def test_text_format_prints_error_field(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "--profile-req", "nope=3",
            "--format", "text", env_extra=env)
    assert p.returncode == 0
    assert "ERROR:" in p.stdout
    assert "INVALID_REQUIREMENT" in p.stdout


def test_profile_project_conflict_reported_stderr(tmp_path):
    """Both --profile and project given → stderr names which wins; JSON pure.

    TR-055: stderr telemetry is quiet by default, so the conflict warning is
    asserted under the opt-in audit trail (ROUTER_MISS_VERBOSE=1) and asserted
    ABSENT on the default run.
    """
    env = _hermetic_env(tmp_path)
    default = run(os.path.join(SCRIPTS, "router_spawn.py"), "coding-hermes-scheduler",
                  "--profile", "P4_SECURITY", "--format", "json", env_extra=env)
    assert default.returncode == 0
    assert default.stderr == ""          # TR-055: quiet by default
    assert json.loads(default.stdout)    # stdout stays pure JSON either way

    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "coding-hermes-scheduler",
            "--profile", "P4_SECURITY", "--format", "json",
            env_extra=dict(env, ROUTER_MISS_VERBOSE="1"))
    assert p.returncode == 0
    assert "WARNING" in p.stderr
    assert "resolving via project" in p.stderr
    data = json.loads(p.stdout)  # stdout must stay pure JSON
    assert data["profile"] != "P4_SECURITY"  # project's profile won


def test_profile_req_valid_still_resolves(tmp_path):
    """Regression guard: valid ad-hoc requirements still resolve (no gate change)."""
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "--profile-req",
            "reasoning=5 debug=3 vision=-2", "--format", "json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "error" not in data
    assert "gate" in data


# ============================================================ TR-024: --json ==

def test_pricing_dry_run_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_pricing.py"), "--dry-run", "--json",
            env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)  # must parse — no prose on stdout
    assert set(data) == {"dry_run", "priced", "gaps", "filled_public"}
    assert data["dry_run"] is True
    assert isinstance(data["priced"], list) and isinstance(data["gaps"], list)


def test_pricing_apply_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_pricing.py"), "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert data["dry_run"] is False
    assert set(data) == {"dry_run", "priced", "gaps", "filled_public"}


def test_plan_sweep_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_plan_sweep.py"), "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)  # pure JSON — prose went to stderr
    assert set(data) == {"disabled", "lanes"}
    assert isinstance(data["lanes"], list)


def test_circuit_status_json_contract(tmp_path):
    env = _hermetic_env(tmp_path)
    # record a failure → pair OPEN; then a second pair with an expired breaker
    p = run(os.path.join(SCRIPTS, "router_circuit.py"), "record-failure",
            "provider-a", "model-1", "boom", env_extra=env)
    assert p.returncode == 0
    st = json.load(open(os.path.join(env["ROUTER_STATE_DIR"], "circuit-state.json")))
    st["pairs"]["provider-b/model-2"] = {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                                         "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"}
    json.dump(st, open(os.path.join(env["ROUTER_STATE_DIR"], "circuit-state.json"), "w"))
    p = run(os.path.join(SCRIPTS, "router_circuit.py"), "status", "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "pairs" in data
    by = {e["pair"]: e for e in data["pairs"]}
    assert by["provider-a/model-1"]["state"] == "OPEN"
    assert by["provider-a/model-1"]["failures"] == 1
    assert by["provider-a/model-1"]["reason"] == "boom"
    assert by["provider-b/model-2"]["state"] == "cooling"  # expired → documented semantics


def test_data_quality_script_no_json_truncation():
    """AC4: the gap report's JSON must never be head/tail-truncated."""
    src = open(os.path.join(SCRIPTS, "router-data-quality.sh")).read()
    gaps_line = [l for l in src.splitlines() if "router_gaps.py" in l]
    assert gaps_line, "gap report line missing"
    assert "head -" not in gaps_line[0], "gap JSON must not be truncated: " + gaps_line[0]
    assert "tail -" not in gaps_line[0], "gap JSON must not be truncated: " + gaps_line[0]
    # real exit codes: required steps must abort the pipeline on failure
    assert "set -euo pipefail" in src or "set -e" in src


def test_pricing_json_dry_run_matches_non_json_summary(tmp_path):
    """--json and prose agree on counts (the flag is real, not a parallel path)."""
    env = _hermetic_env(tmp_path)
    pj = run(os.path.join(SCRIPTS, "router_pricing.py"), "--dry-run", "--json", env_extra=env)
    data = json.loads(pj.stdout)
    assert len(data["priced"]) + len(data["gaps"]) >= 1


# ==================================================== TR-058: usage-error exits

# The installed console script's shape, as a fresh process: `router <args>`.
_ENTRY = ("import sys; sys.argv = ['router'] + sys.argv[1:]; "
          "from task_router.cli import main; sys.exit(main())")


def _router(args, tmp_path, timeout=180):
    """`router <args>` in a FRESH process with a hermetic data home.

    TASK_ROUTER_HOME is redirected to tmp_path so the wrapper's first-run
    bootstrap and env exports never touch real state (same shape as
    tests/test_cli_paths.py `_cli`, which is the module that owns the
    console-script entry point).
    """
    env = dict(os.environ)
    env["TASK_ROUTER_HOME"] = str(tmp_path / "dh")
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([PY, "-c", _ENTRY, *args], capture_output=True,
                          text=True, env=env, timeout=timeout, cwd=REPO)


def test_plan_sweep_usage_error_exits_2(tmp_path):
    """TR-058 AC1/AC6 — a bogus flag on plan-sweep must not read as success.

    Pre-fix: `router plan-sweep --dry-run` printed argparse's usage AND
    "router: plan-sweep exited 2 — fail-open (coerced to 0)", then exited 0 —
    so a cron or script calling it could never detect the typo by exit code.
    """
    p = _router(["plan-sweep", "--dry-run"], tmp_path)
    assert p.returncode != 0, f"usage error masked as success: {p.stderr}"
    assert p.returncode == 2, p.stderr
    assert "unrecognized arguments: --dry-run" in p.stderr
    # no fail-open line for a typo: the coercion must not even claim to run
    assert "coerced to 0" not in p.stderr


def test_spawn_usage_error_stays_fail_open(tmp_path):
    """AC2 — spawn's fail-open contract is absolute (AGENTS.md: router_spawn.py
    must NEVER block the scheduler). Its coercion is unconditional, so a
    caller with a fixed argv sees 0 exactly as before."""
    p = _router(["spawn", "--bogus"], tmp_path)
    assert p.returncode == 0, p.stderr
    assert "coerced to 0" in p.stderr


def test_probefix_usage_error_keeps_its_exit_status(tmp_path):
    """The OTHER fail-open operator tool follows plan-sweep, not spawn: a
    typo'd flag is operator error, so probefix propagates the 2."""
    p = _router(["probefix", "--bogus"], tmp_path)
    assert p.returncode == 2, p.stderr
    assert "coerced to 0" not in p.stderr


@pytest.mark.parametrize("cmd", ["validate", "circuit", "status"])
def test_non_fail_open_usage_errors_unchanged(cmd, tmp_path):
    """AC3/AC4 — commands outside FAIL_OPEN already propagated exit 2; the
    TR-058 guard must not disturb them."""
    p = _router([cmd, "--bogus"], tmp_path)
    assert p.returncode == 2, p.stderr


def test_fail_open_coercion_table(monkeypatch, tmp_path):
    """The policy itself, one row per (command, failure class).

    `dispatch` is monkeypatched so each row exercises the wrapper's own
    decision instead of relying on a script that happens to fail that way:
    SystemExit(1) stands for a RUNTIME failure (a table that cannot be read,
    a provider that is down) and must stay fail-open for every FAIL_OPEN
    command; SystemExit(2) is argparse and propagates only for
    USAGE_ERROR_PROPAGATES.
    """
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from task_router import cli

    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "dh"))

    def rc_for(cmd, exc):
        def _boom(*_a, **_k):
            raise exc
        monkeypatch.setattr(cli, "dispatch", _boom)
        return cli.main([cmd])

    # runtime failure (exit 1) — fail-open preserved for the whole trio
    assert rc_for("plan-sweep", SystemExit(1)) == 0
    assert rc_for("probefix", SystemExit(1)) == 0
    assert rc_for("spawn", SystemExit(1)) == 0
    # dispatch exception (a runtime failure class of its own) — still 0
    assert rc_for("plan-sweep", RuntimeError("unreadable tables")) == 0
    assert rc_for("spawn", RuntimeError("unreadable tables")) == 0

    # usage error (exit 2) — operator error, propagates for plan-sweep/probefix
    assert rc_for("plan-sweep", SystemExit(2)) == 2
    assert rc_for("probefix", SystemExit(2)) == 2
    # ...but spawn's contract is absolute (AC2)
    assert rc_for("spawn", SystemExit(2)) == 0

    # success and the --help convention are untouched
    assert rc_for("plan-sweep", SystemExit(0)) == 0
    assert rc_for("plan-sweep", SystemExit(None)) == 0
    # non-fail-open commands were never coerced
    assert rc_for("validate", SystemExit(1)) == 1
    assert rc_for("validate", RuntimeError("boom")) == 1


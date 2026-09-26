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

TR-059 (bare profile names are not dead ends): the project positional is also
accepted as a PROFILE id/tag (`router spawn P1_CODING` == `--profile
P1_CODING`) and the payload then carries the canonical `use --profile X` hint;
a name that is neither keeps the unchanged 'not in registry' error with no
hint; a case-insensitive near-miss of a profile gains a
'matches profile X, use --profile X' hint; an exact project row still wins
over a profile name; and `router estimate X` accepts the same positional.
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


# =============================================== TR-059: bare profile names

def _open_state_env(tmp_path):
    """_hermetic_env + a quota-state whitelisting EVERY provider as 'open'.

    _hermetic_env leaves the state dir empty, which is fail-closed by design
    (absent gate state = gated). The TR-059 assertions are about a REAL chain
    and about two callers agreeing on it, so the gate policy is made explicit
    here — a resolver change that silently emptied the chain would otherwise
    make the parity assertions pass vacuously.
    """
    env = _hermetic_env(tmp_path)
    with open(os.path.join(DATA_DIR, "providers.jsonl")) as f:
        providers = [json.loads(l)["id"] for l in f if l.strip()]
    with open(os.path.join(env["ROUTER_STATE_DIR"], "quota-state.json"), "w") as f:
        json.dump({"updated": "test",
                   "providers": {p: {"status": "open"} for p in providers}}, f)
    return env


def _spawn(args, env):
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), *args, env_extra=env)
    assert p.returncode == 0, f"fail-open: exit must be 0: {p.stderr}"
    return p, json.loads(p.stdout)


def _pairs(doc):
    return [(h.get("provider"), h.get("model")) for h in doc.get("chain") or []]


def test_bare_profile_name_in_project_slot_resolves(tmp_path):
    """TR-059 (AC1) — `router spawn P1_CODING` is no longer a dead end.

    Pre-fix: {"error": "project P1_CODING not in registry"} even though
    P1_CODING is a valid profile — the caller cannot tell 'typo' from 'right
    id, wrong flag'.
    """
    env = _open_state_env(tmp_path)
    p, data = _spawn(["P1_CODING", "--format", "json"], env)
    assert "error" not in data, data.get("error")
    assert "chain" in data
    assert data["chain"], "empty chain — nothing was resolved"
    assert data["profile"] == "P1_CODING"
    assert data["resolved_as"] == "profile"
    assert data["project"] == "P1_CODING"   # the input is echoed, not swapped
    # TR-059-FIX: the caller is told the canonical form on the way through
    assert data["hint"] == "use --profile P1_CODING"
    assert p.stderr == ""                   # TR-055: quiet by default


def test_profile_flag_run_carries_no_hint(tmp_path):
    """Guard: the hint exists because the PROJECT slot named a profile. The
    documented --profile call must stay byte-hint-free (null), so nothing
    downstream mistakes a correct call for a corrected one."""
    env = _open_state_env(tmp_path)
    _, flag = _spawn(["--profile", "P1_CODING", "--format", "json"], env)
    assert flag["resolved_as"] == "profile-arg"
    assert flag["hint"] is None
    _, proj = _spawn(["9router", "--format", "json"], env)
    assert proj["resolved_as"] == "project"
    assert proj["hint"] is None


def test_bare_profile_resolve_matches_the_profile_flag(tmp_path):
    """TR-059 (AC2) — auto-resolution is CONSISTENT with --profile: same
    profile, same chain (same order), same head. Only provenance differs."""
    env = _open_state_env(tmp_path)
    _, bare = _spawn(["P1_CODING", "--format", "json"], env)
    _, flag = _spawn(["--profile", "P1_CODING", "--format", "json"], env)
    assert bare["profile"] == flag["profile"] == "P1_CODING"
    assert _pairs(bare), "empty chain — the parity assertion would be vacuous"
    assert _pairs(bare) == _pairs(flag)
    assert (bare["head"] or {}).get("model") == (flag["head"] or {}).get("model")
    assert bare["gate"] == flag["gate"]
    assert bare["resolved_as"] == "profile"
    assert flag["resolved_as"] == "profile-arg"


def test_bare_profile_warning_is_opt_in(tmp_path):
    """The auto-resolution is auditable without polluting stdout/stderr on the
    default run (TR-055): ROUTER_MISS_VERBOSE=1 brings the trail back."""
    env = _open_state_env(tmp_path)
    p, data = _spawn(["P1_CODING", "--format", "json"],
                     dict(env, ROUTER_MISS_VERBOSE="1"))
    assert "is a profile, not a project" in p.stderr
    assert data["resolved_as"] == "profile"   # stdout stayed pure JSON


def test_unknown_project_error_unchanged_without_hint(tmp_path):
    """TR-059 (AC3) — a name that is neither a project nor a profile keeps the
    exact pre-fix error and gains NO hint: a false 'use --profile' would send
    the caller chasing a profile that does not exist."""
    env = _open_state_env(tmp_path)
    _, data = _spawn(["some-nonexistent-thing", "--format", "json"], env)
    assert data["error"] == "project some-nonexistent-thing not in registry"
    assert "hint" not in data and "matched_profile" not in data
    assert "use --profile" not in data["error"]
    assert set(data) == {"error", "data_home"}   # shape unchanged too


def test_case_mismatched_profile_name_gets_a_hint(tmp_path):
    """TR-059 bonus — the hint fires ONLY on a real approximate match
    (case-insensitive exact on id or tag) and names the profile to use."""
    env = _open_state_env(tmp_path)
    _, data = _spawn(["p1_coding", "--format", "json"], env)
    assert "not in registry" in data["error"]
    assert "matches profile P1_CODING, use --profile P1_CODING" in data["error"]
    assert data["error"] == ("project p1_coding not in registry — matches "
                            "profile P1_CODING, use --profile P1_CODING")
    assert data["hint"] == "use --profile P1_CODING"
    assert data["matched_profile"] == "P1_CODING"


def test_exact_project_id_still_wins_over_a_profile_name(tmp_path):
    """TR-059 precedence — the project table is consulted FIRST, so a PROJECT
    row that happens to share a profile's name keeps its own meaning (the
    auto-profile path can never shadow an existing project)."""
    env = _open_state_env(tmp_path)
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            with open(os.path.join(DATA_DIR, fn)) as f:
                tables[fn[:-len(".jsonl")]] = [json.loads(l) for l in f if l.strip()]
    tables["projects"] = list(tables.get("projects") or []) + [
        {"id": "P1_CODING", "profile": "P0_FORE"}]
    reg = tmp_path / "collide-registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    _, data = _spawn(["P1_CODING", "--format", "json"],
                     dict(env, ROUTING_REGISTRY=str(reg)))
    assert "error" not in data, data.get("error")
    assert data["profile"] == "P0_FORE"       # the PROJECT row won
    assert data["resolved_as"] == "project"


def test_estimate_accepts_a_positional_project(tmp_path):
    """TR-059 (AC4) — `router estimate X` == `router estimate --project X`:
    same chain priced, same head, exit 0."""
    env = _open_state_env(tmp_path)
    pos = run(os.path.join(SCRIPTS, "router_estimate.py"), "9router", "--json",
              env_extra=env)
    flag = run(os.path.join(SCRIPTS, "router_estimate.py"), "--project", "9router",
               "--json", env_extra=env)
    assert pos.returncode == 0, pos.stderr
    assert flag.returncode == 0, flag.stderr
    a, b = json.loads(pos.stdout), json.loads(flag.stdout)
    assert "error" not in a and "error" not in b
    assert a["chain_estimated"] > 0, "nothing priced — parity would be vacuous"
    assert a["chain_estimated"] == b["chain_estimated"] == len(a["chain"])
    assert (a["head"] or {}).get("model") == (b["head"] or {}).get("model")
    assert a["project"] == b["project"] == "9router"
    assert a["totals"] == b["totals"]


def test_estimate_positional_bare_profile_name(tmp_path):
    """TR-059 — `router estimate P1_CODING` prices the profile exactly like
    `router spawn P1_CODING` resolves it (the estimate never drifts from the
    spawn: it subprocesses the same resolver)."""
    env = _open_state_env(tmp_path)
    est = json.loads(run(os.path.join(SCRIPTS, "router_estimate.py"), "P1_CODING",
                         "--json", env_extra=env).stdout)
    _, spawn = _spawn(["P1_CODING", "--format", "json"], env)
    assert est.get("error") is None, est.get("error")
    assert est["profile"] == "P1_CODING"
    assert spawn["chain"], "empty chain — the comparison would be vacuous"
    assert est["chain_estimated"] == len(spawn["chain"])
    assert (est["head"] or {}).get("model") == (spawn["head"] or {}).get("model")


def test_estimate_without_a_project_is_still_a_usage_error(tmp_path):
    """Accepting a positional must not turn 'no input at all' into a silent
    exit 0 — the documented usage error (exit 2) stays."""
    env = _open_state_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_estimate.py"), "--json", env_extra=env)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "a project is required" in p.stderr


# =================================================== TR-133: --profile hint ==

def test_profile_flag_case_mismatched_profile_name_gets_a_hint(tmp_path):
    """TR-133 — the TR-059 near-miss hint must fire on the --profile error path
    too. Lowercase `p3_docs` names no profile, but it IS a case-insensitive
    exact match of the P3_DOCS tag/id, so the PROFILE_NOT_FOUND error gains the
    same visible did-you-mean the project slot already produces: the error
    names the match, `hint` carries the canonical form, and the fail-open
    contract (pure JSON, exit 0) is untouched."""
    env = _open_state_env(tmp_path)
    _, data = _spawn(["--profile", "p3_docs", "--format", "json"], env)
    assert data["code"] == "PROFILE_NOT_FOUND"
    assert data["retryable"] is False
    assert data["error"] == ("profile p3_docs not in registry — matches "
                             "profile P3_DOCS, use --profile P3_DOCS")
    assert data["hint"] == "use --profile P3_DOCS"
    assert data["matched_profile"] == "P3_DOCS"


def test_profile_flag_bogus_profile_still_gets_no_hint(tmp_path):
    """A name that is no real match of any profile keeps the exact pre-TR-133
    error with no hint — a false did-you-mean is worse than none (TR-059)."""
    _, data = _spawn_err(["--profile", "NOT_A_PROFILE"], tmp_path)
    assert "not in registry" in data["error"]
    assert data["code"] == "PROFILE_NOT_FOUND"
    assert "hint" not in data and "matched_profile" not in data
    assert set(data) == {"error", "code", "retryable", "data_home"}


"""TR-016 tests — data-home resolution + installable CLI smoke (hermetic).

Covers:
  - resolve_data_home() precedence: TASK_ROUTER_HOME > XDG_DATA_HOME > default
  - create=True side effect; create=False read-only
  - per-file helpers (registry/circuit/ledger/health) land inside the home
  - CLI smoke: subcommand list non-empty, dispatch machinery importable,
    env-export map covers only hooks scripts actually read, entry-point
    main() parses a real subcommand end-to-end (circuit status in tmp home)
"""

import importlib
import json
import os
import subprocess
import sys

import pytest

# Hermetic import: make repo root importable regardless of how pytest was
# invoked (python -m pytest adds CWD, the console script does not).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from task_router import cli, paths  # noqa: E402


# --------------------------------------------------------------------------
# data-home resolution order
# --------------------------------------------------------------------------

def test_home_env_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    got = paths.resolve_data_home(create=False)
    assert got == str(tmp_path / "explicit")


def test_xdg_falls_back_when_no_task_router_home(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_ROUTER_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    got = paths.resolve_data_home(create=False)
    assert got == os.path.join(str(tmp_path / "xdg"), "task-router")


def test_default_under_user_local_share(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_ROUTER_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "homedir"))
    got = paths.resolve_data_home(create=False)
    assert got == os.path.join(str(tmp_path / "homedir"),
                               ".local", "share", "task-router")


def test_create_makes_the_dir(tmp_path, monkeypatch):
    home = tmp_path / "made" / "up" / "deep"
    monkeypatch.setenv("TASK_ROUTER_HOME", str(home))
    got = paths.resolve_data_home(create=True)
    assert os.path.isdir(got)
    # idempotent
    assert paths.resolve_data_home(create=True) == str(home)


def test_create_false_does_not_make_the_dir(tmp_path, monkeypatch):
    home = tmp_path / "never-created"
    monkeypatch.setenv("TASK_ROUTER_HOME", str(home))
    got = paths.resolve_data_home(create=False)
    assert got == str(home)
    assert not os.path.exists(got)


def test_tilde_expansion_in_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "homedir"))
    monkeypatch.setenv("TASK_ROUTER_HOME", "~/.task-router-homes/tr016")
    got = paths.resolve_data_home(create=False)
    assert got == str(tmp_path / "homedir" / ".task-router-homes" / "tr016")


# --------------------------------------------------------------------------
# per-file path helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "helper, filename",
    [
        (paths.registry_path, "registry.json"),
        (paths.circuit_state_path, "circuit-state.json"),
        (paths.ledger_path, "ledger.jsonl"),
        (paths.health_state_path, "health-state.json"),
        (paths.quota_state_path, "quota-state.json"),
    ],
)
def test_file_helpers_live_inside_data_home(tmp_path, monkeypatch,
                                            helper, filename):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "dh"))
    got = helper()
    assert os.path.dirname(got) == str(tmp_path / "dh")
    assert os.path.basename(got) == filename


def test_helpers_follow_env_changes_lazily(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "a"))
    first = paths.registry_path()
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "b"))
    second = paths.registry_path()
    assert first != second
    assert os.path.basename(first) == os.path.basename(second)


# --------------------------------------------------------------------------
# CLI structure smoke
# --------------------------------------------------------------------------

EXPECTED_COMMANDS = {
    "spawn", "circuit", "quota", "gaps", "ledger", "maintain", "modelsdev",
    "pricing", "plan-sweep", "learn", "seed", "probe", "clinepass",
    "probefix", "validate", "metrics", "status", "estimate", "diff",
    "web", "server", "outcomes", "chain-run", "pricing-audit",
}


def test_subcommand_list_non_empty_and_complete():
    assert cli.COMMANDS, "COMMANDS must not be empty"
    assert set(cli.COMMANDS) == EXPECTED_COMMANDS
    # validate graduated from reserved stub to the real router_validate.py
    # (dogfood 2026-09-01); nothing is reserved anymore.
    assert cli.RESERVED == ()
    assert "validate" in cli.COMMANDS


def test_every_command_maps_to_an_existing_script():
    for name, script in cli.COMMANDS.items():
        path = os.path.join(cli.SCRIPTS_DIR, script)
        assert os.path.isfile(path), f"{name} -> missing {path}"


def test_dispatch_machinery_importable():
    import runpy  # noqa: F401 — dispatch depends on it

    assert callable(cli.main)
    assert callable(cli.dispatch)
    assert callable(cli._apply_env_exports)
    assert callable(cli._home_env_exports)


def test_top_level_help_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in EXPECTED_COMMANDS:
        assert name in out


def test_validate_dispatches_to_real_validator(capsys):
    """validate is no longer a reserved stub: it dispatches to
    scripts/router_validate.py, prints its pure-JSON report, and returns the
    validator's own exit code (0 = valid, 1 = issues — in a bare test env
    there is no registry, so 1 is the CORRECT result; either way the stub is
    gone). (dogfood fix 2026-09-01)"""
    rc = cli.main(["validate", "--json"])
    assert rc in (0, 1)
    out = capsys.readouterr().out
    parsed = json.loads(out)  # raises unless pure JSON
    assert parsed.get("valid") in (True, False)
    assert isinstance(parsed.get("checks"), list)


def test_subcommand_help_passes_through_to_script_argparse(capsys):
    """AC: `router <cmd> --help` shows the underlying script's argparse.

    (main() catches the script's SystemExit and returns its code as an int.)
    """
    rc = cli.main(["circuit", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: router_circuit.py" in out
    assert "record-failure" in out and "record-success" in out


def test_main_restores_exports_after_dispatch(tmp_path, monkeypatch):
    """Exports are per-dispatch: no ROUTING_*/ROUTER_STATE_DIR/LEDGER_FILE
    leak into the caller's environ after main() returns (regression: an
    earlier in-process dispatch used to pin a stale ROUTER_STATE_DIR onto
    every later dispatch)."""
    for var in ("ROUTER_STATE_DIR", "ROUTING_REGISTRY", "ROUTING_DATA_DIR",
                "LEDGER_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "dh"))
    cli.main(["circuit", "--help"])
    assert "ROUTER_STATE_DIR" not in os.environ
    cli.main(["circuit", "status"])
    assert "ROUTER_STATE_DIR" not in os.environ


# --------------------------------------------------------------------------
# env exports — only real hooks, never clobber explicit overrides
# --------------------------------------------------------------------------

def test_exports_only_reference_hooks_scripts_actually_read():
    """Every env var in the export map must be a hook grep'd in scripts/.

    This is the DATA>CODE guard for the CLI layer: if a hook is removed from
    a script, this test fails until the map is updated.
    """
    import re

    hooks = set()
    for fname in os.listdir(cli.SCRIPTS_DIR):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(cli.SCRIPTS_DIR, fname)) as f:
            hooks |= set(re.findall(
                r"os\.environ\.get\(\s*['\"]([A-Z_]+)['\"]", f.read()))
    # scripts must actually read every var we export
    exported_vars = set()
    for mapping in cli._home_env_exports().values():
        exported_vars |= set(mapping)
    unknown = exported_vars - hooks
    assert not unknown, f"CLI exports vars no script reads: {sorted(unknown)}"
    # and the documented set must all be exercised
    assert {"ROUTING_REGISTRY", "ROUTING_DATA_DIR", "ROUTER_STATE_DIR",
            "LEDGER_FILE"} <= exported_vars


def test_apply_env_exports_setdefault_semantics(monkeypatch):
    """Explicit env overrides must win over derived exports (setdefault)."""
    monkeypatch.setenv("LEDGER_FILE", "/explicit/user/choice.jsonl")
    monkeypatch.setenv("ROUTER_STATE_DIR", "/explicit/state")
    exports = {
        "ledger": {"LEDGER_FILE": "/home/derived/ledger.jsonl"},
        "circuit": {"ROUTER_STATE_DIR": "/home/derived"},
    }
    cli._apply_env_exports("ledger", exports)
    # explicit user override wins — no clobber
    assert os.environ["LEDGER_FILE"] == "/explicit/user/choice.jsonl"
    cli._apply_env_exports("circuit", exports)
    assert os.environ["ROUTER_STATE_DIR"] == "/explicit/state"


def test_apply_env_exports_ignores_unknown_commands():
    assert cli._apply_env_exports("nosuchcmd",
                                  {"circuit": {"ROUTER_STATE_DIR": "/x"}}) is None


def test_spawn_exports_point_into_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "dh"))
    exports = cli._home_env_exports()["spawn"]
    assert exports["ROUTING_REGISTRY"] == os.path.join(str(tmp_path / "dh"),
                                                       "registry.json")
    assert exports["ROUTER_STATE_DIR"] == str(tmp_path / "dh")


# --------------------------------------------------------------------------
# TR-045: seed must not export into the fleet DuckBrain mirror
# --------------------------------------------------------------------------

def test_seed_export_targets_data_home_not_fleet_mirror(tmp_path, monkeypatch):
    """A data-home `router seed` must never write the live DuckBrain mirror.

    scripts/router_seed.py line 25 defaults ROUTING_NS to the hardcoded
    /home/kara/duckbrain/namespaces/routing (the S3-backed fleet mirror), so
    before TR-045 the CLI exported only ROUTING_REGISTRY/ROUTING_DATA_DIR and
    a scratch/data-home seed exported its tables straight into the live
    mirror. The CLI now derives ROUTING_NS under the resolved data home.
    """
    home = str(tmp_path / "dh")
    monkeypatch.setenv("TASK_ROUTER_HOME", home)
    monkeypatch.delenv("ROUTING_NS", raising=False)
    exports = cli._home_env_exports()["seed"]
    assert "ROUTING_NS" in exports, "seed export map lost the TR-045 ns guard"
    assert exports["ROUTING_NS"] == os.path.join(home, "ns", "routing")
    # the derived ns lives under the data home, i.e. never the fleet mirror
    assert os.path.commonpath([exports["ROUTING_NS"], home]) == home
    assert not exports["ROUTING_NS"].startswith("/home/kara/duckbrain")


def test_seed_export_applied_to_env_keeps_operator_override(tmp_path,
                                                            monkeypatch):
    """The mechanism behind the guard: applied to a scratch env, seed's
    ROUTING_NS resolves under the data home — and an explicit operator
    ROUTING_NS still wins (setdefault, never clobber)."""
    home = str(tmp_path / "dh")
    monkeypatch.setenv("TASK_ROUTER_HOME", home)
    monkeypatch.delenv("ROUTING_NS", raising=False)
    cli._apply_env_exports("seed", cli._home_env_exports())
    assert os.environ["ROUTING_NS"] == os.path.join(home, "ns", "routing")
    # explicit override is preserved, not overridden
    monkeypatch.setenv("ROUTING_NS", "/explicit/scratch/ns")
    cli._apply_env_exports("seed", cli._home_env_exports())
    assert os.environ["ROUTING_NS"] == "/explicit/scratch/ns"


# --------------------------------------------------------------------------
# end-to-end dispatch smoke (real subcommand, hermetic home)
# --------------------------------------------------------------------------

def test_circuit_status_end_to_end_in_tmp_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "dh"))
    rc = cli.main(["circuit", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "circuit" in out.lower()
    # the CLI created and used the data home
    assert os.path.isdir(str(tmp_path / "dh"))
    # PROOF the export drove the script: record into the tmp home via the
    # CLI, then read the state file the script must have written there.
    rc = cli.main(["circuit", "record-failure", "prov-a", "mod-x",
                   "timeout after 30s"])
    assert rc == 0
    state_file = os.path.join(str(tmp_path / "dh"), "circuit-state.json")
    assert os.path.isfile(state_file), "circuit state did not land in data home"


def test_circuit_status_json_via_subprocess_entrypoint(tmp_path, monkeypatch):
    """Entry-point shape: `router circuit status --json` as a fresh process.

    Uses sys.executable -c to emulate the installed console script without
    requiring the venv install inside the test sandbox.
    """
    env = dict(os.environ)
    env["TASK_ROUTER_HOME"] = str(tmp_path / "dh")
    env["PYTHONPATH"] = cli.REPO + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['router','circuit','status','--json']; "
         "from task_router.cli import main; sys.exit(main())"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"pairs"' in proc.stdout
    assert os.path.isdir(str(tmp_path / "dh"))


# --------------------------------------------------------------------------
# TR-056: the data-home export covers EVERY subcommand, not a subset
# --------------------------------------------------------------------------

def test_every_command_has_an_export_entry():
    """AC1 — a subcommand missing from the map keeps its own script default
    (repo-relative registry, hardcoded state dir) and disagrees with spawn."""
    exports = cli._home_env_exports()
    missing = sorted(set(cli.COMMANDS) - set(exports))
    assert not missing, f"subcommands with no export entry: {missing}"
    unknown = sorted(set(exports) - set(cli.COMMANDS))
    assert not unknown, f"export entries for unknown subcommands: {unknown}"


def test_tr056_second_wave_commands_export_the_data_home(tmp_path, monkeypatch):
    """status/validate/estimate/server must resolve the SAME registry + state
    dir as spawn (before TR-056 they kept the script defaults)."""
    home = str(tmp_path / "dh")
    monkeypatch.setenv("TASK_ROUTER_HOME", home)
    exports = cli._home_env_exports()
    registry = os.path.join(home, "registry.json")
    tables = os.path.join(cli.REPO, "data", "tables")
    for cmd in ("spawn", "status", "validate", "estimate", "server"):
        assert exports[cmd]["ROUTING_REGISTRY"] == registry, cmd
        assert exports[cmd]["ROUTER_STATE_DIR"] == home, cmd
        assert exports[cmd]["ROUTING_DATA_DIR"] == tables, cmd


def test_tr056_per_command_granularity_matches_the_scripts():
    """Only hooks the target script can actually read are exported.

    TR-056 grepped every script: the brief's assumed mapping was WIDER for
    diff/web/metrics (three commands that read no registry/tables/state hook)
    and NARROWER for validate (which does read ROUTER_STATE_DIR).
    """
    exports = cli._home_env_exports()
    docs = os.path.join(cli.REPO, "docs")
    # router_diff.py reads ROUTING_DOCS_DIR and no registry/tables/state hook
    assert exports["diff"] == {"ROUTING_DOCS_DIR": docs}
    # router_web.py reads ROUTING_DATA_DIR only — resolve_preview() pins the
    # child's ROUTING_REGISTRY to <repo>/registry.json itself, so an export
    # here could never be read
    assert set(exports["web"]) == {"ROUTING_DATA_DIR"}
    # router_metrics.py reads TASK_ROUTER_HOME (not the ROUTING_* hooks) with
    # the same default router_spawn.py's metric writer appends to: exporting
    # TASK_ROUTER_HOME would move the READER off the writer's file
    assert exports["metrics"] == {}
    # router_server.py reads all four data-home hooks
    assert set(exports["server"]) == {"ROUTING_REGISTRY", "ROUTING_DATA_DIR",
                                      "ROUTING_DOCS_DIR", "ROUTER_STATE_DIR"}
    # fleet-state tools keep their own (fleet) defaults by design
    for cmd in ("probefix", "probe", "plan-sweep", "learn"):
        assert exports[cmd] == {}, cmd


_ENTRY = ("import sys; sys.argv = ['router'] + sys.argv[1:]; "
          "from task_router.cli import main; sys.exit(main())")


def _cli(args, home, timeout=240):
    """Run the CLI entry point as a FRESH process with HOME=home and no
    TASK_ROUTER_HOME/XDG_DATA_HOME/ROUTING_* overrides (the acceptance
    condition's shape: no explicit data home, so the default must be used)."""
    stripped = ("TASK_ROUTER_HOME", "XDG_DATA_HOME", "ROUTING_REGISTRY",
                "ROUTING_DATA_DIR", "ROUTING_DOCS_DIR", "ROUTER_STATE_DIR",
                "LEDGER_FILE")
    env = {k: v for k, v in os.environ.items() if k not in stripped}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = cli.REPO + os.pathsep + os.environ.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", _ENTRY, *args],
                          capture_output=True, text=True, env=env,
                          timeout=timeout)


def test_status_and_spawn_report_the_same_registry_without_home(tmp_path):
    """AC2 — with no TASK_ROUTER_HOME, `router status` and `router spawn` name
    the SAME registry. Pre-TR-056 status read the repo default
    (<repo>/registry.json) while spawn read the data home (live repro:
    /home/kara/task-router/registry.json vs
    /home/kara/.local/share/task-router/registry.json)."""
    home = tmp_path / "homedir"
    home.mkdir()
    data_home = home / ".local" / "share" / "task-router"

    st = _cli(["status", "--format", "json"], home)
    assert st.returncode == 0, st.stderr
    status = json.loads(st.stdout)
    sp = _cli(["spawn", "9router", "--format", "json"], home)
    assert sp.returncode == 0, sp.stderr
    spawn = json.loads(sp.stdout)

    expected = str(data_home / "registry.json")
    assert status["data_home"]["registry"] == expected
    assert spawn["data_home"]["registry"] == expected
    assert spawn["head"], "spawn did not resolve a chain — test would be vacuous"
    assert status["data_home"]["state_dir"] == spawn["data_home"]["state_dir"]
    # never the repo-relative default status used to keep
    assert status["data_home"]["registry"] != os.path.join(cli.REPO,
                                                           "registry.json")
    # and once the data-home registry exists, status READS it (not the repo one)
    data_home.mkdir(parents=True, exist_ok=True)
    (data_home / "registry.json").write_text(json.dumps(
        {"tables": {"models": []},
         "generated_at": "2026-09-18T00:00:00+00:00"}))
    st2 = json.loads(_cli(["status", "--format", "json"], home).stdout)
    assert st2["registry"]["path"] == expected
    assert st2["data_home"]["seeded"] is True


def test_estimate_resolves_the_same_source_as_spawn(tmp_path):
    """TR-056 — router_estimate.py subprocesses router_spawn.py with an
    inherited env AND imports it in-process, so the data-home export must
    reach it: pre-fix `router estimate` priced the REPO registry while
    `router spawn` dispatched the data-home resolve (different head)."""
    home = tmp_path / "homedir"
    home.mkdir()
    est_proc = _cli(["estimate", "--project", "9router"], home)
    assert est_proc.returncode == 0, est_proc.stderr
    est = json.loads(est_proc.stdout)
    sp = json.loads(_cli(["spawn", "9router", "--format", "json"], home).stdout)
    assert est.get("head"), "estimate returned no head — comparison would be vacuous"
    assert sp.get("head"), "spawn returned no head — comparison would be vacuous"
    assert est["source"] == sp["source"]
    assert (est.get("head") or {}).get("provider") == \
        (sp.get("head") or {}).get("provider")
    assert (est.get("head") or {}).get("model") == \
        (sp.get("head") or {}).get("model")

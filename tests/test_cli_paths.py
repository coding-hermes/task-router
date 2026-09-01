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
    "spawn", "circuit", "gaps", "ledger", "maintain", "modelsdev",
    "pricing", "plan-sweep", "learn", "seed", "probe", "clinepass",
    "probefix",
}


def test_subcommand_list_non_empty_and_complete():
    assert cli.COMMANDS, "COMMANDS must not be empty"
    assert set(cli.COMMANDS) == EXPECTED_COMMANDS
    assert "validate" in cli.RESERVED
    assert "validate" not in cli.COMMANDS


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


def test_reserved_validate_is_a_usage_error(capsys):
    assert cli.main(["validate"]) == 2
    err = capsys.readouterr().err
    assert "reserved" in err


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

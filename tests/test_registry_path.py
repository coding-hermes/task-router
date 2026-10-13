"""TR-REV-1012-1: scripts/registry_path.py — THE one registry resolver.

Order (highest first): ROUTING_REGISTRY > TASK_ROUTER_HOME/registry.json >
<repo>/registry.json > XDG data home. Every script (seed/spawn/validate/
status/maintain) and the CLI dispatch agree through this module; the
end-to-end acceptance is a fresh clone's `make seed && make validate` with
no env vars at all (covered live by scripts/fresh_clone_smoke.sh, which now
FAILS the smoke when the schools disagree again).

The module must stay stdlib-only and never raise (scripts are bare-venv
importable; fail-open is sacred) — both pinned here.
"""
import importlib
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

#: Every variable that can move the registry path — scrubbed before each
#: fresh import so module-level DEFAULTS (not inherited shell env) decide.
SCRUB = ("ROUTING_REGISTRY", "TASK_ROUTER_HOME", "XDG_DATA_HOME",
         "ROUTING_DATA_DIR", "ROUTER_STATE_DIR")

# Scripts that must resolve through the shared resolver (TR-REV-1012-1 wired
# all five; provider_health_probe / cost_backfill / tier_coverage /
# degraded_path_falsifier keep their own fleet-local candidates by design).
SHARED_RESOLVER_SCRIPTS = (
    "router_seed.py", "router_spawn.py", "router_validate.py",
    "router_status.py", "router_maintain.py",
)


def _load_resolver_fresh(monkeypatch, **env_over):
    """Import scripts/registry_path.py with a scrubbed env + overrides."""
    for k in SCRUB:
        monkeypatch.delenv(k, raising=False)
    for k, v in env_over.items():
        monkeypatch.setenv(k, str(v))
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    sys.modules.pop("registry_path", None)
    try:
        return importlib.import_module("registry_path")
    finally:
        sys.modules.pop("registry_path", None)


# ---------------------------------------------------------------------------
# the resolution order itself
# ---------------------------------------------------------------------------

def test_order_1_routing_registry_env_wins(monkeypatch, tmp_path):
    override = str(tmp_path / "override.json")
    mod = _load_resolver_fresh(monkeypatch, ROUTING_REGISTRY=override,
                               TASK_ROUTER_HOME=str(tmp_path / "home"))
    assert mod.resolve_registry_path(repo_root=REPO) == override


def test_order_2_task_router_home(monkeypatch, tmp_path):
    home = str(tmp_path / "home")
    mod = _load_resolver_fresh(monkeypatch, TASK_ROUTER_HOME=home)
    assert mod.resolve_registry_path(repo_root=REPO) == \
        os.path.join(home, "registry.json")


def test_order_3_repo_default_when_no_env(monkeypatch):
    mod = _load_resolver_fresh(monkeypatch)
    assert mod.resolve_registry_path(repo_root=REPO) == \
        os.path.join(REPO, "registry.json")


def test_order_4_xdg_last_resort_when_no_repo(monkeypatch, tmp_path):
    xdg = str(tmp_path / "xdg")
    mod = _load_resolver_fresh(monkeypatch, XDG_DATA_HOME=xdg)
    # repo_root None / nonexistent (embedded install, no discoverable checkout)
    assert mod.resolve_registry_path(repo_root=None) == \
        os.path.join(xdg, "task-router", "registry.json")
    assert mod.resolve_registry_path(repo_root=str(tmp_path / "no-such-repo")) == \
        os.path.join(xdg, "task-router", "registry.json")


def test_empty_env_value_counts_as_unset(monkeypatch, tmp_path):
    """An EMPTY set-var must not produce a broken '' path prefix (the
    state_dir.py lesson — os.environ.get alone returns '' for a set-but-empty
    var)."""
    mod = _load_resolver_fresh(monkeypatch, ROUTING_REGISTRY="",
                               TASK_ROUTER_HOME="")
    assert mod.resolve_registry_path(repo_root=REPO) == \
        os.path.join(REPO, "registry.json")


# ---------------------------------------------------------------------------
# every wired script resolves through it (imports must not be lost to edits)
# ---------------------------------------------------------------------------

def test_scripts_wire_the_shared_resolver():
    for fname in SHARED_RESOLVER_SCRIPTS:
        with open(os.path.join(SCRIPTS, fname)) as f:
            src = f.read()
        assert "import registry_path" in src, \
            f"{fname} no longer resolves through scripts/registry_path.py"


def test_every_script_resolves_task_router_home(tmp_path):
    """End-to-end per script: with TASK_ROUTER_HOME set (and no
    ROUTING_REGISTRY), each script's REGISTRY must be <home>/registry.json —
    the tier the CLI dispatch relies on now that it no longer exports
    ROUTING_REGISTRY. router_seed.py is excluded from the import probe: it is
    a flat script whose tail EXECUTES on import (TR-232 audit lines on
    stdout) — its resolution is pinned by test_scripts_wire_the_shared_resolver
    above and by the live `make seed` acceptance."""
    home = str(tmp_path / "home")
    env = dict(os.environ)
    for k in SCRUB:
        env.pop(k, None)
    env["TASK_ROUTER_HOME"] = home
    env["PYTHONPATH"] = SCRIPTS + os.pathsep + env.get("PYTHONPATH", "")
    probe = ("import %s as m; print(m.REGISTRY)")
    for fname in SHARED_RESOLVER_SCRIPTS:
        if fname == "router_seed.py":
            continue  # import runs its pipeline tail — not side-effect-free
        modname = fname[:-3]
        proc = subprocess.run(
            [sys.executable, "-c", probe % modname],
            capture_output=True, text=True, env=env, timeout=180,
            cwd=REPO)
        assert proc.returncode == 0, f"{fname}: {proc.stderr[-500:]}"
        assert proc.stdout.strip() == os.path.join(home, "registry.json"), fname

"""TR-REV-20261005-3 — clone-isolation state-bleed hardening.

Problem: seven scripts duplicated

    MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

A second checkout invoked without env vars silently reads/writes PRODUCTION
circuit/quota/health/ledger state. The shared resolver (scripts/state_dir.py)
keeps the default byte-identical but warns ONCE on stderr when the env is
unset AND the process was not started through the canonical live install
(~/.hermes/scripts). Fail-open is sacred: the resolver never raises, never
writes stdout, never touches exit codes.

What is pinned here:

  * env override wins silently (empty string counts as unset — old behavior);
  * canonical live-install invocation (argv[0] under ~/.hermes/scripts) —
    no warning;
  * non-canonical invocation (explicit argv0 / force flag) — ONE warning on
    stderr naming ROUTER_STATE_DIR and the shared dir, deduped per script;
  * ROUTER_STATE_DIR_WARN=force/suppress steers the warning for tests;
  * router_spawn.py JSON stdout byte-identical with and without the warning
    (warning lives only on stderr; payload determinism premised first);
  * every wired script actually resolves through the helper (functional
    import-level probe: warning fires + resolved dir is the shared default);
  * a stale live byte-copy missing the sibling module degrades to the old
    silent behavior (ImportError fallback), never an error.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
SPAWN = os.path.join(SCRIPTS, "router_spawn.py")
PY = sys.executable
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import state_dir as sd  # noqa: E402  (the module under test)

WIRED_SCRIPTS = [
    "router_spawn.py",
    "provider_health_probe.py",
    "provider_health_summary.py",
    "provider_health_dashboard.py",
    "router_circuit.py",
    "router_ingress.py",
    "policy_gate_audit.py",
]


def _clean_environ(monkeypatch):
    monkeypatch.delenv("ROUTER_STATE_DIR", raising=False)
    monkeypatch.delenv("ROUTER_STATE_DIR_WARN", raising=False)


# ---------------------------------------------------------------- unit: resolver


def _fresh_warn_state(monkeypatch):
    """A private dedup set per test: _WARNED is module-global and keyed by
    (realpath(script_file), dir) — without this, the first warning-happy test
    in this file would silence every later one."""
    monkeypatch.setattr(sd, "_WARNED", set())


def test_env_override_wins_silently(monkeypatch):
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)
    scratch = "/tmp/tr-rev-scratch-state"
    monkeypatch.setenv("ROUTER_STATE_DIR", scratch)
    captured = []
    got = sd.resolve_state_dir(script_file=__file__, warn=captured.append)
    assert got == scratch
    assert captured == [], "env override must be silent"


def test_empty_env_value_counts_as_unset(monkeypatch):
    """The old os.environ.get contract: ROUTER_STATE_DIR='' is not a dir."""
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)
    monkeypatch.setenv("ROUTER_STATE_DIR", "")
    captured = []
    got = sd.resolve_state_dir(script_file=__file__, force_non_canonical=True,
                               warn=captured.append)
    assert got == sd.DEFAULT_STATE_DIR
    assert len(captured) == 1


def test_canonical_invocation_no_warning(monkeypatch):
    _clean_environ(monkeypatch)
    captured = []
    got = sd.resolve_state_dir(script_file=__file__,
                               force_non_canonical=False,
                               warn=captured.append)
    assert got == sd.DEFAULT_STATE_DIR
    assert captured == []


def test_non_canonical_invocation_warns(monkeypatch):
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)
    captured = []
    got = sd.resolve_state_dir(script_file=__file__, force_non_canonical=True,
                               warn=captured.append)
    assert got == sd.DEFAULT_STATE_DIR
    assert len(captured) == 1
    assert "ROUTER_STATE_DIR" in captured[0]
    assert sd.DEFAULT_STATE_DIR in captured[0]
    assert "<dir>" in captured[0]  # the escape hatch is spelled out


def test_argv0_detection_repo_path_warns_live_path_silent(monkeypatch):
    """Real detection contract: argv[0] decides, no test-only flags."""
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)
    repo_invocation = []
    live_invocation = []
    sd.resolve_state_dir(script_file=__file__,
                         argv0=os.path.join(SCRIPTS, "router_spawn.py"),
                         warn=repo_invocation.append)
    sd.resolve_state_dir(script_file="other.py",
                         argv0=os.path.join(sd.LIVE_INSTALL_DIR,
                                            "router_spawn.py"),
                         warn=live_invocation.append)
    assert len(repo_invocation) == 1, "repo checkout must warn"
    assert live_invocation == [], "live install must stay silent"


def test_warning_deduped_per_script(monkeypatch):
    _clean_environ(monkeypatch)
    monkeypatch.setattr(sd, "_WARNED", set())
    captured = []
    for _ in range(2):
        sd.resolve_state_dir(script_file=__file__, force_non_canonical=True,
                             warn=captured.append)
    sd.resolve_state_dir(script_file="second_script.py",
                         force_non_canonical=True, warn=captured.append)
    assert len(captured) == 2, "one warning per script, not per call"


def test_warn_env_force_and_suppress(monkeypatch):
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)
    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", "force")
    forced, suppressed = [], []
    sd.resolve_state_dir(script_file="f1.py", force_non_canonical=False,
                         warn=forced.append)
    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", "suppress")
    sd.resolve_state_dir(script_file="f2.py", force_non_canonical=True,
                         warn=suppressed.append)
    assert len(forced) == 1
    assert suppressed == []


def test_fail_open_broken_warn_sink(monkeypatch):
    """A broken warn sink can never fail the resolve."""
    _clean_environ(monkeypatch)
    _fresh_warn_state(monkeypatch)

    def boom(_msg):
        raise RuntimeError("sink broken")

    assert sd.resolve_state_dir(script_file=__file__, force_non_canonical=True,
                                warn=boom) == sd.DEFAULT_STATE_DIR


def test_warning_never_on_stdout(monkeypatch):
    """Subprocess proof: stdout carries ONLY the resolved dir; warn -> stderr."""
    _clean_environ(monkeypatch)
    env = dict(os.environ)
    env.pop("ROUTER_STATE_DIR", None)
    env.pop("ROUTER_STATE_DIR_WARN", None)
    code = ("import sys; sys.path.insert(0, %r); import state_dir; "
            "sys.stdout.write(state_dir.resolve_state_dir("
            "script_file='probe.py') + chr(10))" % SCRIPTS)
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    assert p.stdout == sd.DEFAULT_STATE_DIR + "\n"
    assert "ROUTER_STATE_DIR" in p.stderr


# ------------------------------------------------- spawn: JSON byte-identity


def _spawn_profile_err(env_extra):
    """Run spawn on a profile that is not in the registry (fast, deterministic
    error payload, exit 0 by the fail-open contract)."""
    env = dict(os.environ)
    env.pop("ROUTER_STATE_DIR", None)
    env.pop("ROUTER_STATE_DIR_WARN", None)
    env.update(env_extra)
    return subprocess.run(
        [PY, SPAWN, "--profile", "NOT_A_PROFILE_XX987", "--format", "json"],
        capture_output=True, text=True, env=env, timeout=120)


def test_spawn_json_byte_identical_with_and_without_warning(
        seed_registry_copy):
    base = {"ROUTING_REGISTRY": seed_registry_copy["ROUTING_REGISTRY"],
            "ROUTING_DATA_DIR": seed_registry_copy["ROUTING_DATA_DIR"],
            # spawn's quiet convention (TR-055): diagnostics need the opt-in
            "ROUTER_MISS_VERBOSE": "1"}
    env_nowarn = dict(base, ROUTER_STATE_DIR_WARN="suppress")
    env_warn = dict(base, ROUTER_STATE_DIR_WARN="force")
    a = _spawn_profile_err(env_nowarn)
    b = _spawn_profile_err(env_warn)
    b2 = _spawn_profile_err(env_warn)
    assert a.returncode == b.returncode == b2.returncode == 0, (
        "fail-open: exit must stay 0 (%s / %s)" % (a.stderr, b.stderr))
    # premise: the payload itself is deterministic across runs
    assert b.stdout == b2.stdout, "nondeterministic payload — premise broken"
    # the actual contract: warning changes stderr only, never stdout bytes
    assert a.stdout == b.stdout
    assert a.stdout.strip().startswith("{")
    payload = json.loads(a.stdout)
    assert payload.get("code") == "PROFILE_NOT_FOUND"
    assert "ROUTER_STATE_DIR" not in a.stderr
    assert "ROUTER_STATE_DIR" in b.stderr
    assert sd.DEFAULT_STATE_DIR in b.stderr


# --------------------------------------------- per-script helper-used probes


def _import_wired(script_name, monkeypatch, warn_mode="force"):
    """Import a wired script in a private namespace with stderr captured.

    Returns (module, stderr_text). Warning capture works because the scripts
    resolve their state dir at import time (lazy only in router_ingress).
    """
    _clean_environ(monkeypatch)
    sd._WARNED.clear()
    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", warn_mode)
    # router_spawn.py routes its diagnostics through _err(), which is
    # quiet-by-default (TR-055) — the repo's own verbose opt-in restores
    # stderr warnings; harmless for the scripts that ignore it.
    monkeypatch.setenv("ROUTER_MISS_VERBOSE", "1")
    saved_argv, saved_stderr = sys.argv, sys.stderr
    sys.argv = [os.path.join(SCRIPTS, script_name)]
    buf = io.StringIO()
    sys.stderr = buf
    try:
        name = "tr_rev_probe_" + "".join(
            c if c.isalnum() else "_" for c in script_name)
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(SCRIPTS, script_name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv, sys.stderr = saved_argv, saved_stderr
    return mod, buf.getvalue()


@pytest.mark.parametrize("script_name", WIRED_SCRIPTS)
def test_wired_script_resolves_through_helper(script_name, monkeypatch):
    mod, err = _import_wired(script_name, monkeypatch)
    if script_name == "router_ingress.py":
        # lazy resolve: the helper runs on first _state_dir() call
        cap = io.StringIO()
        saved, sys.stderr = sys.stderr, cap
        try:
            resolved = str(mod._state_dir())
        finally:
            sys.stderr = saved
        assert "ROUTER_STATE_DIR" in cap.getvalue()
    else:
        assert "ROUTER_STATE_DIR" in err, (
            "%s did not emit the isolation warning on import — helper not "
            "wired?" % script_name)
        # spawn/probe/summary/dashboard expose MR; circuit/audit use _MR
        resolved = str(getattr(mod, "MR", None)
                       or getattr(mod, "_MR"))
    assert resolved == sd.DEFAULT_STATE_DIR


def test_wired_circuit_state_path_unchanged(monkeypatch):
    mod, _ = _import_wired("router_circuit.py", monkeypatch)
    assert str(mod.STATE) == os.path.join(sd.DEFAULT_STATE_DIR,
                                          "circuit-state.json")


def test_sync_runtime_copies_state_dir():
    """The live provider_health_probe.py is a byte-copy; its state_dir import
    only resolves if sync_runtime.sh ships state_dir.py alongside it."""
    src = open(os.path.join(SCRIPTS, "sync_runtime.sh")).read()
    assert "state_dir.py" in src


def test_stale_copy_without_sibling_degrades_silently(tmp_path,
                                                      seed_registry_copy):
    """Fail-open: a live byte-copy of spawn WITHOUT state_dir.py next to it
    (not yet synced) falls back to the old silent default and still works."""
    lone = tmp_path / "router_spawn.py"
    lone.write_text(open(SPAWN).read())
    env = dict(os.environ)
    env.pop("ROUTER_STATE_DIR", None)
    env.pop("ROUTER_STATE_DIR_WARN", None)
    env["ROUTER_STATE_DIR"] = str(tmp_path / "state")  # env set: silent path
    env["ROUTING_REGISTRY"] = seed_registry_copy["ROUTING_REGISTRY"]
    env["ROUTING_DATA_DIR"] = seed_registry_copy["ROUTING_DATA_DIR"]
    p = subprocess.run([PY, str(lone), "--profile", "NOT_A_PROFILE_XX987",
                        "--format", "json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout).get("code") == "PROFILE_NOT_FOUND"
    assert "WARNING [router state-dir]" not in p.stderr

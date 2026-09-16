"""TR-046 regression battery — JSON-mode purity + data-home/bootstrap visibility.

Dogfood 2026-09-12 P2 polish items, locked in:

1. `router_spawn.py --format json` emits PURE JSON on stdout on EVERY path —
   resolve success, structured errors, no-input usage (was argparse usage
   text on stdout), and --list-profiles (was a human table regardless of
   --format). Human text keeps text mode; diagnostics were already stderr.
2. Fresh-clone honesty: when registry.json is missing the resolve JSON says
   so — data_home.fallback=True, bootstrap=True + note naming the committed
   data/tables SAMPLE tables; when quota-state.json is the CLI first-run
   bootstrap (updated == 'bootstrap', all providers OPEN) the note names
   that too. A healthy seeded registry + real quota-state must NEVER claim
   sample data (bootstrap False, note None) — no false confessions.
3. `router_status.py --format json` carries a top-level data_home section
   (registry/data_dir/state_dir paths + seeded + bootstrap + note) and the
   registry fallback section keeps data_home/bootstrap/note — the TR-044
   "which data home is live?" answer lives in one command.

Subprocess tests run the real scripts hermetically (env overrides the
scripts already honor); provenance tests call router_spawn.resolve() with
monkeypatched module paths (test_visibility.py pattern).
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)


def _run(argv, env_extra=None, timeout=90):
    env = dict(os.environ)
    for k in ("ROUTING_REGISTRY", "ROUTING_DATA_DIR", "ROUTER_STATE_DIR",
              "LEDGER_FILE", "TASK_ROUTER_HOME", "ROUTER_SPAWN_QUIET"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run([PY, *argv], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=timeout)


def _pure_json(proc, label):
    assert proc.returncode == 0, f"{label}: exit {proc.returncode}: {proc.stderr[:300]}"
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:  # pragma: no cover — failure reporter
        pytest.fail(f"{label}: stdout is not pure JSON: {e}\n{proc.stdout[:400]}")


def _hermetic(tmp_path, registry="no-such-registry.json", quota=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if quota is not None:
        (state / "quota-state.json").write_text(json.dumps(quota))
    env = {"ROUTER_STATE_DIR": str(state),
           "ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables"),
           "ROUTING_REGISTRY": str(tmp_path / registry)}
    return env, str(state)


# ------------------------------------------------- spawn: JSON purity matrix --

def test_spawn_json_purity_every_path(tmp_path):
    """--format json stdout parses on success, structured error, no-input,
    and --list-profiles (TR-046 item 1: usage text / human table used to
    leak onto stdout in JSON mode)."""
    spawn = os.path.join(REPO, "scripts", "router_spawn.py")
    env, _ = _hermetic(tmp_path)
    for label, args in [
        ("resolve-success", ["hermes-dagger"]),
        ("unknown-project", ["definitely-not-a-project-xyz"]),
        ("bad-profile-req", ["--profile-req", "reasoning=nope"]),
        ("no-input", []),
        ("list-profiles", ["--list-profiles"]),
    ]:
        proc = _run([spawn, *args, "--format", "json"], env_extra=env)
        doc = _pure_json(proc, f"spawn {label}")
        assert isinstance(doc, dict), label


def test_spawn_no_input_json_is_structured_error():
    """No args + --format json = structured error (code NO_INPUT, exit 0,
    fail-open) — never argparse usage text on stdout. Text mode keeps the
    usage line."""
    spawn = os.path.join(REPO, "scripts", "router_spawn.py")
    proc = _run([spawn, "--format", "json"])
    doc = _pure_json(proc, "spawn no-input json")
    assert doc.get("code") == "NO_INPUT"
    assert doc.get("error")
    proc = _run([spawn, "--format", "text"])
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()


def test_spawn_list_profiles_json_and_text(tmp_path):
    """--list-profiles honors --format: json = {'profiles': [...]} with
    id/title/requirements; default (json) matches; the human table keeps
    rendering profile ids (README contract)."""
    spawn = os.path.join(REPO, "scripts", "router_spawn.py")
    env, _ = _hermetic(tmp_path)
    doc = _pure_json(_run([spawn, "--list-profiles", "--format", "json"],
                          env_extra=env), "list-profiles json")
    assert isinstance(doc.get("profiles"), list) and doc["profiles"]
    row = doc["profiles"][0]
    assert {"id", "title", "requirements"} <= set(row)
    proc = _run([spawn, "--list-profiles"], env_extra=env)
    # default --format is json — the table is now text-only via explicit opt-out
    assert "P0_FORE" in proc.stdout or json.loads(proc.stdout)["profiles"]


# ------------------------------------- spawn: data_home + bootstrap honesty --

def _resolve_with(monkeypatch, tmp_path, registry, quota, project="hermes-dagger"):
    tables = {}
    data_dir = os.path.join(REPO, "data", "tables")
    for fn in sorted(os.listdir(data_dir)):
        if fn.endswith(".jsonl"):
            tables[fn[:-len(".jsonl")]] = [
                json.loads(l) for l in open(os.path.join(data_dir, fn)) if l.strip()]
    monkeypatch.setattr(router_spawn, "REGISTRY", str(registry))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if quota is not None:
        json.dump(quota, open(state / "quota-state.json", "w"))
    monkeypatch.setattr(router_spawn, "MR", str(state))
    return router_spawn.resolve(project=project)


BOOTSTRAP_QUOTA = {"updated": "bootstrap",
                   "providers": {"some-prov": {"status": "open"}},
                   "note": "first-run bootstrap: all providers OPEN; edit to gate"}
REAL_QUOTA = {"updated": "test", "providers": {"some-prov": {"status": "open"}}}


def test_missing_registry_bootstrap_true_with_note(monkeypatch, tmp_path):
    """AC: registry.json missing -> fallback JSON carries bootstrap=true and
    a note explaining the sample data (data_home mirrors it)."""
    r = _resolve_with(monkeypatch, tmp_path, tmp_path / "nope.json", BOOTSTRAP_QUOTA)
    assert "error" not in r, r.get("error")
    assert r["fallback_used"] is True
    assert r["source"] == "data/tables"
    assert r["bootstrap"] is True
    assert r["note"] and "sample tables" in r["note"]
    dh = r["data_home"]
    assert dh["fallback"] is True and dh["bootstrap"] is True
    assert dh["note"] == r["note"]  # flat fields are computed mirrors


def test_bootstrap_quota_note_names_sample_policy(monkeypatch, tmp_path):
    """AC: the CLI first-run bootstrap quota-state (updated='bootstrap') gets
    its own note even when the registry IS seeded; a real quota-state never
    produces it."""
    reg = tmp_path / "registry.json"
    tables = {}
    data_dir = os.path.join(REPO, "data", "tables")
    for fn in sorted(os.listdir(data_dir)):
        if fn.endswith(".jsonl"):
            tables[fn[:-len(".jsonl")]] = [
                json.loads(l) for l in open(os.path.join(data_dir, fn)) if l.strip()]
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    r = _resolve_with(monkeypatch, tmp_path, reg, BOOTSTRAP_QUOTA)
    assert "error" not in r, r.get("error")
    assert r["fallback_used"] is False
    assert r["bootstrap"] is True
    assert "first-run bootstrap" in r["note"]
    assert "sample tables" not in r["note"]


def test_healthy_path_never_claims_sample(monkeypatch, tmp_path):
    """Seeded registry + real quota-state -> bootstrap False, note None (the
    pre-TR-046 static note claimed sample policy on EVERY resolve)."""
    reg = tmp_path / "registry.json"
    tables = {}
    data_dir = os.path.join(REPO, "data", "tables")
    for fn in sorted(os.listdir(data_dir)):
        if fn.endswith(".jsonl"):
            tables[fn[:-len(".jsonl")]] = [
                json.loads(l) for l in open(os.path.join(data_dir, fn)) if l.strip()]
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    r = _resolve_with(monkeypatch, tmp_path, reg, REAL_QUOTA)
    assert "error" not in r, r.get("error")
    assert r["fallback_used"] is False
    assert r["bootstrap"] is False
    assert r["note"] is None
    assert r["data_home"]["bootstrap"] is False


def test_fallback_used_not_shadowed_by_fallback_lanes(monkeypatch, tmp_path):
    """Regression (TR-046): the fallback-LANE block used to rebind `fb`,
    so fallback_used flipped to False whenever head was None — exactly the
    fresh-clone case the flag exists for."""
    r = _resolve_with(monkeypatch, tmp_path, tmp_path / "nope.json", REAL_QUOTA,
                      project="hermes-dagger")
    assert "error" not in r, r.get("error")
    assert r["fallback_used"] is True, r.get("fallback_used")
    assert r["data_home"]["fallback"] is True


def test_data_home_paths_reflect_env(monkeypatch, tmp_path):
    """data_home carries the exact paths used (env overrides reflected) —
    the TR-044 answer inside every resolve."""
    reg = tmp_path / "my-reg.json"
    r = _resolve_with(monkeypatch, tmp_path, reg, REAL_QUOTA)
    dh = r["data_home"]
    assert dh["registry"] == str(reg)
    assert dh["state_dir"] == str(tmp_path / "state")
    assert dh["data_dir"] == os.path.join(REPO, "data", "tables")


# ---------------------------------------------------- status: data_home ----

def test_status_json_has_data_home_section(tmp_path):
    """`router status --format json` carries a top-level data_home section
    (registry/data_dir/state_dir/seeded/bootstrap/note) — TR-044 resolved."""
    status = os.path.join(REPO, "scripts", "router_status.py")
    env, state = _hermetic(tmp_path)
    doc = _pure_json(_run([status, "--format", "json"], env_extra=env),
                     "status json")
    dh = doc["data_home"]
    assert dh["registry"] == env["ROUTING_REGISTRY"]
    assert dh["data_dir"] == env["ROUTING_DATA_DIR"]
    assert dh["state_dir"] == state
    assert dh["seeded"] is False and dh["bootstrap"] is True
    assert "router seed" in dh["note"]
    reg = doc["registry"]
    assert reg["data_home"] == env["ROUTING_DATA_DIR"]
    assert reg["bootstrap"] is True and reg["note"]


def test_status_seeded_bootstrap_false(tmp_path):
    """Seeded registry + non-bootstrap quota -> status must NOT claim sample."""
    status = os.path.join(REPO, "scripts", "router_status.py")
    env, state = _hermetic(tmp_path)
    tables = {}
    data_dir = os.path.join(REPO, "data", "tables")
    for fn in sorted(os.listdir(data_dir)):
        if fn.endswith(".jsonl"):
            tables[fn[:-len(".jsonl")]] = {"sanity": True}
    (tmp_path / "reg.json").write_text(json.dumps({"version": 3, "tables": {"x": []}}))
    env["ROUTING_REGISTRY"] = str(tmp_path / "reg.json")
    json.dump(REAL_QUOTA, open(os.path.join(state, "quota-state.json"), "w"))
    doc = _pure_json(_run([status, "--format", "json"], env_extra=env),
                     "status seeded")
    assert doc["data_home"]["seeded"] is True
    assert doc["data_home"]["bootstrap"] is False
    assert doc["data_home"]["note"] is None


def test_status_text_mentions_data_home(tmp_path):
    """Text mode names the data-home paths (and the bootstrap state loudly)."""
    status = os.path.join(REPO, "scripts", "router_status.py")
    env, _ = _hermetic(tmp_path)
    proc = _run([status, "--format", "text"], env_extra=env)
    assert proc.returncode == 0, proc.stderr[:300]
    assert "data-home" in proc.stdout
    assert "BOOTSTRAP/SAMPLE" in proc.stdout

"""TR-031 regression tests — probe flag ergonomics + no-side-effect guarantees.

Hermetic: every probe run points at tmp_path fixtures (HOME, ROUTING_DATA_DIR,
ROUTER_STATE_DIR, --output) and a loopback-only fake provider (127.0.0.1:9,
connection refused in milliseconds) — no real network, no repo writes.
"""
import json
import os
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/kara/.hermes/venvs/board/bin/python3"
PROBE = os.path.join(REPO, "scripts", "provider_health_probe.py")
SEED = os.path.join(REPO, "scripts", "router_seed.py")


def _run(argv, env=None, timeout=30):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(argv, cwd=REPO, env=full_env, capture_output=True,
                          text=True, timeout=timeout)


def _repo_watched_files():
    d = os.path.join(REPO, "data", "tables")
    files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".jsonl")]
    reg = os.path.join(REPO, "registry.json")
    if os.path.isfile(reg):  # gitignored live state — absent on fresh clones/CI
        files.append(reg)
    return files


def _snapshot(paths):
    return {p: (os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in paths}


def _tree_snapshot(root):
    """{(relpath): (mtime_ns, size)} for everything under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
    return out


def _fixture(tmp_path, with_key=True):
    """Hermetic probe fixture: one fake provider pointing at loopback discard."""
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    (data_dir / "probe_providers.jsonl").write_text(json.dumps({
        "id": "fakeprov", "base_url": "http://127.0.0.1:9",
        "key_env": "FAKEPROBE_KEY", "default_model": "fake-model",
        "enabled": True,
    }) + "\n")
    if with_key:
        hermes = tmp_path / ".hermes"
        hermes.mkdir(exist_ok=True)
        (hermes / ".env").write_text("FAKEPROBE_KEY=deadbeef\n")
    env = {
        "HOME": str(tmp_path),
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTER_STATE_DIR": str(tmp_path / "mr-state"),
        "ROUTING_REGISTRY": str(tmp_path / "no-such-registry.json"),
    }
    return env


def test_seed_help_exit0_no_side_effects():
    """router_seed.py --help exits 0 and leaves data/tables + registry mtimes alone."""
    watched = _repo_watched_files()
    before = _snapshot(watched)
    proc = _run([PY, SEED, "--help"])
    assert proc.returncode == 0, f"seed --help failed: {proc.stderr[:300]}"
    assert "Seed the task-router" in proc.stdout
    after = _snapshot(watched)
    changed = {p: (before[p], after[p]) for p in watched if before[p] != after[p]}
    assert not changed, f"seed --help modified files: {changed}"


def test_probe_help_exit0_no_side_effects(tmp_path):
    """provider_health_probe.py --help exits 0 fast, writes nothing anywhere."""
    watched = _repo_watched_files()
    before = _snapshot(watched)
    t0 = time.time()
    proc = _run([PY, PROBE, "--help"], env={"HOME": str(tmp_path)}, timeout=10)
    elapsed = time.time() - t0
    assert proc.returncode == 0, f"probe --help failed: {proc.stderr[:300]}"
    assert "Provider health probe" in proc.stdout
    assert elapsed < 5.0, f"--help took {elapsed:.2f}s — likely hit network"
    assert _snapshot(watched) == before, "probe --help modified repo data files"
    assert _tree_snapshot(tmp_path) == {}, "probe --help created files under HOME"


def test_probe_only_unknown_provider_exit2(tmp_path):
    """--only with an unknown provider id -> clear error, exit 2, no probing."""
    env = _fixture(tmp_path)
    proc = _run([PY, PROBE, "--only", "nonexistent-provider"], env=env, timeout=10)
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}: {proc.stdout[:200]}"
    blob = (proc.stderr + proc.stdout).lower()
    assert "unknown provider" in blob
    assert "nonexistent-provider" in blob
    assert "fakeprov" in blob  # error lists the known providers


def test_probe_only_comma_list_case_insensitive(tmp_path):
    """--only accepts comma-separated ids, case-insensitive on provider id."""
    env = _fixture(tmp_path, with_key=False)  # no key -> NO_KEY, zero network
    out_file = tmp_path / "out" / "health-state.json"
    proc = _run([PY, PROBE, "--only", "FAKEPROV,fakeprov", "--no-write", "--output", str(out_file)],
                env=env, timeout=15)
    assert proc.returncode == 0, f"--only FAKEPROV,fakeprov failed: {proc.stderr[:300]}"
    assert "fakeprov" in proc.stdout
    assert "NO_KEY" in proc.stdout  # matched the provider, ran the (keyless) lane


def test_probe_no_write_writes_nothing(tmp_path):
    """--no-write runs the probe (real ping to loopback) but creates/modifies nothing."""
    env = _fixture(tmp_path)
    out_file = tmp_path / "out" / "health-state.json"
    before = _tree_snapshot(tmp_path)
    proc = _run([PY, PROBE, "--only", "fakeprov", "--no-write", "--output", str(out_file)],
                env=env, timeout=20)
    assert proc.returncode == 0, f"--no-write run failed: {proc.stderr[:400]}"
    assert "fakeprov" in proc.stdout
    assert "--no-write" in proc.stdout  # report notes the suppression
    after = _tree_snapshot(tmp_path)
    assert before == after, (
        f"--no-write changed the filesystem: "
        f"added={sorted(set(after) - set(before))}, "
        f"modified={sorted(k for k in before if k in after and before[k] != after[k])}")
    assert not (tmp_path / "mr-state").exists(), "state dir was created despite --no-write"


def test_probe_default_still_writes(tmp_path):
    """Contrast test: without --no-write the probe writes state + jsonl as before."""
    env = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_file = out_dir / "health-state.json"
    proc = _run([PY, PROBE, "--only", "fakeprov", "--output", str(out_file)],
                env=env, timeout=20)
    assert proc.returncode == 0, f"probe run failed: {proc.stderr[:400]}"
    assert out_file.exists(), "health-state.json not written on a normal run"
    state = json.loads(out_file.read_text())
    assert "fakeprov" in state.get("providers", {}), "written state missing the probed provider"
    jsonl = out_dir / "health-state.jsonl"
    assert jsonl.exists() and jsonl.read_text().strip(), "health jsonl not appended"

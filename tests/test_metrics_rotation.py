"""TR-372 — metrics store rotation, retained-family readers, gauges.

Hermetic like the rest of the suite: every test points TASK_ROUTER_HOME at a
tmp_path fixture; the real 6.6 GB data/metrics.jsonl is never touched.

Covers:
  - low-cap appends rotate and honour ROUTER_METRICS_GENERATIONS
  - the shift/drop order (oldest dropped BEFORE the shift, else the shift
    overwrites a retained generation)
  - lowering GENERATIONS prunes stale numbered files on the next rotation
  - readers over a 3-generation fixture return the SAME aggregate as one
    unrotated file with the same rows (shape and numbers)
  - rotation crashing must not break the append path (fail-open contract):
    the append still lands and --rotate-now still exits 0
  - gauge callbacks: current-file gauge == current size; family gauge ==
    the retained set's total; both aliased under task_router_
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable
)
SPAWN = os.path.join(SCRIPTS, "router_spawn.py")
METRICS = os.path.join(SCRIPTS, "router_metrics.py")

sys.path.insert(0, SCRIPTS)


def _run(script_path, argv, env_extra=None, timeout=60):
    full_env = dict(os.environ)
    full_env.pop("ROUTER_METRICS_MAX_BYTES", None)
    full_env.pop("ROUTER_METRICS_GENERATIONS", None)
    if env_extra:
        full_env.update(env_extra)
    return subprocess.run(
        [PY, script_path] + list(argv),
        cwd=REPO, env=full_env, capture_output=True, text=True, timeout=timeout,
    )


def _sizes(home):
    out = {}
    for name in sorted(os.listdir(home)):
        if name.startswith("metrics.jsonl"):
            out[name] = os.path.getsize(os.path.join(home, name))
    return out


# --- rotation mechanics -------------------------------------------------------

def test_rotate_shifts_and_drops_oldest_first(tmp_path, monkeypatch):
    """The load-bearing order: .N is dropped BEFORE the shift. Shifting first
    and deleting .N afterwards would destroy a retained generation, because
    each shift overwrites the next slot."""
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ROUTER_METRICS_MAX_BYTES", raising=False)
    monkeypatch.delenv("ROUTER_METRICS_GENERATIONS", raising=False)
    import router_metrics_store as store

    path = store.metrics_path(repo=str(tmp_path))
    home = os.path.dirname(path)
    os.makedirs(home, exist_ok=True)
    gens = {".1": "gen-one\n", ".2": "gen-two\n", ".3": "gen-three-oldest\n"}
    for suffix, body in gens.items():
        with open(path + suffix, "w") as f:
            f.write(body)
    with open(path, "w") as f:
        f.write("x" * 300 + "\n")  # oversized current file (cap 200)

    n = store.rotate_if_needed(path, cap=200, n=3)
    assert n == 1
    with open(path + ".1") as f:
        assert f.read() == "x" * 300 + "\n"          # current closed into .1
    with open(path + ".2") as f:
        assert f.read() == "gen-one\n"               # .1 shifted to .2
    with open(path + ".3") as f:
        assert f.read() == "gen-two\n"               # .2 shifted to .3
    assert "gen-three-oldest" not in open(path + ".3").read()
    with open(path + ".3") as f:
        assert "gen-three" not in f.read()           # oldest .3 content GONE
    assert not os.path.exists(path)                  # caller re-creates on append

    # a second call on the (now missing) current file is a no-op
    assert store.rotate_if_needed(path, cap=200, n=3) == 0


def test_lowered_generations_prune_stragglers(tmp_path, monkeypatch):
    """An operator who lowers ROUTER_METRICS_GENERATIONS must not leave
    stale .4/.5 files forever — the next rotation prunes them."""
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_metrics_store as store

    path = store.metrics_path(repo=str(tmp_path))
    home = os.path.dirname(path)
    os.makedirs(home, exist_ok=True)
    for suffix in (".1", ".2", ".3", ".4"):
        with open(path + suffix, "w") as f:
            f.write("old" + suffix + "\n")
    with open(path, "w") as f:
        f.write("x" * 300 + "\n")

    assert store.rotate_if_needed(path, cap=200, n=2) == 1
    names = sorted(n for n in os.listdir(home) if n.startswith("metrics.jsonl"))
    assert names == ["metrics.jsonl.1", "metrics.jsonl.2"]  # .3/.4 pruned
    with open(path + ".1") as f:
        assert f.read() == "x" * 300 + "\n"
    with open(path + ".2") as f:
        assert f.read() == "old.1\n"


def test_retained_paths_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_metrics_store as store

    path = store.metrics_path(repo=str(tmp_path))
    home = os.path.dirname(path)
    os.makedirs(home, exist_ok=True)
    # fresh store: only the current path, even though it does not exist yet
    assert store.retained_paths(path, n=3) == [path]
    for suffix in (".1", ".2", ".3"):
        with open(path + suffix, "w") as f:
            f.write("x\n")
    with open(path, "w") as f:
        f.write("y\n")
    family = store.retained_paths(path, n=3)
    assert family == [path + ".3", path + ".2", path + ".1", path]
    # exact bytes: three 2-byte generation files + one 2-byte current file
    assert store.family_bytes(path, n=3) == 8


def test_env_garbage_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_metrics_store as store

    monkeypatch.setenv("ROUTER_METRICS_MAX_BYTES", "not-a-number")
    monkeypatch.setenv("ROUTER_METRICS_GENERATIONS", "0")
    assert store.max_bytes() == store.DEFAULT_MAX_BYTES
    assert store.generations() == store.DEFAULT_GENERATIONS
    monkeypatch.setenv("ROUTER_METRICS_MAX_BYTES", "-5")
    monkeypatch.setenv("ROUTER_METRICS_GENERATIONS", "99")
    assert store.max_bytes() == store.DEFAULT_MAX_BYTES  # <=0 never honoured
    assert store.generations() == 99


# --- end-to-end: real spawn appends under a low cap ---------------------------

def _spawn_fixture(tmp_path):
    """Minimal hermetic registry + open-quota state (same shape as
    tests/test_metrics.py's fixtures)."""
    tables = {
        "providers": [{"id": "prov-a"}, {"id": "prov-b"}],
        "projects": [{"id": "my-project", "profile": "P0_FORE"}],
        "task_profiles": [{"id": "P0_FORE"}],
        "task_profile_requirements": [
            {"task_id": "P0_FORE", "category": "reasoning", "level": 1}],
        "category_levels": [{"category": "reasoning"}],
        "level_defs": [{"level": -5}, {"level": 5}],
        "models": [
            {"provider": "prov-a", "model": "model-a1", "normalized_price": 0.1,
             "token_factor": 1.0, "plan_tier": 1, "data_class": "zdr"},
            {"provider": "prov-a", "model": "model-a2", "normalized_price": 0.2,
             "token_factor": 1.0, "plan_tier": 1, "data_class": "zdr"},
            {"provider": "prov-b", "model": "model-b1", "normalized_price": 0.3,
             "token_factor": 1.0, "plan_tier": 1, "data_class": "zdr"},
        ],
        "model_tier": [
            {"model": "model-a1", "category": "reasoning", "tier": 2},
            {"model": "model-a2", "category": "reasoning", "tier": 2},
            {"model": "model-b1", "category": "reasoning", "tier": 2},
        ],
    }
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(json.dumps(
        {"providers": {"prov-a": {"status": "open"}, "prov-b": {"status": "open"}}}))
    (state / "health-state.json").write_text(json.dumps({"providers": {}}))
    (state / "circuit-state.json").write_text(json.dumps({"version": 1, "pairs": {}}))
    return {"ROUTING_REGISTRY": str(reg), "ROUTER_STATE_DIR": str(state)}


def test_low_cap_appends_rotate_and_honour_generations(tmp_path):
    """ROUTER_METRICS_MAX_BYTES=200 + three real spawns → metrics.jsonl, .1, .2
    exist, no .3 (GENERATIONS=2 honoured), and every retained file is bounded
    by cap + one append (a generation is the file AS IT WAS when rotation
    fired, so it can exceed the cap by less than one append's bytes — the
    same granularity logging.RotatingFileHandler accepts)."""
    home = tmp_path / "home"
    home.mkdir()
    env = _spawn_fixture(tmp_path)
    env["TASK_ROUTER_HOME"] = str(home)
    env["ROUTER_METRICS_MAX_BYTES"] = "200"
    env["ROUTER_METRICS_GENERATIONS"] = "2"

    for _ in range(3):
        proc = _run(SPAWN, ("my-project", "--format", "json"), env_extra=env)
        assert proc.returncode == 0, proc.stderr

    names = sorted(n for n in os.listdir(home) if n.startswith("metrics.jsonl"))
    assert names == ["metrics.jsonl", "metrics.jsonl.1", "metrics.jsonl.2"]
    sizes = _sizes(str(home))
    append_slack = sizes["metrics.jsonl"]  # one append's worth of rows
    for name, size in sizes.items():
        assert size <= 200 + append_slack, (name, size)
    # the current file carries the freshest append (nonempty)
    assert sizes["metrics.jsonl"] > 0
    # generations are closed snapshots: .2 is the OLDEST content
    with open(os.path.join(str(home), "metrics.jsonl.2")) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert rows and rows[0]["order"] == 1


# --- reader parity across the rotation boundary -------------------------------

def _row(i):
    """Deterministic hop row #i with an ascending timestamp. Two distinct
    provider/model pairs so the default --top-pairs 10 never truncates the
    aggregate (12 distinct pairs WOULD truncate and break the invariant)."""
    day = 1 + i // 3
    p = f"prov-{chr(ord('a') + i % 2)}"
    return {
        "ts": f"2026-11-{day:02d}T12:00:{i % 3:02d}+00:00",
        "project": "my-project",
        "profile": "P0_FORE",
        "provider": p,
        "model": f"model-{chr(ord('a') + i % 2)}",
        "order": 1,
        "price_usd_per_m": 0.1,
        "outcome": "resolved",
        "exclusion_reason": None,
        "config_snapshot": {"chain_length": 1},
    }


def test_readers_3gen_parity_vs_unrotated(tmp_path):
    """Twelve rows split across a 3-generation rotated family aggregate to
    EXACTLY the same JSON output as the same twelve rows in one file."""
    rows = [_row(i) for i in range(12)]

    rot_home = tmp_path / "rotated"
    rot_home.mkdir()
    # generation .3 oldest → current newest; ts ordering must survive the
    # boundary (readers iterate oldest generation first)
    layout = [rows[0:3], rows[3:6], rows[6:9], rows[9:12]]  # .3 .2 .1 current
    for suffix, chunk in zip((".3", ".2", ".1", ""), layout):
        with open(str(rot_home / ("metrics.jsonl" + suffix)), "w") as f:
            for r in chunk:
                f.write(json.dumps(r) + "\n")

    plain_home = tmp_path / "plain"
    plain_home.mkdir()
    with open(str(plain_home / "metrics.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    outs = {}
    for label, home in (("rotated", rot_home), ("plain", plain_home)):
        q = _run(METRICS, ("--json",), env_extra={"TASK_ROUTER_HOME": str(home)})
        assert q.returncode == 0, q.stderr
        outs[label] = json.loads(q.stdout)

    a, b = outs["rotated"], outs["plain"]
    assert a["total_hops"] == b["total_hops"] == 12
    assert a["_invariant_check"] is True
    assert a["pairs"] == b["pairs"]
    assert a["providers"] == b["providers"]
    assert a["models"] == b["models"]
    assert sum(a["pairs"].values()) == 12


# --- fail-open contract: a crashing rotation must not break anything ----------

def test_append_survives_rotation_crash(tmp_path, monkeypatch):
    """monkeypatch os.replace to throw: _append_metrics still lands its rows
    and --rotate-now still exits 0 (printing rotated=0)."""
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_metrics_store as store
    import router_spawn

    def boom(src, dst):
        raise OSError("simulated rotation crash")

    monkeypatch.setattr(store.os, "replace", boom)
    path = store.metrics_path(repo=str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x" * 300 + "\n")  # over any cap → rotation would fire

    # the store module itself: crash contained, returns 0, never raises
    assert store.rotate_if_needed(path, cap=200, n=3) == 0

    # the spawn append path: rows land, exception swallowed (same try block)
    result = {
        "project": "my-project", "profile": "P0_FORE",
        "gates_loaded": {}, "source": "registry.json",
        "chain": [{"hop": 1, "provider": "prov-a", "model": "model-a1",
                   "usd_1m": 0.1}],
        "exclusions": [],
    }
    rows = [(1, "prov-a", "model-a1", 0.1, "zdr", {})]
    router_spawn._append_metrics(rows, result)  # must not raise
    with open(path) as f:
        raw = f.read()
    assert "x" * 300 in raw  # append, never truncate: the old bytes survive
    good = [json.loads(line) for line in raw.splitlines()
            if line.startswith("{")]
    assert len(good) == 1 and good[0]["provider"] == "prov-a"


def test_rotate_now_cli_exits_zero_even_when_rotation_crashes(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_metrics as rmetrics
    import router_metrics_store as store

    def boom(src, dst):
        raise OSError("simulated rotation crash")

    monkeypatch.setattr(store.os, "replace", boom)
    with open(os.path.join(str(tmp_path), "metrics.jsonl"), "w") as f:
        f.write('{"a": 1}\n')
    rc = rmetrics.main(["--rotate-now"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("rotated=")
    assert " retained_bytes=" in out[0]


def test_rotate_now_cli_happy_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with open(str(home / "metrics.jsonl.1"), "w") as f:
        f.write("x" * 40 + "\n")
    with open(str(home / "metrics.jsonl"), "w") as f:
        f.write("y" * 300 + "\n")
    q = _run(METRICS, ("--rotate-now",),
             env_extra={"TASK_ROUTER_HOME": str(home),
                        "ROUTER_METRICS_MAX_BYTES": "200",
                        "ROUTER_METRICS_GENERATIONS": "3"})
    assert q.returncode == 0, q.stderr
    lines = q.stdout.strip().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("rotated=1") and " retained_bytes=" in line
    retained = int(line.split("retained_bytes=")[1])
    assert retained == 342  # 41 (.1: 40 x + \n) + 301 (closed current: 300 y + \n)
    assert os.path.exists(str(home / "metrics.jsonl.2"))

    # idle store: one line, rotated=0, still exit 0
    empty = tmp_path / "empty"
    empty.mkdir()
    q2 = _run(METRICS, ("--rotate-now",),
              env_extra={"TASK_ROUTER_HOME": str(empty)})
    assert q2.returncode == 0
    assert q2.stdout.strip() == "rotated=0 retained_bytes=0"


# --- gauges -------------------------------------------------------------------

def test_gauge_callbacks_current_vs_family(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_prometheus as rp

    with open(str(tmp_path / "metrics.jsonl"), "w") as f:
        f.write('{"x": 1}\n' * 3)          # 27 bytes — current file
    with open(str(tmp_path / "metrics.jsonl.1"), "w") as f:
        f.write("abcdefghij\n")            # 11 bytes — closed generation
    with open(str(tmp_path / "metrics.jsonl.2"), "w") as f:
        f.write("klmnopqrst\n")            # 11 bytes — oldest generation

    text = rp.render_metrics().decode()
    assert "router_metrics_jsonl_bytes 27" in text          # CURRENT only
    assert "task_router_metrics_jsonl_bytes 27" in text     # alias intact
    assert "router_metrics_jsonl_family_bytes 49" in text   # 27 + 11 + 11
    assert "task_router_metrics_jsonl_family_bytes 49" in text


def test_family_gauge_fail_open_without_store_files(tmp_path, monkeypatch):
    """No files at all → family gauge 0, current gauge 0, scrape never fails."""
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    import router_prometheus as rp

    text = rp.render_metrics().decode()
    assert "router_metrics_jsonl_family_bytes 0" in text
    assert "router_metrics_jsonl_bytes 0" in text


def test_store_module_stdlib_only():
    """pyproject keeps runtime deps empty on purpose; the rotation module must
    not sneak one in."""
    import router_metrics_store as store
    src = open(store.__file__).read()
    assert "import requests" not in src and "import yaml" not in src
    mods = set()
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("import "):
            mods.add(line[len("import "):].split(".")[0].split(" as ")[0])
        elif line.startswith("from "):
            mods.add(line[len("from "):].split(" import")[0].split(".")[0])
    allowed = {"os", "re", "__future__"}
    assert mods <= allowed, mods

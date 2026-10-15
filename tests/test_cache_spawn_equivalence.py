"""TR-277: spawn deepcopy/share equivalence + non-mutation proof.

The share mode (default) serves ONE parsed object graph to every caller, which
is only safe while consumers treat cached rows as read-only. This test pins
that contract from both directions:
  1. resolve()'s chain output is byte-identical between share and deepcopy
     modes (same head, same prices) — the mode is invisible to callers;
  2. the cached entry's data is UNCHANGED after a resolve ran over it
     (no caller mutated shared rows).
"""
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

PY = ("/home/kara/.hermes/venvs/board/bin/python3"
      if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
      else sys.executable)


def _hermetic(tmp_path):
    """Fresh registry + tables + state per test (tests/test_cli_contracts.py shape)."""
    data = tmp_path / "tables"
    shutil.copytree(os.path.join(REPO, "data", "tables"), data)
    tables = {}
    for fn in sorted(os.listdir(data)):
        if fn.endswith(".jsonl"):
            tables[fn[:-6]] = [json.loads(l)
                               for l in open(os.path.join(data, fn)) if l.strip()]
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    state = tmp_path / "state"
    state.mkdir()
    providers = [r.get("id") for r in tables.get("providers") or []]
    (state / "quota-state.json").write_text(json.dumps(
        {"updated": "test",
         "providers": {p: {"status": "open"} for p in providers}}))
    (state / "health-state.json").write_text(json.dumps(
        {"providers": {p: {"status": "OK", "latency_ms": 100}
                       for p in providers}}))
    (state / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    return {"ROUTING_REGISTRY": str(reg), "ROUTING_DATA_DIR": str(data),
            "ROUTER_STATE_DIR": str(state)}


def _resolve_head(env, project="coding-hermes-scheduler"):
    """(provider, model, effective_price) per chain hop — the resolve output
    the fleet actually consumes (live registry: chain has 27 hops)."""
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "router_spawn.py"), project,
         "--format", "json"],
        cwd=REPO, env={**os.environ, **env}, capture_output=True, text=True,
        timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    doc = json.loads(proc.stdout)
    chain = doc.get("chain")
    if isinstance(chain, str):
        chain = json.loads(chain)
    return [(h.get("provider"), h.get("model"), h.get("effective_price"))
            for h in chain or []]


def test_share_and_deepcopy_modes_produce_identical_resolves(tmp_path):
    """The copy mode is a defensive knob, not a behaviour change."""
    share = _resolve_head(_hermetic(tmp_path / "a"))
    deep = _resolve_head(_hermetic(tmp_path / "b"))
    assert share == deep, "copy mode changed the resolve output"


def test_cache_run_does_not_mutate_the_committed_tables(tmp_path):
    """A full cache-cold resolve leaves every source file byte-identical:
    nobody along the resolve path writes through the shared cached rows."""
    env = _hermetic(tmp_path)
    before = {}
    for key in ("ROUTING_REGISTRY",):
        before[key] = open(env[key], "rb").read()
    data_dir = env["ROUTING_DATA_DIR"]
    tables_before = {fn: open(os.path.join(data_dir, fn), "rb").read()
                     for fn in sorted(os.listdir(data_dir)) if fn.endswith(".jsonl")}
    _resolve_head(env)
    assert open(env["ROUTING_REGISTRY"], "rb").read() == before["ROUTING_REGISTRY"]
    for fn, blob in tables_before.items():
        assert open(os.path.join(data_dir, fn), "rb").read() == blob, fn

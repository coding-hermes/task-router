"""t_market_precedence — the benchmark bridge must rank by SOURCE CLASS before
score.

The doctrine's precedence is internal n>=3/large-bench > external published >
market percentile > n=1 small-probe. The bridge (router_seed.apply_benchmark_categories)
used to pick the winner with `row_number() ... ORDER BY score/max_score DESC` —
a pure max — so a market PERCENTILE outranked a published ABSOLUTE score
whenever the percentile happened to be numerically larger. Measured on
z-ai/glm-5.3: market code_gen 0.8728 vs the publisher's DeepSWE v1.1 absolute
0.669, and the percentile won the cell.

These tests are hermetic: the seed runs against a copied data dir + scratch
registry, and the competing rows are injected for a live lane that carries no
benchmark or perf evidence in the target categories, so no other seed pass
(overlay, estimates) can insert the cell first and mask the bridge.

Red-proof: with the pre-fix ORDER BY (score only) both assertions fail —
code_gen resolves to 0.8728 (the percentile) and long_horizon resolves to 0.95
(the n=1 probe).
"""
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable

# A live lane with NO benchmark rows and NO model_perf rows in code_gen /
# long_horizon / tool_use (verified against data/tables at 2026-11-20), so the
# only evidence for these cells is what this test injects.
MODEL = "MiniMax-M2"

MARKET = "market"
PUBLISHED = "research-bench-2026-11-20"
PROBE = "live-probe-2026-11-20/PROBE: n=1 small-probe, NOT large-bench"

# (category, [(source, score, max_score), ...], expected_rel, expected_source)
CELLS = [
    # external published absolute (0.669) must beat the larger market
    # percentile (0.8728) — the card's exact inversion.
    ("code_gen", [(MARKET, 0.872832369942, 1.0), (PUBLISHED, 66.9, 100.0)],
     0.669, PUBLISHED),
    # market percentile (0.90) must beat a larger n=1 small-probe (0.95):
    # the rank is a class ordering, not "market always last".
    ("long_horizon", [(MARKET, 0.90, 1.0), (PROBE, 0.95, 1.0)],
     0.90, MARKET),
]


def _hermetic_env(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state),
            "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
            "ROUTER_SEED_PYTHON": PY, "ROUTING_BOARD_PY": PY}


def _inject(env):
    """Append the competing benchmark rows for MODEL to the copied table."""
    path = os.path.join(env["ROUTING_DATA_DIR"], "benchmarks.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        for category, rows, _rel, _src in CELLS:
            for source, score, max_score in rows:
                fh.write(json.dumps({
                    "model": MODEL, "category": category, "score": score,
                    "max_score": max_score, "source": source,
                    "valid_from": "2026-11-20",
                    "reasoning_effort": "default",
                }, sort_keys=True) + "\n")


def _seed(env):
    proc = subprocess.run([PY, os.path.join(SCRIPTS, "router_seed.py")],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr[-1200:]
    with open(env["ROUTING_REGISTRY"], encoding="utf-8") as fh:
        return json.load(fh)


def _perf(reg):
    return {(r["model"], r["category"]): r for r in reg["tables"]["model_perf"]}


def test_source_class_outranks_score(tmp_path):
    env = _hermetic_env(tmp_path)
    _inject(env)
    perf = _perf(_seed(env))
    for category, _rows, expected_rel, expected_source in CELLS:
        row = perf.get((MODEL, category))
        assert row is not None, f"no model_perf row for {MODEL}/{category}"
        assert abs(row["perf"] - expected_rel) < 1e-9, (
            f"{category}: expected {expected_rel} from {expected_source}, "
            f"got {row['perf']} from {row['source_ref']}")
        assert expected_source in row["source_ref"], (
            f"{category}: winning row is {row['source_ref']}, "
            f"expected the {expected_source} row")

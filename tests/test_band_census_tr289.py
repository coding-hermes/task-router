"""TR-289 census helper must be copy-only and match resolver join keys."""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "band_census_tr289.py"


def _write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_census_refuses_the_configured_live_store(tmp_path):
    live = tmp_path / "live.jsonl"
    _write_rows(live, [{"provider": "p", "model": "m"}])
    env = dict(os.environ, ROUTING_OUTCOMES_FILE=str(live))
    result = subprocess.run([sys.executable, str(SCRIPT), str(live)], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "refusing the configured live outcome store" in result.stderr


def test_census_copy_counts_resolver_provider_model_band_join(tmp_path):
    live = tmp_path / "live.jsonl"
    copy = tmp_path / "outcomes-copy.jsonl"
    _write_rows(live, [])
    _write_rows(copy, [
        {"source_system": "router-proxy", "provider": "p", "model": "m",
         "required_categories": {"code_gen": 3, "debug": 1}, "cost_usd": 1,
         "chain": [{"provider": "p", "model": "m"}], "ts": 1},
        {"source_system": "router-proxy", "provider": "p", "model": "m",
         "required_categories": {"code_gen": 3, "debug": 2}, "cost_usd": 1,
         "chain": [{"provider": "p", "model": "m"}], "ts": 2},
        {"source_system": "router-proxy", "provider": "p", "model": "m",
         "required_categories": {"mechanical": 1}, "cost_usd": 1,
         "chain": [{"provider": "p", "model": "m"}], "ts": 3},
    ])
    env = dict(os.environ, ROUTING_OUTCOMES_FILE=str(live))
    result = subprocess.run([sys.executable, str(SCRIPT), str(copy)], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "BEFORE — exact complexity signature" in result.stdout
    assert "AFTER — b2 dominant-category band" in result.stdout
    assert "task classes: 3" in result.stdout
    assert result.stdout.count("task classes: 2") == 1
    assert live.read_text(encoding="utf-8") == ""

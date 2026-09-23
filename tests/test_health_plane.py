"""TR-087: control-plane health plane tests.

Covers the /health + / + /model_status surfaces end-to-end through a real
server process, plus the lookup contract (query, not report).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_health  # noqa: E402
from test_server import server_env, _server, _request  # noqa: E402,F401

REPO = Path(__file__).resolve().parents[1]


def test_health_payload_shape(server_env):
    data_dir = server_env["ROUTING_DATA_DIR"]
    payload = router_health.health(mode="read-only", data_dir=data_dir)
    # identity block
    assert payload["status"] == "ok"
    assert payload["service"].startswith("task-router/")
    assert payload["commit"] and payload["commit"] != "unknown"
    assert payload["mode"] == "read-only"
    # registry freshness
    reg = payload["registry"]
    assert reg["models"] > 0
    assert reg["newest_valid_from"]
    assert isinstance(reg["age_days"], int)
    # gate states
    assert isinstance(payload["circuit"].get("pairs", 0), int)
    assert payload["chains_snapshot"]["count"] >= 0


def test_model_status_is_a_lookup_not_a_report(server_env):
    data_dir = server_env["ROUTING_DATA_DIR"]
    payload = router_health.model_status(data_dir=data_dir)
    assert payload["count"] > 0
    assert payload["count"] == len(payload["lanes"])
    lane = payload["lanes"][0]
    assert {"provider", "model", "status", "normalized_price"} <= set(lane)
    assert lane["status"] in {"ok", "down", "slow", "gated", "disabled", "unprobed"}
    # provider filter narrows to exactly that provider
    pick = payload["lanes"][0]["provider"]
    one = router_health.model_status(provider=pick, data_dir=data_dir)
    assert one["count"] > 0
    assert {ln["provider"] for ln in one["lanes"]} == {pick}


def test_model_status_counts_match_registry(server_env):
    data_dir = Path(server_env["ROUTING_DATA_DIR"])
    rows = [json.loads(l) for l in (data_dir / "models.jsonl").read_text().splitlines() if l.strip()]
    payload = router_health.model_status(data_dir=str(data_dir))
    assert payload["count"] == len(rows)


def test_health_endpoints_answer_without_404(server_env):
    # exercised via the shared _server fixture from test_server.py
    with _server(server_env) as port:
        for path in ("/", "/health", "/model_status", "/model_status?provider=deepseek"):
            code, payload = _request(port, path)
            assert code == 200, (path, payload)
        code, payload = _request(port, "/health")
        assert payload["status"] == "ok"
        code, payload = _request(port, "/model_status?provider=deepseek")
        assert payload["provider"] == "deepseek"
        assert payload["count"] > 0


def test_health_degrades_fail_open_on_bad_state_files(server_env, tmp_path, monkeypatch):
    # corrupt probe + circuit state must not raise — blocks degrade to errors
    import os

    mr = tmp_path / "mr"
    mr.mkdir()
    (mr / "health-state.json").write_text("{corrupt json")
    monkeypatch.setattr(router_health, "MR_DIR", mr)
    payload = router_health.health(mode="read-only",
                                   data_dir=server_env["ROUTING_DATA_DIR"])
    assert payload["status"] == "ok"
    assert "error" in payload["probe"]
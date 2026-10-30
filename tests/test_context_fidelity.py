"""TR-356: context fidelity must detect a silent input cap."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_context_fidelity as fidelity  # noqa: E402
import router_spawn  # noqa: E402


class _StubHandler(BaseHTTPRequestHandler):
    cap: int | None = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        prompt = request["messages"][0]["content"]
        expected = fidelity.estimate_prompt_tokens(prompt)
        reported = min(expected, self.cap) if self.cap else expected
        needle = fidelity.make_probe_prompt()["needle"]
        # A capped upstream cannot see the tail. The uncapped control echoes it.
        content = needle if not self.cap or expected <= self.cap else ""
        body = {"model": request["model"],
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": reported, "completion_tokens": 1}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


@pytest.fixture
def stub_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_capped_upstream_is_non_vacuously_truncated(stub_url):
    """The stub really returns HTTP 200 but caps usage at exactly 2004."""
    _StubHandler.cap = 2004
    result = fidelity.probe_lane(stub_url, "xkiro", "claude-opus-5.5",
                                  prompt_chars=12_000)
    assert result["http_status"] == 200
    assert result["reported_prompt_tokens"] == 2004
    assert result["needle_found"] is False
    assert result["status"] == "TRUNCATED"
    assert result["deficit_tokens"] > result["tolerance_tokens"]


def test_uncapped_upstream_is_ok(stub_url):
    _StubHandler.cap = None
    result = fidelity.probe_lane(stub_url, "xkiro", "gpt-6.1-sol",
                                  prompt_chars=12_000)
    assert result["http_status"] == 200
    assert result["reported_prompt_tokens"] == result["expected_prompt_tokens"]
    assert result["needle_found"] is True
    assert result["status"] == "OK"


def test_model_echo_without_usage_is_unknown_not_healthy():
    prompt = fidelity.make_probe_prompt()
    result = fidelity.judge_response({"model": "requested-id", "choices": []}, prompt)
    assert result["status"] == "UNKNOWN"
    assert "usage" in result["reason"]


def test_state_round_trip_is_lane_scoped(tmp_path):
    result = {"provider": "xkiro", "model": "claude-opus-5.5",
              "status": "TRUNCATED", "measured_at": "2026-10-30T00:00:00+00:00"}
    path = fidelity.save_result(result, tmp_path)
    assert path.exists()
    loaded = fidelity.load_state(tmp_path)
    assert loaded["lanes"]["xkiro/claude-opus-5.5"] == result
    assert loaded["updated"]


def _resolver_fixture(tmp_path):
    models = [
        {"provider": "xkiro", "model": "anthropic/bad", "normalized_price": 0.1,
         "plan_tier": 0, "data_class": "unknown", "context_limit": 500000},
        {"provider": "xkiro", "model": "openai/good", "normalized_price": 0.2,
         "plan_tier": 0, "data_class": "unknown", "context_limit": 500000},
    ]
    tables = {
        "models": models,
        "providers": [{"id": "xkiro", "status": "active"}],
        "model_tier": [{"model": row["model"], "category": "reasoning", "tier": 3}
                        for row in models],
        "task_profiles": [{"id": "PCTX", "title": "context floor"}],
        "task_profile_requirements": [
            {"task_id": "PCTX", "category": "reasoning", "level": 1},
            {"task_id": "PCTX", "category": "min_context", "level": 500000},
        ],
    }
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"version": 3, "tables": tables}))
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(json.dumps({"providers": {"xkiro": {"status": "open"}}}))
    (state / "health-state.json").write_text(json.dumps({"providers": {"xkiro": {"status": "OK"}}}))
    (state / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    fidelity.save_result({"provider": "xkiro", "model": "anthropic/bad",
                          "status": "TRUNCATED", "reason": "reported input capped at 2004",
                          "measured_at": "2026-10-30T00:00:00+00:00"}, state)
    fidelity.save_result({"provider": "xkiro", "model": "openai/good",
                          "status": "OK", "reason": "tail present",
                          "measured_at": "2026-10-30T00:00:00+00:00"}, state)
    return registry, state


def test_context_floor_excludes_only_measured_truncating_lane(monkeypatch, tmp_path):
    registry, state = _resolver_fixture(tmp_path)
    monkeypatch.setattr(router_spawn, "REGISTRY", str(registry))
    monkeypatch.setattr(router_spawn, "DATA_DIR", str(tmp_path / "unused-tables"))
    monkeypatch.setattr(router_spawn, "MR", str(state))
    result = router_spawn.resolve(profile_id="PCTX", sort="price")
    assert result["chain"]
    assert {f'{row["provider"]}/{row["model"]}' for row in result["chain"]} == {"xkiro/openai/good"}
    excluded = next(row for row in result["exclusions"] if row["model"] == "anthropic/bad")
    assert "context-fidelity" in excluded["codes"]
    assert any("context fidelity" in why for why in excluded["why"])
    assert any("context fidelity" in reason for reason in result["gate_reasons"])

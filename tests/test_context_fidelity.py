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

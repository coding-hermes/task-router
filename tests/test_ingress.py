"""TR-236 tests — the bus ingress (transport, translation, refusals, ledger).

Everything here is hermetic: the target endpoints are stub HTTP servers on
loopback, the resolver is injected, the bus is never contacted. The one optional
external dependency is the fleet's crier client for the signed-ingress test; that
test skips when `CRIER_CLIENT_PATH` (or the default) is absent, because the
signature scheme is the bus's, not this repo's, to define.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import router_ingress as ri  # noqa: E402


# ---------------------------------------------------------------------------
# stub endpoints
# ---------------------------------------------------------------------------

HERMES_SSE = (
    'event: response.created\n'
    'data: {"type": "response.created", "response": {"id": "resp_1", "model": "Hermes Agent"}}\n'
    '\n'
    'event: response.output_text.delta\n'
    'data: {"type": "response.output_text.delta", "delta": "INGRESS"}\n'
    '\n'
    'event: response.completed\n'
    'data: {"type": "response.completed", "response": {"id": "resp_1", "model": "Hermes Agent", '
    '"output": [{"type": "message", "content": [{"type": "output_text", "text": "INGRESS-OK"}]}], '
    '"usage": {"input_tokens": 11, "output_tokens": 2}}}\n'
    '\n'
)


class StubHandler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json", extra=None):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._send(200, json.dumps({"ok": True}))

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        StubHandler.seen.append({
            "path": path, "body": raw.decode("utf-8", "replace"),
            "auth": self.headers.get("Authorization"),
            "session": self.headers.get("X-Hermes-Session-Key"),
        })
        if "/missing" in path:
            return self._send(404, json.dumps({"error": "not found"}))
        if "broken-sse" in path:
            return self._send(200, "event: response.output_text.delta\n"
                                   'data: {"type": "response.output_text.delta", "delta": "x"}\n\n',
                              "text/event-stream")
        if "/slow" in path:
            time.sleep(4)
            return self._send(200, json.dumps({"slow": True}))
        if "/v1/responses" in path:
            return self._send(200, HERMES_SSE, "text/event-stream",
                              {"X-Hermes-Session-Id": "sess-123"})
        if "/v1/chat/completions" in path:
            return self._send(200, json.dumps({
                "model": "stub/openai-model",
                "choices": [{"message": {"role": "assistant", "content": "OPENAI-OK"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }))
        if "/v1/messages" in path:
            return self._send(200, json.dumps({
                "model": "stub/anthropic-model",
                "content": [{"type": "text", "text": "ANTHROPIC-OK"}],
                "usage": {"input_tokens": 5, "output_tokens": 4},
            }))
        if path.endswith("/hook"):
            return self._send(202, json.dumps({"accepted": True}))
        if path.endswith("/bad-json"):
            return self._send(200, "not json at all")
        return self._send(404, json.dumps({"error": "not found"}))


@pytest.fixture()
def stub():
    StubHandler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    yield base
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write_endpoints(tmp_path, rows):
    path = tmp_path / "endpoints.jsonl"
    path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n",
                    encoding="utf-8")
    return path


def make_ingress(tmp_path, rows, resolver=None, admission=None, opener=None, logger=None):
    path = write_endpoints(tmp_path, rows)
    endpoints, problems = ri.load_endpoints(path)
    assert not problems, problems
    return ri.Ingress(
        endpoints,
        ledger=tmp_path / "ingress-ledger.jsonl",
        resolver=resolver or (lambda profile=None, project=None: (
            {"provider": "stub", "model": "stub/model-1"}, None)),
        admission=admission,
        opener=opener,
        logger=logger,
    )


def envelope(payload, ident="m-1", sender="uhlp", idem=None):
    env = {"id": ident, "sender": sender, "payload": payload}
    if idem:
        env["idempotency_key"] = idem
        env["payload"]["idempotency_key"] = idem
    return env


def ledger_rows(ingress):
    path = Path(ingress.ledger)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def hermes_row(address, **over):
    row = {"id": "hermes-stub", "protocol": "hermes-gateway", "address": address,
           "auth": "none", "reply": "sse-last-message", "timeout_s": 30, "model": "router"}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# declarations are data, and an incomplete one is REFUSED
# ---------------------------------------------------------------------------


def test_endpoint_missing_timeout_is_refused():
    endpoint = ri.Endpoint({"id": "x", "protocol": "hermes-gateway",
                            "address": "http://127.0.0.1:1", "auth": "none",
                            "reply": "sse-last-message"})
    assert not endpoint.usable
    assert "missing timeout_s" in endpoint.refusal_reason()


def test_endpoint_unknown_protocol_is_refused():
    endpoint = ri.Endpoint({"id": "x", "protocol": "grpc", "address": "http://127.0.0.1:1",
                            "auth": "none", "reply": "none", "timeout_s": 5})
    assert not endpoint.usable
    assert "unknown protocol" in endpoint.refusal_reason()


def test_webhook_cannot_declare_an_inline_reply():
    endpoint = ri.Endpoint({"id": "x", "protocol": "webhook", "address": "http://127.0.0.1:1/hook",
                            "auth": "none", "reply": "sse-last-message", "timeout_s": 5})
    assert not endpoint.usable
    assert "inline reply" in endpoint.refusal_reason()


def test_non_http_address_is_refused():
    endpoint = ri.Endpoint({"id": "x", "protocol": "openai-compatible", "address": "127.0.0.1:1",
                            "auth": "none", "reply": "none", "timeout_s": 5})
    assert not endpoint.usable
    assert "not an http(s) URL" in endpoint.refusal_reason()


def test_reply_rule_needs_a_path():
    endpoint = ri.Endpoint({"id": "x", "protocol": "openai-compatible", "address": "http://a:1",
                            "auth": "none", "reply": "json:", "timeout_s": 5})
    assert not endpoint.usable
    assert "needs a path" in endpoint.refusal_reason()


def test_registry_survives_a_damaged_line(tmp_path):
    path = write_endpoints(tmp_path, [
        {"id": "good", "protocol": "webhook", "address": "http://127.0.0.1:1/hook",
         "auth": "none", "reply": "none", "timeout_s": 5},
        "{ this is not json",
        {"id": "good", "protocol": "webhook", "address": "http://127.0.0.1:1/hook2",
         "auth": "none", "reply": "none", "timeout_s": 5},
    ])
    endpoints, problems = ri.load_endpoints(path)
    assert len(endpoints) == 2
    assert any("not JSON" in p for p in problems)
    assert not endpoints[1].usable
    assert "duplicate id" in endpoints[1].refusal_reason()


# ---------------------------------------------------------------------------
# transport: the several wire shapes, end to end against stubs
# ---------------------------------------------------------------------------


def test_hermes_gateway_sse_roundtrip(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hello",
                                    "session": "tick-1", "reply_to": "orchestrator"}))
    row = out["ledger"]
    assert row["ok"] is True and row["outcome"] == "ok", row
    assert out["reply"]["reply"] == "INGRESS-OK"
    assert row["transform"] == "hermes-gateway/v1-responses+sse"
    assert row["tokens_in"] == 11 and row["tokens_out"] == 2
    assert StubHandler.seen[-1]["path"] == "/v1/responses"
    assert StubHandler.seen[-1]["session"] == "tick-1"
    assert out["reply_to"] == "orchestrator"
    # the gateway reports "Hermes Agent", not a provider model: recorded as unknown
    assert row["model_served"] is None
    assert "not reported by endpoint" in json.dumps(row["null_reasons"])
    assert row["model_chosen"] == "stub/model-1"


def test_openai_compatible_json_roundtrip(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "proxy-stub", "protocol": "openai-compatible", "address": stub,
        "auth": "none", "reply": "json:choices.0.message.content", "timeout_s": 30,
    }])
    out = ingress.process(envelope({"endpoint": "proxy-stub", "prompt": "hi"}))
    assert out["reply"]["reply"] == "OPENAI-OK"
    assert out["ledger"]["model_served"] == "stub/openai-model"
    assert out["ledger"]["tokens_in"] == 7
    assert out["ledger"]["transform"] == "openai-compatible/v1-chat-completions+json"


def test_anthropic_messages_is_a_declared_shape(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "anthropic-stub", "protocol": "anthropic-messages", "address": stub,
        "auth": "none", "reply": "json:content.0.text", "timeout_s": 30, "model": "none",
    }])
    out = ingress.process(envelope({"endpoint": "anthropic-stub", "prompt": "hi"}))
    assert out["reply"]["reply"] == "ANTHROPIC-OK"
    assert out["ledger"]["transform"] == "anthropic-messages/v1-messages+json"
    body = json.loads(StubHandler.seen[-1]["body"])
    assert body["max_tokens"] > 0 and body["messages"][0]["role"] == "user"


def test_webhook_is_honest_about_having_no_inline_reply(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "sink", "protocol": "webhook", "address": stub + "/hook",
        "auth": "none", "reply": "webhook-separate", "timeout_s": 10, "model": "none",
    }])
    out = ingress.process(envelope({"endpoint": "sink", "prompt": "job"}))
    row = out["ledger"]
    assert row["outcome"] == "accepted" and row["ok"] is True
    assert out["reply"]["reply"] == ""
    assert "webhook-separate" in out["reply"]["reason"]
    pushed = json.loads(StubHandler.seen[-1]["body"])
    assert pushed["in_reply_to"] == "m-1" and pushed["prompt"] == "job"


def test_unservable_sse_stream_is_not_a_partial_answer(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "broken", "protocol": "hermes-gateway", "address": stub + "/broken-sse",
        "auth": "none", "reply": "sse-last-message", "timeout_s": 10,
    }])
    out = ingress.process(envelope({"endpoint": "broken", "prompt": "hi"}))
    assert out["ledger"]["ok"] is False
    assert "without a terminal event" in out["ledger"]["reason"]


# ---------------------------------------------------------------------------
# refusals: named, bus-visible, ledger-visible
# ---------------------------------------------------------------------------


def test_unknown_endpoint_is_refused_by_name(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    out = ingress.process(envelope({"endpoint": "nope", "prompt": "hi"}))
    assert out["ledger"]["outcome"] == "refused"
    assert "unknown-endpoint" in out["ledger"]["reason"] and "hermes-stub" in out["ledger"]["reason"]
    assert out["reply"]["ok"] is False
    assert out["ack"] is True                      # a named refusal is terminal
    assert out["ledger"]["endpoint_served"] is None


def test_no_prompt_is_refused(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    out = ingress.process(envelope({"endpoint": "hermes-stub"}))
    assert "no-prompt" in out["ledger"]["reason"]


def test_incomplete_endpoint_is_refused_at_use(tmp_path, stub):
    path = write_endpoints(tmp_path, [{"id": "half", "protocol": "hermes-gateway",
                                       "address": stub, "auth": "none",
                                       "reply": "sse-last-message"}])
    endpoints, _ = ri.load_endpoints(path)
    ingress = ri.Ingress(endpoints, ledger=tmp_path / "l.jsonl",
                         resolver=lambda profile=None, project=None: (None, "n/a"))
    out = ingress.process(envelope({"endpoint": "half", "prompt": "hi"}))
    assert "endpoint-incomplete" in out["ledger"]["reason"]


def test_auth_reference_that_cannot_resolve_refuses(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub, auth="env:TR236_DEFINITELY_UNSET")])
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    assert out["ledger"]["outcome"] == "refused"
    assert out["ledger"]["reason"].startswith("auth-unresolved:")
    assert StubHandler.seen == []                  # the doomed call was never fired


def test_auth_env_reference_is_sent_as_bearer(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("TR236_TOKEN", "tok-123")
    ingress = make_ingress(tmp_path, [hermes_row(stub, auth="env:TR236_TOKEN")])
    ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    assert StubHandler.seen[-1]["auth"] == "Bearer tok-123"


def test_auth_env_file_reference(tmp_path, stub):
    env_file = tmp_path / ".env"
    env_file.write_text('OTHER=1\nTR236_FILE_TOKEN="from-file"\n')
    ingress = make_ingress(tmp_path, [hermes_row(stub, auth="env-file:%s:TR236_FILE_TOKEN" % env_file)])
    ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    assert StubHandler.seen[-1]["auth"] == "Bearer from-file"


def test_unreachable_endpoint_produces_a_failed_reply_not_silence(tmp_path):
    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9")], )
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    assert out["ledger"]["ok"] is False
    assert out["ledger"]["outcome"] == "failed"
    assert "transport-error" in out["ledger"]["reason"]
    assert out["reply"]["ok"] is False and out["reply"]["reason"]


def test_slow_endpoint_is_bounded_by_the_declared_timeout(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "slow", "protocol": "openai-compatible", "address": stub + "/slow",
        "auth": "none", "reply": "json:choices.0.message.content", "timeout_s": 1,
    }])
    started = time.time()
    out = ingress.process(envelope({"endpoint": "slow", "prompt": "hi"}))
    wall = time.time() - started
    assert wall < 3.5, wall
    assert out["ledger"]["outcome"] in ("timeout", "failed")
    assert out["reply"]["ok"] is False


def test_http_error_status_is_a_failed_hop(tmp_path, stub):
    ingress = make_ingress(tmp_path, [{
        "id": "missing", "protocol": "openai-compatible", "address": stub + "/missing",
        "auth": "none", "reply": "json:x", "timeout_s": 10,
    }])
    out = ingress.process(envelope({"endpoint": "missing", "prompt": "hi"}))
    assert out["ledger"]["http_status"] == 404
    assert "endpoint-http-404" in out["ledger"]["reason"]


# ---------------------------------------------------------------------------
# amplification bound, idempotency, ledger shape, model ownership
# ---------------------------------------------------------------------------


def test_admission_refuses_instead_of_queueing_forever():
    admission = ri.Admission(max_inflight=1, queue_max=0, wait_s=0.2)
    release = threading.Event()
    entered = threading.Event()

    def hold():
        with admission:
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    assert entered.wait(5)
    with pytest.raises(ri.Refusal) as exc:
        with admission:
            pass
    assert "overloaded" in exc.value.reason
    release.set()
    worker.join(5)
    assert admission.stats()["rejected"] == 1


def test_burst_over_the_bound_is_refused_on_the_bus(tmp_path, stub):
    admission = ri.Admission(max_inflight=1, queue_max=0, wait_s=0.2)
    ingress = make_ingress(tmp_path, [hermes_row(stub)], admission=admission)
    admission.__enter__()                        # occupy the only slot
    try:
        out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    finally:
        admission.__exit__()
    assert out["ledger"]["outcome"] == "refused"
    assert out["ledger"]["reason"].startswith("overloaded")
    assert StubHandler.seen == []


def test_duplicate_idempotency_key_does_not_run_twice(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    first = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}, idem="k1"))
    second = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}, idem="k1"))
    assert first["ledger"]["outcome"] == "ok"
    assert second["ledger"]["outcome"] == "duplicate"
    assert second["ledger"]["ok"] is True
    assert len(StubHandler.seen) == 1


def test_ledger_row_carries_the_audit_fields(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi", "session": "s-1"}))
    row = ledger_rows(ingress)[-1]
    for field in ("ts", "inbound_id", "endpoint_requested", "endpoint_served", "protocol",
                  "transform", "outcome", "ok", "seconds", "model_chosen", "null_reasons"):
        assert field in row, field
    assert row["inbound_id"] == "m-1"
    assert row["endpoint_served"] == "hermes-stub"
    assert row["cost_usd"] is None
    assert "cost_usd" in row["null_reasons"]      # a null carries a reason


def test_reply_envelope_is_pairable_from_the_bus_alone(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi", "session": "s9"}))
    reply = out["reply"]
    for field in ("in_reply_to", "endpoint", "protocol", "ok", "reply", "seconds"):
        assert field in reply, field
    assert reply["in_reply_to"] == "m-1"
    assert reply["endpoint"] == "hermes-stub" and reply["protocol"] == "hermes-gateway"


def test_model_choice_is_the_routers_and_is_recorded(tmp_path, stub):
    calls = []

    def resolver(profile=None, project=None):
        calls.append(profile)
        return {"provider": "xkiro", "model": "xkiro/picked"}, None

    ingress = make_ingress(tmp_path, [hermes_row(stub)], resolver=resolver)
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi",
                                    "profile": "P1_CODING"}))
    assert calls == ["P1_CODING"]
    assert out["ledger"]["model_chosen"] == "xkiro/picked"
    assert json.loads(StubHandler.seen[-1]["body"])["model"] == "xkiro/picked"


def test_model_none_endpoint_never_asks_the_router(tmp_path, stub):
    calls = []

    def resolver(profile=None, project=None):
        calls.append(profile)
        return {"provider": "x", "model": "y"}, None

    ingress = make_ingress(tmp_path, [hermes_row(stub, model="none")], resolver=resolver)
    ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi", "profile": "P1"}))
    assert calls == []
    assert "model" not in json.loads(StubHandler.seen[-1]["body"])


def test_resolver_failure_is_recorded_not_hidden(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)],
                           resolver=lambda profile=None, project=None: (None, "registry missing"))
    out = ingress.process(envelope({"endpoint": "hermes-stub", "prompt": "hi"}))
    assert out["ledger"]["model_chosen"] is None
    assert out["ledger"]["null_reasons"]["model_chosen"] == "registry missing"
    assert out["ledger"]["ok"] is True             # fail-open on routing, loud in the record


# ---------------------------------------------------------------------------
# the push door: authenticated, or refused with a reason in the ledger
# ---------------------------------------------------------------------------


@pytest.fixture()
def ingress_server(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub)])
    delivered = []

    def verify(headers, method, path):
        if headers.get("X-Agent-ID") != "uhlp":
            return "unauthorized: unknown agent id"
        return None

    def bus_sink(payload, target):
        delivered.append({"target": target, "payload": payload})
        return "ok:%s" % target

    handler = ri._make_handler(ingress, "sekret", verify, lambda *_: None, bus_sink=bus_sink)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1], ingress, delivered
    httpd.shutdown()
    httpd.server_close()


def _post(url, payload, token=None, ident=None):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if ident:
        request.add_header("X-Agent-ID", ident)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_push_door_forwards_a_bare_payload(ingress_server, stub):
    base, _, delivered = ingress_server
    status, doc = _post(base + "/ingress/v1/messages",
                        {"endpoint": "hermes-stub", "prompt": "hi"}, token="sekret", ident="uhlp")
    assert status == 200 and doc["ok"] is True, doc
    # no reply_to in the payload -> the declared sender (X-Agent-ID) is answered
    assert doc["outcome"] == "ok" and doc["reply_to"] == "uhlp"
    # the push door reports back on the bus too, and the receipt says so
    assert doc["bus_delivery"] == "ok:uhlp"
    assert delivered and delivered[-1]["target"] == "uhlp"
    assert delivered[-1]["payload"]["in_reply_to"]


def test_push_door_refuses_without_the_token_and_logs_the_refusal(ingress_server):
    base, ingress, _ = ingress_server
    status, doc = _post(base + "/ingress/v1/messages", {"endpoint": "hermes-stub", "prompt": "hi"},
                        ident="uhlp")
    assert status == 401 and doc["ok"] is False
    assert "bearer" in doc["reason"]
    row = ledger_rows(ingress)[-1]
    assert row["outcome"] == "refused" and "bearer" in row["reason"]


def test_push_door_refuses_an_unsigned_request_when_sigs_are_required(ingress_server):
    base, ingress, _ = ingress_server
    status, doc = _post(base + "/ingress/v1/messages", {"endpoint": "hermes-stub", "prompt": "hi"},
                        token="sekret")
    assert status == 401
    assert "unknown agent id" in doc["reason"]


# ---------------------------------------------------------------------------
# signed ingress: the bus's own signature scheme (skips without the client)
# ---------------------------------------------------------------------------


def _crier_or_skip():
    try:
        return ri.load_crier_client()
    except Exception as exc:  # noqa: BLE001
        pytest.skip("crier client unavailable: %s" % exc)


def test_signature_verifier_accepts_the_bus_scheme():
    crier = _crier_or_skip()
    key = crier.SigningKey.from_bytes(bytes(range(32)))
    verifier = ri.make_signature_verifier({"router-ingress": key.public_key_hex})
    ts = str(int(time.time()))
    message = ("POST\n/ingress/v1/messages\n%s" % ts).encode()
    signature = key.sign(message).hex()
    headers = {"X-Agent-ID": "router-ingress", "X-Agent-Ts": ts, "X-Agent-Sig": signature}
    assert verifier(headers, "POST", "/ingress/v1/messages") is None
    assert "does not verify" in verifier({**headers, "X-Agent-Sig": "00" * 64},
                                         "POST", "/ingress/v1/messages")
    assert "unknown agent id" in verifier({**headers, "X-Agent-ID": "nobody"},
                                         "POST", "/ingress/v1/messages")
    old = str(int(time.time()) - 600)
    stale = {"X-Agent-ID": "router-ingress", "X-Agent-Ts": old,
             "X-Agent-Sig": key.sign(("POST\n/ingress/v1/messages\n%s" % old).encode()).hex()}
    assert "window" in verifier(stale, "POST", "/ingress/v1/messages")


def test_signature_verifier_with_empty_keys_refuses_everything():
    crier = _crier_or_skip()  # noqa: F841
    verifier = ri.make_signature_verifier({})
    assert "unknown agent id" in verifier(
        {"X-Agent-ID": "x", "X-Agent-Ts": str(int(time.time())), "X-Agent-Sig": "00"},
        "POST", "/ingress/v1/messages")


# ---------------------------------------------------------------------------
# the registry that ships in the repo must be usable
# ---------------------------------------------------------------------------


def test_fetch_agent_keys_without_a_bus_url_is_empty_not_a_crash():
    assert ri.fetch_agent_keys(None) == {}


def test_shipped_endpoint_registry_parses_and_is_usable():
    endpoints, problems = ri.load_endpoints(ri.DEFAULT_ENDPOINTS_PATH)
    assert not problems, problems
    assert endpoints, "the shipped registry declares no endpoints"
    bad = [e.refusal_reason() for e in endpoints if not e.usable]
    assert not bad, bad
    assert {e.protocol for e in endpoints} >= {"hermes-gateway", "webhook"}


# ---------------------------------------------------------------------------
# per-lane isolation (SCHED-GAP-1713): one lane's trouble is NEVER another's
#
# The router is one path for every lane's ticks, so it must fail PER LANE and
# LOUDLY, must not be a bottleneck, and must not amplify a burst onto a target
# (the TR-169 lesson). These tests pin the separation: a lane that is hung,
# saturated, bursting or tripped cannot refuse, slow or flood a peer.
# ---------------------------------------------------------------------------


def test_stats_show_per_lane_state_and_bounds(tmp_path, stub):
    guard = ri.LaneGuard(max_inflight=3, queue_max=5, failures=4, open_s=12)
    ingress = make_ingress(tmp_path, [hermes_row(stub, id="helix")], admission=guard)
    ingress.process(envelope({"endpoint": "helix", "prompt": "hi"}))
    stats = guard.stats()
    assert stats["per_lane_max_inflight"] == 3 and stats["per_lane_queue_max"] == 5
    assert stats["circuit_failure_threshold"] == 4 and stats["global_max_inflight"] == 0
    lane = stats["lanes"]["helix"]
    assert lane["state"] == "closed" and lane["accepted"] == 1 and lane["total_ok"] == 1


def test_lane_is_recorded_in_the_ledger_and_the_reply(tmp_path, stub):
    ingress = make_ingress(tmp_path, [hermes_row(stub, id="helix")])
    out = ingress.process(envelope({"endpoint": "helix", "prompt": "hi"}))
    assert out["ledger"]["lane"] == "helix"
    assert out["reply"]["lane"] == "helix"


def test_a_shared_pool_is_the_blast_radius_we_removed(tmp_path, stub):
    """The legacy global Admission is the defect: it refuses a HEALTHY lane."""
    shared = ri.Admission(max_inflight=1, queue_max=0, wait_s=0.1)
    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9", id="dead"),
                                      hermes_row(stub, id="live")], admission=shared)
    held = shared.hold("dead")                       # one hung lane, one shared slot
    try:
        out = ingress.process(envelope({"endpoint": "live", "prompt": "hi"}))
    finally:
        held.settle()
    assert out["ledger"]["outcome"] == "refused"
    assert out["ledger"]["reason"].startswith("overloaded")   # the live lane is collateral


def test_a_hung_lane_cannot_refuse_a_peer(tmp_path, stub):
    guard = ri.LaneGuard(max_inflight=1, queue_max=0, wait_s=0.1)
    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9", id="dead"),
                                      hermes_row(stub, id="live")], admission=guard)
    held = guard.hold("dead")                        # the dead lane is saturated
    try:
        refused = ingress.process(envelope({"endpoint": "dead", "prompt": "x"}))
        assert refused["ledger"]["outcome"] == "refused"
        assert refused["ledger"]["reason"].startswith("lane-busy")
        assert "dead" in refused["ledger"]["reason"]
        live = ingress.process(envelope({"endpoint": "live", "prompt": "hi"}, ident="m-2"))
        assert live["ledger"]["outcome"] == "ok", live["ledger"]
    finally:
        held.settle()


def test_lanes_run_concurrently_a_slow_lane_holds_only_its_own_slot(tmp_path, stub):
    guard = ri.LaneGuard(max_inflight=1, queue_max=1, wait_s=3.0)
    ingress = make_ingress(tmp_path, [
        {"id": "slow", "protocol": "openai-compatible", "address": stub + "/slow",
         "auth": "none", "reply": "json:choices.0.message.content", "timeout_s": 15},
        hermes_row(stub, id="live"),
    ], admission=guard)
    results = {}

    def run_slow():
        results["slow"] = ingress.process(envelope({"endpoint": "slow", "prompt": "s"},
                                                   ident="s-1"))

    worker = threading.Thread(target=run_slow, daemon=True)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and guard.lane_stats().get("slow", {}).get("inflight", 0) == 0:
        time.sleep(0.02)
    assert guard.lane_stats()["slow"]["inflight"] == 1, "the slow lane never entered flight"
    # while the slow lane is busy, its own second message waits/refuses but a
    # DIFFERENT lane is served on its own budget.
    peer = ingress.process(envelope({"endpoint": "live", "prompt": "hi"}, ident="m-2"))
    assert peer["ledger"]["outcome"] == "ok", peer["ledger"]
    worker.join(20)
    assert "slow" in results
    assert guard.lane_stats()["slow"]["peak_inflight"] == 1
    assert guard.lane_stats()["live"]["peak_inflight"] == 1


def test_a_lane_that_keeps_failing_trips_and_is_refused_fast(tmp_path):
    guard = ri.LaneGuard(failures=3, open_s=30)
    loud = []
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.URLError("connection refused")

    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9", id="dead")],
                           admission=guard, opener=opener, logger=loud.append)
    for i in range(3):
        out = ingress.process(envelope({"endpoint": "dead", "prompt": "x"}, ident="m-%d" % i))
        assert out["ledger"]["outcome"] == "failed", out["ledger"]
    fired = len(calls)
    assert fired == 3
    late = ingress.process(envelope({"endpoint": "dead", "prompt": "x"}, ident="m-late"))
    assert late["ledger"]["outcome"] == "refused"
    assert late["ledger"]["reason"].startswith("endpoint-circuit-open")
    assert "dead" in late["ledger"]["reason"] and "consecutive failures" in late["ledger"]["reason"]
    assert late["reply"]["ok"] is False
    assert "endpoint-circuit-open" in late["reply"]["reason"]      # loud on the bus too
    assert len(calls) == fired                                    # the target was not touched
    assert late["ledger"]["lane"] == "dead"
    lane = guard.lane_stats()["dead"]
    assert lane["state"] == "open" and lane["consecutive_failures"] == 3
    assert lane["open_count"] == 1
    assert lane["last_failure"].startswith("transport-error")
    assert guard.stats()["trips"] == 1
    assert any("LANE dead circuit OPENED" in line for line in loud), loud


def test_a_tripped_lane_does_not_refuse_a_peer(tmp_path, stub):
    guard = ri.LaneGuard(failures=1, open_s=60)
    loud = []
    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9", id="dead"),
                                      hermes_row(stub, id="live")],
                           admission=guard, logger=loud.append)
    ingress.process(envelope({"endpoint": "dead", "prompt": "x"}))
    assert guard.lane_stats()["dead"]["state"] == "open"
    live = ingress.process(envelope({"endpoint": "live", "prompt": "hi"}, ident="m-2"))
    assert live["ledger"]["outcome"] == "ok"
    assert guard.lane_stats()["live"]["state"] == "closed"
    assert guard.stats()["recoveries"] == 0


def test_a_half_open_probe_closes_the_circuit_loudly(tmp_path, stub):
    guard = ri.LaneGuard(failures=2, open_s=0.05)
    loud = []
    state = {"fail": True}

    class FlakyOpener:
        def __call__(self, request, timeout=None):
            if state["fail"]:
                raise urllib.error.URLError("connection refused")
            return urllib.request.urlopen(request, timeout=timeout)

    ingress = make_ingress(tmp_path, [hermes_row(stub, id="flaky")],
                           admission=guard, opener=FlakyOpener(), logger=loud.append)
    for i in range(2):
        ingress.process(envelope({"endpoint": "flaky", "prompt": "x"}, ident="m-%d" % i))
    assert guard.lane_stats()["flaky"]["state"] == "open"
    refused = ingress.process(envelope({"endpoint": "flaky", "prompt": "x"}, ident="m-open"))
    assert refused["ledger"]["reason"].startswith("endpoint-circuit-open")

    time.sleep(0.1)                                   # the cooldown elapses
    assert guard.lane_stats()["flaky"]["state"] == "half-open"
    state["fail"] = False
    probe = ingress.process(envelope({"endpoint": "flaky", "prompt": "hi"}, ident="m-probe"))
    assert probe["ledger"]["outcome"] == "ok", probe["ledger"]
    lane = guard.lane_stats()["flaky"]
    assert lane["state"] == "closed" and lane["consecutive_failures"] == 0
    assert lane["open_count"] == 1 and guard.stats()["recoveries"] == 1
    assert any("circuit CLOSED" in line for line in loud), loud


def test_a_failed_half_open_probe_reopens_with_backoff(tmp_path):
    guard = ri.LaneGuard(failures=1, open_s=0.05)
    loud = []

    def opener(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    ingress = make_ingress(tmp_path, [hermes_row("http://127.0.0.1:9", id="dead")],
                           admission=guard, opener=opener, logger=loud.append)
    ingress.process(envelope({"endpoint": "dead", "prompt": "x"}, ident="m-1"))
    assert guard.lane_stats()["dead"]["state"] == "open"
    time.sleep(0.08)
    probe = ingress.process(envelope({"endpoint": "dead", "prompt": "x"}, ident="m-probe"))
    assert probe["ledger"]["outcome"] == "failed"          # the probe really ran
    lane = guard.lane_stats()["dead"]
    assert lane["state"] == "open" and lane["open_count"] == 2
    assert any("REOPENED" in line for line in loud), loud
    # the backoff doubled: an immediate retry is refused without firing at all
    again = ingress.process(envelope({"endpoint": "dead", "prompt": "x"}, ident="m-again"))
    assert again["ledger"]["reason"].startswith("endpoint-circuit-open")


def test_a_4xx_is_a_request_problem_not_a_lane_outage(tmp_path, stub):
    guard = ri.LaneGuard(failures=2, open_s=60)
    ingress = make_ingress(tmp_path, [{
        "id": "badreq", "protocol": "openai-compatible", "address": stub + "/missing",
        "auth": "none", "reply": "json:x", "timeout_s": 10,
    }], admission=guard)
    for i in range(3):
        out = ingress.process(envelope({"endpoint": "badreq", "prompt": "x"}, ident="m-%d" % i))
        assert out["ledger"]["http_status"] == 404
    assert guard.lane_stats()["badreq"]["state"] == "closed"
    assert guard.stats()["trips"] == 0


def test_a_burst_is_refused_before_it_reaches_the_target(tmp_path, stub):
    guard = ri.LaneGuard(max_inflight=1, queue_max=0, wait_s=0.2)
    ingress = make_ingress(tmp_path, [hermes_row(stub, id="live")], admission=guard)
    held = guard.hold("live")
    try:
        seen_before = len(StubHandler.seen)
        refusals = [ingress.process(envelope({"endpoint": "live", "prompt": "burst-%d" % i},
                                             ident="b-%d" % i))
                    for i in range(12)]
        assert all(r["ledger"]["outcome"] == "refused"
                   and r["ledger"]["reason"].startswith("lane-busy") for r in refusals)
        assert len(StubHandler.seen) == seen_before     # nothing was amplified onto it
        assert guard.lane_stats()["live"]["rejected"] == 12
    finally:
        held.settle()


def test_health_reports_per_lane_state(ingress_server):
    base, _, _ = ingress_server
    _post(base + "/ingress/v1/messages", {"endpoint": "hermes-stub", "prompt": "hi"},
          token="sekret", ident="uhlp")
    with urllib.request.urlopen(base + "/health", timeout=10) as resp:
        doc = json.loads(resp.read())
    assert doc["status"] == "ok"
    assert "admission" in doc and "lanes" in doc["admission"]
    lane = doc["admission"]["lanes"]["hermes-stub"]
    assert lane["state"] == "closed" and lane["accepted"] == 1

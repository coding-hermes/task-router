"""TR-233 — clinepass lanes are unmeasurable: bare registry ids, wire wants modelType/model.

Root cause (2026-09-28): api.cline.bot rejects EVERY bare registry id with
HTTP 400 {"error":"invalid model format. Expected format: modelType/model"},
and err_class files a 400 as request_rejected — which by design records
NOTHING (a 400 is normally our request's fault). So every clinepass lane
looked merely unprobed while being unprobeable. Live control the same day:
'cline-pass/glm-5.3' answers 200 on the same key that 400s bare 'glm-5.3' —
exactly the 'cline-pass/<bare>' callable form model_catalog.api_id has carried
since 2026-08-27. The probers just never applied it.

The fix lives IN THE PROBERS (wire_model_id), never in the registry: registry
rows and result rows keep the bare id so dedup/ranking/ingest keep working.

Hermetic (same rules as test_probe_flags / test_probe_only_merge): loopback
HTTP fake records every request body; HOME / ROUTING_DATA_DIR / ROUTER_STATE_DIR
under tmp_path; no real network, no repo writes.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
HEALTH_PROBE = os.path.join(REPO, "scripts", "provider_health_probe.py")
RANK_PROBE = os.path.join(REPO, "scripts", "router_probe_run.py")

sys.path.insert(0, os.path.join(REPO, "scripts"))
import provider_health_probe as php  # noqa: E402
import router_probe_run as rpr  # noqa: E402


# ---------------------------------------------------------------------------
# loopback fake provider: 200 pong for everything, but RECORDS every body so
# the tests can assert on the exact model id that went out on the wire.
# ---------------------------------------------------------------------------

class _RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        _SRV["requests"].append({"path": self.path, "model": body.get("model")})
        self._respond(200, {"choices": [{"message": {"content": "pong"}}]})

    def _respond(self, code, obj):
        blob = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


_SRV = {"httpd": None, "requests": [], "base": None}


@pytest.fixture()
def fake_provider():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _SRV["httpd"] = httpd
    _SRV["requests"] = []
    _SRV["base"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield _SRV
    httpd.shutdown()
    httpd.server_close()
    _SRV["httpd"] = None
    _SRV["requests"] = []


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Layer 1 — the pure transform, both probers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,model,expected", [
    ("clinepass", "glm-5.3", "cline-pass/glm-5.3"),
    ("cline-pass", "glm-5.3", "cline-pass/glm-5.3"),   # both registry spellings
    ("clinepass", "mimo-v2.5", "cline-pass/mimo-v2.5"),
    # already wire form: idempotent, never double-prefixed
    ("clinepass", "cline-pass/glm-5.3", "cline-pass/glm-5.3"),
    # a slash id is already modelType/model shaped: the live probe_providers
    # default_model AND the vendor-prefixed probe_fixes alternates pass through
    ("clinepass", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    # other providers: bare stays bare, slash ids untouched
    ("zai-glm", "glm-5.3", "glm-5.3"),
    ("deepseek", "deepseek-v4-flash", "deepseek-v4-flash"),
    ("openrouter", "deepseek/deepseek-v4", "deepseek/deepseek-v4"),
    ("clinepass", "", ""),
])
@pytest.mark.parametrize("mod", [php, rpr],
                         ids=["provider_health_probe", "router_probe_run"])
def test_wire_model_id_table(mod, provider, model, expected):
    assert mod.wire_model_id(provider, model) == expected


# ---------------------------------------------------------------------------
# Layer 2 — the health prober actually SENDS the wire id (and only clinepass)
# ---------------------------------------------------------------------------

def test_health_probe_sends_wire_id_for_clinepass_bare_for_others(tmp_path, fake_provider):
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "probe_providers.jsonl", [
        {"id": "clinepass", "base_url": fake_provider["base"],
         "key_env": "TR233_CP_KEY", "default_model": "glm-5.3", "enabled": True},
        {"id": "ctrlprov", "base_url": fake_provider["base"],
         "key_env": "TR233_CP_KEY", "default_model": "m-ctrl", "enabled": True},
    ])
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text("TR233_CP_KEY=sk-tr233\n")
    out_file = tmp_path / "out" / "health-state.json"
    env = dict(os.environ,
               HOME=str(tmp_path),
               ROUTING_DATA_DIR=str(data_dir),
               ROUTER_STATE_DIR=str(tmp_path / "mr-state"),
               ROUTING_REGISTRY=str(tmp_path / "no-such-registry.json"))
    proc = subprocess.run(
        [PY, HEALTH_PROBE, "--only", "clinepass,ctrlprov", "--output", str(out_file)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:400]

    sent = [r["model"] for r in fake_provider["requests"]]
    assert "cline-pass/glm-5.3" in sent, f"wire id never sent, got: {sent}"
    assert "glm-5.3" not in sent, f"bare registry id leaked to the wire: {sent}"
    assert "m-ctrl" in sent, f"control provider must keep its bare id: {sent}"

    # state keeps the BARE registry key (gate/spawn vocabulary) but records
    # what was actually sent via probed_as
    state = json.load(open(out_file))
    lane = state["providers"]["clinepass"]["models"]["glm-5.3"]
    assert lane["status"] == "OK", lane
    assert lane["probed_as"] == "cline-pass/glm-5.3", lane
    ctrl = state["providers"]["ctrlprov"]["models"]["m-ctrl"]
    assert ctrl["status"] == "OK", ctrl
    assert ctrl.get("probed_as") in (None, "m-ctrl"), ctrl


# ---------------------------------------------------------------------------
# Layer 3 — the ranking prober sends the wire id, records the bare id
# ---------------------------------------------------------------------------

def test_rank_probe_sends_wire_id_and_records_bare(tmp_path, fake_provider):
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "models.jsonl", [{
        "provider": "clinepass", "model": "glm-5.3", "normalized_price": 0.9667,
        "plan_tier": 0, "disabled": False, "archive": False, "valid_to": None,
    }])
    _write_jsonl(data_dir / "model_tier.jsonl", [])  # no tiers -> lane is targeted
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text("TR233_CP_KEY=sk-tr233\n")
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "providers:\n"
        "  clinepass:\n"
        f"    base_url: {fake_provider['base']}\n"
        "    api_key_env: TR233_CP_KEY\n")
    out_dir = tmp_path / "bench"
    out_dir.mkdir(parents=True)  # flush() writes straight into --out-dir
    env = dict(os.environ, HOME=str(tmp_path), ROUTING_DATA_DIR=str(data_dir))
    proc = subprocess.run(
        [PY, RANK_PROBE, "--providers", "clinepass", "--out-dir", str(out_dir),
         "--workers", "1"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[:400]

    sent = [r["model"] for r in fake_provider["requests"]]
    assert sent, "ranking prober never reached the fake provider"
    assert all(m == "cline-pass/glm-5.3" for m in sent), (
        f"every battery call must carry the wire id, got: {sorted(set(sent))}")

    # results keep the BARE registry id — router_probe_ingest maps rows back
    # to registry lanes by (provider, model)
    results_path = next(out_dir.glob("results_lanes_*_v5.json"))
    rows = json.load(open(results_path))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["model"] == "glm-5.3", row
    assert "error" not in row, row

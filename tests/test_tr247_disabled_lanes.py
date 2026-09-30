"""TR-247 — dark-lane attribution: enabled=false rows must carry their reason.

Three lanes went dark for three different reasons (evidence 2026-09-30):
  - crof: vendor shutdown — POST /v1/chat/completions 405 "This is a static
    site", GET /v1/models 302 -> nahcrof.com; quota research archived the
    shutdown notice 2026-09-27.
  - kimi (moonshot.cn): HTTP 401 Invalid Authentication — dead/wrong key; a
    401 is auth/endpoint misconfig, never a provider outage.
  - ollama-cloud: probe id deepseek-v4-flash:0731 retired by the provider at
    2026-09-25 (live HTTP 410); deepseek-v4.1-flash answers HTTP 200.

The probe registry used to silently drop disabled rows in every consumer, so
the report could only show a bare DOWN. Now the rows stay readable: the probe
renders DISABLED with the row's reason (and never alerts a transition into
it), the probefix sync no longer re-appends a disabled row as a fresh enabled
one, and router_probe_run resolve() refuses the lane with the reason.

Hermetic: tmp HOME/data/state dirs, no network, no repo writes.
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable
)
PROBE = os.path.join(REPO, "scripts", "provider_health_probe.py")
PROBEFIX = os.path.join(REPO, "scripts", "router_probefix.py")
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _write(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fixture(tmp_path, provider_rows):
    data = tmp_path / "data" / "tables"
    data.mkdir(parents=True)
    (tmp_path / ".hermes").mkdir()
    _write(data / "probe_providers.jsonl", provider_rows)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "ROUTING_DATA_DIR": str(data),
            "ROUTER_STATE_DIR": str(tmp_path / "mr-state"),
            "ROUTING_REGISTRY": str(tmp_path / "no-such-registry.json"),
        }
    )
    return env, data


def _seed_state(state_dir, entries):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "health-state.json"), "w") as f:
        json.dump(
            {
                "updated": "2026-09-29T00:00:00+00:00",
                "probe_version": 3,
                "providers": entries,
            },
            f,
        )


DEAD_KEY_ROW = {
    "id": "deadvendor",
    "base_url": "https://dead.example/v1",
    "key_env": "DEADVENDOR_KEY",
    "default_model": "m",
    "enabled": False,
    "note": "HTTP 401 Invalid Authentication — key dead; re-enable after key rotation",
}


def test_load_providers_returns_disabled_map_with_reason():
    import provider_health_probe as php

    provs, disabled = php.load_providers()
    live = [
        json.loads(line)
        for line in open(os.path.join(REPO, "data", "tables", "probe_providers.jsonl"))
        if line.strip()
    ]
    assert any(r.get("enabled", True) and r["id"] in provs for r in live), (
        "enabled rows must still load"
    )
    # the shipped data file carries disabled rows (crof/kimi since TR-247) and
    # they must come back with their reason, not vanish
    assert "crof" in disabled or any(r.get("enabled") is False for r in live), (
        "disabled rows dropped silently"
    )


def test_probe_reports_disabled_lane_with_reason_not_down(tmp_path):
    env, data = _fixture(tmp_path, [DEAD_KEY_ROW])
    out = tmp_path / "state.json"
    proc = subprocess.run(
        [PY, PROBE, "--output", str(out)],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    provs = json.load(open(out))["providers"]
    entry = provs["deadvendor"]
    assert entry["status"] == "DISABLED", (
        f"deliberate disable rendered as {entry['status']}"
    )
    assert "401 Invalid Authentication" in (entry.get("error") or ""), (
        "the row's reason must ride the state entry"
    )
    assert "disabled: HTTP 401 Invalid Authentication" in proc.stdout, (
        "report must surface the reason"
    )
    assert "DOWN" not in proc.stdout, "a 401 disable must never read as an outage"


def test_probe_disabled_lane_does_not_fire_down_transition_alert(tmp_path):
    env, data = _fixture(tmp_path, [DEAD_KEY_ROW])
    state_dir = str(tmp_path / "mr-state")
    _seed_state(
        state_dir,
        {
            "deadvendor": {
                "status": "DOWN",
                "model": "m",
                "latency_ms": None,
                "error": "HTTP 401: Invalid Authentication",
                "models": {},
                "ts": "x",
            }
        },
    )
    out = tmp_path / "state.json"
    proc = subprocess.run(
        [PY, PROBE, "--output", str(out)],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    assert "⚠️" not in proc.stdout, (
        "a deliberate disable is not a transition alert (would page as an outage)"
    )


def test_probefix_sync_does_not_readd_disabled_rows(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    rows_before = [
        {
            "id": "crof",
            "base_url": "https://crof.ai/v1",
            "key_env": "CROF_API_KEY",
            "default_model": "deepseek-v4-flash",
            "enabled": False,
            "note": "vendor shutdown (302 -> nahcrof.com)",
        }
    ]
    _write(data / "probe_providers.jsonl", rows_before)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "custom_providers:\n"
        "  - name: crof\n"
        "    api_key_env: CROF_API_KEY\n"
        "    base_url: https://crof.ai/v1\n"
    )
    env = dict(os.environ)
    env.update({"ROUTING_DATA_DIR": str(data), "ROUTER_HERMES_CONFIG": str(cfg)})
    proc = subprocess.run(
        [PY, PROBEFIX, "--sync-providers"],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    out = open(data / "probe_providers.jsonl").read()
    lines = [s for s in out.splitlines() if s.strip()]
    assert len(lines) == 1, "disabled row was re-appended as a fresh enabled one"
    row = json.loads(lines[0])
    assert row.get("enabled") is False, "sync resurrected a disabled row"


def test_router_probe_run_resolve_refuses_disabled_lane_with_reason():
    import router_probe_run as rpr

    probe_data = (
        {"liveprov": ("http://127.0.0.1:9", "K", {})},
        {"deadvendor": "vendor shutdown (TR-247)"},
    )
    base, key, reason = rpr.resolve("deadvendor", {}, {}, probe_data)
    assert base is None, "disabled lane must not resolve a base_url"
    assert "vendor shutdown" in reason
    base, key, extra = rpr.resolve("liveprov", {}, {"K": "sk-x"}, probe_data)
    assert base == "http://127.0.0.1:9", "enabled lanes must still resolve"


def test_repo_rows_pin_tr247_verdicts():
    """The shipped data rows carry the three TR-247 verdicts."""
    rows = [
        json.loads(line)
        for line in open(os.path.join(REPO, "data", "tables", "probe_providers.jsonl"))
        if line.strip()
    ]
    by_id = {r.get("id"): r for r in rows}
    assert by_id["crof"].get("enabled") is False
    assert "vendor shutdown" in (by_id["crof"].get("note") or "")
    assert "nahcrof.com" in (by_id["crof"].get("note") or "")
    assert by_id["kimi"].get("enabled") is False
    assert "401" in (by_id["kimi"].get("note") or "")
    assert "kimi-for-coding" in by_id, "kimi disable must not touch the coding lane"
    assert by_id["kimi-for-coding"].get("enabled", True) is not False
    assert by_id["ollama-cloud"].get("default_model") != "deepseek-v4-flash:0731", (
        "probe default_model must not be the id retired 2026-09-25 (live HTTP 410)"
    )
    assert by_id["ollama-cloud"].get("default_model") == "deepseek-v4.1-flash", (
        "registry replaced_by target is the live successor (HTTP 200, 2026-09-30)"
    )


def test_router_status_counts_disabled_separately_from_down(tmp_path):
    """router_status health_section: DISABLED is its own bucket — never 'down'
    (an outage count) and never dropped into the unknown union."""
    import router_status as rs

    mr = tmp_path / "model-router"
    mr.mkdir()
    (mr / "health-state.json").write_text(
        json.dumps(
            {
                "updated": "2026-09-30T00:00:00+00:00",
                "probe_version": 3,
                "providers": {
                    "deadvendor": {
                        "status": "DISABLED",
                        "model": None,
                        "models": {},
                        "error": "HTTP 401 Invalid Authentication — key dead",
                    },
                    "alive": {"status": "OK", "models": {}},
                    "broken": {"status": "DOWN", "models": {}},
                },
            }
        )
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rs, "MR", str(mr))
    try:
        doc = rs.health_section()
    finally:
        monkeypatch.undo()
    assert doc["disabled"] == ["deadvendor"]
    assert doc["down"] == ["broken"]
    assert doc["ok"] == 1


def test_spawn_exclusion_codes_bucket_health_disabled():
    """router_spawn: 'health DISABLED (...)' maps to a dedicated reason code —
    a deliberate disable must be countable, never 'unknown'."""
    import router_spawn as rsp

    assert "health-disabled" in rsp.EXCLUSION_REASON_CODES
    codes = rsp.exclusion_codes(
        ["health DISABLED (HTTP 401 Invalid Authentication — key dead)"]
    )
    assert codes == ["health-disabled"]


def test_router_health_model_status_renders_provider_disabled(tmp_path, monkeypatch):
    """router_health model_status: a lane under a provider-DISABLED probe entry
    renders 'provider-disabled', not 'unprobed'."""
    import router_health as rh

    data = tmp_path / "tables"
    data.mkdir()
    _write(
        data / "models.jsonl",
        [
            {
                "provider": "deadvendor",
                "model": "m",
                "disabled": None,
                "normalized_price": 1.0,
            }
        ],
    )
    _write(data / "providers.jsonl", [{"id": "deadvendor", "status": "active"}])
    mr = tmp_path / "model-router"
    mr.mkdir()
    with open(mr / "health-state.json", "w") as f:
        json.dump(
            {
                "updated": "2026-09-30T00:00:00+00:00",
                "probe_version": 3,
                "providers": {
                    "deadvendor": {
                        "status": "DISABLED",
                        "model": None,
                        "models": {},
                        "error": "vendor shutdown",
                    }
                },
            },
            f,
        )
    monkeypatch.setattr(rh, "MR_DIR", mr)
    doc = rh.model_status(provider="deadvendor", data_dir=str(data))
    assert doc["lanes"], "lane rows missing"
    assert doc["lanes"][0]["status"] == "provider-disabled", doc["lanes"][0]

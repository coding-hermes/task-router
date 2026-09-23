"""TR-087: control-plane health plane tests.

Covers the /health + / + /model_status surfaces end-to-end through a real
server process, plus the lookup contract (query, not report), plus the
TR-REVIEW-001 blocks (registry age, router_validate gate verdict, commit).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_health  # noqa: E402
from test_server import PY, server_env, _server, _request  # noqa: E402,F401

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


# ---------------------------------------------------------------------------
# TR-REVIEW-001 — /health must report registry age (mtime), the
# router_validate gate verdict, and the running commit
# ---------------------------------------------------------------------------

_MODEL_ROW = {
    "provider": "fakeprov", "model": "fake-model", "normalized_price": 1.0,
    "plan_tier": 1, "token_factor": 1.0, "data_class": "zdr",
    "disabled": False, "archive": False, "valid_to": None,
}


def _touch(path, age_s=0.0):
    """Stamp `path` `age_s` seconds into the past (0 = now)."""
    ts = time.time() - age_s
    os.utime(path, (ts, ts))


@pytest.fixture
def gate_env(tmp_path):
    """A server env whose validate gate is genuinely GREEN.

    Deliberately NOT the shared `server_env`: that fixture points
    ROUTING_REGISTRY at a file that does not exist, so its gate verdict is
    invalid by construction and could not tell a working gate from a stub.
    Mirrors tests/test_validate.py::_valid_fixture's schema floor.
    """
    data_dir = tmp_path / "tables"
    data_dir.mkdir(parents=True)
    (data_dir / "task_profiles.jsonl").write_text(
        json.dumps({"id": "P0_TEST", "title": "fixture profile"}) + "\n")
    (data_dir / "task_profile_requirements.jsonl").write_text(
        json.dumps({"task_id": "P0_TEST", "category": "reasoning", "level": 3}) + "\n")
    (data_dir / "models.jsonl").write_text(json.dumps(_MODEL_ROW) + "\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "version": 3, "generated_at": "2026-09-01T00:00:00+00:00",
        "tables": {"models": [_MODEL_ROW]},
    }))
    _touch(reg)                      # registry at least as new as the tables
    return {"ROUTING_REGISTRY": str(reg), "ROUTING_DATA_DIR": str(data_dir),
            "ROUTER_STATE_DIR": str(state_dir)}


def test_health_survives_a_torn_models_line(tmp_path):
    """TR-REVIEW-001 regression: a malformed line in models.jsonl used to
    propagate out of health() and 500 the endpoint — on the exact corruption
    /health exists to report. Decoding is per-line now: the good rows still
    drive `newest_valid_from`, the bad line is COUNTED, and the block never
    raises."""
    data_dir = tmp_path / "torn"
    data_dir.mkdir()
    (data_dir / "models.jsonl").write_text(
        '{"valid_from": "2026-09-20"}\n'
        "not json at all\n"
        '{"valid_from": "2026-09-22"}\n'
    )
    payload = router_health.health(mode="read-only", data_dir=str(data_dir))
    assert payload["status"] == "ok"
    reg = payload["registry"]
    assert "error" not in reg, reg
    assert reg["models"] == 3           # count is the raw line count
    assert reg["unparsable_lines"] == 1
    assert reg["newest_valid_from"] == "2026-09-22"   # good rows still parsed


def test_health_reports_registry_age_and_gate_verdict(gate_env):
    """AC1: one probe carries the registry's mtime age, the gate verdict and
    the running commit — through the real HTTP surface, not just the module."""
    with _server(gate_env) as port:
        code, payload = _request(port, "/health")
        assert code == 200, payload

        # running commit SHA — a real revision, never the "unknown" sentinel
        assert payload["commit"] and payload["commit"] != "unknown", payload["commit"]
        assert len(payload["commit"]) >= 7

        # the gate verdict is the SAME check set `router validate` publishes
        gate = payload["gate"]
        assert gate["valid"] is True, gate
        assert gate["failed_checks"] == []
        assert "freshness" in gate["check_names"]
        assert gate["checks"] == len(gate["check_names"]) > 0

        # registry age derived from the FILE mtime, not from valid_from dates
        age = payload["registry_age"]
        assert age["exists"] is True
        assert age["path"] == gate_env["ROUTING_REGISTRY"]
        assert isinstance(age["age_s"], (int, float)) and age["age_s"] >= 0
        assert age["mtime"]
        assert age["stale"] is False
        assert age["newest_table"] == "models.jsonl"

        # the CLI and the endpoint must not disagree about the same tree
        proc = subprocess.run(
            [PY, str(REPO / "scripts" / "router_validate.py"), "--json"],
            cwd=REPO, env={**os.environ, **gate_env},
            capture_output=True, text=True, timeout=60)
        cli = json.loads(proc.stdout)
        assert cli["valid"] == gate["valid"], (cli["issues"], gate["issues"])
        assert proc.returncode == 0


def test_health_fails_when_the_registry_is_deliberately_stale(gate_env):
    """AC2's predicate, proven on a serving instance: break freshness
    deliberately and the health payload turns red on BOTH surfaces.

    The probe a canary needs is `registry_age.stale OR gate.valid is false`.
    The control half runs the SAME fixture untouched FIRST, so a red below
    cannot be the fixture being broken from the start."""
    # --- control: untouched fixture is green on both surfaces ----------------
    with _server(gate_env) as port:
        _, healthy = _request(port, "/health")
    assert healthy["registry_age"]["stale"] is False, healthy["registry_age"]
    assert healthy["gate"]["valid"] is True, healthy["gate"]

    # --- deliberately stale: registry older than the newest table -----------
    reg = Path(gate_env["ROUTING_REGISTRY"])
    data_dir = Path(gate_env["ROUTING_DATA_DIR"])
    _touch(reg, age_s=3600)                          # registry an hour old
    models = data_dir / "models.jsonl"
    models.write_text(json.dumps(_MODEL_ROW) + "\n"
                      + json.dumps({**_MODEL_ROW, "model": "m2"}) + "\n")
    _touch(models)                                   # tables rewritten now

    with _server(gate_env) as port:
        code, payload = _request(port, "/health")
        assert code == 200, payload
    age, gate = payload["registry_age"], payload["gate"]
    # the exact predicate a canary must implement
    assert age["stale"] is True, age
    assert age["lag_s"] > 1
    assert gate["valid"] is False, gate
    assert "freshness" in gate["failed_checks"], gate["failed_checks"]
    assert any("stale registry" in issue for issue in gate["issues"]), gate["issues"]
    # the rest of the payload still answers — a red gate is not a dead server
    assert payload["status"] == "ok"
    assert payload["commit"] != "unknown"


def test_registry_age_honours_the_freshness_content_tiebreak(gate_env):
    """A big mtime lag with byte-identical tables is NOT staleness.

    This is the trap that made the first implementation of this block lie: the
    validator's freshness predicate carries a CONTENT tiebreak (seed writes
    registry.json before syncing tables, so a correct checkout can lag by
    thousands of seconds), and a re-derivation from `lag_s` alone reported
    `stale: true` on a tree the gate called valid — a canary built on it would
    have cried wolf on every lagging-but-correct host. Measured on the live
    tree 2026-09-23: 37855s lag, content_match true, gate valid.

    Driven through the serving process, because that is the only surface where
    env and path resolution are the real ones (a single in-process import
    freezes the validator's module constants at first import).
    """
    reg = Path(gate_env["ROUTING_REGISTRY"])
    data_dir = Path(gate_env["ROUTING_DATA_DIR"])
    _touch(reg, age_s=20000)                         # huge mtime lag ...
    _touch(data_dir / "models.jsonl")                # ... tables newest
    assert (data_dir / "models.jsonl").stat().st_mtime - reg.stat().st_mtime > 1000

    with _server(gate_env) as port:
        code, payload = _request(port, "/health")
        assert code == 200, payload
    age, gate = payload["registry_age"], payload["gate"]
    assert age["lag_s"] > 1000, age
    # content IDENTICAL (registry table == the only models row) -> not stale
    assert age["content_match"] is True, age
    assert age["stale"] is False, age
    assert gate["valid"] is True, gate


def test_health_gate_never_reports_invalid_when_the_checks_cannot_run(
        gate_env, monkeypatch):
    """`valid: false` means "the gate says the data is bad". A gate that could
    not RUN must say so as an error, not impersonate a data verdict — a health
    reader that conflates the two chases the wrong thing."""
    def _boom():
        raise RuntimeError("validator exploded")
    monkeypatch.setattr(router_health.router_validate, "run_checks_dict", _boom)
    gate = router_health.validate_gate()
    assert gate["valid"] is None
    assert "validator exploded" in gate["error"]
    # and the endpoint still answers 200 with that block degraded
    with _server(gate_env) as port:
        code, payload = _request(port, "/health")
    assert code == 200 and payload["status"] == "ok"


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
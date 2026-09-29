"""TR-235: a missing router registry must be LOUD, not silent no-hops.

Origin: the 2026-09-28 incident. The serving instance had NO registry.json at
all, so gate.valid was false and every proxied request died as
route_outcome=no-hops with no chain/cost/session — and nothing alerted,
because /health's 'stale' flag only compares the registry's AGE against the
serving commit: an ABSENT registry read as silence.

Contract pinned here:
  * /health (and /model_status) carry `registry_state` in {ok, stale, missing};
    an absent registry is 'missing', never folded into 'stale' or silence.
  * a no-hops row caused by the missing registry says failure_reason=
    'registry-missing' (row + envelope) and the 503 body names the cause.
  * the server logs a loud REGISTRY MISSING line at boot.
  * plain gating no-hops stay 'no-hops' (control) — attribution, not relabeling.
  * fail-open is sacred: nothing here blocks or crashes; every arm answers 200
    (health) or the usual 503 (proxy), just explainably.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import proxy_e2e as pe  # noqa: E402
import router_health  # noqa: E402
import router_spawn  # noqa: E402
import router_server as rsrv  # noqa: E402
from test_server import (  # noqa: E402
    PY,
    REPO as SRV_REPO,
    SERVER,
    _free_port,
    _request,
    _server,
)

COMMITTED_TABLES = str(REPO / "data" / "tables")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_MODEL_ROW = {
    "provider": "fakeprov",
    "model": "fake-model",
    "normalized_price": 1.0,
    "plan_tier": 1,
    "token_factor": 1.0,
    "data_class": "zdr",
    "disabled": False,
    "archive": False,
    "valid_to": None,
}


def _touch(path, age_s=0.0):
    """Stamp `path` `age_s` seconds into the past (0 = now)."""
    ts = time.time() - age_s
    os.utime(path, (ts, ts))


def _fresh_registry_fixture(tmp_path):
    """A registry.json + tables pair whose freshness predicate is GREEN."""
    data_dir = tmp_path / "tables"
    data_dir.mkdir()
    (data_dir / "models.jsonl").write_text(json.dumps(_MODEL_ROW) + "\n")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 3,
                "generated_at": "2026-09-01T00:00:00+00:00",
                "tables": {"models": [_MODEL_ROW]},
            }
        )
    )
    _touch(reg)  # registry at least as new as the tables
    return reg, data_dir


@contextlib.contextmanager
def _server_stderr(env):
    """Like test_server._server, but hands back the process so the test can
    read the boot STDERR (the loud REGISTRY MISSING line is the point)."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            PY,
            str(SERVER),
            "--mode",
            "read-only",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(SRV_REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"server exited {proc.returncode}: {out} {err}")
            try:
                status, _ = _request(port, "/status")
                if status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("server did not become ready")
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def server_env_fx(tmp_path):
    """The TR-235 serving env, byte-compatible with test_server.server_env:
    hermetic tables + state, and ROUTING_REGISTRY pointed at a file that does
    NOT exist — the 2026-09-28 incident shape. Local copy because importing
    the shared fixture under a different name trips ruff F811 against the
    test functions' fixture parameters."""
    data_dir = tmp_path / "tables"
    shutil.copytree(REPO / "data" / "tables", data_dir)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    providers = [
        json.loads(line)["id"]
        for line in (data_dir / "providers.jsonl").read_text().splitlines()
        if line.strip()
    ]
    (state_dir / "quota-state.json").write_text(
        json.dumps({"providers": {p: {"status": "open"} for p in providers}})
    )
    return {
        **os.environ,
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTING_REGISTRY": str(tmp_path / "missing-registry.json"),
        "ROUTER_STATE_DIR": str(state_dir),
        "LEDGER_FILE": str(state_dir / "ledger.jsonl"),
        "TASK_ROUTER_HOME": str(tmp_path / "router-home"),
        "ROUTING_OUTCOMES_FILE": str(tmp_path / "outcomes.jsonl"),
        "ROUTING_AVERAGES_FILE": str(tmp_path / "outcomes-averages.jsonl"),
    }


# ---------------------------------------------------------------------------
# 1. /health and /model_status name the missing registry (module level)
# ---------------------------------------------------------------------------


def test_health_names_a_missing_registry_as_missing(monkeypatch):
    """The incident shape: registry.json ABSENT entirely. registry_state must
    be 'missing' — a distinct value, not stale-None silence."""
    monkeypatch.setenv("ROUTING_REGISTRY", "/nonexistent/tr-235/registry.json")
    monkeypatch.setenv("ROUTING_DATA_DIR", COMMITTED_TABLES)
    payload = router_health.health(mode="read-only", data_dir=COMMITTED_TABLES)
    assert payload["registry_state"] == "missing"
    age = payload["registry_age"]
    assert age["exists"] is False
    assert age["state"] == "missing"
    assert age["age_s"] is None and age["stale"] is None  # not a stale verdict
    # the gate is invalid BECAUSE the registry is missing, and /health says which check
    assert payload["gate"]["valid"] is False
    assert "registry.exists" in payload["gate"]["failed_checks"]


def test_registry_state_vocabulary_ok_stale_missing(monkeypatch, tmp_path):
    """The tri-state: fresh registry -> ok; old-vs-tables -> stale; absent ->
    missing. One helper, three honest answers."""
    reg, data_dir = _fresh_registry_fixture(tmp_path)
    monkeypatch.setenv("ROUTING_REGISTRY", str(reg))
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data_dir))

    assert router_health.registry_age()["state"] == "ok"

    # stale: registry an hour older than a rewritten table
    reg.write_text(json.dumps({"version": 3, "tables": {"models": [_MODEL_ROW]}}))
    _touch(reg, age_s=7200)
    (data_dir / "models.jsonl").write_text(
        json.dumps(_MODEL_ROW) + "\n" + json.dumps({**_MODEL_ROW, "model": "m2"}) + "\n"
    )
    _touch(data_dir / "models.jsonl")
    stale = router_health.registry_age()
    assert stale["state"] == "stale" and stale["stale"] is True, stale

    # missing: the file is gone
    os.remove(reg)
    missing = router_health.registry_age()
    assert missing["exists"] is False
    assert missing["state"] == "missing"


def test_model_status_carries_the_registry_state(monkeypatch):
    """`/model_status if applicable` — it is: the same absence must be visible
    on the per-lane status surface, not only on /health."""
    monkeypatch.setenv("ROUTING_REGISTRY", "/nonexistent/tr-235/registry.json")
    monkeypatch.setenv("ROUTING_DATA_DIR", COMMITTED_TABLES)
    payload = router_health.model_status(data_dir=COMMITTED_TABLES)
    assert payload["registry_state"] == "missing"


# ---------------------------------------------------------------------------
# 2. the serving process says it: /health + /model_status over real HTTP
# ---------------------------------------------------------------------------


def test_health_endpoint_reports_the_missing_registry(server_env_fx):
    """The shared server_env fixture already points ROUTING_REGISTRY at a file
    that does not exist — the incident shape, served for real."""
    with _server(server_env_fx) as port:
        code, payload = _request(port, "/health")
        assert code == 200, payload
        assert payload["registry_state"] == "missing"
        assert payload["registry_age"]["exists"] is False
        assert payload["gate"]["valid"] is False
        assert "registry.exists" in payload["gate"]["failed_checks"]

        ms_code, ms = _request(port, "/model_status")
        assert ms_code == 200, ms
        assert ms["registry_state"] == "missing"


def test_boot_logs_loudly_when_the_registry_is_absent(server_env_fx):
    """Requirement 3: a LOUD log line at load time. The server still starts
    (fail-open is sacred) but it must SAY the registry is gone."""
    with _server_stderr(server_env_fx) as (port, proc):
        code, _ = _request(port, "/health")
        assert code == 200
    out, err = proc.communicate(timeout=10)
    assert "REGISTRY MISSING" in err, err[-2000:]
    assert server_env_fx["ROUTING_REGISTRY"] in err, err[-2000:]
    assert "router_seed" in err, err[-2000:]


# ---------------------------------------------------------------------------
# 3. the proxy row + envelope attribute the no-hops to the missing registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_router_state(tmp_path, monkeypatch):
    """Same isolation TR-182 installed: proxy failures must never land in the
    LIVE ~/.hermes/model-router/circuit-state.json or outcomes ledger."""
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "outcomes.jsonl"))


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("")
    monkeypatch.setattr("router_outcomes.outcomes_path", lambda *a, **k: str(path))
    monkeypatch.setattr(
        rsrv,
        "_proxy_requirements",
        lambda b, h, p: (
            "declared",
            {
                "profile_id": "P1_CODING",
                "matrix": None,
                "complexity_sig": None,
                "problems": [],
            },
        ),
    )
    return path


def _rows(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def _must_not_be_called(*a, **k):
    raise AssertionError("an empty chain must not reach any upstream")


# The two REAL spawn payload shapes that mean "registry.json is missing":
# (a) tables still resolved -> loader warning rides in `warnings` and the
#     data_home note names the unseeded registry (the live 2026-09-28 shape);
# (b) both stores broken -> the resolve dies with an error naming it.
_INCIDENT_RESOLVED = {
    "chain": [],
    "exclusions": [],
    "gate": "NO-CHAIN",
    "sort": "price",
    "source": "data/tables",
    "fallback_used": True,
    "warnings": ["registry.json missing — using committed data/tables fallback"],
    "data_home": {
        "registry": "/x/registry.json",
        "fallback": True,
        "bootstrap": True,
        "note": "registry came from the committed data/tables sample "
        "tables (no seeded registry.json) — run `router seed` "
        "for real state",
    },
}
_BOTH_BROKEN_RESOLVED = {
    "chain": [],
    "exclusions": [],
    "error": "no chain — no eligible model and no fallback lane could serve "
    "(registry.json missing AND data/tables unreadable/empty)",
}


def test_no_hops_from_a_missing_registry_says_registry_missing(ledger, monkeypatch):
    """The incident row, attributed: route_outcome stays no-hops (the outcome
    vocabulary is untouched) but failure_reason names the CAUSE."""
    monkeypatch.setattr(
        rsrv, "_proxy_chain", lambda reqs, **k: dict(_INCIDENT_RESOLVED)
    )
    status, payload = rsrv.proxy_chat(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "tick the fleet"}]},
        {"x-router-session": "sess-tr235"},
        upstream=_must_not_be_called,
    )
    assert status == 503
    rows = [r for r in _rows(ledger) if r.get("route_outcome") == "no-hops"]
    assert rows, f"no no-hops row was written: {_rows(ledger)}"
    row = rows[0]
    assert row["failure_reason"] == "registry-missing", row
    assert row["hops_attempted"] == 0
    # the 503 body names the cause and the fix, and carries the machine flag
    assert "registry.json missing" in payload["error"], payload["error"]
    assert "router_seed" in payload["error"], payload["error"]
    assert payload["registry_missing"] is True
    # the envelope agrees with the row (same request, same answer)
    env = payload["_router"]
    assert env["served_by_reason"] == "registry-missing", env["served_by_reason"]
    assert env["terminal_reason"] == "no-hops"


def test_resolve_error_naming_the_missing_registry_is_attributed(ledger, monkeypatch):
    """Shape (b): BOTH stores broken — the resolve's own error doc names the
    missing registry; the row must still say registry-missing, not no-hops."""
    monkeypatch.setattr(
        rsrv, "_proxy_chain", lambda reqs, **k: dict(_BOTH_BROKEN_RESOLVED)
    )
    status, payload = rsrv.proxy_chat(
        "/v1/chat/completions", {"messages": []}, {}, upstream=_must_not_be_called
    )
    assert status == 503
    rows = [r for r in _rows(ledger) if r.get("route_outcome") == "no-hops"]
    assert rows and rows[0]["failure_reason"] == "registry-missing", _rows(ledger)
    assert payload["registry_missing"] is True


def test_the_detector_reads_real_spawn_payloads(monkeypatch, tmp_path):
    """Bridge to the live incident: a REAL router_spawn.resolve on this
    checkout (no seeded registry.json, committed tables present) must carry
    the loader warning the detector greps — and the detector must fire on it.
    """
    monkeypatch.setattr(router_spawn, "REGISTRY", str(tmp_path / "no-registry.json"))
    monkeypatch.setattr(router_spawn, "DATA_DIR", COMMITTED_TABLES)
    mr = tmp_path / "mr"
    mr.mkdir()
    providers = [
        json.loads(line)["id"]
        for line in open(os.path.join(COMMITTED_TABLES, "providers.jsonl"))
        if line.strip()
    ]
    (mr / "quota-state.json").write_text(
        json.dumps({"providers": {p: {"status": "open"} for p in providers}})
    )
    monkeypatch.setattr(router_spawn, "MR", str(mr))
    r = router_spawn.resolve(project="coding-hermes-scheduler")
    assert "error" not in r, r
    assert any("registry.json missing" in w for w in (r.get("warnings") or [])), r.get(
        "warnings"
    )
    assert rsrv._resolve_registry_missing(r) is True


def test_the_detector_rejects_healthy_and_corrupt_registries():
    """Attribution, not relabeling: a seeded registry, a corrupt one, and a
    stub payload without provenance are NOT registry-missing."""
    assert (
        rsrv._resolve_registry_missing(
            {
                "chain": [],
                "source": "registry.json",
                "fallback_used": False,
                "warnings": [],
                "data_home": {"fallback": False},
            }
        )
        is False
    )
    assert (
        rsrv._resolve_registry_missing(
            {
                "chain": [],
                "warnings": [
                    "registry.json present but not an object "
                    "(corrupt) — using committed data/tables fallback"
                ],
                "data_home": {"fallback": True},
            }
        )
        is False
    )
    assert rsrv._resolve_registry_missing({}) is False
    assert rsrv._resolve_registry_missing(None) is False


def test_plain_gating_no_hops_stays_bare_no_hops(ledger, monkeypatch):
    """Control: exclusions from real gating keep failure_reason='no-hops'.
    The new label must never swallow the ordinary case."""
    monkeypatch.setattr(
        rsrv,
        "_proxy_chain",
        lambda reqs, **k: {
            "chain": [],
            "exclusions": [
                {
                    "hop": 1,
                    "provider": "xkiro",
                    "model": "glm-5.3-flash",
                    "why": ["circuit OPEN (provider-level, api_down)"],
                    "codes": ["circuit-open"],
                },
                {
                    "hop": 2,
                    "provider": "clinepass",
                    "model": "deepseek-v4-flash",
                    "why": ["quota GATED: blocked"],
                    "codes": ["quota-gated"],
                },
            ],
            "gate": {"quota": "blocked"},
            "sort": "price",
        },
    )
    status, payload = rsrv.proxy_chat(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "no-hops probe"}]},
        {"x-router-session": "sess-tr235-control"},
        upstream=_must_not_be_called,
    )
    assert status == 503
    rows = [r for r in _rows(ledger) if r.get("route_outcome") == "no-hops"]
    assert rows and rows[0]["failure_reason"] == "no-hops", _rows(ledger)
    assert payload["registry_missing"] is False
    assert payload["error"] == "no open hop for this request"


# ---------------------------------------------------------------------------
# 4. the E2E classifier must keep calling registry-missing rows reasoned
# ---------------------------------------------------------------------------


def test_e2e_classifier_treats_registry_missing_as_reasoned():
    """proxy_e2e.classify_row is the incident battery: a registry-missing row
    (no-hops WITH a reason) must stay interpretable, never ambiguous."""
    row = {
        "source_system": "router-proxy",
        "session_id": "router-proxy:s-tr235",
        "provider": "none",
        "model": "none",
        "success": False,
        "route_outcome": "no-hops",
        "failure_reason": "registry-missing",
        "hops_attempted": 0,
        "served_by_hop": None,
        "chain": [],
        "chain_length": 0,
        "cost_usd": None,
    }
    verdict = pe.classify_row(row)
    assert verdict == {"verdict": "no-hops-with-reason", "ok": True}, verdict

"""TR-148 — the dispatch hop must send the id the upstream actually serves.

The registry keeps BARE ids (TR-233: dedup/ranking/records vocabulary), but
two dispatch surfaces interpolated that bare registry id into upstream
requests:

  1. router_server.py's proxy hop loop set `fwd['model'] = model` — the bare
     id. For a clinepass hop the Hermes gateway serves 'cline-pass/<bare>'
     (config.yaml clinepass models list) and api.cline.bot itself 400s every
     bare id ('invalid model format. Expected format: modelType/model', live
     control TR-233 2026-09-28) — so a resolved clinepass hop either 400s
     upstream or falls through to a same-named lane of another provider.

  2. router_chain_run.py's Path A executor interpolated {model} from the
     chain hop the same way.

The fix: scripts/router_wire_ids.py — a DATA-FIRST transform (verified
per-lane fixes from data/tables/probe_fixes.jsonl, last row in file order
wins; provider default 'cline-pass/' from model_catalog.api_id, carried since
2026-08-27) applied ONLY to the outgoing wire id at the hop boundary.
Attempts, outcome rows, breaker keys and envelopes keep the bare id
(dedup/join vocabulary); the envelope discloses the wire form per attempt via
'wire_id' / served_by.wire_id.

Hermetic: upstream call injected (no network); ROUTING_DATA_DIR points at
tmp_path; outcome-row writes and breaker evidence are patched out — no repo
or store writes.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_wire_ids as rwi  # noqa: E402
import router_server as rsrv  # noqa: E402


def _write_fixes(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture()
def clean_fixes(tmp_path, monkeypatch):
    """ROUTING_DATA_DIR under tmp_path with an EMPTY probe_fixes.jsonl —
    tests opt INTO specific fixes by writing rows; no ambient host data."""
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    _write_fixes(data_dir / "probe_fixes.jsonl", [])
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data_dir))
    rwi.reset_cache()
    yield data_dir
    rwi.reset_cache()


# ---------------------------------------------------------------------------
# Layer 1 — the pure transform
# ---------------------------------------------------------------------------

def test_default_prefix_only_for_declared_providers(clean_fixes):
    assert rwi.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"
    assert rwi.wire_model_id("cline-pass", "glm-5.3") == "cline-pass/glm-5.3"
    # other providers: bare stays bare (their ids are already wire-shaped)
    assert rwi.wire_model_id("zai-glm", "glm-5.3-flash") == "glm-5.3-flash"
    assert rwi.wire_model_id("deepseek", "deepseek-v4-flash") == "deepseek-v4-flash"


def test_verified_fix_overrides_the_provider_default(clean_fixes):
    # clinepass :free lanes answer ONLY at the vendor-org id (probe battery
    # 2026-09-09: 18/18 probed 200; bare and cline-pass forms 400/404)
    _write_fixes(clean_fixes / "probe_fixes.jsonl", [
        {"provider": "clinepass", "model": "gemma-4-31b-it:free",
         "fix_to": "google/gemma-4-31b-it:free"},
    ])
    rwi.reset_cache()
    assert rwi.wire_model_id("clinepass", "gemma-4-31b-it:free") == "google/gemma-4-31b-it:free"
    # a lane WITHOUT a verified fix keeps the provider default
    assert rwi.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"


def test_last_fix_row_wins_in_file_order(clean_fixes):
    # deepseek-v4-flash moved vendor -> cline-pass on 09-05 and back on 09-27:
    # the append-only ledger's LAST row is the live verdict — file order,
    # never re-sorted by ts (legacy rows carry date-only stamps).
    _write_fixes(clean_fixes / "probe_fixes.jsonl", [
        {"provider": "clinepass", "model": "deepseek-v4-flash",
         "fix_to": "cline-pass/deepseek-v4-flash"},
        {"provider": "clinepass", "model": "deepseek-v4-flash",
         "fix_to": "deepseek/deepseek-v4-flash"},
    ])
    rwi.reset_cache()
    assert rwi.wire_model_id("clinepass", "deepseek-v4-flash") == "deepseek/deepseek-v4-flash"


def test_slash_ids_pass_through_untouched(clean_fixes):
    # an already wire-shaped id never gets transformed (no double prefix)
    assert rwi.wire_model_id("clinepass", "cline-pass/glm-5.3") == "cline-pass/glm-5.3"
    assert rwi.wire_model_id("clinepass", "deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
    assert rwi.wire_model_id("openrouter", "deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_missing_table_and_empty_values_never_raise(clean_fixes):
    os.remove(clean_fixes / "probe_fixes.jsonl")
    rwi.reset_cache()
    assert rwi.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"
    assert rwi.wire_model_id("clinepass", "") == ""
    assert rwi.wire_model_id("zai-glm", "") == ""


def test_missing_data_dir_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTING_DATA_DIR", str(tmp_path / "no-such-dir"))
    rwi.reset_cache()
    try:
        assert rwi.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"
        assert rwi.wire_model_id("zai-glm", "m") == "m"
    finally:
        rwi.reset_cache()


# ---------------------------------------------------------------------------
# Layer 2 — the proxy dispatch hop sends the wire id, records the bare id
# ---------------------------------------------------------------------------

class _RecordingUpstream:
    """Test double for the per-hop upstream call: records bodies, answers 200."""

    def __init__(self):
        self.models = []

    def __call__(self, path, body, headers):
        self.models.append(body.get("model"))
        return 200, {"choices": [{"message": {"content": "pong"}}]}


def _chain(*pairs):
    return {"chain": [{"hop": i + 1, "provider": p, "model": m, "usd_1m": 0.1 * (i + 1),
                       "outcomes": {"stats_fallback": "unconditioned"}}
                      for i, (p, m) in enumerate(pairs)],
            "sort": "price"}


@pytest.fixture()
def proxy_harness(monkeypatch):
    """Edit-mode app + the seams that keep the walk hermetic: chain injection,
    ledger capture (_proxy_record IS the server's outcome/ledger write — its
    calls are captured, never written to a live store)."""
    def make_app():
        records = []

        def _rec(provider, model, ok, *args, **kwargs):
            records.append({"provider": provider, "model": model, "ok": ok})

        monkeypatch.setattr(rsrv, "_proxy_record", _rec)
        monkeypatch.setattr(rsrv, "_proxy_chain", lambda reqs, **k: _chain(*make_app.pairs))
        monkeypatch.setattr(rsrv, "_proxy_requirements", lambda b, h, p: (
            "declared", {"profile_id": "P1_CODING", "matrix": None,
                         "complexity_sig": None, "problems": []}))
        return rsrv.RouterApplication(mode="edit", edit_key="tr148"), records

    make_app.pairs = (("p1", "m1"),)
    return make_app


def _post(app, body=None):
    return app.dispatch("POST", "/v1/chat/completions",
                        body=body if body is not None else {"messages": []},
                        headers={"x-api-key": "tr148"})


def test_proxy_hop_sends_wire_id_for_clinepass(clean_fixes, proxy_harness, monkeypatch):
    up = _RecordingUpstream()
    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", up)
    proxy_harness.pairs = (("clinepass", "glm-5.3"),)
    app, _rows = proxy_harness()
    status, payload = _post(app)
    assert status == 200, payload
    assert up.models == ["cline-pass/glm-5.3"], (
        f"the dispatch hop must send the wire id, got: {up.models}")


def test_proxy_hop_keeps_bare_id_for_other_providers(clean_fixes, proxy_harness, monkeypatch):
    up = _RecordingUpstream()
    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", up)
    proxy_harness.pairs = (("zai-glm", "glm-5.3-flash"),)
    app, _rows = proxy_harness()
    status, _payload = _post(app)
    assert status == 200
    assert up.models == ["glm-5.3-flash"]


def test_proxy_hop_sends_verified_free_lane_fix(clean_fixes, proxy_harness, monkeypatch):
    _write_fixes(clean_fixes / "probe_fixes.jsonl", [
        {"provider": "clinepass", "model": "gemma-4-31b-it:free",
         "fix_to": "google/gemma-4-31b-it:free"},
    ])
    rwi.reset_cache()
    up = _RecordingUpstream()
    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", up)
    proxy_harness.pairs = (("clinepass", "gemma-4-31b-it:free"),)
    app, _rows = proxy_harness()
    status, _payload = _post(app)
    assert status == 200
    assert up.models == ["google/gemma-4-31b-it:free"]


def test_proxy_envelope_reports_wire_form_per_attempt(clean_fixes, proxy_harness, monkeypatch):
    up = _RecordingUpstream()
    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", up)
    proxy_harness.pairs = (("clinepass", "glm-5.3"),)
    app, _rows = proxy_harness()
    status, payload = _post(app)
    assert status == 200
    attempt = payload["_router"]["ladder"][0]
    assert attempt["wire_id"] == "cline-pass/glm-5.3"
    assert attempt["model"] == "glm-5.3"  # registry vocabulary stays bare
    # served_by keeps the registry id: outcome rows and joins key on it
    assert payload["_router"]["served_by"]["model"] == "glm-5.3"
    assert payload["_router"]["served_by"]["wire_id"] == "cline-pass/glm-5.3"


def test_proxy_ladder_failure_discloses_wire_id(clean_fixes, proxy_harness, monkeypatch):
    def failing_upstream(path, body, headers):
        return 500, {"error": "boom"}

    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", failing_upstream)
    proxy_harness.pairs = (("clinepass", "glm-5.3"),)
    app, _records = proxy_harness()
    status, body = _post(app)
    # exhaustion surfaces the LAST hop's status (>=400 passes through, else 502)
    assert status in (500, 502), body
    # the envelope must expose what each dead hop actually SENT (the wire id)
    assert body["_router"]["ladder"][0]["wire_id"] == "cline-pass/glm-5.3"


def test_proxy_outcome_row_keeps_bare_registry_id(clean_fixes, proxy_harness, monkeypatch):
    up = _RecordingUpstream()
    monkeypatch.setattr(rsrv, "_UPSTREAM_CALL", up)
    proxy_harness.pairs = (("clinepass", "glm-5.3"),)
    app, records = proxy_harness()
    status, _payload = _post(app)
    assert status == 200
    assert records, "ledger row never written"
    # the row's model — the (source_system, session_id, model) dedup key —
    # stays the BARE registry id; the wire form rides only in the request body
    assert records[0]["model"] == "glm-5.3"


# ---------------------------------------------------------------------------
# Layer 3 — the Path A executor interpolates the wire id into the command
# ---------------------------------------------------------------------------

def test_chain_run_sends_wire_id_to_the_child(tmp_path, clean_fixes, monkeypatch):
    """router_chain_run.py {model} -> the wire form for clinepass hops.

    Behavioral proof: the child script exits 0 ONLY when ROUTER_MODEL carries
    the wire id — so a success outcome is itself the evidence the wire form
    reached the child process. Registry vocabulary in the recorded attempt
    stays bare.
    """
    import router_chain_run as rcr

    script = tmp_path / "capture.py"
    script.write_text(
        "import os, sys\n"
        "sys.exit(0 if os.environ.get('ROUTER_MODEL') == 'cline-pass/glm-5.3' else 1)\n"
    )
    chain = [{"hop": 1, "provider": "clinepass", "model": "glm-5.3", "usd_1m": 0.1,
              "outcomes": {"stats_fallback": "unconditioned"}}]
    monkeypatch.setattr(rcr.rs, "resolve", lambda **k: {"chain": chain})
    monkeypatch.setattr(rcr, "_breaker", lambda *a, **k: None)
    rows = []
    monkeypatch.setattr(rcr.ro, "append_rows", lambda p, r: rows.extend(r))
    monkeypatch.setattr(sys, "argv", [
        "router_chain_run.py", "--profile", "P1_CODING",
        "--cmd", f"python3 {script} --model {{model}}",
        "--max-hops", "1", "--format", "json",
        "--session-id", "tr148-selftest",
    ])
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rcr.main()
    assert rc == 0, buf.getvalue()
    out = json.loads(buf.getvalue())
    assert out["success"] is True, out  # the child confirmed the WIRE id
    attempt = out["attempts"][0]
    assert attempt["model"] == "glm-5.3"   # recorded vocabulary stays bare
    assert attempt["wire_id"] == "cline-pass/glm-5.3"
    assert "cline-pass/glm-5.3" in attempt["command"]  # the interpolated command
    assert rows and rows[0]["model"] == "glm-5.3"      # outcome row stays bare


# ---------------------------------------------------------------------------
# Layer 4 — data sanity against the LIVE repo tables (read-only)
# ---------------------------------------------------------------------------

def test_host_tables_produce_the_expected_wire_forms(monkeypatch):
    """The three verified id classes resolve exactly as the probe batteries
    recorded them — against the real data/tables/probe_fixes.jsonl."""
    monkeypatch.delenv("ROUTING_DATA_DIR", raising=False)
    rwi.reset_cache()
    try:
        # in-plan lane -> cline-pass/<bare> (09-05 battery; TR-233 live control)
        assert rwi.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"
        assert rwi.wire_model_id("clinepass", "glm-5.2") == "cline-pass/glm-5.2"
        # :free lane -> vendor-org id (09-09 battery, 18/18 probed 200)
        assert rwi.wire_model_id("clinepass", "gemma-4-31b-it:free") == "google/gemma-4-31b-it:free"
        # drifted lane -> its latest verified verdict (09-27 battery)
        assert rwi.wire_model_id("clinepass", "deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
        # a lane with no row and no default (a zai lane) stays bare
        assert rwi.wire_model_id("zai-glm", "glm-5.3-flash") == "glm-5.3-flash"
    finally:
        rwi.reset_cache()

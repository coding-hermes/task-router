"""ACP-ROUTER-001 — the just-in-time execution decision contract.

Covers the route-receipt seam end to end (Python + CLI + HTTP + OpenAPI + MCP):

* the canonical receipt/digest is stable across reordered input object keys and
  process restart (injected clock);
* any change to a category requirement, the brief digest, a context reference
  digest, the candidate order, an exclusion, the source or the expiry
  invalidates verification;
* a receipt verifies before expiry and is refused at/after expiry (injected
  clock);
* no eligible candidate returns no head and NEVER a requirements-unmet
  degraded fallback;
* health/quota/circuit exclusions are retained exactly;
* required connector capabilities are echoed but never resolved — no harness,
  connector, tool or workspace is ever named;
* legacy `router spawn` output and fail-open behavior stay byte/schema
  compatible;
* HTTP/OpenAPI/CLI parity and strict unknown-field rejection;
* no state file or table changes because of a decision call.

Every resolve runs in a REAL subprocess against a hermetic plane (committed
data/tables + a temp gate state) — no mocks, no import-time env games.
"""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO = Path(__file__).resolve().parents[1]
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
SCRIPTS = REPO / "scripts"
DECISION = SCRIPTS / "router_decision.py"
SPAWN = SCRIPTS / "router_spawn.py"
SERVER = SCRIPTS / "router_server.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

#: the one injected decision clock used by the deterministic tests
NOW = "2026-10-17T10:00:00Z"

#: the pinned legacy spawn schema (the resolve document with ad-hoc
#: requirements — byte/schema compatibility for `router spawn` callers)
EXPECTED_SPAWN_KEYS = {
    "project", "profile", "resolved_version", "resolved_at", "lifecycle_counts",
    "resolved_as", "hint", "board_profile", "head", "chain", "exclusions",
    "gate_reasons", "degraded_fallback", "quota_ordering", "source",
    "fallback_used", "data_home", "bootstrap", "note", "gates_loaded",
    "quota_gates", "warnings", "gate", "settings", "sort", "sort_stats",
    "quota",   # added on main after this feature branched (spawn schema drift)
}

#: requirements no lane in the committed tables clears (tiers max out per
#: category, not across this intersection) — deterministic no-eligible case
IMPOSSIBLE_LEVELS = {"e2e_vision": 5, "multilingual": 5, "ui_frontend": 5,
                     "math": 5, "creative": 5}


# ---------------------------------------------------------------------------
# hermetic plane + helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def decision_env(tmp_path, monkeypatch):
    """Committed data/tables (registry-sample fallback source) + a temp gate
    state: every provider OPEN/healthy, no circuits, no ledger, no outcome
    stats — so ordering is the deterministic price order."""
    data = tmp_path / "tables"
    shutil.copytree(REPO / "data" / "tables", data)
    state = tmp_path / "state"
    state.mkdir()
    providers = [
        json.loads(line)["id"]
        for line in (data / "providers.jsonl").read_text().splitlines()
        if line.strip()
    ]
    (state / "quota-state.json").write_text(json.dumps(
        {"updated": "test", "providers": {p: {"status": "open"} for p in providers}}))
    (state / "health-state.json").write_text(json.dumps(
        {"providers": {p: {"status": "UP", "ts": "2026-10-17T09:00:00Z"}
                       for p in providers}}))
    (state / "circuit-state.json").write_text(json.dumps({"version": 1, "pairs": {}}))
    env = dict(os.environ)
    env.update({
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_REGISTRY": str(tmp_path / "missing-registry.json"),
        "ROUTER_STATE_DIR": str(state),
        "ROUTER_CACHE": "0",
        "ROUTER_SPAWN_QUIET": "1",
        "TASK_ROUTER_HOME": str(tmp_path / "router-home"),
        "ROUTING_OUTCOMES_FILE": str(tmp_path / "outcomes.jsonl"),
        "ROUTING_AVERAGES_FILE": str(tmp_path / "outcomes-averages.jsonl"),
    })
    env.pop("ROUTER_DECISION_NOW", None)
    # The hermetic suite pins a 2026 clock, so the dual-clock expiry check
    # (H1-F2) must be collapsed to the injected clock for THESE tests only —
    # exactly the documented test-only escape hatch. Verification without it
    # would (correctly) call a 2026 receipt expired by the host's 2026-10 wall
    # clock.
    monkeypatch.setenv("ROUTER_DECISION_TEST_CLOCK", "1")
    env["ROUTER_DECISION_TEST_CLOCK"] = "1"
    # Verification is server-bound (signing key + issuance ledger live in the
    # state plane), so an in-process `verify_receipt` in a test must resolve the
    # SAME state dir the issuing subprocess wrote to.
    # Via monkeypatch (not a bare assignment): `MR` is module-global resolved
    # state, and leaving it pointed at this test's tmp dir pollutes every later
    # test in the same process — measured as test_quota_gate failing only when
    # this file ran first.
    import router_decision as _rd
    monkeypatch.setattr(_rd._rs, "MR", str(state), raising=False)
    # H1-F4: key creation is an OPERATOR bootstrap step, never a side effect of
    # a request. The fixture plays the operator, exactly as
    # `router_decision.py --init-receipt-key` does in production; every test
    # that wants the refusal path deletes the file first.
    _rd.load_receipt_key(create=True)
    return env


#: The identity a receipt in this suite is issued FOR. Verification requires
#: one (H1-F1): an unbound verify is refused, never a silent ok.
EXPECT = {"parent_run_id": "run-0001", "step_id": "step-7"}


def base_request(**overrides):
    req = {
        "version": 1,
        "parent_run_id": "run-0001",
        "step_id": "step-7",
        "generation": 3,
        "brief_digest": "a" * 64,
        "context_refs": [
            {"name": "repo-readme", "digest": "b" * 64},
            {"name": "spec-anchor", "digest": "c" * 64},
        ],
        "required_levels": {"reasoning": 1, "code_gen": 1},
        "required_context_limit": 100000,
        "policy_labels": ["security-critical"],
        "required_connector_capabilities": ["acp", "ssh"],
        "budget_ceiling_usd": 2.5,
        "budget_remaining_usd": 1.25,
        "attempt_deadline": "2026-10-17T10:30:00Z",
        "requester": "hermes-foreman",
    }
    for key, value in overrides.items():
        if value is _DROP:
            req.pop(key, None)
        else:
            req[key] = value
    return req


class _Drop:
    pass


_DROP = _Drop()


def _workdir(env):
    return Path(env.get("TASK_ROUTER_HOME") or env.get("ROUTER_STATE_DIR")
                or Path.cwd())


def run_json(argv, env, timeout=180):
    proc = subprocess.run([PY, *argv], capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=timeout)
    doc = None
    if proc.stdout.strip():
        doc = json.loads(proc.stdout)
    return proc.returncode, doc, proc


def decide_cli(env, request, now=NOW):
    workdir = _workdir(env)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "request.json"
    path.write_text(json.dumps(request))
    argv = [str(DECISION), "--request", str(path)]
    if now is not None:            # None = no injected clock (the wall clock)
        argv += ["--now", now]
    return run_json(argv, env)


def verify_cli(env, receipt, now=NOW, expect=None):
    """`expect=None` binds to EXPECT; `{}` sends NO expectation (the refusal
    case); a dict sends the mapping's fields as the CLI's expectation flags."""
    workdir = _workdir(env)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "receipt.json"
    path.write_text(json.dumps(receipt))
    argv = [str(DECISION), "--verify", str(path)]
    if now is not None:
        argv += ["--now", now]
    binding = dict(EXPECT if expect is None else expect)
    if binding.get("parent_run_id"):
        argv += ["--expect-parent-run", binding["parent_run_id"]]
    if binding.get("step_id"):
        argv += ["--expect-step", binding["step_id"]]
    if "generation" in binding:
        argv += ["--expect-generation", str(binding["generation"])]
    return run_json(argv, env)


def spawn_cli(env, args):
    return run_json([str(SPAWN), *args], env)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_request(port, path, body, key=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@contextlib.contextmanager
def running_server(env, mode="read-only", key=None):
    port = _free_port()
    env = dict(env)
    if key:
        env["ROUTER_EDIT_API_KEY"] = key
    proc = subprocess.Popen(
        [PY, str(SERVER), "--mode", mode, "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                pytest.fail(f"server exited {proc.returncode}: {stdout} {stderr}")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/status", timeout=5) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("server did not become ready")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def snapshot_tree(path):
    """{relpath: sha256} for every file under `path` — the no-write proof."""
    out = {}
    root = Path(path)
    if not root.exists():
        return out
    for item in sorted(root.rglob("*")):
        if item.is_file():
            out[str(item.relative_to(root))] = hashlib.sha256(
                item.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# canonicalization + digest stability
# ---------------------------------------------------------------------------

def test_canonical_json_is_key_order_independent():
    import router_decision as rd
    left = {"b": 1, "a": {"z": [1, 2], "y": None}}
    right = {"a": {"y": None, "z": [1, 2]}, "b": 1}
    assert rd.canonical_json(left) == rd.canonical_json(right)
    assert rd.sha256_hex(rd.canonical_json(left)) == rd.request_digest(right)


def test_receipt_stable_across_reordered_input_keys_and_restart(decision_env):
    """Same request (reordered OBJECT keys at every level) in two SEPARATE
    processes at the same injected clock -> byte-identical receipt AND digest.
    (List order is semantic and deliberately NOT normalized away.)"""
    left = base_request()
    right = dict(reversed(list(base_request().items())))
    right["required_levels"] = dict(reversed(list(right["required_levels"].items())))
    right["context_refs"] = [dict(reversed(list(ref.items())))
                             for ref in right["context_refs"]]

    rc_a, doc_a, _ = decide_cli(decision_env, left)
    rc_b, doc_b, _ = decide_cli(decision_env, right)
    assert rc_a == 0 and rc_b == 0
    assert doc_a == doc_b
    assert doc_a["receipt_digest"] == doc_b["receipt_digest"]
    assert doc_a["receipt_id"] == doc_b["receipt_id"]
    assert doc_a["request_digest"] == doc_b["request_digest"]

    import router_decision as rd
    assert rd.receipt_digest(doc_a) == doc_a["receipt_digest"]
    result = rd.verify_receipt(doc_a, now="2026-10-17T10:01:00Z",
                                expect=EXPECT)
    assert result["ok"], result["problems"]


def test_receipt_id_and_digest_are_deterministic_functions_of_request_and_clock(
        decision_env):
    import router_decision as rd
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    expected_id = rd.sha256_hex(rd.canonical_json({
        "request_digest": doc["request_digest"],
        "resolved_at": doc["resolved_at"],
        "valid_until": doc["valid_until"],
    }))
    assert doc["receipt_id"] == expected_id
    # the volatile fields are IN the digest (tampering with any of them breaks
    # verification) — the contract says so and the verifier proves it
    for field, value in (("resolved_at", "2026-10-17T09:59:00Z"),
                         ("valid_until", "2026-10-17T23:59:00Z"),
                         ("receipt_id", "f" * 64)):
        tampered = copy.deepcopy(doc)
        tampered[field] = value
        result = rd.verify_receipt(tampered, now="2026-10-17T10:01:00Z",
                                    expect=EXPECT)
        assert not result["ok"]
        assert any(p["code"] == "DIGEST_MISMATCH" for p in result["problems"])


# ---------------------------------------------------------------------------
# verification detects changed authority-bearing fields
# ---------------------------------------------------------------------------

def _mutate_swap_candidates(doc):
    doc["candidates"][0], doc["candidates"][1] = (
        doc["candidates"][1], doc["candidates"][0])


def _mutate_head(doc):
    doc["head"] = {"hop": 99, "provider": "forged", "model": "forged"}


def _mutate_levels(doc):
    doc["requirements"]["levels"]["reasoning"] = 2


def _mutate_brief(doc):
    doc["brief_digest"] = "e" * 64


def _mutate_context(doc):
    doc["context_refs"][0]["digest"] = "f" * 64


def _mutate_exclusion(doc):
    doc["exclusions"].append({"hop": 999, "provider": "x", "model": "y",
                              "why": ["forged"], "codes": ["unknown"]})


def _mutate_source(doc):
    doc["source"] = "registry.json" if doc.get("source") != "registry.json" else "data/tables"


def _mutate_expiry(doc):
    doc["valid_until"] = "2026-10-18T10:00:00Z"


def _mutate_registry_digest(doc):
    doc["registry"]["digest"] = "9" * 64


def _mutate_policy_labels(doc):
    doc["requirements"]["policy_labels"] = ["forged-label"]


MUTATIONS = [
    ("candidate order", _mutate_swap_candidates),
    ("chosen head", _mutate_head),
    ("category requirement", _mutate_levels),
    ("brief digest", _mutate_brief),
    ("context reference digest", _mutate_context),
    ("exclusion", _mutate_exclusion),
    ("source", _mutate_source),
    ("expiry", _mutate_expiry),
    ("registry digest", _mutate_registry_digest),
    ("policy labels", _mutate_policy_labels),
]


def test_changed_authority_bearing_fields_invalidate_verification(decision_env):
    import router_decision as rd
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0 and doc["eligible"] is True and len(doc["candidates"]) >= 2
    for label, mutate in MUTATIONS:
        tampered = copy.deepcopy(doc)
        mutate(tampered)
        result = rd.verify_receipt(tampered, now="2026-10-17T10:01:00Z",
                                    expect=EXPECT)
        assert not result["ok"], f"mutation not detected: {label}"
        assert any(p["code"] == "DIGEST_MISMATCH" for p in result["problems"]), label


def test_receipt_verifies_before_expiry_and_is_refused_at_and_after(decision_env):
    import router_decision as rd
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    assert doc["valid_until"] == "2026-10-17T10:05:00Z"  # default policy TTL
    assert rd.verify_receipt(doc, now="2026-10-17T10:04:59Z", expect=EXPECT)["ok"]
    at = rd.verify_receipt(doc, now="2026-10-17T10:05:00Z", expect=EXPECT)
    assert not at["ok"] and any(p["code"] == "EXPIRED" for p in at["problems"])
    after = rd.verify_receipt(doc, now="2026-10-17T12:00:00Z", expect=EXPECT)
    assert not after["ok"] and any(p["code"] == "EXPIRED" for p in after["problems"])
    # the CLI verifier reports the same with an injected clock
    rc, result, _ = verify_cli(decision_env, doc, now="2026-10-17T10:04:00Z")
    assert rc == 0 and result["ok"]
    rc, result, _ = verify_cli(decision_env, doc, now="2026-10-17T10:05:00Z")
    assert rc == 1 and any(p["code"] == "EXPIRED" for p in result["problems"])


# ---------------------------------------------------------------------------
# no eligible candidate: no head, never a requirements-unmet fallback
# ---------------------------------------------------------------------------

def test_no_eligible_candidate_never_returns_a_degraded_fallback(decision_env):
    rc, doc, _ = decide_cli(decision_env,
                            base_request(required_levels=IMPOSSIBLE_LEVELS,
                                         required_context_limit=_DROP))
    assert rc == 0
    assert "receipt_digest" in doc, "a refusal is a RECEIPT, not an error doc"
    assert doc["eligible"] is False
    assert doc["head"] is None
    assert doc["candidates"] == []
    assert doc["refusal"] is not None
    assert doc["refusal"]["code"] in ("DEGRADED_FALLBACK_REFUSED",
                                      "NO_ELIGIBLE_CANDIDATE")
    if doc["degraded_fallback"]:
        # the requirements-unmet always-run lane was offered and REFUSED — it
        # survives only as an exclusion, never as a candidate or head
        assert doc["refusal"]["code"] == "DEGRADED_FALLBACK_REFUSED"
        refused = [e for e in doc["exclusions"]
                   if "fallback-refused" in e["codes"]]
        assert refused, "fallback lanes must stay visible as exclusions"
        assert all("requirements_unmet" in str(e) or e.get("why")
                   for e in refused)
    # nothing anywhere claims a served route
    assert doc["head"] is None and doc["candidates"] == []
    dump = json.dumps(doc)
    assert '"fallback": true' not in dump.replace(" ", "")


def test_all_gated_chain_is_an_explicit_no_eligible_refusal(decision_env):
    state_path = Path(decision_env["ROUTER_STATE_DIR"]) / "quota-state.json"
    gated = json.loads(state_path.read_text())
    for prov in gated["providers"]:
        gated["providers"][prov] = {"status": "gated", "reason": "test-gate"}
    state_path.write_text(json.dumps(gated))

    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    assert doc["eligible"] is False
    assert doc["head"] is None and doc["candidates"] == []
    assert doc["refusal"]["code"] == "NO_ELIGIBLE_CANDIDATE"
    assert doc["degraded_fallback"] is False
    assert doc["exclusions"], "the gate exclusions must be retained"
    # every survivor-side exclusion is a policy-plane refusal, kept exactly
    for entry in doc["exclusions"]:
        assert set(entry["codes"]) <= {"quota-gated", "policy-gate-missing-row"}
        assert "test-gate" in entry["why"][0] or \
            "policy-gate-missing-row" in entry["why"][0]


def test_data_class_requirement_filters_candidates_and_names_the_exclusion(
        decision_env):
    rc, doc, _ = decide_cli(decision_env,
                            base_request(required_data_class="zdr"))
    assert rc == 0
    if doc["eligible"]:
        assert {c["data_class"] for c in doc["candidates"]} == {"zdr"}
        mismatched = [e for e in doc["exclusions"]
                      if "data-class-mismatch" in e["codes"]]
        assert mismatched
        assert all("data-class-mismatch" in e["why"][0] for e in mismatched)
    else:
        assert doc["refusal"]["code"] == "NO_ELIGIBLE_CANDIDATE"


# ---------------------------------------------------------------------------
# gate exclusions retained exactly
# ---------------------------------------------------------------------------

def _iso(**delta):
    """Wall-clock ISO-Z stamp offset from now. The legacy spawn surface reads
    the REAL clock (only `decide --now` injects one), so gate fixtures must be
    relative or they silently stop gating once the calendar moves past them."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _assert_gate_retained(decision_env, expected_codes, baseline_pairs):
    """One gate family active: its codes appear AND the resolver's exclusions,
    gate_reasons and surviving chain ride the receipt unchanged, in order."""
    rc, spawn_doc, _ = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0
    rc, receipt, _ = decide_cli(decision_env, base_request(
        required_levels={"reasoning": 1}, required_context_limit=_DROP))
    assert rc == 0

    codes = {c for e in spawn_doc["exclusions"] for c in e["codes"]}
    missing = set(expected_codes) - codes
    assert not missing, (missing, sorted(codes))
    # the gated family actually changed the surviving chain
    assert len(spawn_doc["chain"]) < len(baseline_pairs)
    # EXACT retention: the resolver's exclusions ride the receipt unchanged,
    # in order, ahead of any decision-stage exclusion
    kept = receipt["exclusions"][:len(spawn_doc["exclusions"])]
    assert kept == spawn_doc["exclusions"]
    assert receipt["gate_reasons"][:len(spawn_doc["gate_reasons"])] == \
        spawn_doc["gate_reasons"]
    # and the receipt's candidates are the resolver's surviving chain in order
    assert [(c["provider"], c["model"]) for c in receipt["candidates"]] == \
        [(e["provider"], e["model"]) for e in spawn_doc["chain"]]
    assert [c["hop"] for c in receipt["candidates"]] == \
        [e["hop"] for e in spawn_doc["chain"]]


# One gate family per fresh hermetic plane. Stacking all three in a single
# state shadows the circuit pair when the chain is single-vendor (quota gates
# every pair of that provider first), which made the stacked assertion
# unsatisfiable once the committed registry became xkiro/xkiro-2 only.

def test_quota_gate_exclusions_are_retained_exactly(decision_env):
    rc, baseline, _ = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0 and baseline["chain"], baseline
    pairs = [(e["provider"], e["model"]) for e in baseline["chain"]]

    state_dir = Path(decision_env["ROUTER_STATE_DIR"])
    quota = json.loads((state_dir / "quota-state.json").read_text())
    quota["providers"][pairs[0][0]] = {"status": "gated",
                                       "reason": "window exhausted"}
    (state_dir / "quota-state.json").write_text(json.dumps(quota))

    _assert_gate_retained(decision_env, {"quota-gated"}, pairs)


def test_health_gate_exclusions_are_retained_exactly(decision_env):
    rc, baseline, _ = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0 and baseline["chain"], baseline
    pairs = [(e["provider"], e["model"]) for e in baseline["chain"]]

    state_dir = Path(decision_env["ROUTER_STATE_DIR"])
    health = json.loads((state_dir / "health-state.json").read_text())
    health["providers"][pairs[0][0]] = {"status": "DOWN",
                                        "ts": _iso(minutes=-30)}
    (state_dir / "health-state.json").write_text(json.dumps(health))

    _assert_gate_retained(decision_env, {"health-down"}, pairs)


def test_circuit_gate_exclusions_are_retained_exactly(decision_env):
    rc, baseline, _ = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0 and baseline["chain"], baseline
    pairs = [(e["provider"], e["model"]) for e in baseline["chain"]]
    circuit_pair = pairs[0]

    state_dir = Path(decision_env["ROUTER_STATE_DIR"])
    (state_dir / "circuit-state.json").write_text(json.dumps({
        "version": 1,
        "pairs": {f"{circuit_pair[0]}/{circuit_pair[1]}":
                  {"open_until": _iso(hours=1), "failures": 4}}}))

    _assert_gate_retained(decision_env, {"circuit-open"}, pairs)


# ---------------------------------------------------------------------------
# connector capabilities: echoed, never resolved
# ---------------------------------------------------------------------------

def test_connector_capabilities_are_echoed_but_never_resolved(decision_env):
    with_caps = base_request()
    without = base_request(required_connector_capabilities=_DROP)
    rc, doc_caps, _ = decide_cli(decision_env, with_caps)
    rc2, doc_plain, _ = decide_cli(decision_env, without)
    assert rc == 0 and rc2 == 0

    constraints = doc_caps["constraints"]
    assert constraints["required_connector_capabilities"] == ["acp", "ssh"]
    for key in ("selected_connector", "selected_harness",
                "selected_tool", "selected_workspace"):
        assert constraints[key] is None, key
        assert doc_plain["constraints"][key] is None, key
    # capabilities change NOTHING about the routing decision
    assert doc_caps["candidates"] == doc_plain["candidates"]
    assert doc_caps["head"] == doc_plain["head"]
    assert doc_caps["exclusions"] == doc_plain["exclusions"]


def test_receipt_never_names_a_harness_connector_tool_or_workspace(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    assert set(doc) == {
        "version", "receipt_id", "receipt_digest", "request_digest",
        "brief_digest", "parent_run_id", "step_id", "generation",
        "previous_route_digest", "context_refs", "resolved_at", "valid_until",
        "valid_for_s", "eligible", "refusal", "head", "candidates",
        "exclusions", "gate_reasons", "requirements", "constraints", "source",
        "registry", "policy", "fallback_used", "degraded_fallback", "warnings",
        "gate_evidence", "key_id", "signature", "signature_alg",
    }
    # the only selection-shaped values in the whole receipt are the explicit
    # nulls under constraints — a non-null one would be a forged selection
    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("selected_") and value is not None:
                    raise AssertionError(f"selection named at {path}.{key}: {value}")
                if key in ("harness", "workspace", "board_row") and value is not None:
                    raise AssertionError(f"boundary field named at {path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
    walk(doc)
    assert doc["constraints"]["required_connector_capabilities"] == ["acp", "ssh"]


# ---------------------------------------------------------------------------
# legacy `router spawn`: byte/schema compatible, fail-open intact
# ---------------------------------------------------------------------------

def test_legacy_router_spawn_schema_and_fail_open_are_unchanged(decision_env):
    rc, first, proc = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0, proc.stderr
    assert set(first) == EXPECTED_SPAWN_KEYS
    rc, second, _ = spawn_cli(
        decision_env, ["--profile-req", "reasoning=1", "--format", "json"])
    assert rc == 0
    # byte compatibility: only the wall-clock stamp may differ
    for doc in (first, second):
        doc.pop("resolved_at")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    # fail-open contract: every error path is {"error": ...} + exit 0
    rc, doc, _ = spawn_cli(decision_env, ["no-such-project-xyz", "--format", "json"])
    assert rc == 0
    assert doc["error"] == "project no-such-project-xyz not in registry"
    assert set(doc) == {"error", "data_home"}
    rc, doc, _ = spawn_cli(decision_env, ["--profile-req", "bogus_cat=1",
                                          "--format", "json"])
    assert rc == 0 and doc["code"] == "INVALID_REQUIREMENT"
    assert doc["retryable"] is False


def test_spawn_fail_open_coercion_via_the_cli_wrapper(decision_env):
    """`router spawn --bogus` stays exit 0 (AGENTS.md: spawn must NEVER block
    the scheduler) while `router decide` propagates its own codes."""
    code = (
        "import sys\n"
        "from task_router.cli import main\n"
        "sys.exit(main())\n"
    )
    env = dict(decision_env)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([PY, "-c", code, "spawn", "--bogus"], capture_output=True,
                          text=True, env=env, cwd=str(REPO), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "coerced to 0" in proc.stderr
    proc = subprocess.run([PY, "-c", code, "decide", "--request", "-"],
                          input=json.dumps({"version": 1}),
                          capture_output=True, text=True, env=env,
                          cwd=str(REPO), timeout=120)
    assert proc.returncode == 2, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# HTTP / OpenAPI / CLI parity + strict unknown-field rejection
# ---------------------------------------------------------------------------

def test_http_openapi_and_cli_parity(decision_env):
    request = base_request()
    env = dict(decision_env)
    env["ROUTER_DECISION_NOW"] = NOW
    with running_server(env, mode="edit", key="tr-h1-secret") as port:
        status, http_doc = _http_request(port, "/api/v1/execution-decision",
                                         request, key="tr-h1-secret")
        assert status == 200
        # OpenAPI + MCP parity: the same operation is discoverable and callable
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/openapi.json", timeout=15) as response:
            openapi = json.load(response)
        paths = openapi["paths"]
        assert "/api/v1/execution-decision" in paths
        assert "/api/v1/execution-decision/verify" in paths
        assert paths["/api/v1/execution-decision"]["post"]["operationId"] == \
            "postExecutionDecision"
        # H1-F4: a decision appends a durable issuance row, so the contract
        # says it is authenticated — it is NOT the `security: []` read the
        # slice first advertised. Verification stays an anonymous read.
        assert paths["/api/v1/execution-decision"]["post"]["security"] == \
            [{"EditApiKey": []}]
        assert paths["/api/v1/execution-decision/verify"]["post"]["security"] == []
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "postExecutionDecision",
                          "arguments": request}}
        rpc_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", data=json.dumps(rpc).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": "tr-h1-secret"}, method="POST")
        with urllib.request.urlopen(rpc_req, timeout=60) as response:
            mcp_doc = json.load(response)
        mcp_receipt = mcp_doc["result"]["structuredContent"]

    rc, cli_doc, _ = decide_cli(decision_env, request)
    assert rc == 0
    assert http_doc == cli_doc == mcp_receipt
    assert http_doc["receipt_digest"] == cli_doc["receipt_digest"]


def test_strict_unknown_field_rejection_is_identical_everywhere(decision_env):
    import router_decision as rd
    bad = base_request()
    bad["harness"] = "opencode"          # boundary violation AND unknown field
    bad["workspace"] = "/tmp/anything"

    problems = rd.validate_request(bad)[1]
    fields = {p["field"] for p in problems}
    assert "harness, workspace" in fields

    rc, cli_doc, _ = decide_cli(decision_env, bad)
    assert rc == 2
    assert cli_doc["code"] == "INVALID_REQUEST"
    assert any("harness" in p["field"] for p in cli_doc["problems"])

    env = dict(decision_env)
    env["ROUTER_DECISION_NOW"] = NOW
    with running_server(env, mode="edit", key="tr-h1-secret") as port:
        status, http_doc = _http_request(port, "/api/v1/execution-decision", bad,
                                         key="tr-h1-secret")
        assert status == 400
        assert http_doc["code"] == "INVALID_REQUEST"
        assert http_doc["problems"] == cli_doc["problems"]


# ---------------------------------------------------------------------------
# read-only: no state file or table changes from decision calls
# ---------------------------------------------------------------------------

def test_decision_calls_change_no_state_files_or_tables(decision_env):
    state_dir = Path(decision_env["ROUTER_STATE_DIR"])
    data_dir = Path(decision_env["ROUTING_DATA_DIR"])
    registry = Path(decision_env["ROUTING_REGISTRY"])
    before = {
        "state": snapshot_tree(state_dir),
        "data": snapshot_tree(data_dir),
        "registry": registry.read_bytes() if registry.exists() else None,
    }

    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    rc, result, _ = verify_cli(decision_env, doc)
    assert rc == 0
    # The wire has no clock field, so the HTTP receipt must be minted at the
    # WALL clock (no ROUTER_DECISION_NOW here) or verification — which checks
    # the wall clock too (H1-F2) — refuses it as expired.
    env = dict(decision_env)
    with running_server(env, mode="edit", key="tr-h1-secret") as port:
        status, http_doc = _http_request(port, "/api/v1/execution-decision",
                                         base_request(attempt_deadline=_DROP),
                                         key="tr-h1-secret")
        assert status == 200
        status, verify_doc = _http_request(
            port, "/api/v1/execution-decision/verify",
            {"receipt": http_doc,
             "expect": {"parent_run_id": http_doc["parent_run_id"],
                        "step_id": http_doc["step_id"]}})
        # H1-F3: 200 is reserved for ok=true, and this one really is ok
        assert status == 200, verify_doc
        assert verify_doc["ok"] is True

    after = {
        "state": snapshot_tree(state_dir),
        "data": snapshot_tree(data_dir),
        "registry": registry.read_bytes() if registry.exists() else None,
    }
    # A decision writes EXACTLY ONE thing: one issuance record per issued
    # receipt. The signing key is an operator bootstrap step (H1-F4) and was
    # already present, so it must not be re-created or rewritten. No
    # quota/health/circuit/ledger row, no registry or table byte, may change.
    assert after["data"] == before["data"]
    assert after["registry"] == before["registry"]
    added = {name: digest for name, digest in after["state"].items()
             if name not in before["state"]}
    assert added, "expected the issuance record to be written"
    for name, digest in before["state"].items():
        assert after["state"][name] == digest, f"{name} was mutated by a decision"
    for name in added:
        assert name.startswith("decision-receipts/"), \
            f"unexpected state write: {name}"


# ---------------------------------------------------------------------------
# request validation: IDs, levels, digests, timestamps, domains
# ---------------------------------------------------------------------------

def test_validate_request_rejects_bad_ids_levels_digests_timestamps_domains():
    import router_decision as rd
    cases = [
        ("missing required", {"step_id": _DROP}, "step_id"),
        ("unknown version", {"version": 2}, "version"),
        ("string version", {"version": "1"}, "version"),
        ("bad parent id", {"parent_run_id": "has space"}, "parent_run_id"),
        ("empty requester", {"requester": ""}, "requester"),
        ("negative generation", {"generation": -1}, "generation"),
        ("bool generation", {"generation": True}, "generation"),
        ("uppercase digest", {"brief_digest": "A" * 64}, "brief_digest"),
        ("short digest", {"brief_digest": "abc"}, "brief_digest"),
        ("previous route digest", {"previous_route_digest": "nope"},
         "previous_route_digest"),
        ("bool level", {"required_levels": {"reasoning": True}},
         "required_levels.reasoning"),
        ("float level", {"required_levels": {"reasoning": 1.5}},
         "required_levels.reasoning"),
        ("min_context in levels", {"required_levels": {"min_context": 5}},
         "required_levels.min_context"),
        ("empty requirements", {"required_levels": {}, "required_context_limit": _DROP},
         "required_levels"),
        ("zero context limit", {"required_context_limit": 0},
         "required_context_limit"),
        ("negative context limit", {"required_context_limit": -5},
         "required_context_limit"),
        ("bad data class", {"required_data_class": "ZDR!"},
         "required_data_class"),
        ("bad policy label", {"policy_labels": ["OK Label"]}, "policy_labels"),
        ("duplicate policy label", {"policy_labels": ["a", "a"]}, "policy_labels"),
        ("duplicate capability",
         {"required_connector_capabilities": ["acp", "acp"]},
         "required_connector_capabilities"),
        ("budget over ceiling", {"budget_ceiling_usd": 1.0,
                                 "budget_remaining_usd": 2.0},
         "budget_remaining_usd"),
        ("negative budget", {"budget_ceiling_usd": -1}, "budget_ceiling_usd"),
        ("bool budget", {"budget_ceiling_usd": True}, "budget_ceiling_usd"),
        ("naive deadline", {"attempt_deadline": "2026-10-17T10:00:00"},
         "attempt_deadline"),
        ("unparseable deadline", {"attempt_deadline": "next tuesday"},
         "attempt_deadline"),
        ("zero valid_for_s", {"valid_for_s": 0}, "valid_for_s"),
        ("bad context ref digest",
         {"context_refs": [{"name": "x", "digest": "nope"}]},
         "context_refs[0].digest"),
        ("duplicate context ref",
         {"context_refs": [{"name": "x", "digest": "b" * 64},
                           {"name": "x", "digest": "c" * 64}]},
         "context_refs[1].name"),
        ("context ref unknown field",
         {"context_refs": [{"name": "x", "digest": "b" * 64, "content": "hi"}]},
         "context_refs[0]"),
    ]
    for label, overrides, field in cases:
        normalized, problems = rd.validate_request(base_request(**overrides))
        assert normalized is None, label
        assert any(p["field"] == field for p in problems), (label, problems)


def test_unknown_category_and_out_of_range_level_are_invalid_requests(decision_env):
    for overrides, needle in (
            ({"required_levels": {"not_a_real_category": 1}}, "not_a_real_category"),
            ({"required_levels": {"reasoning": 9}}, "out of range")):
        rc, doc, _ = decide_cli(decision_env, base_request(**overrides))
        assert rc == 2, overrides
        assert doc["code"] == "INVALID_REQUEST"
        assert any(needle in p["problem"] for p in doc["problems"]), doc["problems"]


def test_attempt_deadline_bounds_expiry_and_a_passed_deadline_is_refused(
        decision_env):
    rc, doc, _ = decide_cli(decision_env,
                            base_request(attempt_deadline="2026-10-17T10:02:00Z"))
    assert rc == 0
    assert doc["valid_until"] == "2026-10-17T10:02:00Z"
    assert any("bounded by the declared attempt_deadline" in w
               for w in doc["warnings"])
    rc, doc, _ = decide_cli(decision_env,
                            base_request(attempt_deadline="2026-10-17T09:00:00Z"))
    assert rc == 2
    assert doc["code"] == "ATTEMPT_DEADLINE_PASSED"
    assert doc["retryable"] is False


def test_valid_for_s_is_clamped_to_the_policy_ceiling(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request(valid_for_s=5000))
    assert rc == 0
    assert doc["valid_for_s"] == 900
    assert doc["valid_until"] == "2026-10-17T10:15:00Z"
    assert any("clamped to the policy ceiling" in w for w in doc["warnings"])
    rc, doc, _ = decide_cli(decision_env, base_request(valid_for_s=30))
    assert rc == 0
    assert doc["valid_for_s"] == 30
    assert doc["valid_until"] == "2026-10-17T10:00:30Z"


def test_previous_route_digest_is_echoed_for_reconciliation(decision_env):
    """A declared chain link is echoed — and it must be a real one (H1-F5)."""
    rc, prior, _ = decide_cli(decision_env, base_request(step_id="step-7"))
    assert rc == 0, prior
    rc, doc, _ = decide_cli(decision_env, base_request(
        step_id="step-8", generation=4,
        previous_route_digest=prior["receipt_digest"]))
    assert rc == 0, doc
    assert doc["previous_route_digest"] == prior["receipt_digest"]
    rc, doc, _ = decide_cli(decision_env, base_request(previous_route_digest=_DROP))
    assert rc == 0
    assert doc["previous_route_digest"] is None


def test_reissue_at_the_same_second_is_idempotent_in_the_ledger(decision_env):
    """A deterministic receipt_id must not become a duplicate ledger row
    (H1-F5).

    Two identical requests at the same injected second produce the same
    receipt; the issuance ledger must hold exactly one row for that digest, and
    the receipt must still verify.
    """
    state = _state(decision_env)
    rc, first, _ = decide_cli(decision_env, base_request())
    assert rc == 0, first
    rc, second, _ = decide_cli(decision_env, base_request())
    assert rc == 0, second
    assert first["receipt_id"] == second["receipt_id"]
    assert first["receipt_digest"] == second["receipt_digest"]
    month = first["resolved_at"][:7].replace("-", "")
    ledger = state / "decision-receipts" / f"{month}.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    matching = [row for row in rows if row.get("receipt_digest") == first["receipt_digest"]]
    assert len(matching) == 1, f"{len(matching)} issuance rows for one digest"
    import router_decision as rd
    result = rd.verify_receipt(first, now=NOW, expect=EXPECT)
    assert result["ok"] is True, result


def test_previous_route_digest_is_checked_against_the_ledger(decision_env):
    """`previous_route_digest` is a chain link the issuer must validate, not a
    field it merely echoes (H1-F5).

    Unknown digest -> PREVIOUS_ROUTE_UNKNOWN; a genuine digest issued for a
    DIFFERENT parent run -> PREVIOUS_ROUTE_MISMATCH. Both are named refusals
    with no receipt.
    """
    rc, prior, _ = decide_cli(decision_env, base_request(step_id="step-7"))
    assert rc == 0, prior

    rc, doc, _ = decide_cli(decision_env, base_request(
        step_id="step-8", generation=4,
        previous_route_digest="e" * 64))
    assert rc != 0, doc
    assert doc.get("code") == "PREVIOUS_ROUTE_UNKNOWN", doc
    assert "receipt_digest" not in doc, doc
    assert any(p.get("field") == "previous_route_digest"
               for p in doc.get("problems") or []), doc

    rc, other_run, _ = decide_cli(decision_env,
                                  base_request(parent_run_id="run-0002",
                                               step_id="step-1"))
    assert rc == 0, other_run
    rc, doc, _ = decide_cli(decision_env, base_request(
        step_id="step-9", previous_route_digest=other_run["receipt_digest"]))
    assert rc != 0, doc
    assert doc.get("code") == "PREVIOUS_ROUTE_MISMATCH", doc
    assert "receipt_digest" not in doc, doc

    # The chain link from the SAME run is accepted (and echoed).
    rc, chained, _ = decide_cli(decision_env, base_request(
        step_id="step-8", generation=4,
        previous_route_digest=prior["receipt_digest"]))
    assert rc == 0, chained
    assert chained["previous_route_digest"] == prior["receipt_digest"]


# ---------------------------------------------------------------------------
# provenance: source, registry/policy version + digest
# ---------------------------------------------------------------------------

def test_registry_and_policy_provenance_on_the_receipt(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    assert doc["source"] == "data/tables"     # no seeded registry.json here
    assert doc["fallback_used"] is True
    assert doc["registry"]["source"] == "data/tables"
    assert doc["registry"]["version"] is None   # sample tables carry no envelope
    assert len(doc["registry"]["digest"]) == 64
    assert doc["policy"]["source"].endswith("quota-state.json")
    assert len(doc["policy"]["digest"]) == 64
    assert doc["gate_evidence"]["gates_loaded"]["quota"] is True
    assert doc["gate_evidence"]["gates_loaded"]["health"] is True
    assert doc["gate_evidence"]["gates_loaded"]["circuit"] is True


def test_seeded_registry_source_and_version_are_reported(seed_registry_copy,
                                                         tmp_path):
    env = dict(os.environ)
    env.update(seed_registry_copy)
    env.update({
        "ROUTER_CACHE": "0",
        "ROUTER_SPAWN_QUIET": "1",
        "TASK_ROUTER_HOME": str(tmp_path / "router-home"),
        "ROUTING_OUTCOMES_FILE": str(tmp_path / "outcomes.jsonl"),
        "ROUTING_AVERAGES_FILE": str(tmp_path / "outcomes-averages.jsonl"),
    })
    env.pop("ROUTER_DECISION_NOW", None)
    # H1-F4: issuing requires the operator-bootstrapped signing key; the
    # request path never creates it.
    rc, _boot, proc = run_json([str(DECISION), "--init-receipt-key"], env)
    assert rc == 0, proc.stdout + proc.stderr
    rc, doc, _ = decide_cli(env, base_request())
    assert rc == 0
    assert doc["source"] == "registry.json"
    assert doc["fallback_used"] is False
    assert doc["registry"]["version"] == 3
    assert len(doc["registry"]["digest"]) == 64


# ---------------------------------------------------------------------------
# receipt authentication (F1): a self-digest is integrity, never authority
# ---------------------------------------------------------------------------

import router_decision as _rd  # noqa: E402


def _codes(result):
    return {p.get("code") for p in result.get("problems") or []}


def test_forged_route_receipt_is_refused(decision_env):
    """The classic attack: rewrite the route, recompute the unkeyed digest.
    The signature is over the server key, so the forgery cannot be re-signed."""
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    forged = copy.deepcopy(doc)
    forged["head"] = {"hop": 0, "provider": "attacker", "model": "free-lunch"}
    forged["receipt_digest"] = _rd.receipt_digest(forged)
    result = _rd.verify_receipt(forged, now=NOW, expect=EXPECT)
    assert result["ok"] is False
    assert "RECEIPT_SIGNATURE_INVALID" in _codes(result), result


def test_unsigned_receipt_is_refused(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    unsigned = copy.deepcopy(doc)
    unsigned.pop("signature", None)
    unsigned.pop("signature_alg", None)
    unsigned["receipt_digest"] = _rd.receipt_digest(unsigned)
    result = _rd.verify_receipt(unsigned, now=NOW, expect=EXPECT)
    assert result["ok"] is False
    assert "RECEIPT_UNSIGNED" in _codes(result), result


def test_signed_but_unissued_receipt_is_refused(decision_env):
    """Even a receipt signed with the right key is refused when this server
    has no issuance record for it — the ledger is an independent check."""
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    unissued = copy.deepcopy(doc)
    unissued["receipt_id"] = "0" * 64
    unissued.pop("signature", None)
    unissued.pop("signature_alg", None)
    unissued["receipt_digest"] = _rd.receipt_digest(unissued)
    _rd.sign_receipt(unissued)  # signed with the real server key, never issued
    result = _rd.verify_receipt(unissued, now=NOW, expect=EXPECT)
    assert result["ok"] is False
    assert "RECEIPT_NOT_ISSUED" in _codes(result), result


def test_replayed_receipt_under_a_different_step_identity_is_refused(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    replayed = copy.deepcopy(doc)
    replayed["step_id"] = "step-8"
    replayed.pop("signature", None)
    replayed.pop("signature_alg", None)
    replayed["receipt_digest"] = _rd.receipt_digest(replayed)
    _rd.sign_receipt(replayed)  # defence in depth: even re-signed it fails
    result = _rd.verify_receipt(replayed, now=NOW, expect=EXPECT)
    assert result["ok"] is False
    # A re-identified receipt has a new digest, so it matches no issuance
    # record (NOT_ISSUED); if an attacker instead edits a record in place, the
    # identity comparison trips (ISSUANCE_MISMATCH). Both fail closed.
    assert _codes(result) & {"RECEIPT_NOT_ISSUED", "RECEIPT_ISSUANCE_MISMATCH"}, result


def test_tampered_issuance_record_is_refused(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    state = Path(decision_env["ROUTER_STATE_DIR"])
    records = sorted((state / "decision-receipts").glob("*.jsonl"))
    assert records
    path = records[-1]
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        if row.get("receipt_digest") == doc["receipt_digest"]:
            row["step_id"] = "step-999"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    result = _rd.verify_receipt(doc, now=NOW, expect=EXPECT)
    assert result["ok"] is False
    assert "RECEIPT_ISSUANCE_MISMATCH" in _codes(result), result


def test_valid_receipt_reports_both_authentication_facts(decision_env):
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    result = _rd.verify_receipt(doc, now=NOW, expect=EXPECT)
    assert result["ok"] is True, result
    assert result["signature_ok"] is True
    assert result["issued"] is True

# ---------------------------------------------------------------------------
# H1-F1 / H1-F2 — binding and trusted clock (added during coordinator closeout)
# ---------------------------------------------------------------------------

def test_unbound_verification_is_refused_not_silently_ok(decision_env):
    """A receipt on its own authenticates the issuer, not the route (H1-F1).

    Verification without an expected identity must REFUSE — a silent ok here is
    the defect: a genuine receipt for one step would bless any step.
    """
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    import router_decision as rd
    result = rd.verify_receipt(doc, now=NOW)          # no expect=
    assert result["ok"] is False, result
    assert "RECEIPT_UNBOUND_EXPECTATION" in _codes(result), result


def test_receipt_for_one_step_does_not_verify_as_another(decision_env):
    """step-7's receipt must not verify as step-8's route (H1-F1)."""
    rc, doc, _ = decide_cli(decision_env, base_request(step_id="step-7"))
    assert rc == 0
    import router_decision as rd
    wrong = rd.verify_receipt(doc, now=NOW,
                              expect={"parent_run_id": "run-0001",
                                      "step_id": "step-8"})
    assert wrong["ok"] is False, wrong
    assert "RECEIPT_IDENTITY_MISMATCH" in _codes(wrong), wrong
    right = rd.verify_receipt(doc, now=NOW,
                              expect={"parent_run_id": "run-0001",
                                      "step_id": "step-7"})
    assert right["ok"] is True, right


def test_injected_clock_cannot_resurrect_an_expired_receipt(decision_env, monkeypatch):
    """Wall clock outranks an injected `now` for expiry (H1-F2).

    The receipt is minted on a pinned 2026-10-17 clock with a 300s window; the
    host wall clock is days later. Verifying with the old injected `now` must
    still be EXPIRED — a caller must not be able to wind the clock back.
    """
    monkeypatch.delenv("ROUTER_DECISION_TEST_CLOCK", raising=False)
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0
    assert doc["valid_until"] == "2026-10-17T10:05:00Z"
    import router_decision as rd
    result = rd.verify_receipt(doc, now="2026-10-17T10:01:00Z", expect=EXPECT)
    assert result["ok"] is False, result
    assert "EXPIRED" in _codes(result), result


# ---------------------------------------------------------------------------
# H1-F4 — the decision endpoint is a durable WRITE, not a read-only surface
# ---------------------------------------------------------------------------

def _state(env):
    return Path(env["ROUTER_STATE_DIR"])


def _drop_signing_material(state):
    """The empty-install shape: no operator-bootstrapped key, no ledger."""
    key = state / "decision-receipt.key"
    if key.exists():
        key.unlink()
    ledger = state / "decision-receipts"
    if ledger.exists():
        shutil.rmtree(ledger)


@contextlib.contextmanager
def _opened_paths():
    """Record every absolute path opened through builtins `open`."""
    import builtins
    real_open = builtins.open
    opened = []

    def spy(file, *args, **kwargs):
        try:
            opened.append(os.path.abspath(str(file)))
        except Exception:  # noqa: BLE001 — an odd descriptor is not the point
            pass
        return real_open(file, *args, **kwargs)

    builtins.open = spy
    try:
        yield opened
    finally:
        builtins.open = real_open


def _append_ledger_rows(path, count, tag):
    """Append `count` decoy issuance rows (distinct digests) to a month file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(count):
        digest = hashlib.sha256(f"{tag}-{i}".encode()).hexdigest()
        lines.append(json.dumps({"version": 1, "receipt_digest": digest,
                                 "receipt_id": digest, "decoy": True}))
    with path.open("a") as handle:
        handle.write("\n".join(lines) + "\n")


def test_decision_without_a_bootstrapped_key_refuses_and_writes_nothing(decision_env):
    """A decision must never mint signing material (H1-F4).

    Key creation belongs to an operator bootstrap step. Without one the
    decision refuses with a NAMED error and leaves the state dir untouched —
    no key, no ledger directory.
    """
    state = _state(decision_env)
    _drop_signing_material(state)
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc != 0, doc
    assert doc.get("code") == "RECEIPT_KEY_UNAVAILABLE", doc
    assert "receipt_digest" not in doc, doc
    assert not (state / "decision-receipt.key").exists(), \
        "a decision request minted the signing key"
    assert not (state / "decision-receipts").exists(), \
        "a decision request created the issuance ledger"
    # The named error must tell the operator what to run.
    assert any("--init-receipt-key" in str(p) for p in doc.get("problems") or []), doc

    # ... and after the operator bootstrap the same request issues normally.
    rc, boot, proc = run_json([str(DECISION), "--init-receipt-key"], decision_env)
    assert rc == 0, proc.stdout + proc.stderr
    key = state / "decision-receipt.key"
    assert key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0, doc
    assert doc["receipt_digest"]


def test_decision_endpoint_is_not_an_anonymous_write(decision_env):
    """The endpoint appends a durable ledger row, so it is NOT an anonymous
    read (H1-F4).

    With no key bootstrapped an authorized POST must refuse (503) and write
    nothing; in read-only mode the same POST must not answer at all (the write
    path is behind the edit key, like every other mutation).
    """
    state = _state(decision_env)
    _drop_signing_material(state)
    env = dict(decision_env)
    env["ROUTER_DECISION_NOW"] = NOW

    with running_server(env, mode="read-only") as port:
        status, doc = _http_request(port, "/api/v1/execution-decision", base_request())
    assert status == 403, doc
    assert not (state / "decision-receipt.key").exists()
    assert not (state / "decision-receipts").exists()

    with running_server(env, mode="edit", key="tr-h1-secret") as port:
        status, doc = _http_request(port, "/api/v1/execution-decision",
                                    base_request(), key="tr-h1-secret")
    assert status == 503, doc
    assert doc["code"] == "RECEIPT_KEY_UNAVAILABLE", doc
    assert not (state / "decision-receipt.key").exists(), \
        "an authenticated decision request minted the signing key"
    assert not (state / "decision-receipts").exists(), \
        "a refused decision request created the issuance ledger"

    # Verify stays a read: after the operator bootstrap it answers in
    # read-only mode WITHOUT the edit key, and writes nothing itself. The
    # receipt is minted at the WALL clock so its own window is live.
    rc, boot, proc = run_json([str(DECISION), "--init-receipt-key"], decision_env)
    assert rc == 0, proc.stdout + proc.stderr
    rc, receipt, _ = decide_cli(decision_env, base_request(attempt_deadline=_DROP),
                                now=None)
    assert rc == 0, receipt
    before = snapshot_tree(state)
    with running_server(env, mode="read-only") as port:
        status, verify_doc = _http_request(
            port, "/api/v1/execution-decision/verify",
            {"receipt": receipt, "expect": EXPECT})
    assert status == 200, verify_doc
    assert verify_doc["ok"] is True, verify_doc
    assert snapshot_tree(state) == before, "verification wrote to the state dir"


def test_verify_reads_only_the_receipt_month_and_parses_it_once(decision_env, monkeypatch):
    """Verification must not re-parse the ledger on every call (H1-F4).

    The issuance record lives in the month file named by the receipt's own
    (signed) `resolved_at`; verification opens THAT file and no other, parses
    it once, and serves the warm path from an index.
    """
    state = _state(decision_env)
    rc, doc, _ = decide_cli(decision_env, base_request())
    assert rc == 0, doc
    month = doc["resolved_at"][:7].replace("-", "")
    current = state / "decision-receipts" / f"{month}.jsonl"
    assert current.exists(), list((state / "decision-receipts").iterdir())
    # A neighbouring month file that does NOT contain this receipt.
    last_month = (datetime.now(timezone.utc) - timedelta(days=32)).strftime("%Y%m")
    if last_month == month:
        last_month = (datetime.now(timezone.utc) - timedelta(days=64)).strftime("%Y%m")
    neighbour = state / "decision-receipts" / f"{last_month}.jsonl"
    _append_ledger_rows(neighbour, 25, "neighbour")
    _append_ledger_rows(current, 20000, "current")

    import router_decision as rd
    parsed = []
    real_loads = rd.json.loads

    def counting_loads(text, *args, **kwargs):
        parsed.append(1)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(rd.json, "loads", counting_loads)

    with _opened_paths() as opened:
        first = rd.verify_receipt(doc, now=NOW, expect=EXPECT)
    assert first["ok"] is True, first
    ledger_reads = {p for p in opened if "decision-receipts" in p}
    assert ledger_reads == {str(current.resolve())}, \
        f"verify opened more than the receipt's own month: {sorted(ledger_reads)}"
    assert len(parsed) >= 20000, f"cold verify parsed only {len(parsed)} rows"

    parsed.clear()
    with _opened_paths() as opened:
        second = rd.verify_receipt(doc, now=NOW, expect=EXPECT)
    assert second["ok"] is True, second
    assert len(parsed) == 0, \
        f"a warm verify re-parsed {len(parsed)} ledger rows (no index)"
    assert not {p for p in opened if "decision-receipts" in p}, \
        "a warm verify re-opened the ledger it had already indexed"


# ---------------------------------------------------------------------------
# H1-F6 — signing-key lifecycle: age policy, key identity, file mode
# ---------------------------------------------------------------------------

def test_stale_signing_key_is_refused_after_policy_max_age(decision_env):
    """`RECEIPT_KEY_MAX_AGE_S` must be enforced, not merely declared (H1-F6).

    A key older than the policy ceiling is refused with a NAMED code on both
    paths — issuing a new receipt and verifying an existing one.
    """
    state = _state(decision_env)
    key_path = state / "decision-receipt.key"
    rc, receipt, _ = decide_cli(decision_env, base_request())
    assert rc == 0, receipt

    import router_decision as rd
    stale = time.time() - rd.RECEIPT_KEY_MAX_AGE_S - 1
    os.utime(key_path, (stale, stale))

    result = rd.verify_receipt(receipt, now=NOW, expect=EXPECT)
    assert result["ok"] is False, result
    assert "RECEIPT_KEY_EXPIRED" in _codes(result), result

    rc, doc, _ = decide_cli(decision_env, base_request(step_id="step-8"))
    assert rc != 0, doc
    assert doc.get("code") == "RECEIPT_KEY_EXPIRED", doc
    assert "receipt_digest" not in doc, doc


def test_receipt_names_its_signing_key_and_a_rotated_key_is_named(decision_env):
    """A receipt must carry a key id, so rotation is visible (H1-F6).

    Without one, a rotated key turns every outstanding receipt into a generic
    RECEIPT_SIGNATURE_INVALID — indistinguishable from forgery.
    """
    state = _state(decision_env)
    rc, receipt, _ = decide_cli(decision_env, base_request())
    assert rc == 0, receipt
    import router_decision as rd
    current = rd.load_receipt_key(create=False)
    assert receipt["key_id"] == rd.receipt_key_id(current), receipt["key_id"]
    assert isinstance(receipt["key_id"], str) and len(receipt["key_id"]) >= 16

    # Rotate: a NEW key material file (same shape, different bytes).
    (state / "decision-receipt.key").write_bytes(bytes(range(32)))
    result = rd.verify_receipt(receipt, now=NOW, expect=EXPECT)
    assert result["ok"] is False, result
    assert "RECEIPT_KEY_ID_MISMATCH" in _codes(result), result


def test_key_file_mode_must_not_be_group_or_world_readable(decision_env):
    """A key file whose permissions were loosened must be refused (H1-F6).

    The docstring promised 0600 but the mode was only set at creation; a
    `chmod 644` was read happily. It is now a named refusal, and the operator
    is told the exact repair.
    """
    state = _state(decision_env)
    key_path = state / "decision-receipt.key"
    rc, receipt, _ = decide_cli(decision_env, base_request())
    assert rc == 0, receipt
    key_path.chmod(0o644)

    import router_decision as rd
    result = rd.verify_receipt(receipt, now=NOW, expect=EXPECT)
    assert result["ok"] is False, result
    assert "RECEIPT_KEY_INSECURE_MODE" in _codes(result), result

    with pytest.raises(rd.ReceiptKeyError) as refused:
        rd.load_receipt_key(create=False)
    assert refused.value.code == "RECEIPT_KEY_INSECURE_MODE"
    assert "chmod 600" in refused.value.problem, refused.value.problem

    rc, doc, _ = decide_cli(decision_env, base_request(step_id="step-8"))
    assert rc != 0, doc
    assert doc.get("code") == "RECEIPT_KEY_INSECURE_MODE", doc

    # The repair restores service — the refusal is about the mode, not the key.
    key_path.chmod(0o600)
    result = rd.verify_receipt(receipt, now=NOW, expect=EXPECT)
    assert result["ok"] is True, result

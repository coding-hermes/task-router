"""TR-275 — API contract conformance: one schema, every served route.

The dispatch table (what the handler ACTUALLY answers) and the published
/openapi.json contract must be the same set, checked in BOTH directions, by
machine — not by a hand-written list that can rot:

  CONFORM-1  the served surface (enumerated from the code: dispatch()'s own
             AST — `path == "..."` literals, membership tests over module
             constants, startswith prefixes — plus the handler-level GET/POST
             intercepts in RouterHandler.do_GET/do_POST) and the schema are
             equal modulo exactly two pinned, deliberately-undocumented
             surfaces: POST /mcp (the JSON-RPC compatibility bridge whose
             retirement is open decision #1) and GET /ui/ (the alias spelling
             of the documented /ui page). A served route without a schema
             entry fails; a schema entry without a served route fails.
  CONFORM-2  every documented route is probed over HTTP with its DOCUMENTED
             method and must answer one of the status codes that operation
             itself documents — and the schema diff half runs with NO live
             server (pure AST + module introspection).
  CONFORM-3  the operator honesty fields (auth mode, loaded commit/source
             sha, surface mode, cache generation, admission, circuit state)
             are declared IN the schema with types — not only in prose.
  CONFORM-4  TR-140 caller honesty, pinned behaviorally: an UNdocumented path
             answers 404 "not found" — never the read-only 403 that made
             absence indistinguishable from forbidden.

The /v1 parity the row asks for is part of CONFORM-1: GET /v1/models,
GET /models, POST /v1/chat/completions and POST /v1/responses are served by
this process (handler intercept + PROXY_PATHS), so they must be in the schema
like every other route.
"""
import ast
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_server as rsrv  # noqa: E402

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

#: The served-but-undocumented surfaces, pinned EXACTLY. Each entry carries
#: its reason; a new route can only join this set by editing it (and saying
#: why) — silence is never an excuse for an undocumented surface.
DELIBERATE_UNDOCUMENTED = {
    ("GET", "/ui/"):
        "alias spelling of the documented GET /ui page (one page, two "
        "spellings); documenting both would double the operation for tooling",
    ("POST", "/mcp"):
        "JSON-RPC 2.0 compatibility bridge; outside the OpenAPI contract by "
        "the muster-scope decision, parity-guarded live by muster_battery.sh "
        "probe 6 until open decision #1 rules on its retirement",
}


# ---------------------------------------------------------------------------
# The machine-checked served-surface enumerator (CONFORM-1's left side)
# ---------------------------------------------------------------------------

def _is_path_target(node):
    """The compared value is the request path: `path` in dispatch(), or
    `parsed.path` in the HTTP handlers."""
    if isinstance(node, ast.Name) and node.id == "path":
        return True
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "parsed")


def _string_consts(node):
    """String constants out of a literal collection or a module-level
    tuple/list constant (e.g. PROXY_PATHS) — never a hand-written copy."""
    out = set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.add(node.value)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for elt in node.elts:
            out |= _string_consts(elt)
    elif isinstance(node, ast.Name):
        const = getattr(rsrv, node.id, None)
        if isinstance(const, (tuple, list, set)):
            out |= {v for v in const if isinstance(v, str)}
    return out


def _collect_path_comparisons(nodes):
    """(literal paths, startswith prefixes, {name: membership-literals}) in an
    AST node list — the material the dispatcher actually branches on."""
    literals, prefixes, name_sets = set(), set(), {}
    for node in ast.walk(ast.Module(body=list(nodes), type_ignores=[])):
        if isinstance(node, ast.Compare):
            if _is_path_target(node.left):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) \
                            and isinstance(comp.value, str):
                        literals.add(comp.value)
                    elif isinstance(op, ast.In):
                        literals |= _string_consts(comp)
            elif isinstance(node.left, ast.Name) \
                    and any(isinstance(op, ast.In) for op in node.ops):
                # e.g. `kind in {"provider", "model", "profile"}` — the
                # dynamic-prefix branch's own machine-readable value set.
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(op, ast.In):
                        values = _string_consts(comp)
                        if values:
                            name_sets.setdefault(node.left.id, set()).update(values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "startswith" \
                and _is_path_target(node.func.value) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                prefixes.add(arg.value)
    return literals, prefixes, name_sets


def _function_body(fn):
    src = textwrap.dedent(inspect.getsource(fn))
    return ast.parse(src).body[0].body


def _dispatch_halves():
    """dispatch()'s AST split at the `if method != "POST":` marker, so the GET
    and POST route sets are scanned separately."""
    src = textwrap.dedent(inspect.getsource(rsrv.RouterApplication.dispatch))
    tree = ast.parse(src).body[0]
    split_at = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) \
                and node.test.left.id == "method" \
                and any(isinstance(o, ast.NotEq) for o in node.test.ops):
            split_at = i
            break
    get_nodes = tree.body if split_at is None else tree.body[:split_at]
    post_nodes = [] if split_at is None else tree.body[split_at:]
    return get_nodes, post_nodes


def served_routes():
    """Every (method, path) this process answers, derived from the code:
    dispatch()'s branches, the handler-level intercepts, the PROXY_PATHS
    constant and the dynamic /listings/<kind> expansion (kinds read from
    dispatch()'s own membership literal, not copied by hand)."""
    get_lits, get_pfx, _ = _collect_path_comparisons(_dispatch_halves()[0])
    post_lits, post_pfx, name_sets = _collect_path_comparisons(
        _dispatch_halves()[1])
    hget_lits, hget_pfx, _ = _collect_path_comparisons(
        _function_body(rsrv.RouterHandler.do_GET))
    hpost_lits, hpost_pfx, _ = _collect_path_comparisons(
        _function_body(rsrv.RouterHandler.do_POST))
    routes = set()
    for path in get_lits | hget_lits:
        routes.add(("GET", path))
    for prefix in get_pfx | hget_pfx:
        routes.add((f"GET prefix:{prefix}", prefix))
    for path in post_lits | hpost_lits | set(rsrv.PROXY_PATHS):
        routes.add(("POST", path))
    for prefix in post_pfx | hpost_pfx:
        kinds = name_sets.get("kind")
        if prefix.rstrip("/") == "/listings" and kinds:
            for kind in kinds:
                routes.add(("POST", f"{prefix}{kind}"))
        else:
            routes.add((f"POST prefix:{prefix}", prefix))
    return routes


def documented_routes():
    """Every (method, path) the published contract declares."""
    spec = rsrv.build_openapi()
    documented = set()
    for path, path_item in spec["paths"].items():
        for method in path_item:
            if method.lower() in HTTP_METHODS:
                documented.add((method.upper(), path))
    return documented


# ---------------------------------------------------------------------------
# CONFORM-1: the bidirectional diff (no live server)
# ---------------------------------------------------------------------------

def test_openapi_dispatch_bidirectional_conformance():
    """The schema and the served surface are the same set, both directions."""
    served = served_routes()
    documented = documented_routes()

    # Direction 1: a route the handler serves without a schema entry fails —
    # except the two pinned, reasoned, deliberately-undocumented surfaces.
    undocumented = served - documented
    pinned = {route for route in undocumented if route in DELIBERATE_UNDOCUMENTED}
    unpinned = undocumented - pinned
    assert not unpinned, (
        f"served routes with NO schema entry (drifted dispatch table): "
        f"{sorted(unpinned)}; add them to build_openapi() or, only with a "
        f"written reason, to DELIBERATE_UNDOCUMENTED"
    )
    assert pinned == set(DELIBERATE_UNDOCUMENTED), (
        f"the deliberately-undocumented set drifted: pinned {sorted(pinned)} "
        f"!= declared {sorted(DELIBERATE_UNDOCUMENTED)}"
    )
    # The pinned exceptions must still be REAL served routes (a stale pin
    # must not mask a removed surface).
    assert set(DELIBERATE_UNDOCUMENTED) <= served, (
        f"DELIBERATE_UNDOCUMENTED names routes the server no longer serves: "
        f"{sorted(set(DELIBERATE_UNDOCUMENTED) - served)}; prune the pins"
    )

    # Direction 2: a schema entry without a served route fails.
    phantom = documented - served
    assert not phantom, (
        f"schema entries the handler never dispatches: {sorted(phantom)}; "
        f"the contract promises routes that do not exist"
    )


def test_openapi_v1_parity_for_openai_shaped_callers():
    """The /v1 surface an OpenAI-shaped caller probes is IN the contract:
    the model list (both spellings) and both proxy POSTs."""
    served = served_routes()
    for route in (("GET", "/v1/models"), ("GET", "/models"),
                  ("POST", "/v1/chat/completions"), ("POST", "/v1/responses")):
        assert route in served, f"{route} is served but the enumerator missed it"
        assert route in documented_routes(), (
            f"{route} must be in /openapi.json (TR-275 /v1 parity)")


# ---------------------------------------------------------------------------
# CONFORM-3: operator honesty fields with types (no live server)
# ---------------------------------------------------------------------------

def _schema_type(schema):
    types = schema.get("type")
    return {types} if isinstance(types, str) else set(types or [])


def test_openapi_documents_operator_honesty_fields():
    """mode / loaded commit + source sha / cache generation / admission /
    circuit state are declared on GET /health WITH types, not only in prose."""
    spec = rsrv.build_openapi()
    schema = (spec["paths"]["/health"]["get"]["responses"]["200"]
              ["content"]["application/json"]["schema"])
    props = schema.get("properties", {})

    assert "string" in _schema_type(props["mode"]), props.get("mode")
    assert "string" in _schema_type(props["commit"])
    assert "string" in _schema_type(props["source_sha"])
    assert "string" in _schema_type(props["surface_mode"])
    assert "integer" in _schema_type(props["cache_generation"])

    code_props = props["code"].get("properties", {})
    assert "string" in _schema_type(code_props["loaded_commit"])
    assert "string" in _schema_type(code_props["loaded_source_sha"])

    admission = props["admission"]
    assert "object" in _schema_type(admission)
    admission_props = admission.get("properties", {})
    for field in ("inflight", "max_inflight", "max_in_flight",
                  "current_in_flight", "queue_max"):
        assert field in admission_props, f"admission.{field} untyped"
        assert "integer" in _schema_type(admission_props[field])

    circuit = props["circuit"]
    assert "object" in _schema_type(circuit)
    circuit_props = circuit.get("properties", {})
    for field in ("pairs", "open", "open_pairs"):
        assert field in circuit_props, f"circuit.{field} untyped"
    assert "integer" in _schema_type(circuit_props["pairs"])
    assert "integer" in _schema_type(circuit_props["open"])

    # The fields are PROMISED, not merely allowed: a payload without them
    # would not validate against the server's own contract.
    for field in ("mode", "commit", "code", "source_sha", "cache_generation",
                  "admission", "circuit"):
        assert field in schema.get("required", []), (
            f"/health schema must REQUIRE {field} (the handler always "
            f"stamps it; prose-only honesty is what TR-275 removes)")

    # The operator surfaces that split the same fields across endpoints.
    status_props = (spec["paths"]["/status"]["get"]["responses"]["200"]
                    ["content"]["application/json"]["schema"]["properties"])
    assert "string" in _schema_type(status_props["mode"])
    circuit_status_props = (spec["paths"]["/circuit/status"]["get"]
                            ["responses"]["200"]
                            ["content"]["application/json"]["schema"]
                            ["properties"])
    assert "array" in _schema_type(circuit_status_props["pairs"])


# ---------------------------------------------------------------------------
# CONFORM-2 + CONFORM-4: live probe of every documented route (hermetic)
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, path, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, _maybe_json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read())


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


@pytest.fixture
def probe_server(tmp_path):
    """Hermetic read-only server (the test_server.py pattern: a test run
    must never touch the live data/state)."""
    data_dir = tmp_path / "tables"
    shutil.copytree(os.path.join(REPO, "data", "tables"), data_dir)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    providers = [
        json.loads(line)["id"]
        for line in (data_dir / "providers.jsonl").read_text().splitlines()
        if line.strip()
    ]
    (state_dir / "quota-state.json").write_text(
        json.dumps({"providers": {p: {"status": "open"} for p in providers}}))
    env = {
        **os.environ,
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTING_REGISTRY": str(tmp_path / "missing-registry.json"),
        "ROUTER_STATE_DIR": str(state_dir),
        "LEDGER_FILE": str(state_dir / "ledger.jsonl"),
        "TASK_ROUTER_HOME": str(tmp_path / "router-home"),
        "ROUTING_OUTCOMES_FILE": str(tmp_path / "outcomes.jsonl"),
        "ROUTING_AVERAGES_FILE": str(tmp_path / "outcomes-averages.jsonl"),
    }
    env.pop("ROUTER_EDIT_API_KEY", None)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "scripts", "router_server.py"),
         "--mode", "read-only", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(
                    f"probe server exited {proc.returncode}: {out} {err}")
            try:
                status, _ = _request(port, "/status")
                if status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("probe server did not become ready")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_openapi_dispatch_route_probe(probe_server):
    """Every documented route, probed with its DOCUMENTED method, answers a
    status that operation itself documents (CONFORM-2). Read-only mode is the
    probe's auth posture: reads answer their real status, mutations answer
    the documented 403 refusal — never a 404, which would mean the route is
    not actually dispatched."""
    spec = rsrv.build_openapi()
    operations = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS:
                operations.append((method.upper(), path, operation))
    assert len(operations) >= 40, (
        f"the contract shrank: only {len(operations)} operations; the TR-275 "
        f"revision documented 41 — a shrink must be a deliberate spec change")

    problems = []
    for method, path, operation in operations:
        documented_codes = {int(code) for code in operation["responses"]}
        body = None
        if method == "POST":
            body = {}   # read-only mode refuses at the gate before any body matters
        try:
            status, _payload = _request(probe_server, path, method=method,
                                        body=body)
        except Exception as exc:  # noqa: BLE001 — a probe transport failure IS a finding
            problems.append(f"{method} {path}: transport failure {exc!r}")
            continue
        if status not in documented_codes:
            problems.append(
                f"{method} {path}: answered {status}, the operation documents "
                f"{sorted(documented_codes)}")
        if status == 404:
            problems.append(f"{method} {path}: documented route answered 404 "
                            f"(not actually dispatched)")
    assert not problems, "documented routes failed their probe:\n" + "\n".join(problems)


def test_openapi_dispatch_undocumented_paths_answer_404(probe_server):
    """TR-140 caller honesty, behaviorally: an unknown path answers 404
    'not found' in every mode — never the read-only 403 that made 'absent'
    indistinguishable from 'forbidden' (CONFORM-4)."""
    for method in ("GET", "POST"):
        status, payload = _request(
            probe_server, "/definitely-not-a-tr275-route", method=method,
            body={} if method == "POST" else None)
        assert status == 404, (method, status, payload)
        assert isinstance(payload, dict) and payload.get("error") == "not found"
        assert payload.get("error") != "read-only mode"

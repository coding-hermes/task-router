"""TR-127: muster-consumer parity between the OpenAPI contract and the server.

The muster spec (docs/muster-spec.md) freezes ONE canonical artifact — the live
`GET /openapi.json` served by `build_openapi()` — and one rule: the contract a
consumer sees must be the contract the server dispatches. These tests are that
parity rule, executable, hermetic (no network, no muster binaries):

  P1  the served spec is exactly build_openapi() (spec is generated in code,
      not read from a file that can rot);
  P2  every documented operation has a unique, non-empty operationId (muster
      turns operationIds into MCP tool names — a gap or collision here is a
      broken consumer surface);
  P3  every documented path has a real dispatch branch (spec vs dispatch
      table parity, source-derived: dispatch() is an if-chain over `path ==
      "..."` literals / `path.startswith(...)` prefixes);
  P4  documented POSTs are admitted through the known-POST gate
      (known_post_paths(), the actual admission check in dispatch());
  P5  the UNdocumented surfaces within dispatch()'s own branches are NONE
      since TR-275 (the /v1 proxy paths and /readyz joined the contract);
      /mcp remains a handler-level intercept outside the OpenAPI contract,
      parity-guarded live by scripts/muster_battery.sh probe 6 until open
      decision #1 rules on its retirement — the full-surface bidirectional
      diff (handler intercepts included) lives in
      tests/test_openapi_dispatch_conformance.py;
  P6  the read/mutation security boundary: exactly the POST operations carry
      op-level EditApiKey security, no read does, and there is no global
      security block (loopback binding is the reads' entire access control);
  P7  behaviorally, a read-only app refuses a mutation with 403 before any
      handler runs (the server-side gate probe 5 asserts through muster).

Live surface note (TR-275, 2026-11): the API has grown past the muster-scope
doc's 19 operations — 41 today (30 reads, 11 mutations; TR-275 brought the
/v1 interop surface (GET /v1/models, GET /models, POST /v1/chat/completions,
POST /v1/responses) and GET /readyz INTO the contract, typed the operator
honesty fields, and moved the full-surface bidirectional diff to
tests/test_openapi_dispatch_conformance.py). The boundary RULE is unchanged;
these tests pin the live truth, and any count change must be a deliberate
spec revision, not drift.
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

PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
SERVER = os.path.join(REPO, "scripts", "router_server.py")

#: The 8 mutation operations (op-level EditApiKey). The muster-scope doc's 7
#: plus postUiRegistryEdit (TR-155), which postdates it.
MUTATION_OPS = {
    "recordCircuit",
    "postUiRegistryEdit",
    "startLedger",
    "endLedger",
    "ingestOutcome",
    "updateProviderListing",
    "updateModelListing",
    "updateProfileListing",
    # H1-F4 (2026-10-26): issuing a route receipt appends a durable ledger row,
    # so the decision endpoint is a MUTATION, not the read the old
    # `security: []` label claimed. Deliberate spec revision, not drift.
    "postExecutionDecision",
    # TR-275 (2026-11): the /v1 proxy calls append outcome/ledger rows per
    # hop and are auth-gated exactly like the other mutations (read-only
    # 403, edit mode X-API-Key; ROUTER_PROXY_AUTH=passthrough is the
    # documented delegation). Deliberate spec revision, not drift.
    "proxyChatCompletions",
    "proxyResponses",
}


def _spec_operations(spec=None):
    """(method_upper, path, operation) for every operation in the spec."""
    spec = spec if spec is not None else rsrv.build_openapi()
    ops = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                ops.append((method.upper(), path, operation))
    return ops


def _path_literals(nodes):
    """`path == "..."` / `parsed.path == "..."` literals,
    `path.startswith(...)` prefixes, and membership tests over module-level
    string constants (`path in PROXY_PATHS` — TR-275: the /v1 proxy surface
    routes through that membership, so the scan must resolve it) in a
    dispatch branch body."""
    def _is_path_target(node):
        if isinstance(node, ast.Name) and node.id == "path":
            return True
        return (isinstance(node, ast.Attribute) and node.attr == "path"
                and isinstance(node.value, ast.Name)
                and node.value.id == "parsed")

    def _constants(node):
        out = set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for elt in node.elts:
                out |= _constants(elt)
        elif isinstance(node, ast.Name):
            const = getattr(rsrv, node.id, None)
            if isinstance(const, (tuple, list, set)):
                out |= {v for v in const if isinstance(v, str)}
        return out

    literals, prefixes = set(), set()
    for node in ast.walk(ast.Module(body=nodes, type_ignores=[])):
        if isinstance(node, ast.Compare) and _is_path_target(node.left):
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) \
                        and isinstance(comp.value, str):
                    literals.add(comp.value)
                elif isinstance(op, ast.In):
                    literals |= _constants(comp)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "startswith" \
                and _is_path_target(node.func.value) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                prefixes.add(arg.value)
    return literals, prefixes


def _dispatch_branch_literals():
    """(GET literals/prefixes, POST literals/prefixes) in
    RouterApplication.dispatch, split at the `if method != "POST":` marker so
    the GET half and the POST half are scanned separately. dispatch() routes
    GETs through `if path == "..."` chains and POSTs through
    literals/prefixes, so the route table IS the source."""
    src = textwrap.dedent(inspect.getsource(rsrv.RouterApplication.dispatch))
    fn = ast.parse(src).body[0]
    split_at = None
    for i, node in enumerate(fn.body):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) \
                and node.test.left.id == "method" \
                and any(isinstance(o, ast.NotEq) for o in node.test.ops):
            split_at = i
            break
    get_nodes = fn.body if split_at is None else fn.body[:split_at]
    post_nodes = [] if split_at is None else fn.body[split_at:]
    return _path_literals(get_nodes), _path_literals(post_nodes)


#: Handler-level GET intercepts in RouterHandler.do_GET that serve BEFORE
#: dispatch() runs. '/ui' + '/ui/' serve the documented Data Command Center
#: (getUi) as HTML; '/v1/models' + '/models' are the TR-interop OpenAI
#: client-probe surface, deliberately outside the OpenAPI contract.
HANDLER_GET_INTERCEPTS = {"/ui", "/ui/", "/v1/models", "/models"}


# ---------------------------------------------------------------------------
# P1/P2: metadata, operationIds
# ---------------------------------------------------------------------------

def test_spec_metadata_and_absolute_server_url():
    spec = rsrv.build_openapi()
    assert spec["openapi"] == "3.1.0"  # muster's kin-openapi parses 3.1
    assert spec["info"]["title"] == "Task Router API"
    servers = spec.get("servers") or []
    assert servers, "spec must declare servers so muster can resolve the base URL"
    url = servers[0]["url"]
    assert url.startswith(("http://", "https://")), url
    # The frozen loopback decision: reads have NO auth; loopback binding IS
    # the access control (muster-scope §6). A remote bind is a new decision.
    assert url == "http://127.0.0.1:9092"


def test_every_operation_has_unique_nonempty_operation_id():
    ids = [op["operationId"] for _m, _p, op in _spec_operations()]
    assert ids, "spec must declare operations"
    for op_id in ids:
        assert isinstance(op_id, str) and op_id.strip(), op_id
    assert len(ids) == len(set(ids)), "duplicate operationId"


# ---------------------------------------------------------------------------
# P3/P4/P5: dispatch-table parity census
# ---------------------------------------------------------------------------

def test_documented_paths_have_dispatch_branches():
    (get_lits, get_pfx), (post_lits, post_pfx) = _dispatch_branch_literals()

    def _covered(method, path):
        if method == "GET":
            if path in HANDLER_GET_INTERCEPTS:
                return True
            lits, pfx = get_lits, get_pfx
        else:
            lits, pfx = post_lits, post_pfx
        return path in lits or any(path.startswith(p) for p in pfx)

    uncovered = [
        (method, path)
        for method, path, _op in _spec_operations()
        if not _covered(method, path)
    ]
    assert not uncovered, (
        f"documented but not dispatched: {uncovered}; "
        "the served contract and the dispatch table drifted"
    )


def test_documented_post_paths_are_admitted_through_known_post_gate():
    spec_posts = {path for method, path, _op in _spec_operations()
                  if method == "POST"}
    assert spec_posts, "no POST operations in spec"
    admitted = set(rsrv.known_post_paths())
    missing = spec_posts - admitted
    assert not missing, (
        f"documented POSTs the server would 404: {sorted(missing)}"
    )


def test_undocumented_surfaces_are_exactly_the_named_ones():
    """TR-275 revision: the /v1 proxy paths and /readyz are now DOCUMENTED
    (they are real served routes), so within dispatch()'s own branch set
    there is NO served-but-undocumented route left in either direction. The
    handler-level truth (GET /ui/, GET /v1/models, GET /models, POST /mcp
    intercepts) is enumerated and diffed bidirectionally by
    tests/test_openapi_dispatch_conformance.py, which pins the two
    deliberately-undocumented surfaces (POST /mcp, GET /ui/) with reasons."""
    (get_lits, _p), (post_lits, _q) = _dispatch_branch_literals()
    spec_paths = {path for _m, path, _op in _spec_operations()}
    get_extras = get_lits - spec_paths
    assert get_extras == set(), sorted(get_extras)
    admitted = set(rsrv.known_post_paths())
    post_extras = admitted - spec_paths
    assert post_extras == set(), sorted(post_extras)
    # /mcp is a handler-level intercept (RouterHandler.do_POST), NOT in
    # known_post_paths() and NOT in the contract — a compatibility surface
    # whose retirement is open decision #1, parity-guarded live by
    # scripts/muster_battery.sh probe 6.
    assert "/mcp" not in admitted
    assert "/mcp" not in spec_paths


# ---------------------------------------------------------------------------
# P6/P7: the read/mutation security boundary
# ---------------------------------------------------------------------------

def test_security_boundary_reads_vs_mutations():
    spec = rsrv.build_openapi()
    ops = _spec_operations(spec)
    secured = {op["operationId"] for _m, _p, op in ops if op.get("security")}
    assert secured == MUTATION_OPS, (
        "mutation set drifted; update MUTATION_OPS via a deliberate "
        f"spec revision: {sorted(secured)}"
    )
    for method, path, op in ops:
        if op["operationId"] in MUTATION_OPS:
            assert method == "POST", (method, path)
            assert op["security"] == [{"EditApiKey": []}], path
        else:
            assert not op.get("security"), (
                f"read {path} must stay keyless (loopback IS the auth)"
            )
    assert not spec.get("security"), "no global security: auth is op-level"
    scheme = (spec.get("components", {}).get("securitySchemes", {})
              .get("EditApiKey"))
    assert scheme == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": scheme["description"] if scheme else None,
    } and scheme["name"] == "X-API-Key" and scheme["in"] == "header" \
        and scheme["type"] == "apiKey"


def test_read_only_app_refuses_mutation_before_any_handler():
    app = rsrv.RouterApplication(mode="read-only", edit_key="")
    status, payload = app.dispatch(
        "POST", "/circuit/record",
        body={"provider": "p", "model": "m", "outcome": "failure"},
        headers={},
    )
    assert status == 403
    # TR-276: the body gained the `reason` field (WHICH mode read-only is and
    # where a mutation is accepted); the muster battery greps the pinned
    # "read-only mode" text, so the exact legacy string stays in `error`.
    assert payload["error"] == "read-only mode"
    assert isinstance(payload.get("reason"), str) and payload["reason"]


# ---------------------------------------------------------------------------
# P1 behavioral: the SERVED spec is build_openapi()
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.fixture
def server_env(tmp_path):
    """Hermetic server env — the test_server.py pattern (TR-049: a test run
    must never be able to write the live data/state)."""
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


def test_served_openapi_equals_build_openapi(server_env):
    port = _free_port()
    env = dict(server_env)
    env.pop("ROUTER_EDIT_API_KEY", None)
    proc = subprocess.Popen(
        [PY, SERVER, "--mode", "read-only",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
        status, served = _request(port, "/openapi.json")
        assert status == 200
        assert served == rsrv.build_openapi(), (
            "GET /openapi.json must serve build_openapi() byte-for-byte: "
            "the live URL is the canonical contract, generated in code"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

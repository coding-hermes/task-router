"""TR-018 OpenAPI server and native OpenAPI-to-MCP bridge tests."""

import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

REPO = Path(__file__).resolve().parents[1]
PY = "/home/kara/.hermes/venvs/board/bin/python3"
SERVER = REPO / "scripts" / "router_server.py"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, path, *, method="GET", body=None, key=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["X-API-Key"] = key
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.fixture
def server_env(tmp_path):
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
    }


@contextlib.contextmanager
def _server(server_env, mode="read-only", key=None):
    port = _free_port()
    env = dict(server_env)
    if key is None:
        env.pop("ROUTER_EDIT_API_KEY", None)
    else:
        env["ROUTER_EDIT_API_KEY"] = key
    proc = subprocess.Popen(
        [PY, str(SERVER), "--mode", mode, "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                pytest.fail(f"server exited {proc.returncode}: {stdout} {stderr}")
            try:
                status, _ = _request(port, "/status")
                if status == 200:
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


def test_openapi_schema_and_read_endpoints(server_env):
    with _server(server_env) as port:
        status, schema = _request(port, "/openapi.json")
        assert status == 200
        assert schema["openapi"] == "3.1.0"
        assert schema["info"]["title"] == "Task Router API"
        operation_ids = []
        for path_item in schema["paths"].values():
            for method, operation in path_item.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    operation_ids.append(operation["operationId"])
                    assert operation["responses"]
        assert len(operation_ids) == len(set(operation_ids))
        assert len(operation_ids) > 10

        for path in (
            "/status",
            "/profiles",
            "/providers",
            "/circuit/status",
            "/gaps",
            "/pricing",
            "/chains",
        ):
            code, payload = _request(port, path)
            assert code == 200, (path, payload)
            assert isinstance(payload, (dict, list))


def test_edit_mode_requires_configured_key_and_auth_gate(server_env):
    env = dict(server_env)
    env.pop("ROUTER_EDIT_API_KEY", None)
    proc = subprocess.run(
        [PY, str(SERVER), "--mode", "edit", "--port", str(_free_port())],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "ROUTER_EDIT_API_KEY" in proc.stderr

    body = {"provider": "tr018-test", "model": "m1", "outcome": "failure"}
    with _server(server_env, mode="read-only") as port:
        assert _request(port, "/circuit/record", method="POST", body=body)[0] == 403
    with _server(server_env, mode="edit", key="secret123") as port:
        assert _request(port, "/circuit/record", method="POST", body=body)[0] == 401
        assert (
            _request(port, "/circuit/record", method="POST", body=body, key="wrong")[0]
            == 401
        )
        code, payload = _request(
            port, "/circuit/record", method="POST", body=body, key="secret123"
        )
        assert code == 200 and payload["ok"] is True
        _, circuits = _request(port, "/circuit/status")
        assert any(row["pair"] == "tr018-test/m1" for row in circuits["pairs"])


def test_ledger_and_listing_mutation_round_trips(server_env):
    key = "secret123"
    with _server(server_env, mode="edit", key=key) as port:
        code, started = _request(
            port,
            "/ledger/start",
            method="POST",
            key=key,
            body={"provider": "prov-a", "model": "model-a", "project": "my-project"},
        )
        assert code == 200 and started["trace_id"].startswith("tr-")
        code, ended = _request(
            port,
            "/ledger/end",
            method="POST",
            key=key,
            body={
                "trace_id": started["trace_id"],
                "outcome": "success",
                "latency_ms": 7,
            },
        )
        assert code == 200 and ended["trace_id"] == started["trace_id"]

        mutations = [
            ("provider", {"id": "tr018-provider", "plan": "test", "archive": False}),
            (
                "model",
                {
                    "provider": "tr018-provider",
                    "model": "tr018-model",
                    "normalized_price": 0.1,
                    "archive": False,
                },
            ),
            ("profile", {"id": "TR018_PROFILE", "title": "TR-018 test profile"}),
        ]
        for kind, row in mutations:
            code, payload = _request(
                port, f"/listings/{kind}", method="POST", key=key, body=row
            )
            assert code == 200 and payload["appended"] is True

        _, providers = _request(port, "/providers")
        _, profiles = _request(port, "/profiles")
        assert any(row["id"] == "tr018-provider" for row in providers["providers"])
        assert any(row["id"] == "TR018_PROFILE" for row in profiles["profiles"])
        data_dir = Path(server_env["ROUTING_DATA_DIR"])
        assert (
            json.loads((data_dir / "models.jsonl").read_text().splitlines()[-1])[
                "model"
            ]
            == "tr018-model"
        )


def _rpc(port, method, params=None, *, rpc_id=1, key=None):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return _request(port, "/mcp", method="POST", body=body, key=key)


def test_mcp_lists_openapi_tools_and_calls_resolve_and_mutation(server_env):
    key = "secret123"
    with _server(server_env, mode="edit", key=key) as port:
        code, init = _rpc(port, "initialize")
        assert code == 200
        assert init["result"]["serverInfo"]["name"] == "task-router-openapi-mcp"

        _, listed = _rpc(port, "tools/list", rpc_id=2)
        tools = listed["result"]["tools"]
        assert len(tools) > 5
        names = {tool["name"] for tool in tools}
        assert {"resolve", "recordCircuit"} <= names

        _, resolved = _rpc(
            port,
            "tools/call",
            {"name": "resolve", "arguments": {"project": "my-project"}},
            rpc_id=3,
        )
        result = resolved["result"]["structuredContent"]
        assert result["project"] == "my-project"
        assert "error" not in result

        _, mutated = _rpc(
            port,
            "tools/call",
            {
                "name": "recordCircuit",
                "arguments": {
                    "provider": "tr018-mcp",
                    "model": "m1",
                    "outcome": "failure",
                },
            },
            rpc_id=4,
            key=key,
        )
        assert mutated["result"]["isError"] is False
        _, circuits = _request(port, "/circuit/status")
        assert any(row["pair"] == "tr018-mcp/m1" for row in circuits["pairs"])

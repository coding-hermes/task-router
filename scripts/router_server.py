#!/usr/bin/env python3
"""Task Router JSON API and minimal OpenAPI-to-MCP bridge (TR-018).

The server is intentionally stdlib-only. Read-only mode is the default. Edit
mode fails closed unless ROUTER_EDIT_API_KEY is configured; mutating HTTP and
MCP tool calls then require the same value in X-API-Key.
"""

import argparse
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DATA_DIR = Path(os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
DOCS_DIR = Path(os.environ.get("ROUTING_DOCS_DIR", REPO / "docs"))
MAX_BODY_BYTES = 1024 * 1024

JSON_RESPONSE = {
    "description": "JSON response",
    "content": {"application/json": {"schema": {"type": "object"}}},
}
ERROR_RESPONSES = {
    "400": {"description": "Invalid request", "content": JSON_RESPONSE["content"]},
    "401": {
        "description": "Invalid or missing API key",
        "content": JSON_RESPONSE["content"],
    },
    "403": {"description": "Server is read-only", "content": JSON_RESPONSE["content"]},
    "500": {"description": "Fail-open JSON error", "content": JSON_RESPONSE["content"]},
}


def _body_schema(properties, required):
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": True,
                }
            }
        },
    }


def _post_operation(operation_id, summary, properties, required):
    return {
        "operationId": operation_id,
        "summary": summary,
        "security": [{"EditApiKey": []}],
        "requestBody": _body_schema(properties, required),
        "responses": {"200": JSON_RESPONSE, **ERROR_RESPONSES},
    }


def build_openapi():
    """Return the static OpenAPI 3.1 contract used by HTTP and MCP."""
    string = {"type": "string"}
    integer = {"type": "integer"}
    outcome = {"type": "string", "enum": ["failure", "success"]}
    ledger_outcome = {"type": "string", "enum": ["success", "failure", "error"]}
    get_paths = {
        "/openapi.json": ("getOpenAPI", "Get the OpenAPI 3.1 schema", []),
        "/status": ("getStatus", "Server and registry status", []),
        "/profiles": ("listProfiles", "List task profiles", []),
        "/providers": ("listProviders", "List providers", []),
        "/circuit/status": ("getCircuitStatus", "List circuit breaker state", []),
        "/gaps": ("getGaps", "Get registry quality gaps", []),
        "/pricing": ("getPricing", "Get normalized pricing dry-run", []),
        "/chains": ("getChains", "Get the latest chain snapshot", []),
        "/resolve": (
            "resolve",
            "Resolve a project to a routed model chain",
            [
                {
                    "name": "project",
                    "in": "query",
                    "required": True,
                    "schema": string,
                    "description": "Registry project id",
                }
            ],
        ),
    }
    paths = {}
    for path, (operation_id, summary, parameters) in get_paths.items():
        paths[path] = {
            "get": {
                "operationId": operation_id,
                "summary": summary,
                "parameters": parameters,
                "responses": {
                    "200": JSON_RESPONSE,
                    "400": ERROR_RESPONSES["400"],
                    "500": ERROR_RESPONSES["500"],
                },
            }
        }

    paths["/circuit/record"] = {
        "post": _post_operation(
            "recordCircuit",
            "Record circuit failure or success",
            {"provider": string, "model": string, "outcome": outcome, "reason": string},
            ["provider", "model", "outcome"],
        )
    }
    paths["/ledger/start"] = {
        "post": _post_operation(
            "startLedger",
            "Start a routed-call ledger trace",
            {
                "provider": string,
                "model": string,
                "project": string,
                "profile": string,
                "hop": integer,
                "requested_pair": string,
                "reason": string,
                "trace_id": string,
            },
            ["provider", "model"],
        )
    }
    paths["/ledger/end"] = {
        "post": _post_operation(
            "endLedger",
            "End a routed-call ledger trace",
            {
                "trace_id": string,
                "outcome": ledger_outcome,
                "latency_ms": integer,
                "error_class": string,
                "tokens_in": integer,
                "tokens_out": integer,
                "reason": string,
            },
            ["trace_id", "outcome"],
        )
    }
    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["number", "null"]}
    listing_operations = (
        (
            "provider",
            "updateProviderListing",
            ["id"],
            {
                "id": string,
                "plan": string,
                "quota_unit": string,
                "windows": string,
                "concurrency": {"type": ["integer", "null"]},
                "tos_class": string,
                "data_class": string,
                "valid_from": nullable_string,
                "valid_to": nullable_string,
                "archive": {"type": "boolean"},
            },
        ),
        (
            "model",
            "updateModelListing",
            ["provider", "model"],
            {
                "provider": string,
                "model": string,
                "normalized_price": nullable_number,
                "price_evidence": string,
                "public_price": nullable_number,
                "public_in_per_m": nullable_number,
                "public_out_per_m": nullable_number,
                "data_class": string,
                "plan_tier": {"type": ["integer", "null"]},
                "valid_from": nullable_string,
                "valid_to": nullable_string,
                "archive": {"type": "boolean"},
                "token_factor": {"type": "number"},
                "disabled": {"type": "boolean"},
                "disabled_reason": string,
            },
        ),
        (
            "profile",
            "updateProfileListing",
            ["id"],
            {
                "id": string,
                "title": string,
                "created_at": nullable_string,
                "max_consecutive_per_provider": {"type": ["integer", "null"]},
                "max_total_per_provider": {"type": ["integer", "null"]},
            },
        ),
    )
    for kind, operation_id, required, properties in listing_operations:
        paths[f"/listings/{kind}"] = {
            "post": _post_operation(
                operation_id,
                f"Append a validated {kind} listing update",
                properties,
                required,
            )
        }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Task Router API",
            "version": "1.0.0",
            "description": "Fail-open routing reads with API-key-gated listing and state edits.",
        },
        "servers": [{"url": "http://127.0.0.1:9092"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "EditApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Required for mutations when launched in edit mode.",
                }
            }
        },
    }


OPENAPI = build_openapi()


def _read_jsonl(name):
    path = DATA_DIR / f"{name}.jsonl"
    rows = []
    try:
        with path.open() as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name} line {number}: {exc}") from exc
                if isinstance(row, dict):
                    rows.append(row)
        return rows
    except FileNotFoundError:
        return []


def _subprocess_json(script, args, timeout=60):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *[str(arg) for arg in args]],
        cwd=REPO,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"{script}: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{script} returned invalid JSON: {proc.stdout[:200]}"
        ) from exc


def _subprocess_text(script, args, timeout=60):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *[str(arg) for arg in args]],
        cwd=REPO,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"{script}: {detail}")
    return proc.stdout.strip()


def _require_object(body):
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _required_strings(body, *names):
    for name in names:
        if not isinstance(body.get(name), str) or not body[name].strip():
            raise ValueError(f"{name} must be a non-empty string")


def _listing_spec(kind):
    return {
        "provider": ("providers", ("id",)),
        "model": ("models", ("provider", "model")),
        "profile": ("task_profiles", ("id",)),
    }[kind]


def _append_listing(kind, body):
    """Validate against the table shape and append one crash-visible JSONL row."""
    table, required = _listing_spec(kind)
    body = dict(_require_object(body))
    _required_strings(body, *required)
    existing = _read_jsonl(table)
    known = set().union(*(row.keys() for row in existing)) if existing else set(body)
    unknown = sorted(set(body) - known)
    if unknown:
        raise ValueError(f"unknown {kind} fields: {', '.join(unknown)}")
    path = DATA_DIR / f"{table}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    state_dir = Path(
        os.environ.get("ROUTER_STATE_DIR", Path.home() / ".hermes" / "model-router")
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "listing-updates.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with path.open("a") as handle:
            handle.write(json.dumps(body) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"appended": True, "listing": kind, "row": body}


def _latest_chains():
    snapshots = sorted(DOCS_DIR.glob("chains-*.md"), reverse=True)
    if snapshots:
        latest = snapshots[0]
        return {"source": str(latest), "snapshot": latest.read_text()}
    registry = Path(os.environ.get("ROUTING_REGISTRY", REPO / "registry.json"))
    try:
        doc = json.loads(registry.read_text())
        return {
            "source": str(registry),
            "generated_at": doc.get("generated_at"),
            "tables": sorted((doc.get("tables") or {}).keys()),
        }
    except Exception as exc:
        return {"error": f"no chain snapshot available: {exc}"}


class RouterApplication:
    def __init__(self, mode, edit_key):
        self.mode = mode
        self.edit_key = edit_key
        self.openapi = OPENAPI
        self.operations = self._operation_map()

    def _operation_map(self):
        result = {}
        for path, path_item in self.openapi["paths"].items():
            for method, operation in path_item.items():
                if method.lower() in {"get", "post", "put", "patch", "delete"}:
                    result[operation["operationId"]] = (method.upper(), path, operation)
        return result

    def _authorize(self, headers):
        if self.mode == "read-only":
            return 403, {"error": "read-only mode"}
        supplied = headers.get("x-api-key", "")
        if not supplied or not hmac.compare_digest(supplied, self.edit_key):
            return 401, {"error": "unauthorized"}
        return None

    def dispatch(self, method, path, query=None, body=None, headers=None):
        query = query or {}
        headers = headers or {}
        if method == "GET":
            if path == "/openapi.json":
                return 200, self.openapi
            if path == "/status":
                return 200, {
                    "status": "ok",
                    "mode": self.mode,
                    "data_dir": str(DATA_DIR),
                    "profiles": len(_read_jsonl("task_profiles")),
                    "providers": len(_read_jsonl("providers")),
                    "models": len(_read_jsonl("models")),
                }
            if path == "/resolve":
                project = query.get("project")
                if isinstance(project, list):
                    project = project[0] if project else None
                if not project:
                    return 400, {"error": "project query parameter is required"}
                return 200, _subprocess_json(
                    "router_spawn.py", [project, "--format", "json"]
                )
            if path == "/profiles":
                return 200, {"profiles": _read_jsonl("task_profiles")}
            if path == "/providers":
                return 200, {"providers": _read_jsonl("providers")}
            if path == "/circuit/status":
                return 200, _subprocess_json("router_circuit.py", ["status", "--json"])
            if path == "/gaps":
                return 200, _subprocess_json("router_gaps.py", ["--json"])
            if path == "/pricing":
                return 200, _subprocess_json(
                    "router_pricing.py", ["--json", "--dry-run"]
                )
            if path == "/chains":
                return 200, _latest_chains()
            return 404, {"error": "not found"}

        if method != "POST":
            return 405, {"error": "method not allowed"}
        auth_error = self._authorize(headers)
        if auth_error:
            return auth_error
        body = _require_object(body)

        if path == "/circuit/record":
            _required_strings(body, "provider", "model", "outcome")
            outcome = body["outcome"]
            if outcome not in {"failure", "success"}:
                return 400, {"error": "outcome must be failure or success"}
            args = [f"record-{outcome}", body["provider"], body["model"]]
            if outcome == "failure" and body.get("reason"):
                args.append(body["reason"])
            output = _subprocess_text("router_circuit.py", args)
            return 200, {
                "ok": True,
                "provider": body["provider"],
                "model": body["model"],
                "outcome": outcome,
                "output": output,
            }
        if path == "/ledger/start":
            _required_strings(body, "provider", "model")
            args = ["start", "--provider", body["provider"], "--model", body["model"]]
            for field, flag in (
                ("project", "--project"),
                ("profile", "--profile"),
                ("hop", "--hop"),
                ("requested_pair", "--requested-pair"),
                ("reason", "--reason"),
                ("trace_id", "--trace-id"),
            ):
                if body.get(field) is not None:
                    args.extend([flag, body[field]])
            trace_id = _subprocess_text("router_ledger.py", args)
            return 200, {"trace_id": trace_id, "outcome": "started"}
        if path == "/ledger/end":
            _required_strings(body, "trace_id", "outcome")
            if body["outcome"] not in {"success", "failure", "error"}:
                return 400, {"error": "outcome must be success, failure, or error"}
            args = ["end", "--trace-id", body["trace_id"], "--outcome", body["outcome"]]
            for field, flag in (
                ("latency_ms", "--latency-ms"),
                ("error_class", "--error-class"),
                ("tokens_in", "--tokens-in"),
                ("tokens_out", "--tokens-out"),
                ("reason", "--reason"),
            ):
                if body.get(field) is not None:
                    args.extend([flag, body[field]])
            trace_id = _subprocess_text("router_ledger.py", args)
            return 200, {"trace_id": trace_id, "outcome": body["outcome"]}
        if path.startswith("/listings/"):
            kind = path.rsplit("/", 1)[-1]
            if kind in {"provider", "model", "profile"}:
                return 200, _append_listing(kind, body)
        return 404, {"error": "not found"}

    def tools(self):
        """Mechanically derive MCP tools from OpenAPI paths and operations."""
        tools = []
        for name, (_method, _path, operation) in self.operations.items():
            properties = {}
            required = []
            for parameter in operation.get("parameters", []):
                properties[parameter["name"]] = parameter.get("schema", {})
                if parameter.get("required"):
                    required.append(parameter["name"])
            request_schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            properties.update(request_schema.get("properties", {}))
            required.extend(request_schema.get("required", []))
            schema = {"type": "object", "properties": properties}
            if required:
                schema["required"] = list(dict.fromkeys(required))
            tools.append(
                {
                    "name": name,
                    "description": operation.get("summary", name),
                    "inputSchema": schema,
                }
            )
        return tools

    def call_tool(self, name, arguments, headers):
        if name not in self.operations:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        method, path, operation = self.operations[name]
        query = {}
        query_names = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query"
        }
        for key in query_names:
            if key in arguments:
                query[key] = arguments[key]
        body = None
        if operation.get("requestBody"):
            body = {
                key: value for key, value in arguments.items() if key not in query_names
            }
        return self.dispatch(method, path, query=query, body=body, headers=headers)

    def mcp(self, request, headers):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32600,
                "Invalid Request",
            )
        rpc_id = request.get("id")
        method = request.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "task-router-openapi-mcp",
                        "version": "1.0.0",
                    },
                }
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                if not isinstance(params, dict) or not params.get("name"):
                    raise ValueError("tools/call requires params.name")
                status, payload = self.call_tool(
                    params["name"], params.get("arguments") or {}, headers
                )
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": status >= 400 or "error" in payload,
                }
            else:
                return self._rpc_error(rpc_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except ValueError as exc:
            return self._rpc_error(rpc_id, -32602, str(exc))
        except Exception as exc:
            return self._rpc_error(rpc_id, -32603, str(exc))

    @staticmethod
    def _rpc_error(rpc_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }


class RouterHandler(BaseHTTPRequestHandler):
    server_version = "task-router/1.0"

    @property
    def app(self):
        return self.server.app

    def _headers(self):
        # HTTP field names are case-insensitive. BaseHTTPRequestHandler
        # canonicalizes X-API-Key to X-Api-Key, so normalize at the boundary.
        return {key.lower(): value for key, value in self.headers.items()}

    def _read_body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _send(self, status, payload):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            status, payload = self.app.dispatch(
                "GET",
                parsed.path,
                query=parse_qs(parsed.query),
                headers=self._headers(),
            )
            self._send(status, payload)
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            body = self._read_body()
            if parsed.path == "/mcp":
                self._send(200, self.app.mcp(body, self._headers()))
                return
            status, payload = self.app.dispatch(
                "POST",
                parsed.path,
                query=parse_qs(parsed.query),
                body=body,
                headers=self._headers(),
            )
            self._send(status, payload)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def log_message(self, format_, *args):
        print(
            f"router_server: {self.address_string()} {format_ % args}", file=sys.stderr
        )


class RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, RouterHandler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Task Router OpenAPI + MCP server")
    parser.add_argument("--mode", choices=("read-only", "edit"), default="read-only")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9092)
    args = parser.parse_args(argv)

    edit_key = os.environ.get("ROUTER_EDIT_API_KEY", "")
    if args.mode == "edit" and not edit_key:
        print(
            "router_server: ROUTER_EDIT_API_KEY is required in edit mode",
            file=sys.stderr,
        )
        return 2

    app = RouterApplication(args.mode, edit_key)
    server = RouterHTTPServer((args.host, args.port), app)
    actual_host, actual_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "status": "listening",
                "host": actual_host,
                "port": actual_port,
                "mode": args.mode,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

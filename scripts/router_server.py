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
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DATA_DIR = Path(os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
DOCS_DIR = Path(os.environ.get("ROUTING_DOCS_DIR", REPO / "docs"))
MAX_BODY_BYTES = 1024 * 1024

# TR-049: the outcome store lives in scripts/router_outcomes.py (the same
# module the averages CLI and the seed consume) — imported rather than
# re-implemented so the store path/env resolution cannot drift between the
# HTTP ingest and the batch tools.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import router_outcomes  # noqa: E402  (stdlib-only sibling script module)
import router_health  # noqa: E402  (TR-087 health plane)

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
        "/": ("getRoot", "Health + status surface (TR-087)", []),
        "/health": ("getHealth", "Control-plane health: identity, registry freshness, gate states (TR-087)", []),
        "/model_status": ("getModelStatus", "Per-model/provider status lookup: registry + probe + circuit joined per lane (TR-087)", [
            {
                "name": "provider",
                "in": "query",
                "required": False,
                "schema": string,
                "description": "Filter to one provider id (e.g. zai-glm)",
            },
        ]),
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
                },
                {
                    "name": "sort",
                    "in": "query",
                    "required": False,
                    "schema": string,
                    "description": "Chain ordering: price (default) | "
                                   "predicted_cost_per_task | wall_time | turns | "
                                   "ratio:<w>*<cost>+<w>*<time>",
                },
                {
                    "name": "backend",
                    "in": "query",
                    "required": False,
                    "schema": string,
                    "description": "Use only this source_system's outcome stats",
                },
                {
                    "name": "merge_backends",
                    "in": "query",
                    "required": False,
                    "schema": string,
                    "description": "Aggregate outcome stats across all backends",
                },
                {
                    "name": "window_h",
                    "in": "query",
                    "required": False,
                    "schema": integer,
                    "description": "Average window (half-life, hours) for stats-based sorts",
                },
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
    # TR-049 component 1: outcome ingest. One row per completed task; the
    # response carries the normalized row so a reporter can verify the write.
    paths["/api/v1/outcomes"] = {
        "post": _post_operation(
            "ingestOutcome",
            "Ingest one completed-task outcome (cost-per-task engine)",
            {
                "source_system": string,
                "session_id": string,
                "task_label": string,
                "complexity": {
                    "type": ["object", "string", "null"],
                    "description": "per-category required levels "
                                   "{category: level} or a profile id — the "
                                   "task's complexity reference",
                },
                "profile_id": string,
                "required_categories": {"type": ["object", "array", "null"]},
                "provider": string,
                "model": string,
                "turns": {"type": ["integer", "null"]},
                "tokens_in": {"type": ["integer", "null"]},
                "tokens_out": {"type": ["integer", "null"]},
                "tokens_reasoning": {"type": ["integer", "null"]},
                "cost": {"type": ["number", "null"],
                         "description": "task cost in USD (alias: cost_usd)"},
                "cost_usd": {"type": ["number", "null"]},
                "wall_time": {"type": ["number", "null"],
                              "description": "wall seconds (alias: wall_time_s)"},
                "wall_time_s": {"type": ["number", "null"]},
                "success": {"type": ["boolean", "null"]},
                "ts": {"type": ["number", "null"],
                       "description": "epoch seconds (default: server now)"},
            },
            ["source_system", "session_id", "provider", "model"],
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
            if path == "/":
                payload = router_health.health(mode=self.mode)
                payload["_links"] = {
                    "health": "/health",
                    "model_status": "/model_status?provider=<id>",
                    "status": "/status",
                    "openapi": "/openapi.json",
                }
                return 200, payload
            if path == "/health":
                return 200, router_health.health(mode=self.mode)
            if path == "/model_status":
                provider = query.get("provider")
                if isinstance(provider, list):
                    provider = provider[0] if provider else None
                return 200, router_health.model_status(provider=provider)
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
                argv = [project, "--format", "json"]
                # Training-data opt-in (Bane 2026-09-01): default OFF. Pass
                # ?allow_training=1 to include lanes whose terms train on
                # prompts/completions (e.g. muse-spark-1.2-contributor).
                at = query.get("allow_training")
                if at and (isinstance(at, str) and at not in ("0", "false", "no")):
                    argv.append("--allow-training")
                # TR-054: latency-tolerant resolve. ?allow_slow=1 includes lanes
                # the probe marked SLOW (worker batches on free lanes). DOWN /
                # quota / circuit gates still apply.
                aslow = query.get("allow_slow")
                if aslow and (isinstance(aslow, str) and aslow not in ("0", "false", "no")):
                    argv.append("--allow-slow")
                # TR-049: outcomes-driven ordering passthrough (same flags as
                # the CLI, so the HTTP surface can never drift from it).
                for key, flag in (("sort", "--sort"), ("backend", "--backend"),
                                  ("window_h", "--window-h")):
                    value = query.get(key)
                    if isinstance(value, list):
                        value = value[0] if value else None
                    if value:
                        argv.extend([flag, str(value)])
                merge = query.get("merge_backends")
                if isinstance(merge, list):
                    merge = merge[0] if merge else None
                if merge and str(merge) not in ("0", "false", "no"):
                    argv.append("--merge-backends")
                return 200, _subprocess_json("router_spawn.py", argv)
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
        # TR-067: the mirror can opt out of the ROUTER key when the caller's own
        # upstream credential is the real gate (default: OFF — the gate is never
        # weakened silently). Set ROUTER_PROXY_AUTH=passthrough to enable.
        proxy_passthrough = (path in PROXY_PATHS
                             and os.environ.get('ROUTER_PROXY_AUTH') == 'passthrough')
        if not proxy_passthrough:
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
        if path == "/api/v1/outcomes":
            # TR-049 ingest. A malformed payload is a caller bug -> 400 with
            # every problem named; a STORE problem is fail-open -> 200 with
            # appended=false + the reason (the reporter must never be blocked
            # by our disk, and the caller retries on the next tick).
            try:
                return 200, router_outcomes.ingest(body)
            except ValueError as exc:
                return 400, {"error": str(exc)}
        if path in PROXY_PATHS:
            # TR-067 Path B: mirror endpoint — classify (or take the declared
            # profile), build the chain internally, call the upstream gateway
            # with a bounded fallback ladder, return the upstream response shape
            # plus additive _router metadata.
            return proxy_chat(path, body, headers)
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


PROXY_PATHS = ("/v1/chat/completions", "/v1/responses")

#: Injectable upstream call for tests: (path, body, headers) -> (status, payload)
_UPSTREAM_CALL = None


def _proxy_hop_timeout_s():
    """Bounded per-hop timeout (seconds) for the upstream mirror.

    Was a hardcoded 1800s: one unresponsive lane held the request — and with the
    fleet wired through the proxy, the caller's whole tick — for half an hour
    while the ladder sat on hop 1 (measured 2026-09-23: a real proxied request
    exceeded 300s with no response and no row). A hop that cannot answer inside
    the budget must FAIL so the ladder advances; the ladder already treats a
    transport error as a step, so this is a timeout, not a retry policy.
    """
    raw = os.environ.get('ROUTER_PROXY_HOP_TIMEOUT_S', '180')
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 180.0
    return val if val > 0 else 180.0


def _proxy_upstream_default(path, body, headers):
    """POST the request to the upstream gateway (default: the Hermes gateway
    on localhost). Returns (status, payload-dict). Transport failure raises —
    the ladder treats it exactly like a 5xx."""
    base = os.environ.get('ROUTER_PROXY_UPSTREAM', 'http://127.0.0.1:8642')
    req = urllib.request.Request(
        base.rstrip('/') + path, data=json.dumps(body).encode(),
        headers={k: v for k, v in headers.items()
                 if k.lower() in ('authorization', 'x-api-key', 'content-type')}
        | {'Content-Type': 'application/json', 'User-Agent': 'task-router-proxy/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=_proxy_hop_timeout_s()) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:      # a REAL response, not transport
        return exc.code, {'error': exc.read().decode()[:400]}
    try:
        payload = json.loads(raw)
    except ValueError:
        # TR-120: a 200 whose body is not JSON (an upstream that streamed SSE
        # anyway) is NOT a servable answer — the OpenAI clients read `choices`
        # and report an empty stream. Treat it like a transport failure so the
        # ladder advances instead of handing the client garbage.
        return (status if isinstance(status, int) else 200), {
            'error': 'upstream returned non-JSON',
            'raw': raw[:200].decode(errors='replace')}
    return (status if isinstance(status, int) else 200), payload


def _normalize_developer_role(body, headers):
    """Rewrite `role:developer` -> `role:system` (TR-074/TR-075, spec §6.4).

    The spec's §3 wire table predicted this failure: pi and deepseek-harness
    reason about a `compat.supportsDeveloperRole` flag, and OpenAI-compatible
    servers vary on whether they accept `developer`. The proxy forwards the body
    UNCHANGED, so a host that sends `developer` burns the whole ladder on a
    payload the proxy could have accepted — measured: 400 at the upstream, with
    `role:system` of the same content returning 200.

    The two roles are the same message with different spellings (`developer` is
    OpenAI's rename of `system` for newer models), so normalizing is lossless
    for the providers that lack it and harmless for those that have it. Done
    here rather than per-driver because FIVE drivers would otherwise each carry
    the workaround and drift (spec §1).

    Returns (body, count). Count is disclosed in the envelope so the rewrite is
    never silent. Callers that genuinely need `developer` preserved send
    `x-router-developer-role: preserve`.
    """
    if not isinstance(body, dict):
        return body, 0
    if str(headers.get('x-router-developer-role', '')).lower() == 'preserve':
        return body, 0
    msgs = body.get('messages')
    if not isinstance(msgs, list):
        return body, 0
    n = 0
    out = []
    for m in msgs:
        if isinstance(m, dict) and m.get('role') == 'developer':
            m = {**m, 'role': 'system'}
            n += 1
        out.append(m)
    if not n:
        return body, 0
    return {**body, 'messages': out}, n


def _proxy_requirements(body, headers, path):
    """Decide the complexity for this request. Returns (source, payload) where
    source ∈ declared | classifier | default, payload carries the matrix,
    complexity_sig and (for the classifier) prompt version/model/problems."""
    declared = headers.get('x-router-profile')
    if declared:
        sig = None
        try:
            import router_outcomes as ro
            sig = ro.complexity_sig(ro.profile_signature(declared) or {})
        except Exception:  # noqa: BLE001
            pass
        return 'declared', {'profile_id': declared, 'matrix': None,
                            'complexity_sig': sig, 'problems': []}
    text = []
    for m in (body.get('messages') or []) if isinstance(body, dict) else []:
        c = m.get('content') if isinstance(m, dict) else None
        if isinstance(c, str):
            text.append(c)
    if isinstance(body, dict) and isinstance(body.get('input'), str):
        text.append(body['input'])
    text = '\n'.join(text)[-20000:]
    # Scorer selection (Bane 2026-09-20): JEV is the cheap alternative scorer —
    # one decisions call, input cheap / output free, score on a 0..2 scale that
    # selects a band whose LEVELS are the same complexity matrix. Per-request
    # header wins over the deployment default so a caller can choose per call.
    scorer = str(headers.get('x-router-scorer') or os.environ.get('ROUTER_SCORER') or 'classifier').lower()
    if scorer in ('jev', 'decisions'):
        try:
            import router_jev
            jres = router_jev.classify(text)
        except Exception as exc:  # noqa: BLE001
            return 'default', {'profile_id': 'P0_FORE', 'matrix': None, 'complexity_sig': None,
                               'problems': [f'jev scorer unavailable: {str(exc)[:200]}']}
        if jres.get('matrix') is None:
            # Same R10 discipline as the classifier: degrade VISIBLY.
            return 'default', {'profile_id': 'P0_FORE', 'matrix': None, 'complexity_sig': None,
                               'confidence': jres.get('confidence'),
                               'scorer': 'jev', 'score': jres.get('score'),
                               'model': jres.get('model'), 'problems': jres.get('problems') or []}
        return 'jev', {'matrix': jres['matrix'], 'complexity_sig': jres.get('complexity_sig'),
                       'confidence': jres.get('confidence'), 'scorer': 'jev',
                       'score': jres.get('score'), 'band': jres.get('band'),
                       'model': jres.get('model'), 'problems': jres.get('problems') or []}
    try:
        import router_classify
        res = router_classify.classify(text)
    except Exception as exc:  # noqa: BLE001
        return 'default', {'profile_id': 'P0_FORE', 'matrix': None, 'complexity_sig': None,
                           'problems': [f'classifier unavailable: {str(exc)[:200]}']}
    if res.get('matrix') is None:
        # R10: degrade to the default profile, WITH the reason visible.
        return 'default', {'profile_id': 'P0_FORE', 'matrix': None, 'complexity_sig': None,
                           'confidence': res.get('confidence'),
                           'prompt_version': res.get('prompt_version'),
                           'model': res.get('model'), 'problems': res.get('problems') or []}
    return 'classifier', {'matrix': res['matrix'], 'complexity_sig': res.get('complexity_sig'),
                          'confidence': res.get('confidence'),
                          'prompt_version': res.get('prompt_version'),
                          'model': res.get('model'), 'problems': res.get('problems') or []}


def _proxy_chain(requirements, sort_spec=None, window_h=None):
    """Ask the router for the chain (subprocess: the CLI stays the single
    source of truth for resolve semantics)."""
    args = ['--format', 'json']
    if requirements.get('profile_id'):
        args += ['--profile', requirements['profile_id']]
    if requirements.get('matrix'):
        args += ['--profile-req', ' '.join(f'{c}={v}' for c, v in sorted(requirements['matrix'].items()))]
    if sort_spec:
        args += ['--sort', sort_spec]
    if window_h:
        args += ['--window-h', str(window_h)]
    try:
        raw = _subprocess_text("router_spawn.py", args, timeout=180)
        out = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 — resolve problems degrade, never 500
        return {}
    return out if isinstance(out, dict) else {}


def _proxy_usage(payload):
    """(tokens_in, tokens_out) from an OpenAI-compatible usage block.

    Both wire shapes are accepted because both are proxied: /v1/chat/completions
    reports prompt_tokens/completion_tokens, /v1/responses reports
    input_tokens/output_tokens. A missing or non-numeric meter stays None — the
    row must say "not measured", never 0 (Bane: no fake zeros; a zero here would
    drag the cost-per-task average down as if the work were free).
    """
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get('usage')
    if not isinstance(usage, dict):
        return None, None
    def num(*names):
        for n in names:
            v = usage.get(n)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            return int(v)
        return None
    return num('prompt_tokens', 'input_tokens'), num('completion_tokens', 'output_tokens')


def _proxy_cost(hop, tokens_in, tokens_out):
    """(cost_usd, basis) for one attempt, from the HOP's own prices.

    Reporting uses the PUBLIC list price (Bane 2026-08-27). The hop already
    carries the public split — `router_spawn._pub_prices` substitutes the
    normalized rate when a lane was stamped `public_price: 0.0` because it is
    covered by a subscription, so a plan lane cannot report FREE here either.

    A zero-or-absent price is NOT a cost of zero: when nothing usable is priced
    the answer is (None, reason). Unknown is reported as unknown.
    """
    if tokens_in is None and tokens_out is None:
        return None, 'no usage block; price not applied'
    tin, tout = tokens_in or 0, tokens_out or 0
    inp, outp = hop.get('in_per_m'), hop.get('out_per_m')
    if inp or outp:                                  # a real per-1M split
        return (tin / 1e6) * (inp or 0.0) + (tout / 1e6) * (outp or 0.0), \
               'public split (in_per_m/out_per_m)'
    blend = hop.get('usd_1m')
    if blend:
        return ((tin + tout) / 1e6) * blend, 'public blended (usd_1m)'
    return None, 'no price on this hop; cost unknown'


def _proxy_record(provider, model, ok, requirements, reason='', latency_s=None,
                  source='router-proxy', session_id=None,
                  tokens_in=None, tokens_out=None, cost_usd=None,
                  parent_session_id=None):
    """One outcome row per attempt + breaker evidence (best effort, fail-open).

    TR-071: `source` is the DRIVER identity when the caller declared one
    (`x-router-caller`, see drivers/ and SPEC-PROXY-DRIVERS §2). Before this, the
    row was always stamped 'router-proxy', so a proxied hermes session was
    indistinguishable from any other anonymous proxy traffic — the caller could
    not be attributed, which is the whole point of wiring a host to the proxy.
    Default stays 'router-proxy' for undeclared callers, so nothing silently
    changes class.

    `wall_time_s` is the failing hop's latency on failure and the TOTAL ladder
    time on success (see proxy_chat), so the row means "time to get an answer".
    """
    try:
        import router_outcomes as ro
        row = {'source_system': source,
               'session_id': session_id or f'proxy-{time.time()}',
               'parent_session_id': parent_session_id,
               'provider': provider, 'model': model,
               'required_categories': requirements.get('matrix'),
               'complexity_sig': requirements.get('complexity_sig'),
               'profile_id': requirements.get('profile_id'),
               'turns': None, 'steps': 1, 'tokens_in': tokens_in, 'tokens_out': tokens_out,
               'cost_usd': cost_usd, 'wall_time_s': latency_s, 'success': ok,
               'task_label': reason[:200] or None, 'ts': time.time()}
        if parent_session_id:
            # Accumulate: one row per (source_system, session, model) that grows as
            # the session's steps land, so the task's tokens/cost/steps stay
            # associated with the session the caller declared. Also fixes the
            # O(store) scan the bulk append path did on every proxied hop.
            ro.accumulate_row(ro.outcomes_path(), row)
        else:
            ro.append_row_fast(ro.outcomes_path(), row)
    except Exception:  # noqa: BLE001
        pass
    try:
        verb = 'record-success' if ok else 'record-failure'
        args = [verb, provider, model] + ([] if ok else [reason or 'transport failure'])
        _subprocess_text("router_circuit.py", args)
    except Exception:  # noqa: BLE001
        pass


def proxy_chat(path, body, headers, max_hops=None, upstream=None):
    """TR-067 Path B entry point. Never raises: any internal failure returns a
    shaped error with the ladder trail (fail-open, the caller is a live client)."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    body = body if isinstance(body, dict) else {}
    # TR-071: a DRIVER may declare itself so proxied attempts are attributed to
    # it instead of landing as anonymous 'router-proxy' traffic. Validated
    # against the driver registry, so a typo or a random header value cannot
    # invent a source_system; an unknown value degrades to 'router-proxy' with
    # the reason visible in the envelope.
    caller = (headers.get('x-router-caller') or '').strip()
    caller_problems = []
    if caller:
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'drivers'))
            import drivers
            if caller not in drivers.list_drivers():
                caller_problems.append(
                    f'unknown caller {caller!r}; attributed as router-proxy')
                caller = ''
        except Exception as exc:  # noqa: BLE001 — never fail a live request here
            caller_problems.append(f'caller lookup failed: {str(exc)[:120]}')
            caller = ''
    source_system = caller or 'router-proxy'
    # TR-074/TR-075: normalize `role:developer` -> `role:system` BEFORE the walk.
    # A payload the upstream rejects must not burn every hop, and the rewrite is
    # the same for all five drivers (spec §1: one place, not five).
    body, dev_rewrites = _normalize_developer_role(body, headers)
    try:
        hops = int(headers.get('x-router-max-hops') or
                   os.environ.get('ROUTER_PROXY_MAX_HOPS') or (max_hops or 3))
    except (TypeError, ValueError):
        hops = 3
    source, requirements = _proxy_requirements(body, headers, path)
    if source == 'classifier' and not (requirements.get('matrix') or {}):
        # empty matrix = the task pressures no category. The router needs a
        # profile to build a chain, so use the default one — VISIBLY.
        source = 'classifier-empty'
        requirements = {**requirements, 'profile_id': 'P0_FORE',
                        'problems': (requirements.get('problems') or []) +
                                    ['empty matrix -> default profile P0_FORE']}
    resolved = _proxy_chain(requirements, sort_spec=headers.get('x-router-sort'),
                            window_h=headers.get('x-router-window-h'))
    chain = resolved.get('chain') or []
    # TR-081: the ladder must be SELF-AUDITING. The resolver filters gated lanes
    # before the proxy ever sees the chain, so a caller can be served at hop 4
    # (e.g. $1.52/M) while the registry's own chain head was 18x cheaper — and
    # nothing in the response explained it. Pass the skip evidence through and
    # count what was never attempted.
    try:
        exclusions = resolved.get('exclusions') or []
        if not isinstance(exclusions, list):
            exclusions = []
    except Exception:  # noqa: BLE001 — a malformed resolver must not break the proxy
        exclusions = []
    try:
        gate_reasons = resolved.get('gate_reasons') or []
        if not isinstance(gate_reasons, list):
            gate_reasons = []
    except Exception:  # noqa: BLE001
        gate_reasons = []
    # skipped_hops = chain entries the walk will not reach because it is bounded
    # by max_hops. (Hops gated BEFORE the chain was built are not chain entries
    # at all; they are named in `exclusions` with their `why`.) Honest count:
    # only entries that exist in the chain and exceed the bound.
    attempted_bound = max(0, min(len(chain), hops))
    meta = {'complexity_source': source, 'requirements': requirements,
            'sort': resolved.get('sort'), 'chain_length': len(chain),
            'max_hops': hops, 'ladder': [],
            'first_attempt_hop': (chain[0].get('hop') if chain else None),
            'skipped_hops': max(0, len(chain) - attempted_bound),
            'exclusions': exclusions, 'gate_reasons': gate_reasons,
            'degrade_reason': (requirements.get('problems') or [None])[0],
            'caller': source_system,
            'developer_role_rewrites': dev_rewrites,
            'problems': list(caller_problems)}
    if dev_rewrites:
        # never silent: the caller's payload was adjusted
        meta['problems'].append(
            f'normalized {dev_rewrites} role:developer message(s) -> system '
            f'(reference provider compat; x-router-developer-role: preserve to keep)')
    if not chain:
        meta['gate'] = resolved.get('gate')
        return 503, {'error': 'no open hop for this request', '_router': meta}

    call = upstream or _UPSTREAM_CALL or _proxy_upstream_default
    last = None
    # One session id per REQUEST (not per hop): a client's request spans
    # several attempts, and the TR-049 store dedupes on
    # (source_system, session_id, model) — a per-hop id made every collapsed
    # attempt indistinguishable. The ladder is preserved in the envelope's
    # `_router.ladder` for the attempts that the store collapses.
    # TR-049/TR-120 session association: a caller may declare its own session
    # (the Hermes session id) so the router's outcome row can be joined to the
    # session record — cost, provider, steps and tokens land on ONE task row that
    # grows as the session's steps are served (see router_outcomes.accumulate_row).
    declared_session = (headers.get('x-router-session') or '').strip()[:200]
    parent_session_id = declared_session or None
    session_id = (f'{source_system}:{declared_session}' if declared_session
                  else f'{source_system}-{int(time.time() * 1000)}')
    ladder_t0 = time.time()
    for hop in chain[:hops]:
        provider, model = hop.get('provider'), hop.get('model')
        attempt = {'hop': hop.get('hop'), 'provider': provider, 'model': model,
                   'usd_1m': hop.get('usd_1m'),
                   'stats_fallback': (hop.get('outcomes') or {}).get('stats_fallback')}
        fwd = dict(body)
        fwd.pop('stream', None)  # TR-120: the mirror is buffered; strip the client's stream wish
        fwd['model'] = model
        hdrs = {**headers, 'x-router-provider': str(provider)}
        t0 = time.time()
        try:
            status, payload = call(path, fwd, hdrs)
        except Exception as exc:  # noqa: BLE001 — transport failure == ladder step
            status, payload = 0, {'error': str(exc)[:300]}
        attempt['latency_s'] = round(time.time() - t0, 3)
        attempt['status'] = status
        ok = 200 <= int(status or 0) < 300
        if ok and isinstance(payload, dict) and not payload.get('choices') \
                and payload.get('error') and path == '/v1/chat/completions':
            # TR-120: a 2xx that carries an error envelope and no choices is not
            # a servable completion — the client reads `choices` and reports an
            # empty stream. Count the hop as failed so the ladder advances
            # instead of serving garbage with a green status.
            ok = False
            attempt['outcome'] = 'unservable-2xx'
        attempt['outcome'] = ('ok' if ok
                              else attempt.get('outcome') or 'transport-failure')
        meta['ladder'].append(attempt)
        last = (status, payload)
        # On success the row means "time to get an answer" (total ladder time);
        # on failure it is that hop's latency, so a slow dead hop is still
        # visible. Measured ONCE and reused for the envelope, so the row and the
        # client-visible `wall_time_s` cannot disagree — `_proxy_record` runs a
        # subprocess, so measuring again after it would inflate the envelope by
        # the router's own bookkeeping time.
        wall = (round(time.time() - ladder_t0, 3) if ok else attempt['latency_s'])
        # Metering (2026-09-23): a proxied request is the ONLY place the router
        # sees the work itself, so the usage block is what lets the
        # cost-per-task averages learn from real traffic. Without it every
        # proxied row was cost-blind and the ledger could only be filled by
        # post-hoc state.db imports (no prompt, no complexity, no cost).
        tokens_in, tokens_out = _proxy_usage(payload if ok else None)
        cost_usd, price_basis = _proxy_cost(hop, tokens_in, tokens_out)
        attempt['tokens_in'], attempt['tokens_out'] = tokens_in, tokens_out
        attempt['cost_usd'], attempt['price_basis'] = cost_usd, price_basis
        _proxy_record(str(provider), str(model), ok, requirements,
                      reason='' if ok else str(payload.get('error') if isinstance(payload, dict) else payload)[:200],
                      latency_s=wall,
                      source=source_system, session_id=session_id,
                      tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                      parent_session_id=parent_session_id)
        if ok:
            out = dict(payload) if isinstance(payload, dict) else {'upstream': payload}
            out['_router'] = {**meta, 'served_by': {'provider': provider, 'model': model,
                                                    'tokens_in': tokens_in, 'tokens_out': tokens_out,
                                                    'cost_usd': cost_usd, 'price_basis': price_basis},
                              'outcome_row': {'source_system': source_system,
                                              'session_id': session_id,
                                              'parent_session_id': parent_session_id},
                              'wall_time_s': wall}
            return 200, out
    status, payload = last or (502, {'error': 'no hops attempted'})
    out = dict(payload) if isinstance(payload, dict) else {'upstream': payload}
    out['_router'] = {**meta, 'served_by': None, 'exhausted': True}
    return (status if isinstance(status, int) and status >= 400 else 502), out


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
            if status == 200 and isinstance(body, dict) and body.get("stream") \
                    and parsed.path in PROXY_PATHS:
                # TR-120: clients that ask for SSE (the OpenAI SDK always does
                # for agent loops) cannot read a buffered JSON body — the
                # stream reader sees zero chunks and reports an empty stream.
                # The mirror buffers the upstream answer; re-serve it as one
                # synthesized SSE completion so the client's wire shape holds.
                self._send_sse_chat(payload)
                return
            self._send(status, payload)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def _send_sse_chat(self, payload):
        """Buffered completion -> OpenAI chat-completions SSE frames.

        The upstream answer is already complete; emit it as one delta chunk
        (role+content), the finish chunk, and [DONE] — the exact frame set the
        OpenAI SDK's stream reader needs. An error payload is forwarded as a
        data frame so the client's error path sees the reason (never silence).
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        def _frame(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        if isinstance(payload, dict) and payload.get("choices"):
            choice0 = payload["choices"][0] or {}
            msg = choice0.get("message") or {}
            cid = payload.get("id") or "chatcmpl-router"
            model = payload.get("model") or ""
            _frame({"id": cid, "object": "chat.completion.chunk", "created": payload.get("created") or int(time.time()),
                    "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            content = msg.get("content") or ""
            if content:
                _frame({"id": cid, "object": "chat.completion.chunk", "created": payload.get("created") or int(time.time()),
                        "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            _frame({"id": cid, "object": "chat.completion.chunk", "created": payload.get("created") or int(time.time()),
                    "model": model, "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": choice0.get("finish_reason") or "stop"}]})
            if isinstance(payload.get("usage"), dict):
                _frame({"id": cid, "object": "chat.completion.chunk", "created": payload.get("created") or int(time.time()),
                        "model": model, "choices": [], "usage": payload["usage"]})
        else:
            # Unserved (ladder exhausted / shaped error): keep the visible reason.
            _frame(payload if isinstance(payload, dict) else {"error": str(payload)})
        self.wfile.write(b"data: [DONE]\n\n")

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

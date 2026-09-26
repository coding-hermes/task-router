#!/usr/bin/env python3
"""Task Router JSON API and minimal OpenAPI-to-MCP bridge (TR-018).

The server is intentionally stdlib-only. Read-only mode is the default. Edit
mode fails closed unless ROUTER_EDIT_API_KEY is configured; mutating HTTP and
MCP tool calls then require the same value in X-API-Key.
"""

import argparse
import datetime
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
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
import router_ui_page  # noqa: E402  (TR-150: the one self-contained page)
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
    number = {"type": "number"}
    outcome = {"type": "string", "enum": ["failure", "success"]}
    ledger_outcome = {"type": "string", "enum": ["success", "failure", "error"]}
    get_paths = {
        "/openapi.json": ("getOpenAPI", "Get the OpenAPI 3.1 schema", []),
        "/": ("getRoot", "Health + status surface (TR-087)", []),
        "/v1/capabilities": ("getCapabilities", "Discover the upstream gateway's capabilities through the proxy (TR-140): live probe, else the startup probe marked stale, else an honest error", []),
        "/health": ("getHealth", "Control-plane health: identity, registry age (mtime) + freshness, router_validate gate verdict, gate states (TR-087, TR-REVIEW-001)", []),
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
        "/proxy/stats": ("getProxyStats", "Rolling per-model and per-complexity-band averages over the proxy's own traffic (TR-144): samples, success rate, cost/task, steps, wall time, cache ratio + failure reason mix. `windows` = hours (csv), `grouping` = model|band|model_band", []),
        "/ui": ("getUi", "TR-150: the Data Command Center page, served by this service (one self-contained document, no CDN, read-only)", []),
        "/api/ui/series": ("getUiSeries", "TR-152: traffic + cost over time, bucketed hourly or daily, by lane / band / total. Every bucket carries its sample count and how many samples were priced.", [
            {"name": "bucket", "in": "query", "required": False, "schema": string, "description": "hour (default) | day"},
            {"name": "window_h", "in": "query", "required": False, "schema": number, "description": "How far back to read (default 24)"},
            {"name": "group", "in": "query", "required": False, "schema": string, "description": "total (default) | lane | band"},
            {"name": "scan_limit", "in": "query", "required": False, "schema": integer, "description": "Store rows to tail-scan (default 200000)"},
        ]),
        "/api/ui/board": ("getUiBoard", "TR-150/156: search the board JSONL for the UI. Reports total_rows/total_matched like the ledger search.", [
            {"name": "q", "in": "query", "required": False, "schema": string, "description": "Free text over id, title, status, reasoning, notes"},
            {"name": "status", "in": "query", "required": False, "schema": string, "description": "Exact board status"},
            {"name": "limit", "in": "query", "required": False, "schema": integer, "description": "Page size (default 25, max 200)"},
            {"name": "offset", "in": "query", "required": False, "schema": integer, "description": "Matches to skip"},
        ]),
        "/api/ui/ledger": ("getUiLedger", "TR-151: search the outcome ledger (free text q + outcome/complexity_source/provider/model/band/since/until filters). Every response reports rows_scanned, scan_limit and truncation so a search can never look complete when it was cut short.", [
            {"name": "q", "in": "query", "required": False, "schema": string, "description": "Free text over provider, model, session, label, failure reason, band"},
            {"name": "provider", "in": "query", "required": False, "schema": string, "description": "Exact provider id"},
            {"name": "model", "in": "query", "required": False, "schema": string, "description": "Exact model id"},
            {"name": "band", "in": "query", "required": False, "schema": string, "description": "complexity_sig to match"},
            {"name": "outcome", "in": "query", "required": False, "schema": string, "description": "success | failed | a route_outcome value"},
            {"name": "since", "in": "query", "required": False, "schema": number, "description": "Epoch seconds lower bound (inclusive)"},
            {"name": "until", "in": "query", "required": False, "schema": number, "description": "Epoch seconds upper bound (inclusive)"},
            {"name": "limit", "in": "query", "required": False, "schema": integer, "description": "Page size (default 50, max 500)"},
            {"name": "offset", "in": "query", "required": False, "schema": integer, "description": "Matches to skip"},
            {"name": "scan_limit", "in": "query", "required": False, "schema": integer, "description": "Rows to read before stopping (default 200000); reported back as rows_scanned"},
        ]),
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
        # TR-129: populated by main() from the upstream's /v1/capabilities at
        # startup (advisory metadata; empty dict when the probe has not run).
        self.hermes_capabilities = {}

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
                # TR-129: what the upstream /v1/capabilities probe learned at
                # startup (additive; empty when the probe has not run).
                if self.hermes_capabilities:
                    payload["hermes_upstream"] = self.hermes_capabilities
                payload["_links"] = {
                    "health": "/health",
                    "model_status": "/model_status?provider=<id>",
                    "status": "/status",
                    "openapi": "/openapi.json",
                }
                return 200, payload
            if path == "/v1/capabilities":
                # TR-140: callers must be able to discover the upstream's surface
                # through the proxy instead of guessing at it. Live probe first
                # (so a restarted gateway is not misrepresented), then the
                # startup probe, then an honest error — never an invented
                # capability.
                live = _hermes_capabilities_metadata(_hermes_upstream_base())
                cached = getattr(self, 'hermes_capabilities', {}) or {}
                if not live.get('error'):
                    return 200, {'proxy': 'task-router', 'upstream': live, 'source': 'live'}
                if cached:
                    return 200, {'proxy': 'task-router', 'upstream': cached, 'source': 'startup-probe',
                                 'stale': True, 'live_error': live.get('error')}
                return 200, {'proxy': 'task-router', 'upstream': None, 'source': 'unavailable',
                             'error': live.get('error') or 'capabilities unavailable'}
            if path == "/api/ui/series":
                # TR-152: traffic and cost over time, bucketed, with per-bucket sample counts.
                return 200, router_ui_page.series(
                    query, router_outcomes.outcomes_path())
            if path == "/api/ui/board":
                # TR-150/156: the board panel's search over the repo's JSONL board.
                return 200, router_ui_page.board_search(
                    query, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        '.coding-hermes', 'board', 'tasks.jsonl'))
            if path == "/api/ui/ledger":
                # TR-151: the raw-data search. Reports how much of the store it read.
                return 200, ui_ledger(query)
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
            if path == "/proxy/stats":
                # TR-144: the rolling averages the whole proxy exists to learn
                # from, per model AND per complexity band. `windows` is the one
                # knob (hours, comma separated); `grouping` collapses the keys.
                # Every window discloses how much of the ledger it scanned and
                # the cache AGE, so a stale number is visible as a stale number.
                raw_windows = query.get("windows")
                windows = None
                if isinstance(raw_windows, list):
                    raw_windows = raw_windows[0] if raw_windows else None
                if raw_windows:
                    windows = []
                    for part in str(raw_windows).split(","):
                        try:
                            h = float(part)
                        except ValueError:
                            continue
                        if h > 0:
                            windows.append(h)
                    windows = tuple(windows) or None
                grouping = query.get("grouping")
                if isinstance(grouping, list):
                    grouping = grouping[0] if grouping else None
                if grouping not in (None, "model", "band", "model_band"):
                    return 400, {"error": "grouping must be model, band or model_band"}
                try:
                    import router_proxy_stats as rps
                    import router_outcomes as _ro
                    return 200, rps.get_rollup_cached(_ro.outcomes_path(), windows=windows,
                                                      grouping=grouping or "model_band")
                except Exception as exc:  # noqa: BLE001 — fail-open, like every read
                    return 200, {"error": f"stats unavailable: {exc}"[:200], "windows": {}}
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
            # TR-140: a typo must read as a typo. The auth gate used to run
            # first, so POSTing to an unknown path in read-only mode answered
            # "read-only mode" — a wrong answer that hides the real problem and
            # hides the surface the caller can use.
            if not _is_known_post_path(path):
                return 404, {"error": "not found", "path": path,
                             "hint": "this is not a POST endpoint; GET /openapi.json lists the contract",
                             "surface": known_post_paths()}
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
            # TR-129: /v1/responses through a Hermes-gateway upstream speaks the
            # gateway's session protocol (X-Hermes-Session-Key forward,
            # X-Hermes-Session-Id echo, SSE keepalive handling, idle deadline).
            # The OpenAI chat path keeps its original handler.
            if path == "/v1/responses":
                return _hermes_proxy_chat(body, headers)
            # TR-145: the client surface drops the internal routing markers; the
            # in-process consumers keep them (see _client_body).
            chat_status, chat_payload = proxy_chat(path, body, headers)
            return chat_status, _client_body(chat_payload)
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

#: POST paths that are NOT in the OpenAPI contract because they are dynamic.
_DYNAMIC_POST_PREFIXES = ("/listings/",)


def known_post_paths():
    """Every path this server answers a POST on (TR-140).

    The auth gate runs before path resolution, so an unknown POST in read-only
    mode used to answer "read-only mode" — a wrong answer to a question nobody
    asked: the caller had a typo, not a permission problem. This is the surface
    the 404 can name instead.
    """
    paths = set(PROXY_PATHS)
    try:
        for path, item in build_openapi().get("paths", {}).items():
            if "post" in item:
                paths.add(path)
    except Exception:  # noqa: BLE001 — advisory: a broken contract must not block serving
        pass
    return sorted(paths)


def _is_known_post_path(path):
    if path in PROXY_PATHS:
        return True
    try:
        if "post" in (build_openapi().get("paths", {}).get(path) or {}):
            return True
    except Exception:  # noqa: BLE001
        return True          # fail-open: never turn a real endpoint into a 404
    return any(path.startswith(p) and len(path) > len(p) for p in _DYNAMIC_POST_PREFIXES)



#: TR-129: the upstream is the REAL Hermes gateway when ROUTER_PROXY_UPSTREAM
#: points at one (the deployment recipe names the gateway's own port; the
#: legacy default is the gateway too). Feature-detect via /v1/capabilities.
_HERMES_CAPABILITY_MARKER = 'hermes.api_server.capabilities'
_HERMES_DEFAULT_UPSTREAM = 'http://127.0.0.1:8642'

#: SOURCE B caps session keys at 256 chars (the gateway echoes the key back in
#: a response header — a longer key is a header-injection risk on that path).
_HERMES_MAX_SESSION_HEADER_LEN = 256

#: TR-129 idle deadline (SOURCE A): a gateway turn that produces no real SSE
#: event for this long is aborted so the ladder can advance. Keepalives are
#: deliberately NOT activity (gateway emits them on a bare timer regardless of
#: agent liveness — a keepalive reset would disable the deadline entirely).
_HERMES_IDLE_TIMEOUT_S = 300.0


class _HermesIdleTimeout(Exception):
    """The idle deadline fired: no real SSE event inside the budget."""


class _HermesIdleWatch:
    """Resettable idle deadline for one gateway stream (SOURCE A turnWatch).

    Real events call reset(); the reader checks expired() BEFORE each blocking
    readline, so a keepalive-only stream dies inside the budget instead of
    hanging until the socket does. _clock is injectable for tests.
    """

    def __init__(self, timeout_s, _clock=time.monotonic):
        self._timeout_s = float(timeout_s)
        self._clock = _clock
        self._deadline = _clock() + self._timeout_s
        self.fired = False

    def reset(self):
        self._deadline = self._clock() + self._timeout_s

    def expired(self):
        if self.fired:
            return True
        if self._clock() >= self._deadline:
            self.fired = True
        return self.fired


def _hermes_idle_timeout_s():
    raw = os.environ.get('ROUTER_HERMES_IDLE_TIMEOUT_S', '')
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _HERMES_IDLE_TIMEOUT_S
    return val if val > 0 else _HERMES_IDLE_TIMEOUT_S


def _sse_lines(reader, watch=None):
    """Yield raw SSE lines from `reader`, enforcing the idle watch.

    The deadline check sits BEFORE the blocking readline: a stream that keeps
    bytes arriving (keepalives) but produces no real event still trips it. A
    watch-free reader (tests, buffered bodies) never times out.
    """
    while True:
        if watch is not None and watch.expired():
            raise _HermesIdleTimeout('no real SSE event inside the idle budget')
        line = reader.readline()
        if not line:
            return
        yield line


def _read_hermes_sse(lines, on_event=None, watch=None):
    """Consume a Hermes gateway /v1/responses SSE stream to its terminal event.

    SOURCE A parser semantics: `event:` sets the event name, `data:` lines
    accumulate, a blank line dispatches, a JSON body's `type` field wins over
    the event: line. SSE comments (`: keepalive` — the gateway's idle-time
    frame) are skipped WITHOUT counting as activity. response.completed
    returns the inner response envelope; response.failed raises (a failed turn
    is not a servable answer). Any other typed event is real activity and
    resets the idle watch. A stream that ends without a terminal event raises
    (SOURCE A: that is a transient — the gateway died mid-turn).

    `lines` is any iterable of raw lines (the _sse_lines generator carries the
    idle enforcement; a plain list works for buffered bodies/tests).
    """
    event_name = ''
    data_lines = []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        line = raw.rstrip('\r\n')
        if line == '':
            if data_lines:
                data = '\n'.join(data_lines)
                try:
                    payload = json.loads(data)
                except ValueError:
                    payload = None        # non-JSON data: activity, keep reading
                evt = (payload.get('type') if isinstance(payload, dict) else None) \
                    or event_name
                if evt in ('response.completed', 'response.failed'):
                    if watch is not None:
                        watch.reset()
                    if on_event is not None:
                        on_event(evt)
                    if not isinstance(payload, dict) or \
                            not isinstance(payload.get('response'), dict):
                        raise ValueError(f'{evt} envelope without a response object')
                    if evt == 'response.failed':
                        raise ValueError('upstream turn failed: response.failed')
                    return payload['response']
                if evt:                    # any typed event = real activity
                    if watch is not None:
                        watch.reset()
                    if on_event is not None:
                        on_event(evt)
                event_name = ''
                data_lines = []
            continue
        if line.startswith(':'):
            # SSE comment — the gateway's keepalive. Deliberately NOT activity
            # (same ruling as the scheduler's readSSEResponse).
            continue
        if line.startswith('event:'):
            event_name = line[len('event:'):].strip()
            continue
        if line.startswith('data:'):
            data_lines.append(line[len('data:'):].strip())
            continue
        # Unknown line shape: ignore (fail-open; SOURCE A keeps reading too).
    raise ValueError('upstream SSE stream ended without a terminal event')


def _hermes_session_key_error(session_key):
    """Validate a caller-declared X-Hermes-Session-Key (SOURCE B rules).

    Returns None when usable (possibly the stripped value), or the 400 error
    message when the gateway itself would reject it. Absent/empty is valid —
    the header is optional.
    """
    if session_key is None:
        return None
    raw = str(session_key).strip()
    if not raw:
        return None
    if re.search(r'[\r\n\x00]', raw):
        return 'Invalid session key (control characters are not allowed)'
    if len(raw) > _HERMES_MAX_SESSION_HEADER_LEN:
        return f'Session key too long (max {_HERMES_MAX_SESSION_HEADER_LEN} chars)'
    return None



# ---------------------------------------------------------------- TR-151 UI API

_UI_LEDGER_DEFAULT_LIMIT = 50
_UI_LEDGER_MAX_LIMIT = 500
#: Rows the search will read before it stops. Reported back, never hidden: a search that silently
#: truncated is exactly how a browser looks complete when it is not.
_UI_LEDGER_SCAN_LIMIT = 200000


def _ui_one(query, name, default=None):
    v = (query or {}).get(name)
    if isinstance(v, list):
        v = v[0] if v else None
    return v if v not in (None, '') else default


def _ui_int(query, name, default):
    try:
        return int(str(_ui_one(query, name, default)).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _ui_float(query, name):
    v = _ui_one(query, name)
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def ui_ledger(query):
    """TR-151: search the outcome ledger and say how much of it was actually read.

    The ledger is the raw data every other stats surface summarises, so a browser over it must not
    imply completeness it does not have: every response carries rows_scanned, scan_limit,
    scan_truncated and total_matched, and `truncated` is true whenever either the scan window or the
    page cut the result short.

    `order=recent` (the default) reads the newest scan_limit rows and serves them newest-first: a
    browser over 325k rows has to start at the newest end, or it shows the oldest rows first and
    looks empty of anything current. `order=oldest` walks from the head for replay-style reading.
    """
    q = (_ui_one(query, 'q') or '').lower()
    f_outcome = _ui_one(query, 'outcome')
    f_source = _ui_one(query, 'complexity_source')
    f_provider = _ui_one(query, 'provider')
    f_model = _ui_one(query, 'model')
    f_band = _ui_one(query, 'band')
    since = _ui_float(query, 'since')
    until = _ui_float(query, 'until')
    order = (_ui_one(query, 'order') or 'recent').lower()
    limit = max(1, min(_ui_int(query, 'limit', _UI_LEDGER_DEFAULT_LIMIT), _UI_LEDGER_MAX_LIMIT))
    offset = max(0, _ui_int(query, 'offset', 0))
    scan_limit = max(1, _ui_int(query, 'scan_limit', _UI_LEDGER_SCAN_LIMIT))

    path = router_outcomes.outcomes_path()
    size = os.path.getsize(path) if os.path.exists(path) else None
    scanned = 0
    matched = 0
    page = []
    scan_truncated = False

    def _lines():
        if order != 'recent':
            with open(path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if line.strip():
                        yield line
            return
        import collections
        tail = collections.deque(maxlen=scan_limit)
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if line.strip():
                    tail.append(line)
        nonlocal scan_truncated
        scan_truncated = len(tail) >= scan_limit
        for line in reversed(tail):
            yield line

    try:
        for line in _lines():
            if scanned >= scan_limit:
                scan_truncated = True
                break
            scanned += 1
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if f_provider and d.get('provider') != f_provider:
                continue
            if f_model and d.get('model') != f_model:
                continue
            if f_source and d.get('complexity_source') != f_source:
                continue
            if f_band and str(d.get('complexity_sig') or '') != f_band:
                continue
            if f_outcome:
                ok = d.get('success')
                outcome = ('success' if ok is True else
                           'failed' if ok is False else
                           str(d.get('route_outcome') or 'unknown'))
                if f_outcome not in (outcome, str(d.get('route_outcome') or '')):
                    continue
            ts = d.get('ts')
            if since is not None and not (isinstance(ts, (int, float)) and ts >= since):
                continue
            if until is not None and not (isinstance(ts, (int, float)) and ts <= until):
                continue
            if q:
                hay = ' '.join(str(d.get(k) or '') for k in (
                    'provider', 'model', 'session_id', 'task_label', 'failure_reason',
                    'served_by_hop', 'complexity_sig', 'source_system', 'price_basis')).lower()
                if q not in hay:
                    continue
            matched += 1
            if offset <= matched - 1 < offset + limit:
                page.append(d)
    except OSError as e:
        return {'error': f'ledger unreadable: {e}', 'store': path, 'rows': [],
                'total_matched': 0, 'rows_scanned': 0, 'truncated': False,
                'scan_truncated': False}
    return {'rows': page, 'returned': len(page), 'total_matched': matched,
            'rows_scanned': scanned, 'scan_limit': scan_limit,
            'scan_truncated': scan_truncated,
            'order': order, 'scan_window': 'newest-first' if order == 'recent' else 'oldest-first',
            'offset': offset, 'limit': limit,
            'truncated': scan_truncated or matched > offset + len(page),
            'store': path, 'store_bytes': size,
            'filters': {'q': q or None, 'outcome': f_outcome, 'complexity_source': f_source,
                        'provider': f_provider, 'model': f_model, 'band': f_band,
                        'since': since, 'until': until}}


def _hermes_capabilities_metadata(base, _opener=None):
    """Read session metadata from the upstream's /v1/capabilities at startup.

    Fails OPEN: any problem is recorded in the returned dict as `error` and
    the proxy serves /v1/responses anyway (the capabilities endpoint is
    advisory; the wire behavior does not depend on it).
    """
    opener = _opener or urllib.request.urlopen
    out = {}
    try:
        req = urllib.request.Request(
            base.rstrip('/') + '/v1/capabilities',
            headers={'User-Agent': 'task-router-proxy/1.0'})
        with opener(req, timeout=10) as resp:
            doc = json.loads(resp.read())
        if not isinstance(doc, dict):
            raise ValueError('capabilities payload is not an object')
        out['object'] = doc.get('object')
        out['is_hermes_gateway'] = doc.get('object') == _HERMES_CAPABILITY_MARKER
        features = doc.get('features') if isinstance(doc.get('features'), dict) else {}
        out['responses_api'] = bool(features.get('responses_api'))
        out['session_key_header'] = features.get('session_key_header')
        out['session_continuity_header'] = features.get('session_continuity_header')
        endpoints = doc.get('endpoints') if isinstance(doc.get('endpoints'), dict) else {}
        ep = endpoints.get('responses') or {}
        out['responses_endpoint'] = ep.get('path')
        out['responses_method'] = ep.get('method')
    except Exception as exc:  # noqa: BLE001 — advisory probe must never block startup
        out['error'] = f'capabilities probe failed: {str(exc)[:200]}'
    return out


def _hermes_upstream_base():
    return os.environ.get('ROUTER_PROXY_UPSTREAM', _HERMES_DEFAULT_UPSTREAM)


def _hermes_responses_call(path, body, headers, _opener=None):
    """One hop to the REAL Hermes gateway on /v1/responses (SOURCE A/B wire).

    Returns (status, payload_dict, session_id). A streamed answer is consumed
    through the SSE parser (keepalives skipped, idle deadline enforced); a
    buffered JSON answer passes through. X-Hermes-Session-Id is read from the
    RESPONSE headers in both cases. HTTP error statuses are returned (not
    raised) so the ladder treats them as a step; transport failures raise.
    """
    base = _hermes_upstream_base()
    fwd_headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'task-router-proxy/1.0',
    }
    for name in ('authorization', 'x-api-key'):
        if headers.get(name):
            fwd_headers[name] = headers[name]
    session_key = headers.get('x-hermes-session-key')
    if session_key:
        fwd_headers['X-Hermes-Session-Key'] = session_key
    fwd_body = dict(body)
    want_stream = bool(fwd_body.pop('stream', None))
    timeout = max(_proxy_hop_timeout_s(), _hermes_idle_timeout_s())
    req = urllib.request.Request(
        base.rstrip('/') + path,
        data=json.dumps(fwd_body).encode(),
        headers=fwd_headers,
        method='POST')
    opener = _opener or urllib.request.urlopen
    try:
        resp = opener(req, timeout=timeout)
    except urllib.error.HTTPError as exc:      # a REAL response, not transport
        try:
            detail = exc.read().decode(errors='replace')[:400]
        except Exception:  # noqa: BLE001
            detail = ''
        return exc.code, {'error': detail or f'HTTP {exc.code}'}, ''
    if not hasattr(resp, '__enter__'):         # test double returning bare object
        resp = _BareResponse(resp)
    try:
        session_id = resp.headers.get('X-Hermes-Session-Id') or ''
        ctype = resp.headers.get('Content-Type') or ''
        if want_stream or 'text/event-stream' in ctype:
            events = []
            result = _read_hermes_sse(
                _sse_lines(resp, _HermesIdleWatch(_hermes_idle_timeout_s())),
                on_event=events.append)
            resp._router_events = events       # test visibility only
            return 200, result, session_id
        raw = resp.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            return (resp.status if isinstance(resp.status, int) else 200), \
                {'error': 'upstream returned non-JSON'}, session_id
        return (resp.status if isinstance(resp.status, int) else 200), payload, \
            session_id
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


class _BareResponse:
    """Adapt a bare (non-context-manager) response object to the interface
    _hermes_responses_call expects (headers/status/read/close)."""

    def __init__(self, resp):
        self._resp = resp

    @property
    def status(self):
        return getattr(self._resp, 'status', None)

    @property
    def headers(self):
        return getattr(self._resp, 'headers', _HeadersLike())

    def read(self, n=-1):
        return self._resp.read(n)

    def readline(self):
        return self._resp.readline()

    def close(self):
        try:
            self._resp.close()
        except Exception:  # noqa: BLE001
            pass


class _HeadersLike:
    def get(self, name, default=None):
        return default

    def close(self):
        pass


def _hermes_proxy_chat(body, headers, upstream=None):
    """TR-129 Path H: /v1/responses through a REAL Hermes gateway upstream.

    Reuses the ladder (chain, metering, outcome rows, fail-open) from
    proxy_chat and only swaps the hop: each attempt goes out with the caller's
    X-Hermes-Session-Key and the gateway's stream semantics (idle deadline, no
    stream wish forwarded — the gateway streams when its answer is SSE, which
    the parser handles either way). The caller gets the gateway's response
    envelope plus additive _router metadata with the session echo.
    """
    upstream = upstream or _UPSTREAM_CALL or _hermes_responses_call
    hermes_session_key = None
    key_error = None
    if isinstance(headers, dict):
        raw_key = headers.get('X-Hermes-Session-Key')
        if raw_key is None:
            raw_key = headers.get('x-hermes-session-key')
        if raw_key:
            key_error = _hermes_session_key_error(raw_key)
            if key_error is None:
                hermes_session_key = str(raw_key).strip()
    if key_error:
        return 400, {'error': key_error}
    t0 = time.time()

    def _hop(path, hop_body, hop_headers):
        merged = {**(headers if isinstance(headers, dict) else {})}
        if hermes_session_key:
            merged['x-hermes-session-key'] = hermes_session_key
        status, payload, session_id = upstream(path, hop_body, merged)
        if session_id:
            payload = dict(payload)
            payload['_router_hermes_session_id'] = session_id
        return status, payload

    status, payload = proxy_chat('/v1/responses', body, headers,
                                 upstream=_hop)
    wall = round(time.time() - t0, 3)
    out = payload if isinstance(payload, dict) else {'upstream': payload}
    meta = out.get('_router')
    if isinstance(meta, dict):
        session_id = out.pop('_router_hermes_session_id', '') or ''
        # Echo ONLY what the gateway actually advertised — never a fabricated id.
        meta['hermes_session_id'] = str(session_id)[:256]
        meta['hermes_session_key'] = hermes_session_key
        meta['hermes_wall_time_s'] = wall
        meta['hermes_idle_timeout_s'] = _hermes_idle_timeout_s()
    return status, out

#: Injectable upstream call for tests: (path, body, headers) -> (status, payload)
_UPSTREAM_CALL = None


#: Every way a hop can end without serving an answer (TR-137). A DEADLINE hit is
#: deliberately its own class: measured 2026-09-24, every hop killed by the 180s
#: budget reported `transport-failure`/status=0, so a slow-but-alive model was
#: indistinguishable from a dead one — exactly the information a fallback
#: decision needs. Keep this tuple the single source of the vocabulary; the
#: tests import it so a new code cannot drift in unannounced.
HOP_FAILURE_REASONS = (
    "idle-timeout",      # the gateway SSE idle deadline fired (no real event in budget)
    "hop-wall-timeout",  # the transport budget expired (slow, not necessarily dead)
    "transport-error",   # connection refused / DNS / reset / TLS
    "upstream-4xx",      # the upstream answered with a client error
    "upstream-5xx",      # the upstream answered with a server error
    "unservable-2xx",    # 2xx carrying an error envelope and no content
    "no-hops",           # nothing eligible to attempt
)


def _classify_hop_failure(exc=None, status=None, unservable=False):
    """(reason_code, detail) for one failed hop attempt.

    Timeouts are matched by exception type AND by message, because the two
    budgets surface differently: the SSE idle deadline raises the module's own
    _HermesIdleTimeout, while the transport wall raises socket.timeout /
    TimeoutError from urllib. Anything unrecognised degrades to
    transport-error with the exception name in the detail — never to a code the
    tuple does not define.
    """
    if unservable:
        return "unservable-2xx", "2xx with an error envelope and no choices"
    if exc is not None:
        text = str(exc)
        low = text.lower()
        # The idle deadline is decided by its own message FIRST: the exception
        # type varies (the gateway watch may surface a plain RuntimeError with
        # the deadline text), and a message that says "idle" is decisive on its
        # own. Checking the type before the text mislabelled it transport-error.
        if "idle" in low:
            return "idle-timeout", text or "no real SSE event inside the idle budget"
        timeoutish = isinstance(exc, (TimeoutError,)) or type(exc).__name__ in (
            "timeout", "Timeout", "socket.timeout", "_HermesIdleTimeout")
        if timeoutish or "timed out" in low or "timeout" in low:
            return "hop-wall-timeout", text or "transport budget expired"
        return "transport-error", f"{type(exc).__name__}: {text}"[:200]
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        code = 0
    if code <= 0:
        return "transport-error", "no HTTP status (transport failure)"
    if 400 <= code < 500:
        return "upstream-4xx", f"upstream HTTP {code}"
    if code >= 500:
        return "upstream-5xx", f"upstream HTTP {code}"
    return "transport-error", f"unclassified status {code}"


def _failure_envelope(meta, session_id, source_system, parent_session_id, ladder_t0,
                      terminal, tried, error_reason):
    """The _router block for ANY path that served nothing (TR-136).

    Measured 2026-09-24: a real proxied 502 returned served_by/usage/cost/
    session/wall_time all null while the ledger held the chain, the hops and the
    latencies. There are TWO such exits in this function (nothing eligible, and
    an exhausted ladder) and both must explain themselves identically — which is
    why the block lives here instead of being copied into each return.
    """
    return {**meta,
            'served_by': None,
            'served_by_reason': error_reason,
            'rolling': None,
            'rolling_reason': 'no served lane to average',
            'gateway_session_id': None,
            'gateway_session_reason': 'no hop produced a session id',
            # the success envelope names the caller's declared session; the failure
            # envelope must carry the same key or the superset contract breaks
            'parent_session_id': parent_session_id,
            'exhausted': True,
            'terminal_reason': terminal,
            'hops_attempted': len(tried),
            # TR-148: parity with the success envelope — one field, both paths.
            'steps': len(tried),
            'usage': None, 'usage_reason': 'no hop produced a usage block',
            'cost_usd': None, 'cost_reason': 'no served hop to price',
            'session_id': session_id,
            'outcome_row': {'source_system': source_system, 'session_id': session_id,
                            'parent_session_id': parent_session_id},
            'wall_time_s': round(time.time() - ladder_t0, 3)}


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


#: Internal routing markers. They are EVIDENCE for the caller's envelope and the
#: ledger, and several in-process consumers read them off the payload — but a
#: model-answer body is not the place for them: an OpenAI client would have to
#: filter them out of `choices`. One place decides.
_INTERNAL_BODY_KEYS = ('_router_hermes_session_id', '_router_stream')


def _client_body(payload):
    """The client-visible body: the answer plus `_router`, minus internal markers."""
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if k not in _INTERNAL_BODY_KEYS}


def _stream_hops_enabled():
    """Ask gateway hops to stream (TR-138). Default ON; 0/false disables."""
    raw = (os.environ.get('ROUTER_PROXY_STREAM_HOPS') or '').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


def _proxy_idle_budget_s():
    """Idle budget for a proxied HOP (TR-138). Distinct from the wall budget.

    Measured 2026-09-24: a real long prompt through :9391 died at exactly
    180.1s x 3 hops because the hop budget was a WALL clock — a turn that was
    alive and working was killed for being slow. An idle deadline is the better
    shape: a hop that keeps producing events may run as long as it needs.

    DO NOT justify this budget by "the gateway keepalives keep a slow hop warm".
    Measured 2026-09-26 that premise is false: a turn whose TOOL ran quietly for
    330s produced no events at all and died at ~305s on two hops
    (failure_reason=idle-timeout). An idle deadline cannot tell a slow tool from
    a dead upstream, so the budget must come from the CALLER's tolerance, never
    from a hope about keepalives.

    CALLER TOLERANCE RULE: this budget must be >= the caller's per-turn
    tolerance (the scheduler's SCHEDULER_GATEWAY_RESPONSE_TIMEOUT, 30m default,
    idle mode). A middle layer that is stricter than its caller does not protect
    anything — it converts a working turn into a failure and burns the fallback
    hops doing it (3 hops x 5m of the caller's tick, observed). Set from
    ROUTER_PROXY_IDLE_TIMEOUT_S; the live value is 1800s.
    """
    raw = os.environ.get('ROUTER_PROXY_IDLE_TIMEOUT_S', '')
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _hermes_idle_timeout_s()
    return val if val > 0 else _hermes_idle_timeout_s()


def _proxy_wall_ceiling_s():
    """The ABSOLUTE per-hop ceiling (a backstop, not the primary budget).

    The idle watch bounds a stalled stream; this bounds a pathological one that
    keeps dribbling events forever. Without it a runaway hop holds a caller's
    whole tick indefinitely (the reason the wall existed at all).
    """
    raw = os.environ.get('ROUTER_PROXY_HOP_WALL_S', '3600')
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 3600.0
    return val if val > 0 else 3600.0


def _collect_openai_stream(lines, path='/v1/chat/completions', on_event=None, watch=None):
    """Assemble an SSE stream into the buffered payload a non-streaming client expects.

    Mirror semantics are preserved deliberately: the client asked for a buffered
    answer (the proxy strips `stream` for that reason) and still gets one. What
    changes is that we consume the upstream AS a stream, so the idle watch can
    see progress and a long turn survives. Returns the accumulated
    chat-completions (or responses) payload, with the live stream facts attached
    so the row and the envelope can prove it streamed.
    """
    text, reasoning, role = [], [], None
    usage, model, finish = None, None, None
    events = 0

    def handle(event_name, data):
        nonlocal events, usage, model, finish
        events += 1
        # TR-138: a real frame is activity, so it resets the idle watch. Without
        # this the watch would fire on a turn that is working steadily but takes
        # longer than the budget — the exact defect this fix exists to remove.
        # (Keepalives do not reset it: they are connection liveness, not work.)
        if watch is not None:
            watch.reset()
        if on_event:
            on_event(event_name, data)
        if not isinstance(data, dict):
            return
        if isinstance(data.get('model'), str) and data['model']:
            model = data['model']
        if isinstance(data.get('usage'), dict):
            usage = data['usage']
        for choice in (data.get('choices') or []):
            if not isinstance(choice, dict):
                continue
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
            delta = choice.get('delta') or choice.get('message') or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get('role'), str):
                role = delta['role']
            for key, sink in (('content', text), ('reasoning_content', reasoning)):
                v = delta.get(key)
                if isinstance(v, str):
                    sink.append(v)
        # /v1/responses shape: a completed envelope carries the final text
        if data.get('type') == 'response.completed':
            resp = data.get('response') or {}
            if isinstance(resp.get('model'), str):
                model = resp['model']
            if isinstance(resp.get('usage'), dict):
                usage = resp['usage']
            for item in (resp.get('output') or []):
                for chunk in (item.get('content') or []) if isinstance(item, dict) else []:
                    if isinstance(chunk, dict) and isinstance(chunk.get('text'), str):
                        text.append(chunk['text'])

    event_name, data_lines = '', []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        line = raw.rstrip('\r\n')
        if line == '':
            if data_lines:
                blob = '\n'.join(data_lines)
                try:
                    parsed = json.loads(blob)
                except ValueError:
                    parsed = None
                if parsed is not None:
                    handle(event_name, parsed)
            event_name, data_lines = '', []
            continue
        if line.startswith(':'):
            continue    # keepalive: activity for the watch, not data for the answer
        if line.startswith('event:'):
            event_name = line[6:].strip()
        elif line.startswith('data:'):
            data_lines.append(line[5:].strip())

    body = ''.join(text)   # deltas are text fragments: concatenate, never join
    payload = {'choices': [{'index': 0, 'message': {'role': role or 'assistant', 'content': body},
                            'finish_reason': finish or 'stop'}],
               'model': model, 'usage': usage}
    if reasoning:
        payload['choices'][0]['message']['reasoning_content'] = ''.join(reasoning)
    # the streamed facts, for the row/envelope (TR-138 evidence)
    payload['_router_stream'] = {'events': events, 'streamed': True,
                                 'idle_budget_s': _proxy_idle_budget_s(),
                                 'wall_ceiling_s': _proxy_wall_ceiling_s()}
    return payload


def _proxy_upstream_default(path, body, headers):
    """POST the request to the upstream gateway (default: the Hermes gateway
    on localhost). Returns (status, payload-dict). Transport failure raises —
    the ladder treats it exactly like a 5xx."""
    base = os.environ.get('ROUTER_PROXY_UPSTREAM', 'http://127.0.0.1:8642')
    # TR-138: ask the gateway to STREAM even though the client wants a buffered
    # answer. Measured 2026-09-24: three live hops were killed at exactly 180.1s
    # each (502 after 553s) because the budget was a wall clock, while the
    # gateway was emitting tool progress and `: keepalive` every 10s — a
    # slow-but-alive turn looked dead. A stream is the only way to see progress,
    # so the idle watch can replace the wall. This decision lives HERE, in the
    # gateway caller, not in the ladder: the ladder's body contract (TR-120
    # strips the client's stream wish) and any provider-specific upstream are
    # untouched, and the assembled answer is what the caller gets.
    ask_stream = (_stream_hops_enabled() and str(path).rstrip('/').endswith('/chat/completions'))
    send_body = {**body, 'stream': True} if ask_stream else body
    req = urllib.request.Request(
        base.rstrip('/') + path, data=json.dumps(send_body).encode(),
        headers={k: v for k, v in headers.items()
                 if k.lower() in ('authorization', 'x-api-key', 'content-type')}
        | {'Content-Type': 'application/json', 'User-Agent': 'task-router-proxy/1.0'})
    # TR-138: when the hop asked the upstream to stream, the budget is the IDLE
    # watch (plus the wall as a backstop), and the stream is assembled into the
    # buffered payload the client asked for. A hop that cannot stream is
    # unaffected: same wall, same buffered read.
    want_stream = ask_stream or bool(body.get('stream'))
    budget = (max(_proxy_idle_budget_s(), _proxy_wall_ceiling_s()) if want_stream
              else _proxy_hop_timeout_s())
    try:
        with urllib.request.urlopen(req, timeout=budget) as resp:
            ctype = ''
            try:
                ctype = (resp.headers.get('Content-Type') or '')
            except Exception:  # noqa: BLE001 — test doubles may lack headers
                ctype = ''
            # TR-145: capture the GATEWAY's session id and hand it back with the
            # payload, so the row can be joined to the Hermes session it served.
            # The /v1/responses path has done this since TR-129, but chat
            # completions — the shape every harness and the scheduler actually
            # send — never captured it, so every row from that path carried
            # gateway_session_id: null and "which Hermes session did this pay for"
            # was unanswerable. The header rides the RESPONSE headers, so it is
            # available before the body is read (streamed or not).
            gw_session = ''
            try:
                gw_session = resp.headers.get('X-Hermes-Session-Id') or ''
            except Exception:  # noqa: BLE001 — test doubles may lack headers
                gw_session = ''
            # Assemble ONLY a real stream. Asking for one does not guarantee it:
            # an upstream that ignores `stream` answers with JSON, and treating
            # that body as SSE produced an empty answer (caught by the chat-shape
            # regression guard). The content type is the fact, the request is
            # only a wish.
            if 'text/event-stream' in ctype:
                watch = _HermesIdleWatch(_proxy_idle_budget_s())
                payload = _collect_openai_stream(_sse_lines(resp, watch), path, watch=watch)
                if gw_session:
                    payload['_router_hermes_session_id'] = gw_session
                return 200, payload
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
    if gw_session and isinstance(payload, dict) and not payload.get('_router_hermes_session_id'):
        payload['_router_hermes_session_id'] = gw_session
    return (status if isinstance(status, int) else 200), payload


def _provider_upstream_factory(provider_id, providers_map):
    """Return a hop-specific upstream call function for a provider, or None.

    If the provider row carries api_base_url, the returned function POSTs
    directly to that URL (the 'last mile'), injecting the provider's own API
    key from the env var named in api_key_env.  If no api_base_url is set for
    this provider, returns None — caller should use the global upstream."""
    info = providers_map.get(provider_id, {})
    base = info.get('api_base_url')
    if not base:
        return None
    key_env = info.get('api_key_env', '')
    api_key = os.environ.get(key_env, '') if key_env else ''

    def _call(path, body, headers):
        req = urllib.request.Request(
            base.rstrip('/') + path,
            data=json.dumps(body).encode(),
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'task-router-proxy/1.0',
                **({'Authorization': f'Bearer {api_key}'} if api_key else {}),
            })
        try:
            with urllib.request.urlopen(req, timeout=_proxy_hop_timeout_s()) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {'error': exc.read().decode()[:400]}
        except Exception as exc:
            return 0, {'error': str(exc)[:300]}
    return _call


_PROVIDER_ROUTING_CACHE = None


def _load_provider_routing():
    """Load providers.jsonl and return {provider_id: {api_base_url, api_key_env}}.
    Cached in-process after first call."""
    global _PROVIDER_ROUTING_CACHE
    if _PROVIDER_ROUTING_CACHE is not None:
        return _PROVIDER_ROUTING_CACHE
    path = os.path.join(DATA_DIR, 'providers.jsonl')
    result = {}
    try:
        with open(path) as f:
            for line in f:
                row = json.loads(line)
                pid = row.get('id')
                if pid and row.get('api_base_url'):
                    result[pid] = {
                        'api_base_url': row['api_base_url'],
                        'api_key_env': row.get('api_key_env', ''),
                    }
    except Exception:
        pass
    _PROVIDER_ROUTING_CACHE = result
    return result


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


_CATS_CACHE: list = []
_CATS_CACHE_AT = 0.0


def _registry_categories(registry_path=None):
    """The registry's category list — data-driven: whatever the profiles declare.

    Used to build a FLOOR requirement set when the classifier reports that a prompt
    presses no category (TR-161). Returns [] when the registry cannot be read, so the
    caller can fall back to the previous behaviour instead of inventing a scale.
    """
    global _CATS_CACHE_AT, _CATS_CACHE
    ttl = 300.0
    if registry_path is None and _CATS_CACHE and (time.time() - _CATS_CACHE_AT) < ttl:
        return list(_CATS_CACHE)
    path = Path(registry_path or os.environ.get("ROUTING_REGISTRY", REPO / "registry.json"))
    try:
        # 2.8 MB of generated registry: parse once per TTL, never per request.
        tables = (json.loads(path.read_text()) or {}).get("tables", {})
    except (ValueError, OSError):
        return []
    cats = []
    for r in tables.get("task_profile_requirements") or []:
        c = r.get("category")
        if c and c not in cats:
            cats.append(c)
    if registry_path is None:
        _CATS_CACHE_AT, _CATS_CACHE = time.time(), list(cats)
    return cats


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


def _proxy_usage_full(payload):
    """Every meter the wire reports, each None when unreported (TR-143).

    The row was blind to cache traffic: 291 lanes now carry a published
    cache-read rate (a5b0bf0) and the economics of a long agent turn are mostly
    cache reads, so a cost-per-task average that ignores them overstates the
    cost of exactly the workloads this proxy exists to serve. Both wire shapes
    are read (OpenAI `prompt_tokens_details.cached_tokens`, OpenAI-compatible
    `*_input_tokens` detail blocks) and an absent detail block stays None —
    never 0, because 0 cached tokens and "the provider did not say" are
    different facts and only one of them is free.
    """
    tokens_in, tokens_out = _proxy_usage(payload)
    out = {'tokens_in': tokens_in, 'tokens_out': tokens_out,
           'cache_read_tokens': None, 'cache_write_tokens': None,
           'tokens_reasoning': None}
    if not isinstance(payload, dict) or not isinstance(payload.get('usage'), dict):
        return out
    usage = payload['usage']

    def deep(*path):
        cur = usage
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur if isinstance(cur, int) and not isinstance(cur, bool) else None

    cache_read = (deep('prompt_tokens_details', 'cached_tokens')
                  or deep('input_tokens_details', 'cached_tokens'))
    if cache_read is None:
        v = usage.get('cache_read_input_tokens')
        cache_read = v if isinstance(v, int) and not isinstance(v, bool) else None
    out['cache_read_tokens'] = cache_read
    v = usage.get('cache_creation_input_tokens')
    out['cache_write_tokens'] = v if isinstance(v, int) and not isinstance(v, bool) else None
    out['tokens_reasoning'] = (deep('completion_tokens_details', 'reasoning_tokens')
                               or deep('output_tokens_details', 'reasoning_tokens'))
    return out


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



#: How much of the option chain and the gate evidence a row carries. A chain can hold
#: 160+ lanes; a row is read by a human or a query, and an unbounded list makes both
#: useless (and the ledger huge). Truncation is EXPLICIT (chain_truncated) — a capped
#: list that does not say it was capped is a lie about the resolver's options.
_PROXY_CHAIN_ROW_CAP = 20
_PROXY_EXCLUSION_ROW_CAP = 30


def _prompt_evidence(body):
    """(chars, sha256-hex) of the caller's prompt text — never the text itself.

    Rows are queried by GROUPING: a fleet tick's prompt recurs almost verbatim across
    nudges, so the hash lets a reader ask "how many requests shared this prompt" without
    the ledger becoming a store of everything the fleet has ever been asked. Returns
    (None, None) when no text can be found — an unmeasured row says nothing, like every
    other meter here.
    """
    try:
        parts = []
        if isinstance(body, dict):
            for m in (body.get('messages') or []):
                if isinstance(m, dict):
                    c = m.get('content')
                    if isinstance(c, str):
                        parts.append(c)
                    elif isinstance(c, list):
                        parts.extend(str(x.get('text') or '') for x in c if isinstance(x, dict))
            for m in (body.get('input') or []):
                if isinstance(m, dict):
                    c = m.get('content')
                    if isinstance(c, str):
                        parts.append(c)
        text = '\n'.join(parts) if parts else ''
        if not text:
            return None, None
        import hashlib as _hl
        return len(text), _hl.sha256(text.encode('utf-8', 'replace')).hexdigest()
    except Exception:  # noqa: BLE001 — evidence gathering never fails a request
        return None, None


def _classifier_evidence(requirements, source):
    """Why the rating is what it is, from the row alone.

    `parse` is the single word a reader needs: ok (the classifier produced a usable
    matrix), empty-matrix (it ran and said this prompt presses no category),
    no-json (it answered in prose and the parse failed -> fail-open),
    declared (the CALLER named a profile, so the classifier never ran).
    """
    if source in ('classifier', 'jev'):
        parse = 'ok'
    elif source == 'classifier-empty':
        parse = 'empty-matrix'
    elif source == 'declared':
        parse = 'declared'
    else:
        parse = 'no-json'
    try:
        problems = list(requirements.get('problems') or [])[:6]
    except Exception:  # noqa: BLE001
        problems = []
    return {'source': source, 'parse': parse, 'model': requirements.get('model'),
            'prompt_version': requirements.get('prompt_version'),
            'confidence': requirements.get('confidence'),
            'problems': problems}


def _chain_evidence(resolved, chain):
    """The OPTION CHAIN as resolvable evidence: what the resolver offered, in order.

    Shipped after the 2026-09-26 incident: a row read 'route_outcome no-hops,
    hops_attempted 0' and could not say WHY nothing was eligible — the exclusions had
    been computed and handed to the envelope, then dropped on the floor before the row
    was written. The ledger is the artefact that survives; it has to explain itself.
    """
    def _hops(items, cap):
        out = []
        for h in (items or [])[:cap]:
            if not isinstance(h, dict):
                continue
            out.append({k: h.get(k) for k in
                        ('hop', 'provider', 'model', 'why', 'codes', 'price', 'effective_price')
                        if k in h})
        return out
    chain = chain or []
    excl = (resolved or {}).get('exclusions') or []
    return {
        'chain': _hops(chain, _PROXY_CHAIN_ROW_CAP),
        'chain_length': len(chain),
        'chain_truncated': len(chain) > _PROXY_CHAIN_ROW_CAP,
        'exclusions': _hops(excl, _PROXY_EXCLUSION_ROW_CAP),
        'exclusions_truncated': len(excl) > _PROXY_EXCLUSION_ROW_CAP,
        'skipped_hops': (resolved or {}).get('skipped_hops'),
        'first_attempt_hop': (resolved or {}).get('first_attempt_hop'),
        'gate': (resolved or {}).get('gate'),
    }


# ---------------------------------------------------------------------------
# TR-172 Session stats in the ledger.
#
# Bane's design intent: the end-of-request reply carries the SESSION's accounting
# (tokens, time, turns, other stats) and that belongs in the JSONL.
#
# Measured against the live gateway (2026-09-26) on both wire shapes: the reply does
# NOT carry them. A /v1/chat/completions body is {choices, created, id, model, object,
# usage{prompt_tokens, completion_tokens, total_tokens}}, the streamed /v1/responses
# final event is {id, object, status, created_at, model, output[], usage{input_tokens,
# output_tokens, total_tokens}}, and the only session fact either one sends is the
# `X-Hermes-Session-Id` HEADER. Turns, cumulative session tokens, cache/reasoning
# splits, duration, tool calls and cost are all absent — the per-request `usage` is
# coarse (47,030/2 on a measured one-word turn) while the session's own fine-grained
# meters live in the gateway's state.db.
#
# So the router FETCHES them for the session it just used and stamps them on the row.
# Read-only, one bounded lookup per request, and every field None when the session
# cannot be read — a missing measurement is never an invented 0.
# ---------------------------------------------------------------------------
def _hermes_state_db():
    """The gateway's state.db path (env override wins, then the standard location)."""
    return (os.environ.get('ROUTER_HERMES_STATE_DB')
            or str(Path.home() / '.hermes' / 'state.db'))


#: Columns the row wants, in the shape state.db stores them. Kept as data so a schema
#: move shows up as a missing field rather than an exception in the request path.
_SESSION_FIELDS = ('message_count', 'tool_call_count', 'input_tokens', 'output_tokens',
                   'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
                   'api_call_count', 'started_at', 'last_activity_at', 'title',
                   'billing_provider', 'billing_base_url', 'estimated_cost_usd',
                   'actual_cost_usd', 'cost_status', 'cost_source')


def _hermes_session_stats(gateway_session_id, timeout_s=3.0):
    """The session's own accounting, straight from the gateway's state.db.

    Returns None when there is no id to look up, the session is not there yet, or the
    database cannot be read — the caller stamps None and says nothing false. Never
    raises and never blocks the request path beyond `timeout_s`.
    """
    if not gateway_session_id:
        return None
    import sqlite3
    db = _hermes_state_db()
    con = None
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=timeout_s)
        cur = con.execute(
            'SELECT ' + ', '.join(_SESSION_FIELDS) + ' FROM sessions WHERE id = ?',
            (gateway_session_id,))
        row = cur.fetchone()
        if not row:
            return None
        stats = dict(zip(_SESSION_FIELDS, row))

        def _num(v):
            """state.db may store these as TEXT; a tokens field that is a string breaks
            every downstream sum silently, so coerce, and leave an unparseable value
            alone rather than inventing one."""
            if v is None or isinstance(v, (int, float)):
                return v
            try:
                f = float(v)
            except (TypeError, ValueError):
                return v
            return int(f) if f.is_integer() else f

        for _k in ('message_count', 'tool_call_count', 'input_tokens', 'output_tokens',
                   'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
                   'api_call_count', 'started_at', 'last_activity_at',
                   'estimated_cost_usd', 'actual_cost_usd'):
            if _k in stats:
                stats[_k] = _num(stats[_k])
        # turns: the session's own api_call_count IS the turn count. `turns` has been a
        # hardcoded None on every proxy row since the field was added; this is the fact
        # it was waiting for.
        stats['turns'] = stats.get('api_call_count')
        # duration: measured from the session's own clock, only when both ends are
        # present (state.db stores epoch seconds).
        try:
            if stats.get('started_at') is not None and stats.get('last_activity_at') is not None:
                stats['duration_s'] = round(
                    float(stats['last_activity_at']) - float(stats['started_at']), 3)
        except (TypeError, ValueError):
            stats['duration_s'] = None
        # per-model split: what this session actually ran on, and how much of it.
        try:
            models = con.execute(
                'SELECT model, billing_provider, api_call_count, input_tokens, output_tokens '
                'FROM session_model_usage WHERE session_id = ? ORDER BY api_call_count DESC',
                (gateway_session_id,)).fetchall()
            stats['models'] = [{'model': m, 'provider': p, 'api_calls': c,
                                'tokens_in': ti, 'tokens_out': to} for m, p, c, ti, to in models]
        except Exception:  # noqa: BLE001
            stats['models'] = None
        stats['source'] = 'state.db'
        return stats
    except Exception:  # noqa: BLE001 — the ledger must never fail a served request
        return None
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:  # noqa: BLE001
            pass


_NOT_A_LANE = frozenset(('', 'none', 'unknown', 'n/a', 'null'))


def _circuit_class(reason):
    """Decide the failure CLASS, because the class decides the blast radius (TR-182).

    HARD (api_down 1800s / out_of_credit 14400s) opens a PROVIDER-WIDE breaker once >=3 of
    the same class land inside the class window, across any model of that provider; SOFT
    (overload 120s / quota_window 300s) gates only the (provider, model) pair.

    Every proxy failure used to be recorded with NO class, so all of them took the hard
    default: three slow hops or three rate-limited hops removed a whole provider for 30
    minutes. That is the mechanism measured in the 2026-09-25 lockup -- 65 failed rows
    became provider-wide breakers, then every chain was gated, then every request died as
    "no open hop". A timeout is not a provider outage, so it must not be recorded as one.
    """
    r = (reason or '').lower()
    if any(t in r for t in ('timeout', 'timed out', 'idle', 'deadline', 'too slow')):
        return 'overload'
    if any(t in r for t in ('429', 'quota', 'rate limit', 'rate-limit', 'rate_limit')):
        return 'quota_window'
    return 'api_down'


def _proxy_record(provider, model, ok, requirements, reason='', latency_s=None,
                  source='router-proxy', session_id=None,
                  tokens_in=None, tokens_out=None, cost_usd=None,
                  parent_session_id=None, price_basis=None,
                  cache_read_tokens=None, cache_write_tokens=None,
                  tokens_reasoning=None, gateway_session_id=None,
                  route_outcome=None, failure_reason=None, hops_attempted=None,
                  served_by_hop=None, max_hops=None, complexity_source=None,
                  degrade_reason=None, steps=None, chain_evidence=None,
                  classifier_evidence=None, attempts=None,
                  prompt_chars=None, prompt_sha=None, session_stats=None):
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
               # The store contract lists `complexity` AND `required_categories`
               # (the latter is the matrix). Proxy rows only ever carried the
               # richer pair (matrix + sig), so a row read through the documented
               # STORE_FIELDS came up short; the alias is filled from the same
               # matrix rather than invented.
               'complexity': requirements.get('matrix'),
               'required_categories': requirements.get('matrix'),
               'complexity_sig': requirements.get('complexity_sig'),
               'profile_id': requirements.get('profile_id'),
               'turns': None,
               # TR-172: the SESSION's own accounting (its turn count, cumulative
               # tokens, duration, tool calls, cost) fetched from the gateway's
               # state.db, because the reply only carries per-request usage plus a
               # session-id header. Deliberately NOT folded into `turns`: that field
               # already means the accumulated turns of THIS ledger row, and two
               # different quantities under one name is how a column stops being
               # trustworthy.
               'session': session_stats,
               # TR-143: steps used to be the constant 1 on every row, so a
               # task that needed four fallback hops and one that answered
               # first-try were the same row. Real step count (the ladder
               # attempts this request made) is what makes the average
               # meaningful.
               'steps': steps if steps is not None else 1,
               'tokens_in': tokens_in, 'tokens_out': tokens_out,
               # Meters the proxy was blind to until TR-143: the economics of a
               # long agent turn are mostly cache reads.
               'cache_read_tokens': cache_read_tokens,
               'cache_write_tokens': cache_write_tokens,
               'tokens_reasoning': tokens_reasoning,
               'cost_usd': cost_usd, 'wall_time_s': latency_s, 'success': ok,
               # Which price basis produced cost_usd (list vs plan-offset), so a
               # reader can tell a cheap lane from a plan lane without guessing.
               'price_basis': price_basis,
               # The Hermes session this call ran under, when the driver could
               # report it: the join key back to state.db (TR-129/TR-145).
               'gateway_session_id': gateway_session_id or None,
               # served | failed | no-hops — the row says which door it came out
               # of, and a failure carries its reason CODE, not just prose.
               'route_outcome': route_outcome,
               'failure_reason': failure_reason,
               'hops_attempted': hops_attempted, 'served_by_hop': served_by_hop,
               'max_hops': max_hops, 'complexity_source': complexity_source,
               'degrade_reason': degrade_reason,
               # TR-163: the OPTION CHAIN, the gate evidence, and the rating's own
               # explanation. Without these the row could say a request was served from
               # hop 3 but not what hops 1-2 were or why they were skipped, and a
               # 'no-hops' row could not say why nothing was eligible — which is exactly
               # the question the 2026-09-26 incident needed answered from disk.
               'chain': (chain_evidence or {}).get('chain'),
               'chain_length': (chain_evidence or {}).get('chain_length'),
               'chain_truncated': (chain_evidence or {}).get('chain_truncated'),
               'exclusions': (chain_evidence or {}).get('exclusions'),
               'exclusions_truncated': (chain_evidence or {}).get('exclusions_truncated'),
               'skipped_hops': (chain_evidence or {}).get('skipped_hops'),
               'first_attempt_hop': (chain_evidence or {}).get('first_attempt_hop'),
               'gate': (chain_evidence or {}).get('gate'),
               'classifier': classifier_evidence,
               'attempts': attempts,
               # A HASH and a length, never the prompt text.
               'prompt_chars': prompt_chars, 'prompt_sha': prompt_sha,
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
        _p = (provider or '').strip()
        _m = (model or '').strip()
        if _p.lower() in _NOT_A_LANE or _m.lower() in _NOT_A_LANE:
            # TR-182: a router-internal outcome is NOT a provider event. The no-hops row
            # calls _proxy_record('none', 'none', ...) with the router's own error string
            # as the reason, and this branch shelled out unchanged -- so it opened a
            # breaker for a provider called `none` whose reason read "no open hop for
            # this request". Observed live in circuit-state.json (open_until in the
            # future), i.e. the router was gating traffic on its own failure text.
            pass
        elif ok:
            _subprocess_text("router_circuit.py", ['record-success', _p, _m])
        else:
            _subprocess_text("router_circuit.py",
                             ['record-failure', _p, _m,
                              '--class', _circuit_class(reason),
                              reason or 'transport failure'])
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
    # TR-163: one measurement per request, reused by every row this request writes.
    _prompt_stats = _prompt_evidence(body)
    # TR-142: the envelope states the LEVELS that admitted the served lane, resolved
    # through the one authority (router_outcomes.required_levels) rather than being
    # re-derived here. None when the levels are genuinely unknown (an unrated prompt
    # with no profile) — never an invented set.
    if isinstance(requirements, dict) and not requirements.get('levels'):
        try:
            import router_outcomes as _ro_levels
            requirements['levels'] = _ro_levels.required_levels(
                profile_id=requirements.get('profile_id'),
                matrix=requirements.get('matrix'))
        except Exception:  # noqa: BLE001 — advisory evidence, never fatal
            requirements['levels'] = None
    if source == 'classifier' and not (requirements.get('matrix') or {}):
        # An empty matrix is a SUCCESSFUL rating that says "this prompt presses no
        # category": any lane can serve it, so the chain must be built from a FLOOR
        # requirement set (every category at the scale minimum). That yields the full
        # eligible pool sorted by effective price, which puts the cheapest capable lane
        # first. Probed: P0_FORE -> 16 lanes, head $0.108/M; a lenient requirement -> 166
        # lanes, head $0.000/M (a free lane).
        #
        # This branch used to substitute P0_FORE, the PRICIEST profile, so "needs
        # nothing" was billed as "needs the best": on live rows, degraded calls averaged
        # $0.0654 against $0.0278 for rated calls, on a median of 10 output tokens.
        # The fall-back for a genuine rating FAILURE is a separate question (TR-139) and
        # is deliberately untouched below.
        cats = _registry_categories()
        if cats:
            source = 'classifier-empty'
            requirements = {**requirements, 'profile_id': None,
                            'matrix': {c: -5 for c in cats},
                            'problems': (requirements.get('problems') or []) +
                                        ['empty matrix -> no category pressurised: floor '
                                         'requirements, cheapest eligible lane']}
        else:
            # No category list available: keep the old, visible fall-back rather than
            # guess a scale.
            source = 'classifier-empty'
            requirements = {**requirements, 'profile_id': 'P0_FORE',
                            'problems': (requirements.get('problems') or []) +
                                        ['empty matrix -> default profile P0_FORE '
                                         '(registry categories unreadable)']}
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
    # TR-136: the ladder clock and the session identity are needed by EVERY exit
    # from this function — including the early "nothing eligible" return, which
    # used to leave the caller with no session id and no wall time at all. They
    # are derived here (both are pure functions of the headers and the clock)
    # rather than duplicated at each return.
    ladder_t0 = time.time()
    # Both are pure functions of the headers, so they move up with the clock: the
    # early "nothing eligible" exit needs them too (TR-136).
    declared_session = (headers.get('x-router-session') or '').strip()[:200]
    parent_session_id = declared_session or None
    session_id = (f'{source_system}:{declared_session}' if declared_session
                  else f'{source_system}-{int(time.time() * 1000)}')

    meta = {'complexity_source': source, 'requirements': requirements,
            'sort': resolved.get('sort'), 'chain_length': len(chain),
            'max_hops': hops, 'ladder': [],
            'first_attempt_hop': (chain[0].get('hop') if chain else None),
            'skipped_hops': max(0, len(chain) - attempted_bound),
            'exclusions': exclusions, 'gate_reasons': gate_reasons,
            'degrade_reason': (requirements.get('problems') or [None])[0],
            'caller': source_system,
            'developer_role_rewrites': dev_rewrites,
            # TR-138: disclose the budget MODE, because "the hop timed out" means
            # something different under a wall clock than under an idle watch.
            'stream_hops': _stream_hops_enabled(),
            'idle_budget_s': _proxy_idle_budget_s(),
            'wall_ceiling_s': _proxy_wall_ceiling_s(),
            'problems': list(caller_problems)}
    if dev_rewrites:
        # never silent: the caller's payload was adjusted
        meta['problems'].append(
            f'normalized {dev_rewrites} role:developer message(s) -> system '
            f'(reference provider compat; x-router-developer-role: preserve to keep)')
    if not chain:
        meta['gate'] = resolved.get('gate')
        # TR-136: this is the OTHER blind exit — it returns before the ladder, so
        # it needs the same envelope or a caller cannot tell it apart from a
        # transport death.
        _proxy_record('none', 'none', False, requirements,
                      reason='no open hop for this request',
                      latency_s=round(time.time() - ladder_t0, 3), source=source_system,
                      session_id=session_id, parent_session_id=parent_session_id,
                      route_outcome='no-hops', failure_reason='no-hops',
                      hops_attempted=0, max_hops=hops, complexity_source=source,
                      degrade_reason=(requirements.get('problems') or [None])[0],
                      steps=0,
                      chain_evidence=_chain_evidence(resolved, chain),
                      classifier_evidence=_classifier_evidence(requirements, source),
                      prompt_chars=_prompt_stats[0], prompt_sha=_prompt_stats[1])
        return 503, {'error': 'no open hop for this request',
                     '_router': _failure_envelope(meta, session_id, source_system,
                                                  parent_session_id, ladder_t0,
                                                  'no-hops', [], 'nothing eligible after gating')}

    # Load per-provider routing (last-mile): providers with api_base_url defined
    # get their own hop-level upstream; others fall back to the global upstream.
    provider_routing = _load_provider_routing()
    _default_upstream = upstream or _UPSTREAM_CALL or _proxy_upstream_default

    def _hop_call(prov_id):
        return _provider_upstream_factory(prov_id, provider_routing) or _default_upstream

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
    # TR-122 fallback: the OpenAI `user` field (a stable client-side id) when the
    # header is absent.  This lets any OpenAI-shaped client send a session marker
    # without knowing the router's custom header name.
    if not declared_session:
        body_user = (body.get('user') or '').strip()
        if body_user:
            declared_session = body_user[:200]
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
        hop_call = _hop_call(provider)
        exc_seen = None
        try:
            status, payload = hop_call(path, fwd, hdrs)
        except Exception as exc:  # noqa: BLE001 — transport failure == ladder step
            status, payload = 0, {'error': str(exc)[:300]}
            exc_seen = exc
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
        if ok:
            attempt['outcome'] = 'ok'
        else:
            reason, detail = _classify_hop_failure(
                exc=exc_seen, status=status,
                unservable=(attempt.get('outcome') == 'unservable-2xx'))
            attempt['reason'] = reason
            attempt['reason_detail'] = detail
            # TR-137: keep the legacy vocabulary for genuine transport/HTTP
            # failures (consumers and tests read 'transport-failure' /
            # 'unservable-2xx'), but a DEADLINE hit now says 'timeout' with the
            # specific kind, so slow-but-alive stops looking dead.
            if attempt.get('outcome') == 'unservable-2xx':
                pass
            elif reason in ('idle-timeout', 'hop-wall-timeout'):
                attempt['outcome'] = 'timeout'
                attempt['timeout_kind'] = reason
            else:
                attempt['outcome'] = 'transport-failure'
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
        # Served hops only. The meters are still taken from the payload alone —
        # never estimated — and a FAILED hop records None on purpose: a 5xx that
        # echoes a usage block is not trustworthy evidence of spend, and letting
        # it price the row would inflate exactly the failure costs an operator
        # reads to decide whether a lane is worth keeping (test_proxy_metering
        # pins this). TR-143 keeps that contract and adds the ladder facts.
        meters = _proxy_usage_full(payload if ok else None)
        tokens_in, tokens_out = meters['tokens_in'], meters['tokens_out']
        cost_usd, price_basis = _proxy_cost(hop, tokens_in, tokens_out)
        attempt['tokens_in'], attempt['tokens_out'] = tokens_in, tokens_out
        attempt['cache_read_tokens'] = meters['cache_read_tokens']
        attempt['cost_usd'], attempt['price_basis'] = cost_usd, price_basis
        _proxy_record(str(provider), str(model), ok, requirements,
                      reason='' if ok else str(payload.get('error') if isinstance(payload, dict) else payload)[:200],
                      latency_s=wall,
                      source=source_system, session_id=session_id,
                      tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                      parent_session_id=parent_session_id,
                      price_basis=price_basis,
                      cache_read_tokens=meters['cache_read_tokens'],
                      cache_write_tokens=meters['cache_write_tokens'],
                      tokens_reasoning=meters['tokens_reasoning'],
                      gateway_session_id=(payload.get('_router_hermes_session_id')
                                          if isinstance(payload, dict) else None),
                      session_stats=_hermes_session_stats(
                          payload.get('_router_hermes_session_id')
                          if isinstance(payload, dict) else None),
                      route_outcome='served' if ok else 'failed',
                      failure_reason=None if ok else attempt.get('reason'),
                      hops_attempted=len(meta['ladder']), served_by_hop=(hop.get('hop') if ok else None),
                      max_hops=hops, complexity_source=source,
                      degrade_reason=(requirements.get('problems') or [None])[0],
                      steps=len(meta['ladder']),
                      chain_evidence=_chain_evidence(resolved, chain),
                      classifier_evidence=_classifier_evidence(requirements, source),
                      attempts=list(meta['ladder']),
                      prompt_chars=_prompt_stats[0], prompt_sha=_prompt_stats[1])
        if ok:
            out = dict(payload) if isinstance(payload, dict) else {'upstream': payload}
            # TR-138: the live-stream facts (events seen, idle budget, wall
            # ceiling) are routing evidence, so they go in the ladder/envelope —
            # never into the model's answer body, where an OpenAI client would
            # have to filter them out.
            if isinstance(out, dict) and isinstance(out.get('_router_stream'), dict):
                attempt['stream'] = out.pop('_router_stream')
            # TR-145: read the marker BEFORE popping it — it is the value the
            # envelope reports, and popping first is how the first version of
            # this patch reported a session of None while holding the real id.
            # TR-145: capture it into the envelope/row WITHOUT removing it — this
            # ladder is shared, and the /v1/responses handler above reads this very
            # marker off the payload we return. Popping it here reported a session
            # of None in the chat envelope and broke the responses echo in the same
            # edit. Stripping is the CLIENT surface's job (see _client_body).
            gw_session_id = out.get('_router_hermes_session_id') if isinstance(out, dict) else None
            # TR-144: the caller learns how THIS lane has been performing for THIS
            # complexity band without a second round trip. None (with a reason on
            # the failure path) when the lane has no history yet — a first call
            # must not read as a lane with a 0% success rate.
            try:
                import router_proxy_stats as rps
                band, _ = rps.band_for({'required_categories': requirements.get('matrix')})
                rolling = rps.rolling_for(str(provider), str(model), band)
            except Exception:  # noqa: BLE001
                rolling = None
            out['_router'] = {**meta, 'served_by': {'provider': provider, 'model': model,
                                                    'tokens_in': tokens_in, 'tokens_out': tokens_out,
                                                    'cost_usd': cost_usd, 'price_basis': price_basis},
                              'outcome_row': {'source_system': source_system,
                                              'session_id': session_id,
                                              'parent_session_id': parent_session_id},
                              'rolling': rolling,
                              # TR-145: the router's OWN session id and the gateway's.
                              # The row had both while the envelope reported
                              # session_id: null, so a caller could not reconcile the
                              # two without parsing the ledger — the whole point of
                              # the custody check.
                              'session_id': session_id,
                              'parent_session_id': parent_session_id,
                              'gateway_session_id': gw_session_id,
                              # TR-148: the envelope carried the full per-hop ladder but
                              # NOT the aggregate step count the ledger row records, so a
                              # caller had to count hops to answer "how many attempts did
                              # this take?". Same expression the row uses, so the two can
                              # never disagree.
                              'steps': len(meta['ladder']),
                              'wall_time_s': wall}
            return 200, out
    status, payload = last or (502, {'error': 'no hops attempted'})
    out = dict(payload) if isinstance(payload, dict) else {'upstream': payload}
    # TR-136: the failure envelope must be as informative as the LEDGER. Measured
    # 2026-09-24: a real proxied 502 returned served_by/usage/cost/session/
    # wall_time all null while the ledger row for the same request held the
    # chain, the three hops attempted and their latencies — the caller (a
    # foreman) learned "timed out" and nothing else, and the operator could not
    # audit the failed route. Every field below is either a real value or an
    # explicit null WITH a reason; nothing is fabricated.
    tried = meta.get('ladder') or []
    terminal = (tried[-1].get('reason') if tried else None) or ('no-hops' if not tried else 'unknown')
    out['_router'] = _failure_envelope(meta, session_id, source_system, parent_session_id,
                                       ladder_t0, terminal, tried,
                                       'no hop served a response')
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

    def _send(self, status, payload, extra_headers=None):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status, html):
        encoded = html.encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path in ('/ui', '/ui/'):
                # TR-150: the data command center, served by this service itself. No separate build,
                # no second deploy, and nothing fetched from the network at view time.
                self._send_html(200, router_ui_page.page_html())
                return
            if parsed.path in ('/v1/models', '/models'):
                # Interop: every OpenAI-compatible client probes the model list
                # first, and Hermes does too. A 404 here reads as "provider is
                # broken" and sends the caller to a fallback lane.
                self._send(200, openai_models_payload())
                return
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
                # TR-129: /v1/responses emits the Responses-API event set
                # (response.created -> response.completed/failed), not chat
                # chunks; X-Hermes-Session-Id rides the stream headers.
                if parsed.path == "/v1/responses":
                    self._send_sse_responses(payload)
                else:
                    self._send_sse_chat(payload)
                return
            # Buffered answer (or non-200). On /v1/responses the upstream
            # session id (SOURCE B: the gateway sends it on buffered responses
            # too) rides the response headers.
            extra = {}
            if parsed.path == "/v1/responses" and status == 200 \
                    and isinstance(payload, dict):
                meta = payload.get("_router")
                if isinstance(meta, dict) and meta.get("hermes_session_id"):
                    extra["X-Hermes-Session-Id"] = str(meta["hermes_session_id"])
            self._send(status, payload, extra_headers=extra)
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

    def _send_sse_responses(self, payload):
        """Buffered /v1/responses envelope -> Responses-API SSE frames (TR-129).

        The gateway's own event set is response.created -> (deltas/items) ->
        response.completed / response.failed; a buffered mirror cannot replay
        the live deltas, so it emits the created frame and the terminal frame
        carrying the full envelope — the same wire shape the OpenAI Responses
        stream reader consumes. X-Hermes-Session-Id (when the upstream echoed
        one) rides the stream headers, exactly as the gateway's own SSE does.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        session_id = ""
        if isinstance(payload, dict):
            meta = payload.get("_router")
            if isinstance(meta, dict):
                session_id = str(meta.get("hermes_session_id") or "")
        if session_id:
            self.send_header("X-Hermes-Session-Id", session_id)
        self.end_headers()

        def _frame(event, obj):
            self.wfile.write(f"event: {event}\n".encode()
                             + b"data: " + json.dumps(obj).encode() + b"\n\n")

        if isinstance(payload, dict) and not payload.get("error") \
                and (payload.get("object") == "response" or "output" in payload):
            response_id = payload.get("id") or "resp-router"
            created = payload.get("created_at") or int(time.time())
            _frame("response.created", {"type": "response.created",
                                        "response": {"id": response_id,
                                                     "created_at": created}})
            _frame("response.completed", {"type": "response.completed",
                                          "response": payload})
        else:
            # Unserved / shaped error: forward as a data frame so the client's
            # error path sees the reason (never silence).
            self.wfile.write(b"data: " + json.dumps(
                payload if isinstance(payload, dict) else {"error": str(payload)}
            ).encode() + b"\n\n")
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


def _classifier_failure_is_fatal(problem_text):
    """Is a classifier failure a DEPLOYMENT error or a transient blip?

    Fatal (refuse to serve): credential/permission failures — a 401/403 means
    every request would silently degrade to the default profile, which is exactly
    the misconfiguration the startup self-check exists to catch.

    Transient (warn and START): 429 rate limits, 5xx, timeouts, transport errors.
    Refusing to boot on a rate limit makes the proxy unavailable precisely when
    its classifier is busiest, and the request path already degrades VISIBLY (R10).
    Measured 2026-09-23: z.ai returned 429 at startup, the self-check returned
    False, and the server sys.exit(1) — so the wired client had no router at all.

    Module level rather than nested in main() so the rule is unit-testable.
    """
    text = str(problem_text or '')
    low = text.lower()
    return ('401' in text) or ('403' in text) or ('unauthor' in low) \
        or ('invalid api key' in low) or ('authentication' in low)


_OPENAI_MODELS_CACHE = {"at": 0.0, "payload": None}


def openai_models_payload(data_dir=None, max_age_s=300):
    """GET /v1/models — the OpenAI-compatible model list for this router.

    Clients probe this before sending traffic (the OpenAI SDK, Hermes itself and
    other harnesses all do); a 404 makes them treat the provider as broken and
    fall back. Built from the registry's own table (DATA, never a hardcoded
    list), filtered to lanes a request could actually reach: not archived, not
    disabled, inside the lifecycle window (available_from <= today < valid_to).
    Cached briefly so a hammering client does not re-scan the table.
    """
    now = time.time()
    if (_OPENAI_MODELS_CACHE["payload"] is not None
            and (now - _OPENAI_MODELS_CACHE["at"]) < max_age_s):
        return _OPENAI_MODELS_CACHE["payload"]
    ddir = Path(data_dir) if data_dir else DATA_DIR
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    data = []
    try:
        with open(Path(ddir) / 'models.jsonl') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('archive') or row.get('disabled'):
                    continue
                af = row.get('available_from')
                if af and str(af)[:10] > today:        # announced, not yet routable
                    continue
                vt = row.get('valid_to')
                if vt and str(vt)[:10] <= today:       # retired
                    continue
                model = row.get('model')
                if not model:
                    continue
                data.append({'id': str(model), 'object': 'model', 'created': 0,
                             'owned_by': str(row.get('provider') or 'unknown')})
    except OSError:
        data = []
    payload = {'object': 'list', 'data': data}
    _OPENAI_MODELS_CACHE.update({'at': now, 'payload': payload})
    return payload


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

    # TR-129: read session metadata from the upstream /v1/capabilities at
    # startup. This is an ADVISORY probe — it records what the upstream
    # advertises (session headers, the responses endpoint, whether it IS a
    # Hermes gateway) so the deployment recipe can be verified at boot. It
    # never blocks startup: an unreachable or foreign upstream only logs.
    app.hermes_capabilities = _hermes_capabilities_metadata(_hermes_upstream_base())
    if app.hermes_capabilities.get("error"):
        print(
            "router_server: upstream /v1/capabilities probe failed "
            f"({app.hermes_capabilities['error']}) — continuing (advisory)",
            file=sys.stderr,
        )
    else:
        print(
            "router_server: upstream capabilities: "
            f"hermes_gateway={app.hermes_capabilities.get('is_hermes_gateway')} "
            f"responses={app.hermes_capabilities.get('responses_method')} "
            f"{app.hermes_capabilities.get('responses_endpoint')} "
            f"session_key_header={app.hermes_capabilities.get('session_key_header')}",
            file=sys.stderr,
        )

    # TR-119 startup self-check: the proxy must not serve traffic with a broken
    # classifier.  ROUTER_CLASSIFIER_* env vars are the deployment artifact — the
    # docs recipe (docs/integration.md) names them; server code reads the env and
    # nothing else.  If the env is not set we still start (the proxy degrades
    # visibly), but a SET env that is unreachable is a deployment error we catch
    # here before accepting any request.
    def _check_classifier():
        """Quick ping: call the classifier once, 10 s timeout.

        Two outcomes:
        - env NOT configured → matrix=null is EXPECTED, proxy starts with
          visible degrade (R10) — do not block.
        - env IS configured but returns null or raises → the user set a
          classifier that is broken; fail fast so they fix the env before
          accepting traffic.
        """
        try:
            import router_classify as _rc
            # If no env is configured we skip the check entirely (degrade path).
            if not os.environ.get('ROUTER_CLASSIFIER_BASE_URL'):
                print(
                    "router_server: classifier env not configured — "
                    "proxy will degrade visibly to default profile",
                    file=sys.stderr,
                )
                return True
            res = _rc.classify(
                "ping",
                categories=["code_gen", "debug", "test"],
                timeout_s=10,
            )
            if res.get("matrix") is None:
                problems = res.get('problems')
                fatal = _classifier_failure_is_fatal(problems)
                print(
                    f"router_server: classifier self-check returned matrix=null — "
                    f"{'REFUSING TO SERVE (deployment error)' if fatal else 'starting anyway (transient)'}"
                    f". Problems: {problems}",
                    file=sys.stderr,
                )
                return not fatal
            print(
                f"router_server: classifier self-check OK "
                f"(matrix={res['matrix']}, confidence={res.get('confidence')})",
                file=sys.stderr,
            )
            return True
        except Exception as exc:
            fatal = _classifier_failure_is_fatal(exc)
            print(
                f"router_server: classifier self-check FAILED ({exc}) — "
                f"{'misconfigured, refusing to serve' if fatal else 'transient, starting anyway'}",
                file=sys.stderr,
            )
            return not fatal

    if not _check_classifier():
        sys.exit(1)

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

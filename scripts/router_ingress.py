#!/usr/bin/env python3
"""router_ingress — the bus ingress for the task-router (TR-236).

The router was built to be CALLED. It has two HTTP doors (the classified proxy on
`:9391` and the read API on `:9092`) and no way to be TOLD anything: a Crier bus
message addressed to it sits in an inbox forever. This module is the missing
inbound leg — and, per the row it implements, the genuinely new work is the
TRANSLATION from a bus message into the request a target endpoint's protocol
requires, with the model choice left to the router.

Three things are deliberately separated so each can be tested and, when it
misbehaves, named:

  1. the ENDPOINT DECLARATION — data, not code (`data/endpoints.jsonl`).
     An endpoint declares protocol, address, auth reference, response-extraction
     rule and a timeout. An endpoint that cannot declare them is REFUSED with a
     named reason; there is no silent default to Hermes.
  2. the TRANSFORM — protocol -> (url, headers, body) and reply -> text. The
     closed protocol set lives in :data:`PROTOCOLS`; adding a target that speaks
     an existing protocol touches no code.
  3. the TRANSPORT — `poll` (drain the bus inbox; works with the box offline),
     `serve` (an authenticated inbound endpoint for push), or `forward`
     (one message from a file; what the tests and the e2e use).

What the ledger records, per attempt, is the audit surface the row demands:
inbound message id, requested vs SERVED endpoint, protocol, transform, outcome,
seconds, model chosen vs model that ran, and cost (with a stated reason wherever
a value is unknown — an unexplained null is junk).

Boundaries (from the filed row, TR-236):
  * NOT the scheduler's dispatch decision (SCHED-GAP-1665) and not peer
    federation (REMOTE-*).
  * Model selection is the router's job and stays here: for an endpoint that
    takes a model, the pair comes from `router_spawn.py` (the same resolver the
    scheduler and the proxy use), never from the bus message.
  * Fail-open on the way out (a forwarding error is a recorded failure, never a
    crash), fail-loud on visibility (an unknown target, a missing auth, an
    overloaded ingress and a timeout all produce a reply that says so).

Invocation example (box-local, one pass over the inbox):

    ROUTER_INGRESS_BUS_TOKEN_FILE=~/.hermes/secrets/crier-fleet.token \
      python3 scripts/router_ingress.py poll --once --limit 5

No third-party imports. The bus client is the one the fleet already ships
(`crier/clients/python/crier_client.py`), loaded from ``CRIER_CLIENT_PATH``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# ---------------------------------------------------------------------------
# Contract constants. These are the CLOSED sets the refusal rule keys on: an
# unknown protocol or reply rule is a named refusal, never a guess.
# ---------------------------------------------------------------------------

PROTOCOLS = (
    "hermes-gateway",      # POST <address>/v1/responses, SSE reply
    "openai-compatible",   # POST <address>/v1/chat/completions, JSON reply
    "anthropic-messages",  # POST <address>/v1/messages, JSON reply
    "webhook",             # POST <address>, 202, no inline reply
    "bus-native",          # another bus participant; no HTTP at all
)

#: Reply rules: 'json:<dotted.path>' is the only parameterised one.
REPLY_RULES = ("sse-last-message", "json:", "webhook-separate", "none")

REQUIRED_FIELDS = ("id", "protocol", "address", "auth", "reply", "timeout_s")

#: Protocol -> (default path appended to the endpoint address, transform id).
PROTOCOL_PATHS = {
    "hermes-gateway": "/v1/responses",
    "openai-compatible": "/v1/chat/completions",
    "anthropic-messages": "/v1/messages",
}

DEFAULT_ENDPOINTS_PATH = REPO_ROOT / "data" / "endpoints.jsonl"
DEFAULT_BUS_URL = "http://100.97.236.14:8767"
DEFAULT_KEY_DIR = Path.home() / "crier-fleet" / "keys"
DEFAULT_BUS_TOKEN_FILE = Path.home() / ".hermes" / "secrets" / "crier-fleet.token"
DEFAULT_CRIER_CLIENT_PATH = Path.home() / "crier" / "clients" / "python"
DEFAULT_RESOLVER = SCRIPT_DIR / "router_spawn.py"
DEFAULT_REPLY_TO = "orchestrator"


def _state_dir() -> Path:
    """Where runtime artifacts live (mirrors the proxy's ROUTER_STATE_DIR).

    TR-REV-20261005-3: the resolve moved into scripts/state_dir.py (env wins
    silently; ONE stderr warning on non-canonical, env-unset invocations).
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import state_dir as _state_dir_mod
    except ImportError:  # live byte-copy not yet synced -> old behavior, silent
        return Path(os.environ.get("ROUTER_STATE_DIR")
                    or (Path.home() / ".hermes" / "model-router"))
    return Path(_state_dir_mod.resolve_state_dir(script_file=__file__))


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def ledger_path() -> Path:
    return Path(os.environ.get("ROUTER_INGRESS_LEDGER") or (_state_dir() / "ingress-ledger.jsonl"))


def endpoints_path() -> Path:
    return Path(os.environ.get("ROUTER_INGRESS_ENDPOINTS") or DEFAULT_ENDPOINTS_PATH)


# ---------------------------------------------------------------------------
# Endpoint declarations — data, not code.
# ---------------------------------------------------------------------------


class Endpoint:
    """One declared target. ``problems`` is empty for a usable endpoint.

    Validation is total: every field the row requires is checked here, once, so
    the forwarding path can never meet a half-declared endpoint and improvise.
    """

    def __init__(self, raw, source: str = ""):
        self.raw = dict(raw or {})
        self.source = source
        self.problems = []
        self._validate()

    # -- validation --------------------------------------------------------
    def _validate(self):
        for field in REQUIRED_FIELDS:
            value = self.raw.get(field)
            if field == "timeout_s":
                if value is None:
                    self.problems.append("missing timeout_s")
                    continue
                try:
                    seconds = float(value)
                except (TypeError, ValueError):
                    self.problems.append("timeout_s is not a number: %r" % (value,))
                    continue
                if seconds <= 0:
                    self.problems.append("timeout_s must be > 0, got %r" % (value,))
                continue
            if not isinstance(value, str) or not value.strip():
                self.problems.append("missing %s" % field)
        protocol = self.raw.get("protocol")
        if isinstance(protocol, str) and protocol.strip() and protocol not in PROTOCOLS:
            self.problems.append(
                "unknown protocol %r (known: %s)" % (protocol, ", ".join(PROTOCOLS)))
        reply = self.raw.get("reply")
        if isinstance(reply, str) and reply.strip():
            if not (reply in REPLY_RULES or reply.startswith("json:")):
                self.problems.append("unknown reply rule %r" % (reply,))
            if reply.startswith("json:") and not reply[len("json:"):].strip():
                self.problems.append("reply rule 'json:' needs a path")
        model = self.raw.get("model", "router")
        if model not in ("router", "none"):
            self.problems.append("model must be 'router' or 'none', got %r" % (model,))
        if protocol == "webhook" and reply not in ("webhook-separate", "none"):
            self.problems.append(
                "webhook endpoints cannot declare an inline reply (%r); "
                "use 'webhook-separate' or 'none'" % (reply,))
        address = self.raw.get("address")
        if isinstance(address, str) and address.strip() and protocol != "bus-native":
            if not address.strip().startswith(("http://", "https://")):
                self.problems.append("address is not an http(s) URL: %r" % (address,))

    # -- accessors ---------------------------------------------------------
    @property
    def id(self):
        return str(self.raw.get("id") or "").strip()

    @property
    def protocol(self):
        return str(self.raw.get("protocol") or "").strip()

    @property
    def address(self):
        return str(self.raw.get("address") or "").strip()

    @property
    def auth_ref(self):
        return str(self.raw.get("auth") or "").strip()

    @property
    def reply_rule(self):
        return str(self.raw.get("reply") or "").strip()

    @property
    def timeout_s(self):
        try:
            return float(self.raw.get("timeout_s"))
        except (TypeError, ValueError):
            return 0.0

    @property
    def model_source(self):
        return str(self.raw.get("model") or "router").strip()

    @property
    def description(self):
        return str(self.raw.get("description") or "").strip()

    @property
    def usable(self):
        return not self.problems

    def refusal_reason(self):
        """The named reason this endpoint is refused ('' when it is usable)."""
        if not self.problems:
            return ""
        return "endpoint-incomplete: %s" % "; ".join(self.problems)

    def public(self):
        out = {
            "id": self.id,
            "protocol": self.protocol,
            "address": self.address,
            "auth": self.auth_ref,
            "reply": self.reply_rule,
            "timeout_s": self.raw.get("timeout_s"),
            "model": self.model_source,
            "usable": self.usable,
            "problems": list(self.problems),
        }
        if self.description:
            out["description"] = self.description
        return out


def load_endpoints(path=None):
    """Read the endpoint registry. Returns (endpoints, problems).

    A damaged line is a problem, not a crash: the rest of the registry still
    loads and the damaged row is refused by name (its id is often unknown, so it
    is reported by line number).
    """
    path = Path(path or endpoints_path())
    endpoints, problems = [], []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], ["endpoints file unreadable: %s (%s)" % (path, exc.__class__.__name__)]
    seen = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            problems.append("line %d: not JSON (%s)" % (lineno, exc))
            continue
        if not isinstance(raw, dict):
            problems.append("line %d: not an object" % lineno)
            continue
        endpoint = Endpoint(raw, source="%s:%d" % (path, lineno))
        if endpoint.id and endpoint.id in seen:
            endpoint.problems.append("duplicate id %r" % endpoint.id)
        seen.add(endpoint.id)
        endpoints.append(endpoint)
    return endpoints, problems


# ---------------------------------------------------------------------------
# Auth references: a NAME for a secret, never the secret itself.
# ---------------------------------------------------------------------------


def resolve_auth(ref, env=None):
    """Resolve an endpoint's auth reference. Returns (value_or_None, reason).

    References:
      ``none``                     no credential (honest, not a default)
      ``env:NAME``                 environment variable
      ``file:PATH``                whole file, trimmed
      ``env-file:PATH:NAME``       NAME= in a dotenv file (the fleet's shape)

    A reference that cannot be resolved returns a NAMED reason and no value —
    the caller refuses; it never proceeds unauthenticated "well enough".
    """
    env = os.environ if env is None else env
    ref = (ref or "").strip()
    if not ref:
        return None, "auth-unresolved: empty auth reference"
    if ref == "none":
        return "", None
    if ref.startswith("env:"):
        name = ref[4:].strip()
        if not name:
            return None, "auth-unresolved: env: without a name"
        value = (env.get(name) or "").strip()
        if not value:
            return None, "auth-unresolved: %s is empty in the environment" % name
        return value, None
    if ref.startswith("env-file:"):
        rest = ref[len("env-file:"):]
        if ":" not in rest:
            return None, "auth-unresolved: env-file: needs PATH:NAME"
        path, _, name = rest.rpartition(":")
        name = name.strip()
        path = os.path.expanduser(path.strip())
        if not name:
            return None, "auth-unresolved: env-file: without a name"
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    if key.strip() == name:
                        value = value.strip().strip('"').strip("'")
                        if value:
                            return value, None
        except OSError as exc:
            return None, "auth-unresolved: cannot read %s (%s)" % (path, exc.__class__.__name__)
        return None, "auth-unresolved: %s not set in %s" % (name, path)
    if ref.startswith("file:"):
        path = os.path.expanduser(ref[5:].strip())
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            return None, "auth-unresolved: cannot read %s (%s)" % (path, exc.__class__.__name__)
        if not value:
            return None, "auth-unresolved: %s is empty" % path
        return value, None
    return None, "auth-unresolved: unknown reference form %r" % (ref,)


# ---------------------------------------------------------------------------
# Translation: bus message -> prompt + session + model + request.
# ---------------------------------------------------------------------------

_PROMPT_KEYS = ("prompt", "text", "task", "message", "body", "input")


class Refusal(Exception):
    """A named, bus-visible refusal. Never carries a secret."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def normalize_message(payload, envelope=None):
    """Flatten a bus payload + its envelope into the ingress's message shape.

    Accepted shapes deliberately overlap with what the fleet already emits:
    the dispatcher reads ``task``/``text``/``prompt``/``message``/``body``, so
    this reader accepts the same, plus a bare string payload.
    """
    envelope = envelope if isinstance(envelope, dict) else {}
    if isinstance(payload, str):
        payload = {"prompt": payload}
    if not isinstance(payload, dict):
        payload = {}

    prompt = None
    for key in _PROMPT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            prompt = value
            break
    system = payload.get("system")
    system = system if isinstance(system, str) and system.strip() else None

    def _first_str(*keys):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    target = _first_str("endpoint", "to", "target")
    if not target:
        env_to = envelope.get("to") or envelope.get("endpoint") or envelope.get("target")
        if isinstance(env_to, str) and env_to.strip():
            target = env_to.strip()

    session = _first_str("session", "session_key")
    if not session:
        corr = envelope.get("correlation_id") or envelope.get("correlation")
        session = corr if isinstance(corr, str) and corr.strip() else None

    profile = _first_str("profile", "capability_profile")

    reply_to = _first_str("reply_to")
    if not reply_to:
        sender = envelope.get("sender")
        reply_to = sender.strip() if isinstance(sender, str) and sender.strip() else None
    if not reply_to:
        reply_to = DEFAULT_REPLY_TO

    timeout_s = payload.get("timeout_s")
    try:
        timeout_s = float(timeout_s) if timeout_s is not None else None
    except (TypeError, ValueError):
        timeout_s = None

    idem = _first_str("idempotency_key")
    if not idem:
        key = envelope.get("idempotency_key")
        idem = key.strip() if isinstance(key, str) and key.strip() else None

    return {
        "prompt": prompt,
        "system": system,
        "target": target,
        "session": session,
        "profile": profile,
        "reply_to": reply_to,
        "timeout_s": timeout_s,
        "idempotency_key": idem,
        "sender": (envelope.get("sender") or "").strip(),
        "inbound_id": str(envelope.get("id") or ""),
        "raw_payload": payload,
    }


def resolve_model(profile=None, project=None, runner=None, timeout_s=None):
    """Ask the ROUTER which pair should serve this request (never the caller).

    Returns (choice, reason) where choice is ``{'provider': .., 'model': ..}`` or
    None. A failure is recorded, not raised: the hop still goes out, but the
    reply carries the reason so a routing failure cannot look like a routing
    decision.
    """
    runner = runner or _run_resolver
    timeout_s = timeout_s if timeout_s else _env_float("ROUTER_INGRESS_RESOLVE_TIMEOUT_S", 30.0)
    if not profile and not project:
        return None, ("message named no profile/project: the router is not asked to "
                      "guess a pair (nothing is injected)")
    args = []
    if project:
        args.append(project)
    else:
        args += ["--profile", profile]
    args += ["--format", "json"]
    try:
        proc_out = runner(args, timeout_s)
    except Exception as exc:  # noqa: BLE001 — a resolver failure is not fatal here
        return None, "resolver failed: %s" % exc.__class__.__name__
    try:
        doc = json.loads(proc_out) if isinstance(proc_out, str) else proc_out
    except ValueError:
        return None, "resolver output is not JSON"
    head = (doc or {}).get("head") if isinstance(doc, dict) else None
    if isinstance(head, dict) and head.get("model"):
        return {"provider": head.get("provider"), "model": head.get("model"),
                "hop": head.get("hop"), "usd_1m": head.get("usd_1m")}, None
    if isinstance(doc, dict) and doc.get("error"):
        return None, "resolver error: %s" % str(doc["error"])[:200]
    return None, "resolver returned no head pair"


def _run_resolver(args, timeout_s):
    import subprocess
    resolver = os.environ.get("ROUTER_INGRESS_RESOLVE") or str(DEFAULT_RESOLVER)
    env = dict(os.environ)
    # The repo-relative default keeps one checkout self-contained (see AGENTS.md
    # on the registry split): a script next to this one writes the repo registry.
    env.setdefault("ROUTING_REGISTRY", str(REPO_ROOT / "registry.json"))
    argv = [sys.executable, resolver] + list(args)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env)
    return proc.stdout or ""


def _dotted_get(obj, path):
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _hermes_response_text(payload):
    """Extract the assistant text from a Hermes /v1/responses envelope."""
    if not isinstance(payload, dict):
        return None, None
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text, "output_text"
    output = payload.get("output")
    if isinstance(output, list):
        chunks = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str) and item["text"].strip():
                chunks.append(item["text"])
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    for key in ("text", "output_text"):
                        if isinstance(part.get(key), str) and part[key].strip():
                            chunks.append(part[key])
                            break
        if chunks:
            return "\n".join(chunks), "output"
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"], "choices.message.content"
    return None, None


def _served_model(value):
    """The model an endpoint claims it ran, or None when it claims nothing real.

    The Hermes gateway answers `model: "Hermes Agent"` (a product name, not a
    model id). Recording that as the served model would be a fabricated billing
    fact, so it is treated as unreported — the null carries a reason.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if value.strip() in ("Hermes Agent", "hermes-agent"):
        return None
    return value.strip()


def _usage(payload):
    """(tokens_in, tokens_out) from whichever usage shape the endpoint used."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tin = usage.get("input_tokens", usage.get("prompt_tokens"))
    tout = usage.get("output_tokens", usage.get("completion_tokens"))
    try:
        tin = int(tin) if tin is not None else None
    except (TypeError, ValueError):
        tin = None
    try:
        tout = int(tout) if tout is not None else None
    except (TypeError, ValueError):
        tout = None
    return tin, tout


def parse_sse_events(lines):
    """Consume an SSE byte/line stream; yield decoded JSON events in order.

    Mirrors the proxy's reader (TR-129) on the points that matter: `:` comments
    (the gateway's keepalive) are skipped, `event:` names the event when the
    payload has no `type`, and a stream that ends mid-turn is an error, not a
    partial answer.
    """
    event_name = ""
    data_lines = []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        line = raw.rstrip("\r\n")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    payload = json.loads(data)
                except ValueError:
                    payload = None
                evt = (payload.get("type") if isinstance(payload, dict) else None) or event_name
                yield {"event": evt, "payload": payload}
                event_name = ""
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())


def extract_reply(reply_rule, status, body_text, headers=None):
    """Apply an endpoint's declared extraction rule to a response.

    Returns (reply_text, reason, served_model, tokens_in, tokens_out, terminal).
    ``terminal`` is False for webhook-style rules where the answer arrives later
    as its own bus message — the caller must not wait for it.
    """
    times = None
    if reply_rule == "none":
        return "", "reply rule 'none' (fire and forget)", None, None, None, False
    if reply_rule == "webhook-separate":
        return "", "reply rule 'webhook-separate' (the answer arrives as its own bus message)", \
            None, None, None, False
    if reply_rule == "sse-last-message":
        final = None
        for event in parse_sse_events((line + "\n") for line in (body_text or "").splitlines()):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("event") == "response.failed":
                return None, "endpoint reported response.failed", None, None, None, True
            if event.get("event") == "response.completed" or isinstance(payload.get("response"), dict):
                final = payload.get("response") or payload
        if final is None:
            return None, "SSE stream ended without a terminal event", None, None, None, True
        text, how = _hermes_response_text(final)
        tin, tout = _usage(final)
        if text is None:
            return None, "terminal event carried no extractable text", None, tin, tout, True
        return text, "extracted from %s" % how, final.get("model"), tin, tout, True
    if reply_rule.startswith("json:"):
        path = reply_rule[len("json:"):].strip()
        try:
            doc = json.loads(body_text or "")
        except ValueError:
            return None, "declared json:%s but the body is not JSON" % path, None, None, None, True
        value = _dotted_get(doc, path)
        if not isinstance(value, str):
            return None, "json path %r found no string (got %s)" % (
                path, type(value).__name__), None, None, None, True
        tin, tout = _usage(doc)
        model = doc.get("model") if isinstance(doc, dict) else None
        return value, "extracted from json:%s" % path, model, tin, tout, True
    return None, "unknown reply rule %r" % (reply_rule,), None, None, None, True


def build_hop(endpoint, message, model_choice, credential):
    """Translate one message for one endpoint into a concrete HTTP request.

    Returns a dict: url, headers, body (dict), transform (a stable id for the
    ledger) and reply_rule. Raises Refusal(reason) for anything it cannot state
    honestly — the model field, the session key, or the address.
    """
    if not endpoint.address:
        raise Refusal("address-unresolvable: endpoint %r declares no address" % endpoint.id)
    protocol = endpoint.protocol
    session = message.get("session")
    model = (model_choice or {}).get("model")
    headers = {"Content-Type": "application/json",
               "User-Agent": "task-router-ingress/1.0"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    if session:
        headers["X-Hermes-Session-Key"] = session

    if protocol == "hermes-gateway":
        path = PROTOCOL_PATHS[protocol]
        url = endpoint.address.rstrip("/") + path
        body = {"input": message["prompt"], "stream": True}
        if message.get("system"):
            body["instructions"] = message["system"]
        if model:
            body["model"] = model
        transform = "hermes-gateway/v1-responses+sse"
    elif protocol == "openai-compatible":
        url = endpoint.address.rstrip("/") + PROTOCOL_PATHS[protocol]
        prompt = message["prompt"]
        if message.get("system"):
            prompt = message["system"] + "\n\n" + prompt
        body = {"messages": [{"role": "user", "content": prompt}], "stream": False}
        if model:
            body["model"] = model
        transform = "openai-compatible/v1-chat-completions+json"
    elif protocol == "anthropic-messages":
        url = endpoint.address.rstrip("/") + PROTOCOL_PATHS[protocol]
        body = {"max_tokens": int(os.environ.get("ROUTER_INGRESS_MAX_TOKENS") or 4096),
                "messages": [{"role": "user", "content": message["prompt"]}],
                "stream": False}
        if message.get("system"):
            body["system"] = message["system"]
        if model:
            body["model"] = model
        transform = "anthropic-messages/v1-messages+json"
    elif protocol == "webhook":
        url = endpoint.address
        payload = {
            "in_reply_to": message.get("inbound_id") or "",
            "sender": message.get("sender") or "",
            "session": session,
            "prompt": message["prompt"],
            "reply_to": message.get("reply_to"),
        }
        body = payload
        transform = "webhook/raw-payload"
    elif protocol == "bus-native":
        raise Refusal("bus-native endpoints are delivered by the bus transport, not HTTP")
    else:
        raise Refusal("endpoint-unsupported-protocol: %r (known: %s)"
                      % (protocol, ", ".join(PROTOCOLS)))
    return {"url": url, "headers": headers, "body": body, "transform": transform,
            "reply_rule": endpoint.reply_rule}


# ---------------------------------------------------------------------------
# Admission control — the TR-169 lesson: an ingress must not amplify.
#
# The bound is PER LANE (SCHED-GAP-1713). A single global pool is itself the
# shared blast radius this file exists to avoid: one unreachable endpoint
# parked on every slot refuses EVERY lane at once, so one dead agent degrades
# the whole fleet. Per-lane buckets mean a lane that is hung, dead or being
# burst at can only spend its own budget; a lane that keeps failing trips its
# own circuit and is then refused FAST and LOUDLY without taking a slot or
# touching a peer. The global cap is an opt-in backstop
# (ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT, 0 = off) and is never what trips first.
# ---------------------------------------------------------------------------


def lane_failure(outcome, reason=None, http_status=None):
    """Is this attempt evidence that the LANE (endpoint) is unhealthy?

    A 4xx is a request problem, not an endpoint outage: tripping a lane on it
    would take a healthy endpoint away over a caller's bad payload. Transport
    errors, timeouts, 5xx and an endpoint that answers without a terminal
    answer ARE lane failures.
    """
    if outcome in ("timeout", "unservable"):
        return True
    if outcome == "failed":
        if http_status is not None:
            try:
                code = int(http_status)
            except (TypeError, ValueError):
                return True
            if 400 <= code < 500:
                return False
        return True
    return False


class LaneLease:
    """The slot a lane handed out. The Ingress reports the outcome back here."""

    def __init__(self, guard, lane):
        self.guard = guard
        self.lane = lane
        self.outcome = None
        self.reason = None
        self.http_status = None
        self.ok = False
        self.settled = False

    def settle(self, outcome=None, reason=None, http_status=None, ok=False):
        """Record what the hop did and give the slot back. Idempotent."""
        if self.settled:
            return None
        self.settled = True
        self.outcome, self.reason = outcome, reason
        self.http_status, self.ok = http_status, bool(ok)
        return self.guard.release(self)


class _LaneState:
    """One lane's budget and circuit. All mutation happens under ``lock``."""

    def __init__(self, name, max_inflight, queue_max, wait_s, failure_threshold, open_s):
        self.name = name
        self.max_inflight = max(1, int(max_inflight))
        self.queue_max = int(queue_max)
        self.wait_s = float(wait_s)
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_open_s = float(open_s)
        self.open_s = float(open_s)
        self._sem = threading.BoundedSemaphore(self.max_inflight)
        self.lock = threading.Lock()
        self.inflight = 0
        self.waiting = 0
        self.peak = 0
        self.accepted = 0
        self.rejected = 0
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_ok = 0
        self.last_failure = None
        self.open_until = 0.0
        self.open_count = 0
        self.probe_inflight = False
        self.last_state_change = None

    def state(self, now=None):
        now = time.time() if now is None else now
        if self.open_until and now < self.open_until:
            return "open"
        if self.open_until:
            return "half-open"
        return "closed"

    def admit(self):
        """Take the lane's slot, or raise Refusal naming why. Bounded, never forever."""
        now = time.time()
        state = self.state(now)
        if state == "open":
            self.rejected += 1
            raise Refusal(
                "endpoint-circuit-open: lane %r tripped after %d consecutive failures "
                "(%s); refusing without forwarding, retry in %.0fs"
                % (self.name, self.consecutive_failures,
                   self.last_failure or "reason unrecorded",
                   max(0.0, self.open_until - now)))
        if state == "half-open":
            with self.lock:
                if self.probe_inflight:
                    self.rejected += 1
                    raise Refusal("endpoint-circuit-open: lane %r half-open probe already "
                                  "in flight" % self.name)
                self.probe_inflight = True
        acquired = self._sem.acquire(blocking=False)
        if not acquired:
            with self.lock:
                if self.waiting >= self.queue_max:
                    self.rejected += 1
                    self.probe_inflight = False
                    raise Refusal("lane-busy: lane %r queue full (%d waiting, %d in flight)"
                                  % (self.name, self.queue_max, self.inflight))
                self.waiting += 1
            try:
                acquired = self._sem.acquire(timeout=self.wait_s)
            finally:
                with self.lock:
                    self.waiting -= 1
            if not acquired:
                with self.lock:
                    self.rejected += 1
                    self.probe_inflight = False
                raise Refusal("lane-busy: lane %r no slot within %.1fs (max %d in flight)"
                              % (self.name, self.wait_s, self.max_inflight))
        with self.lock:
            self.inflight += 1
            self.accepted += 1
            self.peak = max(self.peak, self.inflight)

    def release(self):
        try:
            self._sem.release()
        finally:
            with self.lock:
                self.inflight = max(0, self.inflight - 1)
                self.probe_inflight = False

    def record_failure(self, reason):
        """Returns 'opened'/'reopened' when the circuit changed, else None."""
        now = time.time()
        with self.lock:
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_failure = reason or "reason unrecorded"
            if self.open_until and now >= self.open_until:
                # a half-open probe failed: re-open, backing off further
                self.open_count += 1
                self.open_s = min(self.open_s * 2, 3600.0)
                self.open_until = now + self.open_s
                self.last_state_change = "reopened"
                return "reopened"
            if self.consecutive_failures >= self.failure_threshold:
                self.open_count += 1
                self.open_until = now + self.open_s
                self.last_state_change = "opened"
                return "opened"
        return None

    def record_success(self):
        """Returns True when a lane that had a circuit just came back."""
        with self.lock:
            self.total_ok += 1
            was_open = bool(self.open_until)
            self.consecutive_failures = 0
            self.last_failure = None
            self.open_until = 0.0
            self.open_s = self.base_open_s
            if was_open:
                self.last_state_change = "closed"
            return was_open

    def stats(self, now=None):
        with self.lock:
            return {
                "state": self.state(now),
                "max_inflight": self.max_inflight,
                "queue_max": self.queue_max,
                "inflight": self.inflight,
                "waiting": self.waiting,
                "peak_inflight": self.peak,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "consecutive_failures": self.consecutive_failures,
                "total_failures": self.total_failures,
                "total_ok": self.total_ok,
                "failure_threshold": self.failure_threshold,
                "open_count": self.open_count,
                "open_until": round(self.open_until, 3) if self.open_until else None,
                "last_failure": self.last_failure,
                "last_state_change": self.last_state_change,
            }


#: Outcomes that are real evidence the endpoint served the work.
_SUCCESS_OUTCOMES = ("ok", "accepted")


class LaneGuard:
    """Per-lane admission + circuit breaker. The Ingress default.

    ``hold(lane)`` returns a LaneLease or raises Refusal with a lane-scoped,
    named reason. One lane's trouble can never become another lane's refusal:
    that separation is the whole point (SCHED-GAP-1713b).
    """

    def __init__(self, max_inflight=None, queue_max=None, wait_s=None,
                 failures=None, open_s=None, global_max=None, logger=None):
        self.per_lane_inflight = int(max_inflight if max_inflight is not None
                                     else _env_int("ROUTER_INGRESS_LANE_MAX_INFLIGHT", 2))
        self.per_lane_queue = int(queue_max if queue_max is not None
                                  else _env_int("ROUTER_INGRESS_LANE_QUEUE_MAX", 8))
        self.wait_s = float(wait_s if wait_s is not None
                            else _env_float("ROUTER_INGRESS_LANE_QUEUE_WAIT_S", 20.0))
        self.failure_threshold = int(failures if failures is not None
                                     else _env_int("ROUTER_INGRESS_CIRCUIT_FAILURES", 3))
        self.open_s = float(open_s if open_s is not None
                            else _env_float("ROUTER_INGRESS_CIRCUIT_OPEN_S", 30.0))
        self.global_max = int(global_max if global_max is not None
                              else _env_int("ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT", 0))
        self._global = (threading.BoundedSemaphore(self.global_max)
                        if self.global_max > 0 else None)
        self._lanes = {}
        self._lock = threading.Lock()
        self.trips = 0
        self.recoveries = 0
        self.log = logger or (lambda *_a, **_k: None)

    def set_logger(self, logger):
        self.log = logger or (lambda *_a, **_k: None)

    def lane(self, name):
        name = (name or "").strip() or "<unaddressed>"
        with self._lock:
            state = self._lanes.get(name)
            if state is None:
                state = _LaneState(name, self.per_lane_inflight, self.per_lane_queue,
                                   self.wait_s, self.failure_threshold, self.open_s)
                self._lanes[name] = state
            return state

    def hold(self, lane_name):
        """Enter the lane. Raises Refusal (lane-scoped) instead of queueing forever."""
        state = self.lane(lane_name)
        if self._global is not None and not self._global.acquire(blocking=False):
            raise Refusal("overloaded: global backstop full (max %d in flight, all lanes)"
                          % self.global_max)
        try:
            state.admit()
        except Refusal:
            if self._global is not None:
                self._global.release()
            raise
        return LaneLease(self, state.name)

    def release(self, lease):
        """Give the slot back and settle the circuit. Returns the event or None."""
        state = self._lanes.get(lease.lane)
        if state is None:
            return None
        event = None
        if lane_failure(lease.outcome, lease.reason, lease.http_status):
            event = state.record_failure(lease.reason)
            if event:
                self.trips += 1
                stats = state.stats()
                self.log("LANE %s circuit %s: %d consecutive failures, last=%s; "
                         "cooldown %.0fs — refusing without forwarding until then"
                         % (state.name, event.upper(), stats["consecutive_failures"],
                            stats["last_failure"],
                            max(0.0, (stats["open_until"] or 0.0) - time.time())))
        elif lease.ok and lease.outcome in _SUCCESS_OUTCOMES:
            if state.record_success():
                event = "closed"
                self.recoveries += 1
                stats = state.stats()
                self.log("LANE %s circuit CLOSED: recovered (%d ok, %d failures total)"
                         % (state.name, stats["total_ok"], stats["total_failures"]))
        state.release()
        if self._global is not None:
            self._global.release()
        return event

    def lane_stats(self):
        with self._lock:
            lanes = list(self._lanes.items())
        return {name: state.stats() for name, state in lanes}

    def stats(self):
        lanes = self.lane_stats()
        return {"lanes": lanes, "trips": self.trips, "recoveries": self.recoveries,
                "per_lane_max_inflight": self.per_lane_inflight,
                "per_lane_queue_max": self.per_lane_queue,
                "per_lane_queue_wait_s": self.wait_s,
                "circuit_failure_threshold": self.failure_threshold,
                "circuit_open_s": self.open_s,
                "global_max_inflight": self.global_max,
                "inflight": sum(s["inflight"] for s in lanes.values())}


class Admission:
    """ONE shared pool for every lane — the shared blast radius of SCHED-GAP-1713.

    Kept for callers that explicitly want a single global cap; the Ingress
    default is LaneGuard. ``hold`` gives it the same interface so the forwarding
    path never branches on which governor it was handed.
    """

    def __init__(self, max_inflight=None, queue_max=None, wait_s=None):
        self.max_inflight = int(max_inflight if max_inflight is not None
                                else _env_int("ROUTER_INGRESS_MAX_INFLIGHT", 4))
        self.queue_max = int(queue_max if queue_max is not None
                             else _env_int("ROUTER_INGRESS_QUEUE_MAX", 16))
        self.wait_s = float(wait_s if wait_s is not None
                            else _env_float("ROUTER_INGRESS_QUEUE_WAIT_S", 20.0))
        self._sem = threading.BoundedSemaphore(max(1, self.max_inflight))
        self._lock = threading.Lock()
        self.inflight = 0
        self.waiting = 0
        self.peak = 0
        self.accepted = 0
        self.rejected = 0
        self.log = lambda *_a, **_k: None

    def set_logger(self, logger):
        self.log = logger or (lambda *_a, **_k: None)

    def hold(self, lane=None):
        self.__enter__()
        name = (lane or "").strip() or "<shared>"
        return LaneLease(self, name)

    def release(self, lease):
        self.__exit__(None, None, None)
        return None

    def __enter__(self):
        acquired = self._sem.acquire(blocking=False)
        if not acquired:
            with self._lock:
                if self.waiting >= self.queue_max:
                    self.rejected += 1
                    raise Refusal("overloaded: queue full (%d waiting, %d in flight)"
                                  % (self.queue_max, self.inflight))
                self.waiting += 1
            try:
                acquired = self._sem.acquire(timeout=self.wait_s)
            finally:
                with self._lock:
                    self.waiting -= 1
            if not acquired:
                with self._lock:
                    self.rejected += 1
                raise Refusal("overloaded: no slot within %.1fs (max %d in flight)"
                              % (self.wait_s, self.max_inflight))
        with self._lock:
            self.inflight += 1
            self.accepted += 1
            self.peak = max(self.peak, self.inflight)
        return self

    def __exit__(self, *exc):
        try:
            self._sem.release()
        finally:
            with self._lock:
                self.inflight = max(0, self.inflight - 1)
        return False

    def stats(self):
        return {"max_inflight": self.max_inflight, "queue_max": self.queue_max,
                "queue_wait_s": self.wait_s, "inflight": self.inflight,
                "waiting": self.waiting, "peak_inflight": self.peak,
                "accepted": self.accepted, "rejected": self.rejected}


# ---------------------------------------------------------------------------
# The ingress itself.
# ---------------------------------------------------------------------------


class Ingress:
    """Translate + forward + reply + ledger. Bus-agnostic (the transport injects)."""

    def __init__(self, endpoints, ledger=None, resolver=None, opener=None,
                 admission=None, logger=None):
        self.endpoints = {e.id: e for e in endpoints if e.id}
        self.endpoint_problems = [e.refusal_reason() for e in endpoints if not e.usable]
        self.ledger = Path(ledger or ledger_path())
        self.resolver = resolver or resolve_model
        self.opener = opener or urllib.request.urlopen
        self.log = logger or (lambda *_a, **_k: None)
        # The default governor is PER LANE. A shared pool is the blast radius
        # SCHED-GAP-1713 forbids, so it must be asked for explicitly (Admission).
        self.admission = admission or LaneGuard()
        if hasattr(self.admission, "set_logger"):
            self.admission.set_logger(self.log)
        self._seen = self._load_idempotency_keys()
        self._seen_lock = threading.Lock()

    # -- ledger ------------------------------------------------------------
    def _load_idempotency_keys(self):
        keys = set()
        try:
            with open(self.ledger, "r", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    key = row.get("idempotency_key")
                    if key and row.get("outcome") == "ok":
                        keys.add(key)
        except OSError:
            pass
        return keys

    def append_ledger(self, row):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, sort_keys=True)
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return row

    # -- one message -------------------------------------------------------
    def process(self, envelope, payload=None):
        """Run one bus envelope end to end. Returns a result dict.

        Keys: reply (the reply envelope to deliver, or None), ledger (the row),
        ack (bool — may this message be acked?), outcome.
        """
        envelope = dict(envelope or {})
        if payload is None:
            payload = envelope.get("payload")
        message = normalize_message(payload, envelope)
        inbound_id = message["inbound_id"]
        started = time.time()
        row = {
            "ts": _iso_now(),
            "ingress": "task-router",
            "inbound_id": inbound_id,
            "sender": message["sender"] or None,
            "session": message["session"],
            "lane": (message["target"] or "").strip() or None,
            "endpoint_requested": message["target"],
            "endpoint_served": None,
            "protocol": None,
            "transform": None,
            "model_chosen": None,
            "model_served": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "ok": False,
            "outcome": "failed",
            "reason": None,
            "seconds": None,
            "idempotency_key": message["idempotency_key"],
            "null_reasons": {},
        }

        def finish(outcome, reason, ok=False, reply_text=None, ack=True, extra=None):
            row["outcome"] = outcome
            row["reason"] = reason
            row["ok"] = ok
            row["seconds"] = round(time.time() - started, 3)
            for field in ("model_served", "tokens_in", "tokens_out", "cost_usd"):
                if row[field] is None:
                    row["null_reasons"][field] = (
                        "not reported by endpoint"
                        if outcome in ("ok", "accepted") else "no successful hop")
            if extra:
                row.update(extra)
            self.append_ledger(row)
            reply = self.build_reply(message, row, reply_text)
            return {"reply": reply, "reply_to": self.reply_target(message),
                    "ledger": row, "ack": ack, "outcome": outcome}

        # -- admission first: a burst must be refused before it reaches anything,
        #    and the refusal is scoped to THIS lane. Nothing is queued forever.
        lane_name = (message["target"] or "").strip()
        try:
            lease = self.admission.hold(lane_name)
        except Refusal as refusal:
            return finish("refused", refusal.reason)
        try:
            return self._process_locked(message, row, finish, started)
        finally:
            # the outcome is what settles the lane's circuit: failures trip it,
            # a real answer closes it. Never guessed — taken from the ledger row.
            lease.settle(outcome=row.get("outcome"), reason=row.get("reason"),
                         http_status=row.get("http_status"), ok=bool(row.get("ok")))

    def _process_locked(self, message, row, finish, started):
        if not message["prompt"]:
            return finish("refused", "no-prompt: payload carried none of "
                                     "prompt/text/task/message/body/input")

        # -- idempotency: a redelivered message must not run the work twice
        key = message["idempotency_key"]
        if key:
            with self._seen_lock:
                if key in self._seen:
                    row["idempotency_key"] = key
                    return finish("duplicate", "idempotency_key already forwarded", ok=True)
                self._seen.add(key)

        # -- address resolution: unknown target is refused BY NAME
        if not message["target"]:
            return finish("refused", "unknown-endpoint: the message named no endpoint")
        endpoint = self.endpoints.get(message["target"])
        if endpoint is None:
            return finish("refused", "unknown-endpoint: %r is not declared (known: %s)"
                          % (message["target"], ", ".join(sorted(self.endpoints)) or "none"))
        row["protocol"] = endpoint.protocol
        row["endpoint_served"] = endpoint.id
        if not endpoint.usable:
            return finish("refused", endpoint.refusal_reason())

        # -- auth: a reference that cannot resolve is a refusal, never a default
        credential, auth_problem = resolve_auth(endpoint.auth_ref)
        if auth_problem:
            return finish("refused", auth_problem)

        # -- model choice is the ROUTER's, resolved here, recorded either way
        model_choice, model_reason = (None, "endpoint declared model=none")
        if endpoint.model_source == "router":
            model_choice, model_reason = self.resolver(profile=message.get("profile"))
        row["model_chosen"] = (model_choice or {}).get("model")
        if not model_choice:
            row["null_reasons"]["model_chosen"] = model_reason

        # -- the timeout bound: the endpoint must have declared one
        timeout_s = endpoint.timeout_s
        if message.get("timeout_s"):
            timeout_s = min(timeout_s, message["timeout_s"])
        if timeout_s <= 0:
            return finish("refused", "timeout-not-declared: endpoint %r has no bound" % endpoint.id)

        try:
            hop = build_hop(endpoint, message, model_choice, credential)
        except Refusal as refusal:
            return finish("refused", refusal.reason)
        row["transform"] = hop["transform"]

        # -- the forward
        status, body_text = None, ""
        try:
            request = urllib.request.Request(
                hop["url"], data=json.dumps(hop["body"]).encode(),
                headers=hop["headers"], method="POST")
            with self.opener(request, timeout=timeout_s) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body_text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body_text = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body_text = ""
        except TimeoutError:
            return finish("timeout", "endpoint did not answer within %ss" % timeout_s)
        except Exception as exc:  # noqa: BLE001 — transport failure is a recorded failure
            return finish("failed", "transport-error: %s (%s)"
                          % (exc.__class__.__name__, str(exc)[:200]))
        row["http_status"] = status

        if status is not None and int(status) >= 400:
            return finish("failed", "endpoint-http-%s: %s" % (status, (body_text or "")[:300]))

        reply_text, why, served_model, tin, tout, terminal = extract_reply(
            hop["reply_rule"], status, body_text)
        row["model_served"] = _served_model(served_model)
        row["tokens_in"] = tin
        row["tokens_out"] = tout
        if hop["reply_rule"] == "webhook-separate" or hop["reply_rule"] == "none":
            # Honest: accepted, no inline answer, nothing to wait for.
            return finish("accepted", why, ok=True, reply_text="", ack=True)
        if reply_text is None:
            return finish("unservable", why)
        return finish("ok", why, ok=True, reply_text=reply_text)

    # -- reply envelope ----------------------------------------------------
    def build_reply(self, message, row, reply_text):
        return {
            "in_reply_to": message.get("inbound_id") or "",
            "sender_idempotency_key": message.get("idempotency_key"),
            "endpoint": row.get("endpoint_served") or row.get("endpoint_requested"),
            "endpoint_requested": row.get("endpoint_requested"),
            "lane": row.get("lane"),
            "protocol": row.get("protocol"),
            "transform": row.get("transform"),
            "ok": bool(row.get("ok")),
            "outcome": row.get("outcome"),
            "reason": row.get("reason"),
            "session": row.get("session"),
            "model_chosen": row.get("model_chosen"),
            "model_served": row.get("model_served"),
            "tokens_in": row.get("tokens_in"),
            "tokens_out": row.get("tokens_out"),
            "cost_usd": row.get("cost_usd"),
            "seconds": row.get("seconds"),
            "reply": reply_text if reply_text is not None else "",
        }

    def reply_target(self, message):
        return message.get("reply_to") or DEFAULT_REPLY_TO


def _iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


# ---------------------------------------------------------------------------
# Transports.
# ---------------------------------------------------------------------------


def load_crier_client(path=None):
    """Import the fleet's crier client (never a vendored fork of it)."""
    path = Path(path or os.environ.get("CRIER_CLIENT_PATH") or DEFAULT_CRIER_CLIENT_PATH)
    path = Path(os.path.expanduser(str(path)))
    if not (path / "crier_client.py").exists():
        raise RuntimeError("crier client not found at %s (set CRIER_CLIENT_PATH)" % path)
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import crier_client  # noqa: WPS433 — deliberate runtime import from a configured path
    return crier_client


def _bus_url(explicit=None):
    """The bus base URL, resolved once (explicit > env > default)."""
    return (explicit or os.environ.get("ROUTER_INGRESS_BUS_URL") or os.environ.get("CRIER_URL")
            or DEFAULT_BUS_URL)


class BusTransport:
    """Crier inboxes: retrieve (leased) -> process -> reply -> ack."""

    def __init__(self, base=None, token=None, ids=None, key_dir=None, client_path=None,
                 ingress=None, logger=None):
        self.base = _bus_url(base)
        self.token = token if token is not None else _bus_token()
        ids = ids or [i.strip() for i in
                      (os.environ.get("ROUTER_INGRESS_BUS_IDS") or "task-router").split(",")
                      if i.strip()]
        self.ids = ids
        key_dir = Path(os.path.expanduser(str(key_dir or os.environ.get("ROUTER_INGRESS_KEY_DIR")
                                             or DEFAULT_KEY_DIR)))
        self.key_dir = key_dir
        self.ingress = ingress or Ingress(load_endpoints()[0])
        self.log = logger or (lambda *_a, **_k: None)
        self._crier = load_crier_client(client_path)
        self._clients = {}
        for ident in ids:
            key_path = key_dir / ("%s.key" % ident)
            if not key_path.exists():
                raise RuntimeError("no private key for bus identity %r at %s" % (ident, key_path))
            self._clients[ident] = self._crier.Crier(
                self.base, agent_id=ident, key_path=str(key_path), token=self.token, timeout=30.0)

    def poll_once(self, limit=1, lease_seconds=1800):
        """One pass over every configured inbox. Returns the results list."""
        results = []
        for ident, client in self._clients.items():
            try:
                messages = client.retrieve(limit=limit, lease_seconds=lease_seconds)
            except Exception as exc:  # noqa: BLE001 — a bus hiccup is not a crash
                self.log("retrieve failed for %s: %s" % (ident, exc))
                results.append({"inbox": ident, "error": "retrieve failed: %s" % exc})
                continue
            for msg in messages:
                envelope = dict(msg.raw or {})
                envelope.setdefault("id", msg.id)
                envelope.setdefault("payload", msg.payload)
                out = self.ingress.process(envelope, payload=msg.payload)
                row = out["ledger"]
                self.log("inbound %s -> %s [%s] %ss" % (
                    msg.id, row.get("endpoint_served"), row.get("outcome"), row.get("seconds")))
                if out["reply"] is not None:
                    target = out.get("reply_to") or DEFAULT_REPLY_TO
                    try:
                        client.deliver(target, out["reply"], sender=ident)
                    except Exception as exc:  # noqa: BLE001
                        self.log("reply delivery failed for %s (left unacked): %s" % (msg.id, exc))
                        results.append({"inbox": ident, "inbound_id": msg.id,
                                        "outcome": row.get("outcome"),
                                        "reply_delivery": "failed: %s" % exc})
                        continue
                if out["ack"]:
                    try:
                        client.ack(msg)
                    except Exception as exc:  # noqa: BLE001
                        self.log("ack failed for %s (will redeliver): %s" % (msg.id, exc))
                results.append({"inbox": ident, "inbound_id": msg.id,
                                "outcome": row.get("outcome"), "ack": out["ack"]})
        return results


def _bus_token():
    value = (os.environ.get("CR_AUTH_TOKEN") or "").strip()
    if value:
        return value
    path = os.environ.get("ROUTER_INGRESS_BUS_TOKEN_FILE") or str(DEFAULT_BUS_TOKEN_FILE)
    try:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def forward_file(ingress, path, endpoint_id=None, json_out=False):
    """Load one envelope from a file and run it (the offline path tests use)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict) and "payload" in doc and "id" in doc:
        envelope = doc
    else:
        envelope = {"id": "file-%s" % Path(path).stem, "payload": doc}
    if endpoint_id:
        payload = dict(envelope.get("payload") or {}) if isinstance(envelope.get("payload"), dict) \
            else {"prompt": envelope.get("payload")}
        payload["endpoint"] = endpoint_id
        envelope["payload"] = payload
    out = ingress.process(envelope)
    if json_out:
        print(json.dumps({"reply": out["reply"], "ledger": out["ledger"]}, indent=2, sort_keys=True))
    else:
        row = out["ledger"]
        print("inbound %s -> endpoint=%s outcome=%s ok=%s in %ss"
              % (row.get("inbound_id"), row.get("endpoint_served"), row.get("outcome"),
                 row.get("ok"), row.get("seconds")))
        if out["reply"]:
            print("reply_to=%s ok=%s reason=%s"
                  % (out.get("reply_to") or DEFAULT_REPLY_TO,
                     out["reply"].get("ok"), out["reply"].get("reason")))
            print((out["reply"].get("reply") or "")[:4000])
    return out


# -- inbound HTTP (push mode) ------------------------------------------------


def _make_handler(ingress, token, verify_sig, logger, bus_sink=None):
    from http.server import BaseHTTPRequestHandler

    class IngressHandler(BaseHTTPRequestHandler):
        server_version = "task-router-ingress/1.0"

        def _json(self, status, doc):
            body = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # keep the server quiet; the ledger is the record
            logger("http %s" % (fmt % args))

        def _refuse(self, status, reason):
            row = ingress.append_ledger({
                "ts": _iso_now(), "ingress": "task-router", "inbound_id": None,
                "endpoint_requested": None, "endpoint_served": None, "protocol": None,
                "transform": None, "outcome": "refused", "ok": False, "reason": reason,
                "seconds": 0.0, "null_reasons": {"model_chosen": "refused before routing"},
            })
            self._json(status, {"ok": False, "outcome": "refused", "reason": reason,
                                "ledger_ts": row["ts"]})

        def do_GET(self):  # noqa: N802
            if urllib.parse.urlsplit(self.path).path == "/health":
                # admission.stats() carries per-lane state: that is the operator's
                # view of WHICH lane is down (state/consecutive_failures/last_failure).
                return self._json(200, {"status": "ok", "ingress": "task-router",
                                        "admission": ingress.admission.stats(),
                                        "endpoints": sorted(ingress.endpoints)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path != "/ingress/v1/messages":
                return self._json(404, {"error": "not found", "known": ["/ingress/v1/messages"]})
            reason = None
            if token:
                supplied = self.headers.get("Authorization") or ""
                expected = "Bearer " + token
                if not hmac.compare_digest(supplied, expected):
                    reason = "unauthorized: bad or missing bearer token"
            if reason is None and verify_sig is not None:
                reason = verify_sig(self.headers, "POST", path)
            if reason:
                return self._refuse(401, reason)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                doc = json.loads(raw or b"{}")
            except ValueError:
                return self._refuse(400, "bad-request: body is not JSON")
            if not isinstance(doc, dict):
                return self._refuse(400, "bad-request: body is not an object")
            # Two accepted shapes: a full envelope ({"id","sender","payload"}) or a
            # bare payload. The bare shape keeps a minimal pusher honest — the
            # envelope metadata it omits is simply absent, never invented.
            if "payload" in doc:
                envelope = doc
            else:
                envelope = {"id": doc.get("id") or ("http-%d" % int(time.time() * 1000)),
                            "sender": doc.get("sender") or self.headers.get("X-Agent-ID"),
                            "payload": doc}
            # push mode answers with a receipt; the forward happens inline so the
            # receipt carries the outcome it observed (never a claim it did not)
            out = ingress.process(envelope)
            reply = out["reply"] or {}
            bus_delivery = None
            if bus_sink is not None and reply:
                bus_delivery = bus_sink(reply, out.get("reply_to") or DEFAULT_REPLY_TO)
            self._json(200, {
                "ok": reply.get("ok", False),
                "outcome": out["outcome"],
                "in_reply_to": reply.get("in_reply_to"),
                "reason": reply.get("reason"),
                "endpoint": reply.get("endpoint"),
                "seconds": reply.get("seconds"),
                "reply_to": out.get("reply_to"),
                "bus_delivery": bus_delivery,
            })

    return IngressHandler


def serve(ingress, host, port, token, verify_sig=None, logger=None, bus_sink=None):
    from http.server import ThreadingHTTPServer
    handler = _make_handler(ingress, token, verify_sig, logger or (lambda *_a: None),
                            bus_sink=bus_sink)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    logger("ingress listening on http://%s:%s/ingress/v1/messages (auth=%s, bus_reply=%s)"
           % (host, port, "bearer+agent-sig" if verify_sig else
              ("bearer" if token else "NONE"), "on" if bus_sink else "off"))
    httpd.serve_forever()


class BusSink:
    """Deliver an ingress reply to a bus inbox as the ingress's own identity.

    Push mode has no lease to settle, so the reply itself is the only trace on
    the bus; this is what makes the two doors symmetric (both report back).
    """

    def __init__(self, ident=None, base=None, key_dir=None, token=None, client_path=None):
        self.ident = ident or (os.environ.get("ROUTER_INGRESS_BUS_IDS") or "task-router").split(",")[0].strip()
        base = _bus_url(base)
        key_dir = Path(os.path.expanduser(str(key_dir or os.environ.get("ROUTER_INGRESS_KEY_DIR")
                                             or DEFAULT_KEY_DIR)))
        key_path = key_dir / ("%s.key" % self.ident)
        if not key_path.exists():
            raise RuntimeError("no private key for bus identity %r at %s" % (self.ident, key_path))
        crier = load_crier_client(client_path)
        self.client = crier.Crier(base, agent_id=self.ident, key_path=str(key_path),
                                  token=token if token is not None else _bus_token(), timeout=30.0)

    def __call__(self, payload, target):
        """Returns a stated delivery outcome — never a bare None (fail loud)."""
        try:
            self.client.deliver(target, payload, sender=self.ident)
            return "ok:%s" % target
        except Exception as exc:  # noqa: BLE001
            return "failed:%s (%s)" % (exc.__class__.__name__, str(exc)[:200])


def make_signature_verifier(agent_keys, client_path=None, window_s=30):
    """Build a verifier for the bus's own scheme: X-Agent-ID/Ts/Sig.

    The signature is the crier signing string ("METHOD\\npath\\nts", hex ed25519)
    verified with the bus client's own primitive — the same code the server runs,
    so the ingress cannot drift from the bus's definition of a valid signature.
    An empty key map verifies nothing: every signed request is refused by name.
    """
    crier = load_crier_client(client_path)

    def verify(headers, method, path):
        ident = (headers.get("X-Agent-ID") or "").strip()
        ts = (headers.get("X-Agent-Ts") or "").strip()
        sig = (headers.get("X-Agent-Sig") or "").strip()
        if not (ident and ts and sig):
            return ("unauthorized: signed ingress requires "
                    "X-Agent-ID/X-Agent-Ts/X-Agent-Sig")
        try:
            age = abs(time.time() - float(ts))
        except ValueError:
            return "unauthorized: X-Agent-Ts is not a unix timestamp"
        if age > window_s:
            return "unauthorized: X-Agent-Ts outside the +/-%ss window" % window_s
        public_hex = agent_keys.get(ident)
        if not public_hex:
            return "unauthorized: unknown agent id %r" % ident
        try:
            signature = bytes.fromhex(sig)
            public_key = bytes.fromhex(str(public_hex))
        except ValueError:
            return "unauthorized: signature or public key is not hex"
        message = ("%s\n%s\n%s" % (method, path, ts)).encode()
        if not crier.ed25519_verify(signature, message, public_key):
            return "unauthorized: signature does not verify for %r" % ident
        return None

    return verify


def fetch_agent_keys(base, token=None, opener=None):
    """Snapshot the bus registry's public keys (for signed-ingress verification).

    Returns {agent_id: public_key_hex}. An unreachable registry is an empty map,
    which makes every signed request refusable BY NAME — never an open door.
    """
    opener = opener or urllib.request.urlopen
    if not base:
        return {}
    url = base.rstrip("/") + "/agents"
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with opener(request, timeout=10) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}
    agents = doc.get("agents") if isinstance(doc, dict) else doc
    out = {}
    for agent in agents or []:
        if isinstance(agent, dict) and agent.get("id") and agent.get("public_key"):
            out[str(agent["id"])] = str(agent["public_key"])
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_ingress(args):
    endpoints, problems = load_endpoints(args.endpoints)
    for problem in problems:
        print("endpoint registry problem: %s" % problem, file=sys.stderr)
    for endpoint in endpoints:
        if not endpoint.usable:
            print("endpoint %s REFUSED: %s" % (endpoint.id or "?", endpoint.refusal_reason()),
                  file=sys.stderr)
    return Ingress(endpoints, ledger=args.ledger, logger=lambda m: print(m, file=sys.stderr))


def cmd_endpoints(args):
    endpoints, problems = load_endpoints(args.endpoints)
    if args.json:
        print(json.dumps({"endpoints": [e.public() for e in endpoints], "problems": problems},
                         indent=2, sort_keys=True))
        return 0
    for endpoint in endpoints:
        mark = "ok " if endpoint.usable else "REFUSED"
        print("%-9s %-18s %-20s reply=%-18s timeout=%ss auth=%s"
              % (mark, endpoint.id, endpoint.protocol, endpoint.reply_rule,
                 endpoint.raw.get("timeout_s"), endpoint.auth_ref))
        for problem in endpoint.problems:
            print("            - %s" % problem)
    for problem in problems:
        print("registry problem: %s" % problem)
    return 0 if not any(not e.usable for e in endpoints) and not problems else 1


def cmd_translate(args):
    ingress = _build_ingress(args)
    doc = json.loads(Path(args.message_file).read_text(encoding="utf-8"))
    envelope = doc if isinstance(doc, dict) and "payload" in doc else {"id": "translate", "payload": doc}
    payload = envelope.get("payload")
    message = normalize_message(payload, envelope)
    target = args.endpoint or message["target"]
    endpoint = ingress.endpoints.get(target)
    if endpoint is None:
        print("refused: unknown-endpoint: %r" % target, file=sys.stderr)
        return 1
    if not endpoint.usable:
        print("refused: %s" % endpoint.refusal_reason(), file=sys.stderr)
        return 1
    choice, reason = (None, "model=none") if endpoint.model_source == "none" else ingress.resolver(
        profile=message.get("profile"))
    credential, auth_problem = resolve_auth(endpoint.auth_ref)
    if auth_problem:
        print("refused: %s" % auth_problem, file=sys.stderr)
        return 1
    try:
        hop = build_hop(endpoint, message, choice, credential)
    except Refusal as refusal:
        print("refused: %s" % refusal.reason, file=sys.stderr)
        return 1
    out = {"endpoint": endpoint.id, "protocol": endpoint.protocol, "transform": hop["transform"],
           "url": hop["url"], "session": message["session"],
           "model_chosen": (choice or {}).get("model"), "model_reason": reason,
           "timeout_s": endpoint.timeout_s, "reply_to": message["reply_to"],
           "prompt_chars": len(message["prompt"] or ""),
           "body_keys": sorted(hop["body"].keys())}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_forward(args):
    ingress = _build_ingress(args)
    out = forward_file(ingress, args.message_file, endpoint_id=args.endpoint, json_out=args.json)
    return 0 if out["ledger"].get("ok") else 1


def cmd_poll(args):
    ingress = _build_ingress(args)
    transport = BusTransport(ingress=ingress, base=args.bus_url, ids=args.bus_ids,
                             key_dir=args.key_dir, logger=lambda m: print(m, file=sys.stderr))
    while True:
        results = transport.poll_once(limit=args.limit, lease_seconds=args.lease_seconds)
        if args.json:
            print(json.dumps(results, sort_keys=True))
        if args.once:
            return 0
        if not results:
            time.sleep(args.poll_s)


def cmd_serve(args):
    ingress = _build_ingress(args)
    token = None
    if args.token_file:
        token = Path(os.path.expanduser(args.token_file)).read_text(encoding="utf-8").strip()
    elif os.environ.get("ROUTER_INGRESS_TOKEN"):
        token = os.environ["ROUTER_INGRESS_TOKEN"]
    verify_sig = None
    if args.require_sig:
        agent_keys = None
        if args.agent_keys_file:
            doc = json.loads(Path(os.path.expanduser(args.agent_keys_file)).read_text(
                encoding="utf-8"))
            agent_keys = doc.get("agents") if isinstance(doc, dict) and "agents" in doc else doc
        else:
            agent_keys = fetch_agent_keys(_bus_url(args.bus_url), token=_bus_token())
        agent_keys = {str(k): str(v) for k, v in (agent_keys or {}).items()}
        if not agent_keys:
            print("agent key snapshot is EMPTY — every signed request will be refused",
                  file=sys.stderr)
        verify_sig = make_signature_verifier(agent_keys)
    if not token and verify_sig is None:
        print("refusing to serve an UNauthenticated ingress: set --token-file, "
              "ROUTER_INGRESS_TOKEN, or --require-sig", file=sys.stderr)
        return 2
    bus_sink = None
    if not args.no_bus_reply:
        try:
            bus_sink = BusSink(base=args.bus_url)
        except Exception as exc:  # noqa: BLE001 — degraded is visible, never silent
            print("bus reply DISABLED: %s" % exc, file=sys.stderr)
    serve(ingress, args.host, args.port, token, verify_sig=verify_sig, bus_sink=bus_sink,
          logger=lambda m: print(m, file=sys.stderr))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="router_ingress",
                                     description="Crier -> router ingress (TR-236).")
    parser.add_argument("--endpoints", default=None, help="endpoints registry (JSONL)")
    parser.add_argument("--ledger", default=None, help="ledger JSONL path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("endpoints", help="list + validate the declared endpoints")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_endpoints)

    p = sub.add_parser("translate", help="show the translation for one message (no forward)")
    p.add_argument("--message-file", required=True)
    p.add_argument("--endpoint", default=None)
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("forward", help="forward one message file (no bus)")
    p.add_argument("--message-file", required=True)
    p.add_argument("--endpoint", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("poll", help="drain bus inboxes (the offline-tolerant door)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--lease-seconds", type=int, default=1800)
    p.add_argument("--poll-s", type=float, default=5.0)
    p.add_argument("--bus-url", default=None)
    p.add_argument("--bus-ids", default=None, help="comma list; default ROUTER_INGRESS_BUS_IDS")
    p.add_argument("--key-dir", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("serve", help="authenticated inbound endpoint (the push door)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9410)
    p.add_argument("--token-file", default=None)
    p.add_argument("--require-sig", action="store_true",
                   help="also require the crier X-Agent-ID/Ts/Sig trio")
    p.add_argument("--agent-keys-file", default=None,
                   help="offline snapshot of {agent_id: public_key_hex} for --require-sig")
    p.add_argument("--no-bus-reply", action="store_true",
                   help="do not deliver the reply to the bus (receipt only)")
    p.add_argument("--bus-url", default=None)
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.endpoints = args.endpoints or str(endpoints_path())
    args.ledger = args.ledger or str(ledger_path())
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

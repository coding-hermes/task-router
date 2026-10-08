#!/usr/bin/env python3
"""router_hop_taxonomy.py — the ONE failure taxonomy for every surface (TR-288).

The hourly probe already knows WHY a lane failed: 429 capacity pressure vs a
quota window (business codes 1308/1310/1316/1317), credit exhausted (402),
auth misconfig (401/403), wrong route (404/405), overloaded (503). The request
path collapsed every 4xx into one `upstream-4xx` bucket, so the ladder could
not tell a no-credit 402 from a concurrency 429, and the ledger could not say
which happened.

This module is the single vocabulary, imported by:

  - scripts/router_server.py      (_classify_hop_failure -> hop rows)
  - scripts/router_circuit.py     (FAILURE_CLASS_MAP / circuit classes)
  - scripts/provider_health_probe.py (probe battery reasons)

Each code carries its BLAST RADIUS, which is the owner's rule (2026-10-03):

  model   — a failure naming a MODEL on one provider takes out only that
            (provider, model) pair; the provider keeps serving its other
            models. (429-capacity, 5xx-overloaded, context-length, timeouts)
  provider— a provider-wide condition downs the PROVIDER: account concurrency
            cap, credit exhausted, auth failure. (429-quota-window, 402,
            401/403, provider outage)

and its RETRY POLICY on the hop ladder:

  retry-same    — transient; retry the same hop once before advancing.
  skip          — do not re-send this payload to this hop; advance.
  shrink-or-skip— context-length: shrinking the payload may rescue the hop;
                  otherwise skip.
"""
import json
import re

#: The hop failure codes. THE vocabulary — probe, ladder and ledger row all
#: speak exactly these.
HOP_FAILURE_CODES = (
    "429-capacity",            # concurrent/rate pressure on THIS model
    "429-quota-window",        # plan/weekly window exhausted (incl. business codes)
    "402-no-credit",           # credit/balance exhausted (provider-wide)
    "401-403-auth",            # auth/permission misconfig (provider-wide)
    "404-405-route",           # wrong endpoint/model id for this provider
    "408-timeout",             # the upstream itself reported a timeout
    "5xx-overloaded",          # transient server-side capacity
    "context-length-exceeded", # payload larger than the model's context
    # Structural / transport (no upstream status available):
    "idle-timeout",
    "hop-wall-timeout",
    "transport-error",
    "unservable-2xx",
)

#: blast radius: which level of state a code demotes.
BLAST_RADIUS = {
    "429-capacity": "model",
    "429-quota-window": "provider",
    "402-no-credit": "provider",
    "401-403-auth": "provider",
    "404-405-route": "model",
    "408-timeout": "model",
    "5xx-overloaded": "model",
    "context-length-exceeded": "model",
    "idle-timeout": "model",
    "hop-wall-timeout": "model",
    "transport-error": "model",
    "unservable-2xx": "model",
}

#: per-code retry policy for the hop ladder.
RETRY_POLICY = {
    "429-capacity": "skip",
    "429-quota-window": "skip",
    "402-no-credit": "skip",
    "401-403-auth": "skip",
    "404-405-route": "skip",
    "408-timeout": "retry-same",
    "5xx-overloaded": "retry-same",
    "context-length-exceeded": "shrink-or-skip",
    "idle-timeout": "skip",
    "hop-wall-timeout": "skip",
    "transport-error": "skip",
    "unservable-2xx": "skip",
}

#: status-code -> taxonomy code, when the body gives nothing more specific.
_STATUS_CODE_MAP = {
    402: "402-no-credit",
    404: "404-405-route",
    405: "404-405-route",
    408: "408-timeout",
    429: "429-capacity",       # refined by the body below
    503: "5xx-overloaded",
}

#: probe business codes that mean a QUOTA WINDOW, not capacity pressure
#: (provider_health_probe v3: zai/deepseek-family 429 bodies carry them).
QUOTA_WINDOW_BUSINESS_CODES = ("1308", "1310", "1316", "1317")

#: body substrings that refine a raw status into a specific code. Order
#: matters: the first match wins, most specific first.
_BODY_MARKERS = (
    ("access_terminated", "429-quota-window"),   # kimi weekly window (probe v3)
    ("insufficient", "402-no-credit"),
    ("insufficient_quota", "402-no-credit"),
    ("billing", "402-no-credit"),
    ("quota", "429-quota-window"),
    ("budget", "429-quota-window"),
    ("credit", "402-no-credit"),
    ("unauthorized", "401-403-auth"),
    ("forbidden", "401-403-auth"),
    ("api key", "401-403-auth"),
    ("permission", "401-403-auth"),
    ("context length", "context-length-exceeded"),
    ("context_length", "context-length-exceeded"),
    ("maximum context", "context-length-exceeded"),
    ("too large", "context-length-exceeded"),
    ("overloaded", "5xx-overloaded"),
    ("rate limit", "429-capacity"),
)


def code_from_status(status, body=None):
    """Taxonomy code for an HTTP status, refined by the response body.

    Status wins for the unambiguous codes (402/404/405/408); 429 and 5xx are
    refined by body text (a 'quota window' 429 and a 'context length' 400
    say WHY in the body — the probe has taught the body for years).
    """
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        code = 0
    if code in (401, 403):
        return "401-403-auth"
    if code in _STATUS_CODE_MAP:
        base = _STATUS_CODE_MAP[code]
    elif 500 <= code < 600:
        base = "5xx-overloaded"
    elif code == 400:
        base = None  # only the body can say (context length vs bad request)
    else:
        base = None
    text = (body or '').lower()
    if text:
        for marker, mapped in _BODY_MARKERS:
            if marker in text:
                # a body marker refines 4xx/5xx regardless of base; but never
                # let a stray word upgrade a plain 404 route error into auth
                if base in (None, "429-capacity", "5xx-overloaded"):
                    return mapped
    if base:
        return base
    if 400 <= code < 500:
        return None  # unclassified client error -> the caller's upstream-4xx fallback
    return None


def code_from_exception(exc):
    """Taxonomy code for a transport-level exception (timeout vs dead)."""
    name = type(exc).__name__ if exc is not None else ''
    text = str(exc or '').lower()
    if "idle" in text:
        return "idle-timeout"
    if isinstance(exc, TimeoutError) or name in (
            "timeout", "Timeout", "socket.timeout", "_HermesIdleTimeout") \
            or "timed out" in text or "timeout" in text:
        return "hop-wall-timeout"
    return "transport-error"


def context_length_from_body(body):
    """True when the body says the payload exceeded the model's context."""
    text = (body or '').lower()
    return any(m in text for m in ("context length", "context_length",
                                   "maximum context", "too large"))


def retry_once(code):
    """True when the code's retry policy allows ONE same-hop retry."""
    return RETRY_POLICY.get(code) == "retry-same"


def demote_pair(code):
    """True when the code demotes only the (provider, model) pair."""
    return BLAST_RADIUS.get(code) == "model"


def demote_provider(code):
    """True when the code demotes the whole provider (hard condition)."""
    return BLAST_RADIUS.get(code) == "provider"

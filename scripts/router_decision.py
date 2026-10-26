#!/usr/bin/env python3
"""router_decision.py — the just-in-time execution decision contract (ACP-ROUTER-001).

A CLOSED, versioned request/response seam for the Hermes foreman. Immediately
before every execution attempt the foreman posts complete routing-relevant
task/step context (brief digest, context reference digests, raw per-category
levels, context limit, data class, policy labels, connector capabilities,
budget/deadline, identity, optional previous route digest) and receives a
canonical, digestable, expiring ROUTE RECEIPT: the exact ordered
model/provider chain, the exact exclusions, the gate evidence and the policy
bound expiry.

Authority boundary — Task Router stays model/provider-only. This surface
NEVER chooses or names a harness, connector, tool, workspace, Bunker, Crier
recipient or board row; it never dispatches, never creates sessions, never
writes outcomes and never mutates live routing state (read-only by
construction: no metrics append, no ledger write, no state file write).
Required connector capabilities are echoed as constraints only — they never
select anything here. Candidate order is the resolver order, unmodified.

Fail-closed on the route itself (unlike `router spawn`, which is fail-open for
the scheduler): an unsatisfiable requirement or a fully gated chain yields an
explicit machine-readable refusal (`eligible=false`, `head=null`) — the
requirements-unmet always-run fallback lane is NEVER returned as an execution
route (`refusal.code = DEGRADED_FALLBACK_REFUSED`).

Surfaces (identical semantics, one resolver — router_spawn.resolve):

  * Python:  import router_decision; router_decision.decide(request, now=...)
  * HTTP:    POST /api/v1/execution-decision (+ /verify), read-only mode OK
  * CLI:     router decide --request req.json [--now RFC3339]
             router decide --verify receipt.json [--now RFC3339]

Exit codes (CLI): 0 = decision/verification produced (inspect the JSON — a
refusal is a RESULT, not a crash); 2 = usage error or INVALID_REQUEST /
ATTEMPT_DEADLINE_PASSED (caller bug, propagates — TR-058 doctrine); 1 =
RESOLVER_ERROR (infrastructure failure; fail closed, never route).

Canonical form (the ONE wire format for every digest):

    json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
               allow_nan=False)

Deterministic across key order and process restart. `request_digest` covers
the NORMALIZED request (absent optional fields stay absent — absent and
explicit are distinct). `receipt_digest` covers the receipt WITHOUT the
`receipt_digest` key itself and NOTHING else is excluded: every
authority-bearing field is covered, including the volatile ones. Volatile
fields included in the digest by design:

    receipt_id       deterministic id (sha256 over request_digest +
                     resolved_at + valid_until) — re-issuing the same request
                     at the same clock yields the same receipt, byte for byte
    resolved_at      the decision clock, UTC second precision
    valid_until      policy-bound expiry, UTC second precision

so tampering with the expiry or the issuance time is a digest mismatch, and a
receipt verifier refuses an expired receipt explicitly (`EXPIRED`).

Policy (the only knobs, both fixed here — never caller-settable past the
ceiling): `POLICY_DEFAULT_TTL_S` (300) and `POLICY_MAX_TTL_S` (900). The
caller may request a shorter/longer window via `valid_for_s`, clamped to the
policy ceiling; `valid_until` is additionally bounded by `attempt_deadline`
when the request declares one. ISSUANCE takes an injectable clock (`now=` /
`--now` / ROUTER_DECISION_NOW) for deterministic replay and tests only — the
HTTP surface never takes it from the request body. VERIFICATION is different
(H1-F2): it ignores ROUTER_DECISION_NOW and checks `valid_until` against both
an explicit `now=` and the host wall clock, so an injected clock can only
make a receipt more expired. VERIFICATION is also BOUND (H1-F1): it requires
the expected `parent_run_id` + `step_id` (and optionally the generation/head)
and refuses an unbound call rather than returning a silent ok.

Schema decisions (v1, closed — unknown fields are rejected, never ignored):

  * required: version, parent_run_id, step_id, generation, brief_digest,
    required_levels, requester
  * optional: context_refs, required_context_limit, required_data_class,
    policy_labels, required_connector_capabilities, budget_ceiling_usd,
    budget_remaining_usd, attempt_deadline, valid_for_s,
    previous_route_digest
  * required_levels uses the canonical category vocabulary (validated against
    category_levels.jsonl / level_defs.jsonl through the resolver's own
    ad-hoc validator); `min_context` is deliberately NOT accepted there — the
    context requirement lives in `required_context_limit` alone
  * at least one requirement must be declared (levels and/or context limit)
  * `required_data_class` is an exact-match eligibility constraint against the
    lane's registry data_class (no lattice is invented: unknown lane class
    never satisfies a declared class)
  * `policy_labels` / `required_connector_capabilities` are validated and
    echoed as informational constraints — they never reorder or filter
  * numbers are normalized to JSON floats, timestamps to UTC `...Z` seconds
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from typing import Any

# scripts/ must be importable when this file is dispatched via runpy (the
# `router decide` CLI path) — same convention as router_spawn.py.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import router_spawn as _rs  # noqa: E402  (THE resolver — reused, never forked)

#: Version of the request contract this build accepts.
DECISION_REQUEST_VERSION = 1
#: Version stamped on every receipt / verification result this build emits.
RECEIPT_VERSION = 1
#: Policy bounds for the decision window. Caller influence stops at the
#: ceiling ("not caller-controlled beyond an allowed ceiling").
POLICY_DEFAULT_TTL_S = 300
POLICY_MAX_TTL_S = 900

_ID_RE = r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}"
_DIGEST_RE = r"[0-9a-f]{64}"
_LABEL_RE = r"[a-z0-9][a-z0-9._-]{0,63}"

#: Machine-readable refusal codes carried ON the receipt (a decision was
#: computed; the route itself is refused).
REFUSAL_NO_ELIGIBLE = "NO_ELIGIBLE_CANDIDATE"
REFUSAL_FALLBACK = "DEGRADED_FALLBACK_REFUSED"

#: Chain-binding refusals (H1-F5). `previous_route_digest` is a CLAIM about the
#: route the previous attempt used; it is checked against the issuance ledger
#: instead of being echoed unverified.
ERR_PREVIOUS_ROUTE_UNKNOWN = "PREVIOUS_ROUTE_UNKNOWN"
ERR_PREVIOUS_ROUTE_MISMATCH = "PREVIOUS_ROUTE_MISMATCH"

#: Machine-readable error codes for NON-receipt responses (the request itself
#: was refused — no route decision was computed).
ERR_INVALID_REQUEST = "INVALID_REQUEST"
ERR_DEADLINE_PASSED = "ATTEMPT_DEADLINE_PASSED"
ERR_RESOLVER_ERROR = "RESOLVER_ERROR"

#: Decision-stage exclusion codes (extending the resolver's gate vocabulary
#: with the two reasons only this surface can produce).
CODE_DATA_CLASS = "data-class-mismatch"
CODE_FALLBACK_REFUSED = "fallback-refused"

#: Receipt AUTHENTICATION. The canonical digest proves integrity only: it is
#: unkeyed, so anyone can recompute it over a receipt they invented. Every
#: issued receipt is therefore signed with a server-held key AND recorded in a
#: durable issuance ledger; verification requires both.
SIGNATURE_ALG = "hmac-sha256-v1"
SIGNATURE_FIELDS = ("signature", "signature_alg")
_SIGNATURE_FIELDS = frozenset(SIGNATURE_FIELDS)
_DIGEST_EXCLUDED = frozenset(("receipt_digest",) + SIGNATURE_FIELDS)

#: Where the signing key and the issuance ledger live (inside the router state
#: dir, so the hermetic test plane and the live install are the same shape).
RECEIPT_KEY_FILENAME = "decision-receipt.key"
ISSUANCE_DIRNAME = "decision-receipts"
RECEIPT_KEY_MAX_AGE_S = 90 * 24 * 3600

#: Verification failure codes for the authentication layer.
ERR_RECEIPT_UNSIGNED = "RECEIPT_UNSIGNED"
ERR_RECEIPT_SIGNATURE_INVALID = "RECEIPT_SIGNATURE_INVALID"
ERR_RECEIPT_NOT_ISSUED = "RECEIPT_NOT_ISSUED"
ERR_RECEIPT_ISSUANCE_MISMATCH = "RECEIPT_ISSUANCE_MISMATCH"
ERR_RECEIPT_ISSUANCE_CONFLICT = "RECEIPT_ISSUANCE_CONFLICT"
ERR_RECEIPT_ISSUANCE_WRITE_FAILED = "RECEIPT_ISSUANCE_WRITE_FAILED"
ERR_RECEIPT_KEY_UNAVAILABLE = "RECEIPT_KEY_UNAVAILABLE"

#: Signing-key lifecycle refusals (H1-F6). The key has an age ceiling, an
#: identity, and a file-mode floor; each is enforced, not documented only.
ERR_RECEIPT_KEY_EXPIRED = "RECEIPT_KEY_EXPIRED"
ERR_RECEIPT_KEY_INSECURE_MODE = "RECEIPT_KEY_INSECURE_MODE"
ERR_RECEIPT_KEY_ID_MISMATCH = "RECEIPT_KEY_ID_MISMATCH"

#: Verification failure codes for BINDING and TRUST (H1-F1 / H1-F2).
#: `RECEIPT_UNBOUND_EXPECTATION` is the no-expectation refusal: a receipt on
#: its own proves integrity and provenance, never that it is the receipt FOR
#: the step the caller is about to execute.
ERR_RECEIPT_UNBOUND_EXPECTATION = "RECEIPT_UNBOUND_EXPECTATION"
ERR_RECEIPT_IDENTITY_MISMATCH = "RECEIPT_IDENTITY_MISMATCH"
ERR_RECEIPT_HEAD_MISMATCH = "RECEIPT_HEAD_MISMATCH"
ERR_RECEIPT_EXPIRED = "EXPIRED"

#: The keys a caller may bind a receipt to. `parent_run_id` + `step_id` are
#: mandatory (the identity), `generation` and `head` are optional.
_EXPECT_KEYS = frozenset({"parent_run_id", "step_id", "generation", "head"})
_REQUIRED_EXPECT_KEYS = ("parent_run_id", "step_id")

#: TEST-ONLY escape hatch for the dual-clock expiry check. Verification
#: compares the receipt's `valid_until` against the decision clock AND the
#: wall clock; the hermetic suite pins a 2026 clock, so it must opt out
#: explicitly. Exactly the string "1" arms it; anything else is ignored and
#: the safe (dual) path runs. This is NOT a replay knob: it cannot make a
#: receipt that is expired by the wall clock verify inside a live process
#: unless the operator deliberately exports it.
TEST_CLOCK_ENV = "ROUTER_DECISION_TEST_CLOCK"

_REQUIRED_REQUEST_FIELDS = (
    "version", "parent_run_id", "step_id", "generation", "brief_digest",
    "required_levels", "requester",
)
_OPTIONAL_REQUEST_FIELDS = (
    "context_refs", "required_context_limit", "required_data_class",
    "policy_labels", "required_connector_capabilities", "budget_ceiling_usd",
    "budget_remaining_usd", "attempt_deadline", "valid_for_s",
    "previous_route_digest",
)
_REQUEST_FIELDS = frozenset(_REQUIRED_REQUEST_FIELDS + _OPTIONAL_REQUEST_FIELDS)

#: The CLOSED receipt key set. Every key is always present on a receipt from
#: this build; a request field that was absent echoes as null (the REQUEST
#: distinguishes absent from explicit — an explicit null is rejected there).
RECEIPT_KEYS = frozenset({
    "version", "receipt_id", "receipt_digest", "request_digest",
    "brief_digest", "parent_run_id", "step_id", "generation",
    "previous_route_digest", "context_refs", "resolved_at", "valid_until",
    "valid_for_s", "eligible", "refusal", "head", "candidates", "exclusions",
    "gate_reasons", "requirements", "constraints", "source", "registry",
    "policy", "fallback_used", "degraded_fallback", "warnings",
    "gate_evidence", "key_id", "signature", "signature_alg",
})


# ---------------------------------------------------------------------------
# canonical form + digests
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """THE canonical serialization: sorted keys, no whitespace, ASCII, no NaN.

    Deterministic across input key order and process restart; every digest in
    this contract is computed over this exact byte string."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(request: dict) -> str:
    """Digest over the NORMALIZED request (absent optional fields stay absent)."""
    return sha256_hex(canonical_json(request))


def _body_without_digest(receipt: dict) -> dict:
    """Everything but the digest and the SIGNATURE fields.

    The signature is computed over (and therefore cannot be covered by) a
    digest that the signature itself covers — so `receipt_digest` excludes
    exactly `receipt_digest`, `signature` and `signature_alg`. Every
    authority-bearing field (including the volatile receipt_id /
    resolved_at / valid_until) stays inside the digest.
    """
    return {k: v for k, v in receipt.items() if k not in _DIGEST_EXCLUDED}


def receipt_digest(receipt: dict) -> str:
    """Digest over the receipt WITHOUT `receipt_digest` / signature fields."""
    return sha256_hex(canonical_json(_body_without_digest(receipt)))


def signature_message(receipt: dict) -> str:
    """The exact byte string the HMAC covers: the receipt INCLUDING its
    canonical digest, excluding only the signature fields."""
    return canonical_json({k: v for k, v in receipt.items()
                           if k not in _SIGNATURE_FIELDS})


# ---------------------------------------------------------------------------
# clock (injectable — deterministic replay and tests only)
# ---------------------------------------------------------------------------

def _canonical_ts(value: datetime.datetime) -> str:
    """UTC, second precision, `...Z` — the ONE timestamp form on the wire."""
    utc = value.astimezone(datetime.timezone.utc).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any, field: str, problems: list) -> datetime.datetime | None:
    """RFC3339 timestamp → aware datetime, or None with a named problem."""
    if not isinstance(value, str) or not value.strip():
        problems.append({"field": field, "problem": "must be an RFC3339 timestamp string"})
        return None
    text = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        problems.append({"field": field,
                         "problem": f"unparseable timestamp {text!r}"})
        return None
    if parsed.tzinfo is None:
        problems.append({"field": field,
                         "problem": f"timestamp {text!r} must carry an explicit "
                                    "offset (Z or +hh:mm)"})
        return None
    return parsed.astimezone(datetime.timezone.utc)


def decision_clock(now: Any = None, allow_env: bool = True) -> datetime.datetime:
    """The decision clock: explicit `now` > ROUTER_DECISION_NOW > system clock.

    Never raises on a bad override: an unparseable ROUTER_DECISION_NOW falls
    through to the system clock (a typo must not block the foreman). An
    explicit `now` argument that cannot be parsed DOES raise ValueError —
    that is programmatic input, not ambient state.

    `allow_env=False` (the VERIFICATION path, H1-F2) drops the ambient
    ROUTER_DECISION_NOW step entirely: an exported replay clock must never be
    able to decide that a receipt is still inside its window."""
    if now is not None:
        if isinstance(now, datetime.datetime):
            if now.tzinfo is None:
                raise ValueError("naive datetime passed as decision clock")
            return now.astimezone(datetime.timezone.utc)
        if isinstance(now, str):
            parsed = datetime.datetime.fromisoformat(now.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("decision clock string must carry an explicit offset")
            return parsed.astimezone(datetime.timezone.utc)
        raise ValueError("decision clock must be a datetime or RFC3339 string")
    if allow_env:
        env = (os.environ.get("ROUTER_DECISION_NOW") or "").strip()
        if env:
            try:
                parsed = datetime.datetime.fromisoformat(env.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(datetime.timezone.utc)
            except ValueError:
                pass  # fail-open: fall through to the system clock
    return datetime.datetime.now(datetime.timezone.utc)


def wall_clock() -> datetime.datetime:
    """The trusted clock: the host's UTC clock, never caller-supplied."""
    return datetime.datetime.now(datetime.timezone.utc)


def test_clock_armed() -> bool:
    """Is the TEST-ONLY single-clock mode armed (`ROUTER_DECISION_TEST_CLOCK=1`)?"""
    return (os.environ.get(TEST_CLOCK_ENV) or "").strip() == "1"


# ---------------------------------------------------------------------------
# request validation (closed: unknown fields rejected, never ignored)
# ---------------------------------------------------------------------------

def _match(pattern: str, value: str) -> bool:
    import re
    return re.fullmatch(pattern, value) is not None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_request(obj: Any) -> tuple[dict | None, list]:
    """Validate + normalize the closed v1 request.

    Returns (normalized, problems). `normalized` carries ONLY fields the
    request actually declared (absent stays absent — absent and explicit are
    distinct and both are digest-visible); every timestamp is canonicalized
    and every number is normalized to a JSON float where the contract says so.
    """
    problems: list = []
    if not isinstance(obj, dict):
        return None, [{"field": "", "problem": "request body must be a JSON object"}]
    unknown = sorted(set(obj) - _REQUEST_FIELDS)
    if unknown:
        problems.append({"field": ", ".join(unknown),
                         "problem": "unknown field(s) — the v1 request is closed"})
    for name in _REQUIRED_REQUEST_FIELDS:
        if name not in obj:
            problems.append({"field": name, "problem": "required field missing"})

    out: dict = {}

    version = obj.get("version")
    if "version" in obj:
        if isinstance(version, bool) or not isinstance(version, int) \
                or version != DECISION_REQUEST_VERSION:
            problems.append({"field": "version",
                             "problem": f"must be the integer "
                                        f"{DECISION_REQUEST_VERSION}"})
        else:
            out["version"] = version

    for name in ("parent_run_id", "step_id", "requester"):
        if name not in obj:
            continue
        value = obj.get(name)
        if not isinstance(value, str) or not _match(_ID_RE, value):
            problems.append({"field": name,
                             "problem": f"must match {_ID_RE} (1-128 chars)"})
        else:
            out[name] = value

    if "generation" in obj:
        gen = obj.get("generation")
        if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
            problems.append({"field": "generation",
                             "problem": "must be an integer >= 0"})
        else:
            out["generation"] = gen

    for name in ("brief_digest", "previous_route_digest"):
        if name not in obj:
            continue
        value = obj.get(name)
        if not isinstance(value, str) or not _match(_DIGEST_RE, value):
            problems.append({"field": name,
                             "problem": "must be a sha256 digest (64 lowercase hex chars)"})
        else:
            out[name] = value

    if "context_refs" in obj:
        refs = obj.get("context_refs")
        if not isinstance(refs, list):
            problems.append({"field": "context_refs", "problem": "must be a list"})
        else:
            norm_refs, seen = [], set()
            for i, ref in enumerate(refs):
                where = f"context_refs[{i}]"
                if not isinstance(ref, dict):
                    problems.append({"field": where, "problem": "must be an object"})
                    continue
                extra = sorted(set(ref) - {"name", "digest"})
                if extra:
                    problems.append({"field": where,
                                     "problem": f"unknown field(s): {', '.join(extra)}"})
                    continue
                name, digest = ref.get("name"), ref.get("digest")
                if not isinstance(name, str) or not _match(_ID_RE, name):
                    problems.append({"field": f"{where}.name",
                                     "problem": f"must match {_ID_RE}"})
                    continue
                if name in seen:
                    problems.append({"field": f"{where}.name",
                                     "problem": f"duplicate context reference {name!r}"})
                    continue
                if not isinstance(digest, str) or not _match(_DIGEST_RE, digest):
                    problems.append({"field": f"{where}.digest",
                                     "problem": "must be a sha256 digest "
                                                "(64 lowercase hex chars)"})
                    continue
                seen.add(name)
                norm_refs.append({"name": name, "digest": digest})
            out["context_refs"] = norm_refs

    if "required_levels" in obj:
        levels = obj.get("required_levels")
        if not isinstance(levels, dict):
            problems.append({"field": "required_levels", "problem": "must be an object"})
        else:
            norm_levels: dict = {}
            for cat, lvl in levels.items():
                where = f"required_levels.{cat}"
                if not isinstance(cat, str) or not cat.strip():
                    problems.append({"field": "required_levels",
                                     "problem": "category names must be non-empty strings"})
                    continue
                if cat == "min_context":
                    problems.append({"field": where,
                                     "problem": "min_context is not accepted here — the "
                                                "context requirement belongs in "
                                                "required_context_limit"})
                    continue
                if isinstance(lvl, bool) or not isinstance(lvl, int):
                    problems.append({"field": where,
                                     "problem": "level must be an integer"})
                    continue
                norm_levels[cat] = lvl
            out["required_levels"] = norm_levels

    if "required_context_limit" in obj:
        limit = obj.get("required_context_limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            problems.append({"field": "required_context_limit",
                             "problem": "must be an integer > 0 (tokens)"})
        else:
            out["required_context_limit"] = limit

    for name, pattern in (("required_data_class", _LABEL_RE),):
        if name not in obj:
            continue
        value = obj.get(name)
        if not isinstance(value, str) or not _match(pattern, value):
            problems.append({"field": name, "problem": f"must match {pattern}"})
        else:
            out[name] = value

    for name in ("policy_labels", "required_connector_capabilities"):
        if name not in obj:
            continue
        values = obj.get(name)
        if not isinstance(values, list):
            problems.append({"field": name, "problem": "must be a list"})
            continue
        norm_values, bad = [], []
        for value in values:
            if not isinstance(value, str) or not _match(_LABEL_RE, value):
                bad.append(repr(value))
                continue
            norm_values.append(value)
        if bad:
            problems.append({"field": name,
                             "problem": "every entry must match "
                                        f"{_LABEL_RE} — bad: {', '.join(bad)}"})
        elif len(set(norm_values)) != len(norm_values):
            problems.append({"field": name, "problem": "duplicate entries"})
        else:
            out[name] = norm_values

    money: dict = {}
    for name in ("budget_ceiling_usd", "budget_remaining_usd"):
        if name not in obj:
            continue
        value = obj.get(name)
        if not _is_number(value) or float(value) < 0.0:
            problems.append({"field": name,
                             "problem": "must be a number >= 0 (USD)"})
        else:
            money[name] = float(value)
    if "budget_ceiling_usd" in money and "budget_remaining_usd" in money:
        if money["budget_remaining_usd"] > money["budget_ceiling_usd"]:
            problems.append({"field": "budget_remaining_usd",
                             "problem": "must be <= budget_ceiling_usd"})
    out.update(money)

    if "attempt_deadline" in obj:
        parsed = _parse_ts(obj.get("attempt_deadline"), "attempt_deadline", problems)
        if parsed is not None:
            out["attempt_deadline"] = _canonical_ts(parsed)

    if "valid_for_s" in obj:
        want = obj.get("valid_for_s")
        if isinstance(want, bool) or not isinstance(want, int) or want <= 0:
            problems.append({"field": "valid_for_s",
                             "problem": "must be an integer > 0 (seconds)"})
        else:
            out["valid_for_s"] = want

    # At least one requirement must exist — an empty bar is not a routing
    # question (and the resolver itself treats an empty requirement list as
    # "no chain", which would be a silent lie here).
    if "required_levels" in out or "required_context_limit" in out:
        if not out.get("required_levels") and "required_context_limit" not in out:
            problems.append({"field": "required_levels",
                             "problem": "at least one requirement is required "
                                        "(levels and/or required_context_limit)"})
    elif not problems:
        problems.append({"field": "required_levels",
                         "problem": "at least one requirement is required "
                                    "(levels and/or required_context_limit)"})

    if problems:
        return None, problems
    return out, []


# ---------------------------------------------------------------------------
# resolver wiring (the SAME resolver router spawn uses)
# ---------------------------------------------------------------------------

def _adhoc_for(request: dict) -> list:
    adhoc = [f"{cat}={lvl}" for cat, lvl in
             sorted((request.get("required_levels") or {}).items())]
    if "required_context_limit" in request:
        adhoc.append(f"min_context={request['required_context_limit']}")
    return adhoc


def _strip_internal(obj: Any) -> Any:
    """Drop '_'-prefixed internal bookkeeping keys (cache/sort stamps) so a
    registry digest covers the DATA, never a parse-time annotation."""
    if isinstance(obj, dict):
        return {k: _strip_internal(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, list):
        return [_strip_internal(v) for v in obj]
    return obj


def _registry_meta() -> dict:
    """{source, version, digest} of the registry tables the resolver used.

    digest = sha256 of the canonical tables JSON (internal '_'-prefixed stamps
    stripped) — identical for the registry.json and the data/tables arms when
    they carry the same rows, so the digest identifies CONTENT. `version` is
    the registry envelope's own version field (registry.json arm only; null
    for the data/tables fallback, which has no envelope)."""
    tables, source, _fb, _warn = _rs._load_registry_with_meta()
    digest = sha256_hex(canonical_json(_strip_internal(tables or {})))
    version = None
    if source == "registry.json":
        try:
            with open(_rs.REGISTRY) as handle:
                envelope = json.load(handle)
            raw = envelope.get("version") if isinstance(envelope, dict) else None
            if isinstance(raw, int) and not isinstance(raw, bool):
                version = raw
        except Exception:  # noqa: BLE001 — provenance is visibility, never fatal
            version = None
    return {"source": source, "version": version, "digest": digest}


def _policy_meta() -> dict:
    """{source, version, digest} of the policy plane: quota-state.json — the
    file the resolver gates providers on (TR-203 "policy gate plane").
    digest is null when the file is absent (unknown policy is not empty
    policy)."""
    path = os.path.join(_rs.MR, "quota-state.json")
    present = os.path.isfile(path)
    version = None
    digest = None
    if present:
        doc = _rs.load_json(path, {})
        if not isinstance(doc, dict):
            doc = {}
        raw = doc.get("version")
        if isinstance(raw, int) and not isinstance(raw, bool):
            version = raw
        digest = sha256_hex(canonical_json(doc))
    return {"source": path, "version": version, "digest": digest}


def _tier_index(tables: dict) -> tuple:
    tiers: dict = {}
    for row in tables.get("model_tier") or []:
        if isinstance(row, dict):
            tiers.setdefault(row.get("model"), {})[row.get("category")] = row.get("tier")
    return tiers, _rs._fold_tier_names(tiers)


def _requirement_evidence(model: str, tiers: dict, folded: dict,
                          request: dict) -> list:
    """Per-category eligibility evidence: required level vs the tier the
    resolver compared against (missing tier = -1, the resolver's own rule)."""
    row = _rs._alias_tiers(tiers, folded, model)
    evidence = []
    for cat, level in sorted((request.get("required_levels") or {}).items()):
        raw = row.get(cat)
        evidence.append({"category": cat, "required_level": level,
                         "tier": raw,
                         "tier_effective": raw if raw is not None else -1})
    return evidence


def _candidate_entry(entry: dict, request: dict, tiers: dict,
                     folded: dict) -> dict:
    """One ordered candidate: identity, price basis, context, data class and
    the eligibility evidence. Null/absent semantics of the resolver's own
    price fields are preserved verbatim (0.0 is 0.0, unknown is null)."""
    context_required = request.get("required_context_limit")
    context_limit = entry.get("context_limit")
    if context_required is None:
        context_rule = "not-required"
    elif context_limit is None:
        context_rule = "min_context-lenient-unknown"
    else:
        context_rule = "min_context"
    data_required = request.get("required_data_class")
    return {
        "hop": entry.get("hop"),
        "provider": entry.get("provider"),
        "model": entry.get("model"),
        "price": {
            "list_usd_1m": entry.get("usd_1m"),
            "in_per_m": entry.get("in_per_m"),
            "out_per_m": entry.get("out_per_m"),
            "cache_read_per_m": entry.get("cache_read_per_m"),
            "cache_write_per_m": entry.get("cache_write_per_m"),
            "normalized": entry.get("price"),
            "effective": entry.get("effective_price"),
            "basis": entry.get("price_basis"),
        },
        "context_limit": context_limit,
        "data_class": entry.get("data_class"),
        "eligibility": {
            "requirements": _requirement_evidence(entry.get("model"), tiers,
                                                  folded, request),
            "context": {"required": context_required,
                        "limit": context_limit,
                        "rule": context_rule,
                        "note": entry.get("context_note")},
            "data_class": {"required": data_required,
                           "lane": entry.get("data_class"),
                           "met": True},
        },
    }


def _exclusion_entry(entry: dict, why: list, codes: list) -> dict:
    return {"hop": entry.get("hop"), "provider": entry.get("provider"),
            "model": entry.get("model"), "why": list(why), "codes": list(codes)}


def _resolve_view(resolved: dict) -> dict:
    """Uniform view over the two shapes resolve() returns.

    (a) the normal document (chain/exclusions/gate_reasons/source/...) and
    (b) the 'no chain — no eligible model and no fallback lane could serve'
    error document (reasons/exclusions truncated + data_home provenance).
    The decision contract must report the same receipt schema for both — the
    refusal is a RESULT, not an error."""
    if resolved.get("error"):
        data_home = resolved.get("data_home") if isinstance(resolved.get("data_home"), dict) else {}
        reasons = [str(r) for r in (resolved.get("reasons") or [])]
        return {
            "chain": [],
            "exclusions": [e for e in (resolved.get("exclusions") or []) if isinstance(e, dict)],
            "gate_reasons": reasons,
            "reasons": reasons,
            "source": data_home.get("source"),
            "fallback_used": bool(data_home.get("fallback")),
            "degraded_fallback": False,
            "warnings": [],
        }
    return {
        "chain": [e for e in (resolved.get("chain") or []) if isinstance(e, dict)],
        "exclusions": [e for e in (resolved.get("exclusions") or []) if isinstance(e, dict)],
        "gate_reasons": [str(r) for r in (resolved.get("gate_reasons") or [])],
        "reasons": [str(r) for r in (resolved.get("gate_reasons") or [])],
        "source": resolved.get("source"),
        "fallback_used": bool(resolved.get("fallback_used")),
        "degraded_fallback": bool(resolved.get("degraded_fallback")),
        "warnings": [str(w) for w in (resolved.get("warnings") or [])],
    }


def _gate_evidence(considered: list) -> dict:
    """The health/quota/circuit state the decision actually consulted,
    restricted to the lanes it considered (candidates + exclusions).

    Rows are copied VERBATIM from the state files — evidence, never a
    paraphrase. Presence is reported LOUDLY (`gates_loaded`, the same
    semantics as the resolver's own block): a missing state file is never
    silently 'open'."""
    quota_path = os.path.join(_rs.MR, "quota-state.json")
    health_path = os.path.join(_rs.MR, "health-state.json")
    circuit_path = os.path.join(_rs.MR, "circuit-state.json")
    qdoc = _rs.load_json(quota_path, {})
    hdoc = _rs.load_json(health_path, {})
    cdoc = _rs.load_json(circuit_path, {})
    if not isinstance(qdoc, dict):
        qdoc = {}
    if not isinstance(hdoc, dict):
        hdoc = {}
    if not isinstance(cdoc, dict):
        cdoc = {}

    def _present(name: str) -> bool:
        try:
            return os.path.isfile(os.path.join(_rs.MR, name))
        except Exception:  # noqa: BLE001 — visibility only, never raise
            return False

    inflight = _rs.ledger_in_flight(_rs.MR)
    gates_loaded = {
        "health": _present("health-state.json"),
        "circuit": _present("circuit-state.json"),
        "quota": _present("quota-state.json"),
        "ledger": _rs.ledger_has_traces(_rs.MR),
        "ledger_rows": len(inflight),
    }
    quota_gates = _rs.quota_gate_summary(_rs.load_quota_gates(qdoc))
    quota_gates["source"] = quota_path
    providers = {prov for prov, _model in considered}
    pairs = {f"{prov}/{model}" for prov, model in considered}

    quota_rows = {}
    qproviders = qdoc.get("providers") if isinstance(qdoc.get("providers"), dict) else {}
    for prov in sorted(providers):
        row = qproviders.get(prov)
        if row is not None:
            quota_rows[prov] = row

    health_providers, health_models = {}, {}
    hproviders = hdoc.get("providers") if isinstance(hdoc.get("providers"), dict) else {}
    for prov in sorted(providers):
        row = hproviders.get(prov)
        if not isinstance(row, dict):
            continue
        health_providers[prov] = {k: v for k, v in row.items() if k != "models"}
        for model in sorted(m for p, m in considered if p == prov):
            mrow = (row.get("models") or {}).get(model) if isinstance(row.get("models"), dict) else None
            if mrow is not None:
                health_models[f"{prov}/{model}"] = mrow

    circuit_pairs = {}
    cpairs = cdoc.get("pairs") if isinstance(cdoc.get("pairs"), dict) else {}
    for pair in sorted(pairs):
        for key in (pair, tuple(pair.split("/", 1))):
            row = cpairs.get(key)
            if row is not None:
                circuit_pairs[pair] = row
                break
    breakers = {}
    v2 = cdoc.get("v2") if isinstance(cdoc.get("v2"), dict) else {}
    pbreakers = v2.get("provider_breakers") if isinstance(v2.get("provider_breakers"), dict) else {}
    for prov in sorted(providers):
        row = pbreakers.get(prov)
        if row is not None:
            breakers[prov] = row

    return {
        "gates_loaded": gates_loaded,
        "quota": {"source": quota_path, "providers": quota_rows,
                  "plan_window_gates": quota_gates},
        "health": {"source": health_path, "providers": health_providers,
                   "models": health_models},
        "circuit": {"source": circuit_path, "pairs": circuit_pairs,
                    "provider_breakers": breakers},
    }


# ---------------------------------------------------------------------------
# receipt authentication: signing key, signature, durable issuance ledger
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    """The router state dir the resolver already reads (MR)."""
    return _rs.MR


def receipt_key_path() -> str:
    return os.path.join(_state_dir(), RECEIPT_KEY_FILENAME)


def issuance_dir() -> str:
    return os.path.join(_state_dir(), ISSUANCE_DIRNAME)


class ReceiptKeyError(ValueError):
    """A named signing-key refusal, never a silent success.

    Carries the machine-readable `code` so a caller can tell "no key
    bootstrapped" from "key too old" from "key file permissions were
    loosened" — each needs a different operator action.
    """

    def __init__(self, code: str, problem: str):
        super().__init__(problem)
        self.code = code
        self.problem = problem


def receipt_key_id(key: bytes) -> str:
    """The public identity of a signing key: sha256(key), first 16 hex bytes.

    Distinct from the key material itself (the digest is not usable as a MAC
    key), so it can ride the receipt: rotation then shows up as a named
    `RECEIPT_KEY_ID_MISMATCH` instead of a generic signature failure.
    """
    return hashlib.sha256(key).hexdigest()[:32]


def _reject_unsafe_key_file(path: str) -> None:
    """Enforce the key-file floor and ceiling (H1-F6).

    mode: no group/other permission bits (the key is 0600 or it is refused);
    age: mtime within RECEIPT_KEY_MAX_AGE_S (rotation is an operator action,
    but a key nobody rotated must not stay authoritative forever).
    """
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ReceiptKeyError(ERR_RECEIPT_KEY_UNAVAILABLE,
                              f"{path} cannot be inspected: {exc}") from exc
    mode = info.st_mode & 0o777
    if mode & 0o077:
        raise ReceiptKeyError(
            ERR_RECEIPT_KEY_INSECURE_MODE,
            f"{path} is mode {oct(mode)} — the receipt signing key must be 0600 "
            f"(repair: chmod 600 {path}); it was readable by group/other")
    age = time.time() - info.st_mtime
    if age > RECEIPT_KEY_MAX_AGE_S:
        raise ReceiptKeyError(
            ERR_RECEIPT_KEY_EXPIRED,
            f"{path} is {int(age)}s old, beyond RECEIPT_KEY_MAX_AGE_S="
            f"{RECEIPT_KEY_MAX_AGE_S}s — rotate it: stop issuing, archive and "
            f"delete the key, then re-run --init-receipt-key")


def load_receipt_key(create: bool = False) -> bytes:
    """Read the 32-byte HMAC key, mode 0600.

    `create=True` is the OPERATOR BOOTSTRAP path (`--init-receipt-key`) and
    the ONLY way a key comes into existence; it uses O_CREAT|O_EXCL so two
    concurrent bootstraps cannot race into two different keys — exactly one
    wins, the other reads the winner. Request paths (issue, verify) call with
    `create=False` (the default), so a decision can never mint signing
    material as a side effect of an HTTP call (H1-F4).
    """
    path = receipt_key_path()
    try:
        with open(path, "rb") as handle:
            key = handle.read()
    except OSError as exc:
        if not create:
            raise ReceiptKeyError(
                ERR_RECEIPT_KEY_UNAVAILABLE,
                f"no signing key at {path} ({type(exc).__name__}) — issuing and "
                f"verifying receipts require the operator bootstrap step: "
                f"router_decision.py --init-receipt-key") from exc
        key = None
    if key:
        if len(key) != 32:
            raise ReceiptKeyError(
                ERR_RECEIPT_KEY_UNAVAILABLE,
                f"{path} is not a 32-byte key")
        _reject_unsafe_key_file(path)
        return key
    if not create:
        raise ReceiptKeyError(
            ERR_RECEIPT_KEY_UNAVAILABLE,
            f"no signing key at {path} — issuing and verifying receipts require "
            f"the operator bootstrap step: router_decision.py --init-receipt-key")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fresh = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        with open(path, "rb") as handle:
            key = handle.read()
        if len(key) != 32:
            raise ReceiptKeyError(ERR_RECEIPT_KEY_UNAVAILABLE,
                                  f"{path} is not a 32-byte key")
        _reject_unsafe_key_file(path)
        return key
    try:
        written = os.write(fd, fresh)
        if written != len(fresh):
            raise OSError(f"short key write ({written}/{len(fresh)} bytes)")
        os.fsync(fd)
    finally:
        os.close(fd)
    return fresh


def bootstrap_receipt_key() -> str:
    """The operator bootstrap step: create the key if, and only if, absent.

    Idempotent and non-destructive — it never overwrites or rotates an
    existing key (rotation is an explicit operator action: stop issuing,
    archive the old key, delete the file, re-run this, and every outstanding
    receipt becomes unverifiable, which is the point of rotating).
    """
    load_receipt_key(create=True)
    return receipt_key_path()


def sign_receipt(receipt: dict, key: bytes | None = None) -> dict:
    """Stamp `key_id`, `signature_alg` + `signature` over the canonical body.

    `key_id` is stamped here too, so the receipt names the key that signed it
    (H1-F6) — rotation is then a named mismatch, not a generic invalid MAC.
    """
    if key is None:
        key = load_receipt_key(create=False)
    receipt["key_id"] = receipt_key_id(key)
    mac = hmac.new(key, signature_message(receipt).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    receipt["signature_alg"] = SIGNATURE_ALG
    receipt["signature"] = mac
    return receipt


def verify_signature(receipt: dict) -> tuple[bool, str]:
    """(ok, problem-code) for the receipt's HMAC."""
    alg = receipt.get("signature_alg")
    sig = receipt.get("signature")
    if sig is None and alg is None:
        return False, ERR_RECEIPT_UNSIGNED
    if alg != SIGNATURE_ALG:
        return False, ERR_RECEIPT_SIGNATURE_INVALID
    if not isinstance(sig, str) or not _match(_DIGEST_RE, sig):
        return False, ERR_RECEIPT_SIGNATURE_INVALID
    try:
        key = load_receipt_key(create=False)
    except ReceiptKeyError as exc:
        return False, exc.code
    except (OSError, ValueError):
        return False, ERR_RECEIPT_KEY_UNAVAILABLE
    named = receipt.get("key_id")
    if named is not None and named != receipt_key_id(key):
        # H1-F6: the receipt names a different key. Rotation is visible as its
        # own refusal instead of a generic "signature does not verify".
        return False, ERR_RECEIPT_KEY_ID_MISMATCH
    expected = hmac.new(key, signature_message(receipt).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, ERR_RECEIPT_SIGNATURE_INVALID
    return True, ""


def issuance_record(receipt: dict) -> dict:
    """The durable record proving THIS server issued THIS receipt."""
    registry = receipt.get("registry") if isinstance(receipt.get("registry"), dict) else {}
    evidence = receipt.get("gate_evidence")
    return {
        "version": RECEIPT_VERSION,
        "receipt_id": receipt.get("receipt_id"),
        "receipt_digest": receipt.get("receipt_digest"),
        "request_digest": receipt.get("request_digest"),
        "parent_run_id": receipt.get("parent_run_id"),
        "step_id": receipt.get("step_id"),
        "generation": receipt.get("generation"),
        "issued_at": receipt.get("resolved_at"),
        "valid_until": receipt.get("valid_until"),
        "eligible": receipt.get("eligible"),
        "head": receipt.get("head"),
        "registry_digest": registry.get("digest"),
        "evidence_digest": sha256_hex(canonical_json(evidence)) if evidence is not None else None,
        "source": receipt.get("source"),
        "signature_alg": receipt.get("signature_alg"),
    }


def _ledger_month(issued_at: Any) -> str | None:
    """`YYYYMM` for a canonical `resolved_at`, or None when it is unusable.

    The month is NOT guessed from the wall clock: the receipt's own (signed,
    digest-covered) `resolved_at` says where its issuance row lives.
    """
    if not isinstance(issued_at, str) or len(issued_at) < 7:
        return None
    head = issued_at[:7]
    if len(head) != 7 or head[4] != "-":
        return None
    year, month = head[:4], head[5:]
    if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
        return None
    return year + month


def ledger_path(month: str) -> str:
    return os.path.join(issuance_dir(), f"{month}.jsonl")


#: Per-month ledger index: {path: {"offset": bytes parsed, "rows": {digest: [row]}}}.
#: The server is a ThreadingHTTPServer, so the index is shared state behind a
#: lock. Verification reads ONE month file, parses only the bytes appended
#: since the last look, and answers from the digest map (H1-F4: a small
#: unauthenticated POST must not cost a full-ledger parse).
_ISSUANCE_INDEX: dict[str, dict] = {}
_ISSUANCE_LOCK = threading.Lock()


def _index_ledger(path: str) -> dict:
    """{receipt_digest: [issuance row]} for one month file, tail-updated.

    The stat comes first so a warm lookup opens nothing at all. A file that
    shrank (rotation, truncation, restore) is re-indexed from byte 0; a
    trailing partial line is left unparsed and re-read on the next call, so a
    writer in flight can never hide a committed row.
    """
    with _ISSUANCE_LOCK:
        entry = _ISSUANCE_INDEX.get(path)
        offset = entry["offset"] if entry else 0
        rows = entry["rows"] if entry else {}
        try:
            size = os.path.getsize(path)
        except OSError:
            _ISSUANCE_INDEX.pop(path, None)
            return {}
        if size < offset:                 # truncated or replaced on disk
            offset, rows = 0, {}
        if size == offset:
            _ISSUANCE_INDEX[path] = {"offset": offset, "rows": rows}
            return rows
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read(size - offset)
        except OSError:
            _ISSUANCE_INDEX.pop(path, None)
            return {}
        *complete, tail = chunk.split(b"\n")
        for raw in complete:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue                       # torn line: never guessed at
            if isinstance(row, dict) and isinstance(row.get("receipt_digest"), str):
                rows.setdefault(row["receipt_digest"], []).append(row)
        offset += len(chunk) - len(tail)
        _ISSUANCE_INDEX[path] = {"offset": offset, "rows": rows}
        return rows


def issuance_rows_for_month(digest: str, month: str) -> list:
    """Every issuance row for `digest` in exactly that month file."""
    rows = _index_ledger(ledger_path(month))
    return list(rows.get(digest) or [])


def issuance_rows_any_month(digest: str) -> list:
    """Every issuance row for `digest`, newest month file first.

    Used only for chain lookups (a previous route may have been issued in an
    earlier month); each month file is still parsed at most once per process.
    """
    directory = issuance_dir()
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        return []
    found: list = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        stem = name[:-6]
        if len(stem) != 6 or not stem.isdigit():
            continue
        found.extend(issuance_rows_for_month(digest, stem))
        if found:
            break
    return found


def _reset_issuance_index() -> None:
    """Drop the in-process index (tests, and after an external repair)."""
    with _ISSUANCE_LOCK:
        _ISSUANCE_INDEX.clear()


class IssuanceError(RuntimeError):
    """A named issuance-ledger failure (H1-F5 idempotency, H1-F7 durability)."""

    def __init__(self, code: str, problem: str):
        super().__init__(problem)
        self.code = code
        self.problem = problem


def append_issuance(receipt: dict) -> None:
    """Append the issuance record. This is the ONLY write a decision makes.

    Idempotent per `receipt_digest` (H1-F5): `receipt_id` is deterministic, so
    re-issuing the SAME request at the SAME clock produces a byte-identical
    receipt — and must not append a second row for it. A digest that is already
    on file with a DIFFERENT record is a ledger inconsistency and refuses
    (`RECEIPT_ISSUANCE_CONFLICT`) rather than silently duplicating or
    overwriting.
    """
    record = issuance_record(receipt)
    directory = issuance_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    month = _ledger_month(record.get("issued_at")) or _canonical_ts(
        datetime.datetime.now(datetime.timezone.utc))[:7].replace("-", "")
    path = ledger_path(month)
    digest = record.get("receipt_digest")
    line = canonical_json(record)
    if isinstance(digest, str):
        existing = issuance_rows_for_month(digest, month)
        if existing:
            if any(canonical_json(row) == line for row in existing):
                return          # re-issue: the durable record already exists
            raise IssuanceError(
                ERR_RECEIPT_ISSUANCE_CONFLICT,
                f"ledger {path} already records digest {digest} with a "
                f"different issuance record — refusing to duplicate it")
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    start = None
    try:
        # H1-F7: a short write must never be mistaken for a durable record. The
        # O_APPEND offset is the file size, so the partial bytes (if any) are
        # truncated away again: a torn row would be skipped by every later
        # parse and the receipt would fail verification as if it were forged.
        start = os.fstat(fd).st_size
        written = os.write(fd, (line + "\n").encode("utf-8"))
        if written != len(line) + 1:
            raise IssuanceError(
                ERR_RECEIPT_ISSUANCE_WRITE_FAILED,
                f"short write to {path}: {written} of {len(line) + 1} bytes — "
                f"the receipt was NOT issued")
        os.fsync(fd)
    except BaseException:
        try:
            if start is not None:
                os.ftruncate(fd, start)
                os.fsync(fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def verify_issuance(receipt: dict) -> tuple[bool, str, dict | None]:
    """(ok, problem-code, record) — was this exact receipt issued here?

    The lookup is scoped to the month file named by the receipt's own
    `resolved_at` and served from the digest index (H1-F4), so verification
    cost does not grow with the ledger.
    """
    digest = receipt.get("receipt_digest")
    if not isinstance(digest, str) or not _match(_DIGEST_RE, digest):
        return False, ERR_RECEIPT_NOT_ISSUED, None
    month = _ledger_month(receipt.get("resolved_at"))
    if month is None:
        return False, ERR_RECEIPT_NOT_ISSUED, None
    rows = issuance_rows_for_month(digest, month)
    if not rows:
        return False, ERR_RECEIPT_NOT_ISSUED, None
    for row in rows:
        if (row.get("receipt_id") == receipt.get("receipt_id")
                and row.get("request_digest") == receipt.get("request_digest")
                and row.get("parent_run_id") == receipt.get("parent_run_id")
                and row.get("step_id") == receipt.get("step_id")
                and row.get("generation") == receipt.get("generation")
                and row.get("valid_until") == receipt.get("valid_until")):
            return True, "", row
    return False, ERR_RECEIPT_ISSUANCE_MISMATCH, None


def _previous_route_problem(normalized: dict) -> dict | None:
    """Validate the declared chain link (H1-F5) — a claim, not a formality.

    `previous_route_digest` names the receipt the previous attempt used, so the
    issuer can check it exists in ITS OWN ledger and belongs to the same
    parent run; otherwise the chain a foreman reconstructs is fiction. Absent
    is absent (a first attempt declares nothing and is not checked).

    Returns an error doc, or None when the link is sound.
    """
    declared = normalized.get("previous_route_digest")
    if declared is None:
        return None
    rows = issuance_rows_any_month(declared)
    if not rows:
        return _error_doc(
            ERR_PREVIOUS_ROUTE_UNKNOWN,
            f"previous_route_digest {declared} has no issuance record on this "
            f"router — the chain link cannot be verified (a first attempt must "
            f"declare no previous route)", False,
            [{"field": "previous_route_digest",
              "problem": "no issuance record for this digest on this router"}])
    parent = normalized.get("parent_run_id")
    if not any(row.get("parent_run_id") == parent for row in rows):
        seen = sorted({str(row.get("parent_run_id")) for row in rows})
        return _error_doc(
            ERR_PREVIOUS_ROUTE_MISMATCH,
            f"previous_route_digest {declared} was issued for parent_run_id "
            f"{seen} but this request is for {parent!r} — the chain link "
            f"crosses runs", False,
            [{"field": "previous_route_digest",
              "problem": f"issued for {seen}, request is for {parent!r}"}])
    return None


def _refusal(code: str, reasons: list) -> dict:
    return {"code": code, "reasons": list(reasons)}


def decide(request: Any, now: Any = None) -> dict:
    """Compute one route receipt (or a machine-readable request refusal).

    Never raises for request problems — the body is untrusted; unexpected
    resolver failures become a RESOLVER_ERROR error doc (fail closed, never a
    fabricated route).

    Writes: exactly ONE durable thing — an issuance record in
    `<state>/decision-receipts/<YYYYMM>.jsonl` — and only for a receipt that
    is actually handed out (H1-F4). It does NOT create signing material: the
    key is an operator bootstrap step (`--init-receipt-key`), so an
    unkeyed install refuses with `RECEIPT_KEY_UNAVAILABLE` and writes
    nothing rather than minting authority as a side effect of a request."""
    try:
        clock = decision_clock(now)
    except ValueError as exc:
        return _error_doc(ERR_INVALID_REQUEST, str(exc), False,
                          [{"field": "now", "problem": str(exc)}])
    normalized, problems = validate_request(request)
    if normalized is None:
        return _error_doc(ERR_INVALID_REQUEST,
                          "invalid execution-decision request", False, problems)

    # H1-F5: a declared chain link is validated against the issuance ledger,
    # never echoed unverified.
    chain_problem = _previous_route_problem(normalized)
    if chain_problem is not None:
        return chain_problem

    resolved_at = _canonical_ts(clock)
    if "attempt_deadline" in normalized:
        deadline = normalized["attempt_deadline"]
        if deadline <= resolved_at:
            return _error_doc(
                ERR_DEADLINE_PASSED,
                f"attempt_deadline {deadline} is not after the decision clock "
                f"{resolved_at} — no decision can be issued inside the window",
                False,
                [{"field": "attempt_deadline",
                  "problem": f"must be after the decision clock ({resolved_at})"}])

    warnings: list = []
    ttl = POLICY_DEFAULT_TTL_S
    if "valid_for_s" in normalized:
        ttl = normalized["valid_for_s"]
        if ttl > POLICY_MAX_TTL_S:
            warnings.append(f"valid_for_s {ttl} clamped to the policy ceiling "
                            f"{POLICY_MAX_TTL_S}s")
            ttl = POLICY_MAX_TTL_S
    valid_until_dt = clock + datetime.timedelta(seconds=ttl)
    if "attempt_deadline" in normalized:
        deadline_dt = datetime.datetime.fromisoformat(
            normalized["attempt_deadline"].replace("Z", "+00:00"))
        if deadline_dt < valid_until_dt:
            valid_until_dt = deadline_dt
            warnings.append("valid_until bounded by the declared attempt_deadline")
    valid_until = _canonical_ts(valid_until_dt)

    adhoc = _adhoc_for(normalized)
    try:
        tables, _src, _fb, _load_warn = _rs._load_registry_with_meta()
    except Exception as exc:  # noqa: BLE001 — fail closed, never a traceback
        return _error_doc(ERR_RESOLVER_ERROR,
                          f"registry load failed: {type(exc).__name__}: {exc}",
                          True, [])
    # Levels are validated against the CANONICAL vocabulary by the resolver's
    # own ad-hoc validator (categories from category_levels.jsonl, range from
    # level_defs.jsonl; syntax-only when the data is missing — the resolver's
    # documented fail-open). A typo'd category is a request problem, never a
    # silent weakening and never a RESOLVER_ERROR.
    _reqs, level_err = _rs._validate_adhoc(adhoc, tables or {})
    if level_err:
        return _error_doc(ERR_INVALID_REQUEST, str(level_err.get("error")),
                          False,
                          [{"field": "required_levels",
                            "problem": str(level_err.get("error"))}])
    try:
        resolved = _rs.resolve(adhoc=adhoc, use_health=True)
    except Exception as exc:  # noqa: BLE001 — fail closed, never a traceback
        return _error_doc(ERR_RESOLVER_ERROR,
                          f"resolve failed: {type(exc).__name__}: {exc}", True, [])
    if not isinstance(resolved, dict):
        return _error_doc(ERR_RESOLVER_ERROR,
                          "resolve returned a non-object payload", True, [])
    if resolved.get("error") and "no chain" not in str(resolved.get("error")):
        return _error_doc(ERR_RESOLVER_ERROR, str(resolved.get("error")), True,
                          [{"field": "", "problem": str(resolved.get("error"))}])

    try:
        tiers, folded = _tier_index(tables or {})
        receipt = _build_receipt(normalized, resolved, tiers, folded,
                                 resolved_at, valid_until, ttl, warnings)
    except ReceiptKeyError as exc:
        # H1-F4: no key bootstrapped, key too old, or key file permissions
        # loosened — a NAMED refusal that writes nothing. Signing material is
        # never created by a request.
        return _error_doc(exc.code,
                          f"receipt cannot be signed: {exc.problem}", False,
                          [{"field": "signing_key", "problem": exc.problem}])
    except Exception as exc:  # noqa: BLE001 — fail closed, never a traceback
        return _error_doc(ERR_RESOLVER_ERROR,
                          f"receipt build failed: {type(exc).__name__}: {exc}",
                          True, [])
    # Durable issuance is part of ISSUING the receipt: a receipt that cannot be
    # recorded cannot be verified later, so it is never handed out.
    try:
        append_issuance(receipt)
    except IssuanceError as exc:
        # H1-F5 (conflicting duplicate) / H1-F7 (short write): named, and the
        # receipt is never handed out. A short write is a storage fault, so it
        # is retryable; a conflicting duplicate is not.
        retryable = exc.code == ERR_RECEIPT_ISSUANCE_WRITE_FAILED
        return _error_doc(exc.code, f"receipt issuance record failed: {exc.problem}",
                          retryable,
                          [{"field": "issuance_ledger", "problem": exc.problem}])
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _error_doc(ERR_RESOLVER_ERROR,
                          f"receipt issuance record failed: "
                          f"{type(exc).__name__}: {exc}", True, [])
    return receipt


def _build_receipt(request: dict, resolved: dict, tiers: dict,
                   folded: dict, resolved_at: str, valid_until: str,
                   ttl: int, warnings: list) -> dict:
    view = _resolve_view(resolved)
    chain_entries = view["chain"]
    resolver_exclusions = view["exclusions"]
    gate_reasons = view["gate_reasons"]
    fallback_used = view["degraded_fallback"]

    data_required = request.get("required_data_class")
    candidates, decision_exclusions = [], []
    for entry in chain_entries:
        lane_class = entry.get("data_class")
        if data_required is not None and lane_class != data_required:
            decision_exclusions.append(_exclusion_entry(
                entry,
                [f"data-class-mismatch: lane data_class "
                 f"{lane_class!r} != required {data_required!r}"],
                [CODE_DATA_CLASS]))
            continue
        candidates.append(_candidate_entry(entry, request, tiers, folded))

    if fallback_used:
        # The requirements-unmet always-run path is NEVER an execution route:
        # keep the lanes visible as exact exclusions and refuse.
        for entry in chain_entries:
            unmet = entry.get("requirements_unmet")
            detail = f" (requirements_unmet: {unmet})" if unmet else ""
            decision_exclusions.append(_exclusion_entry(
                entry,
                [f"fallback-refused: requirements-unmet always-run lane "
                 f"refused for execution decisions{detail}"],
                [CODE_FALLBACK_REFUSED]))
        candidates = []

    eligible = bool(candidates) and not fallback_used
    if eligible:
        head_entry = candidates[0]
        head = {"hop": head_entry["hop"], "provider": head_entry["provider"],
                "model": head_entry["model"]}
        refusal = None
    else:
        head = None
        reasons = list(view["reasons"])
        if fallback_used:
            code = REFUSAL_FALLBACK
            reasons = ([f"resolver offered the requirements-unmet always-run "
                        f"fallback route — refused for execution decisions "
                        f"(degraded_fallback=true)"] + reasons)
        else:
            code = REFUSAL_NO_ELIGIBLE
            if not reasons:
                reasons = ["no candidate satisfies the declared requirements"]
        refusal = _refusal(code, reasons[:12])

    exclusions = [_exclusion_entry(e, e.get("why") or [], e.get("codes") or [])
                  for e in resolver_exclusions] + decision_exclusions
    considered = [(e.get("provider"), e.get("model")) for e in
                  candidates] + [(e.get("provider"), e.get("model"))
                                 for e in exclusions]

    req = dict(request)
    levels = dict(req.get("required_levels") or {})
    context_refs = req.get("context_refs")
    constraints = {
        "required_connector_capabilities":
            list(req.get("required_connector_capabilities") or []),
        # The hard boundary, explicit on the receipt: nothing below is ever
        # chosen here. The foreman picks the tool/connector AFTER this
        # decision; a non-null value would be a forged selection.
        "selected_connector": None,
        "selected_harness": None,
        "selected_tool": None,
        "selected_workspace": None,
    }
    receipt: dict = {
        "version": RECEIPT_VERSION,
        "receipt_id": "",  # stamped below — deterministic (see module docstring)
        "receipt_digest": "",
        "request_digest": request_digest(req),
        "brief_digest": req.get("brief_digest"),
        "parent_run_id": req.get("parent_run_id"),
        "step_id": req.get("step_id"),
        "generation": req.get("generation"),
        "previous_route_digest": req.get("previous_route_digest"),
        "context_refs": list(context_refs) if context_refs is not None else None,
        "resolved_at": resolved_at,
        "valid_until": valid_until,
        "valid_for_s": ttl,
        "eligible": eligible,
        "refusal": refusal,
        "head": head,
        "candidates": candidates,
        "exclusions": exclusions,
        "gate_reasons": gate_reasons,
        "requirements": {
            "levels": levels,
            "min_context": req.get("required_context_limit"),
            "data_class": req.get("required_data_class"),
            "policy_labels": list(req.get("policy_labels") or []),
        },
        "constraints": constraints,
        "source": view["source"],
        "registry": _registry_meta(),
        "policy": _policy_meta(),
        "fallback_used": view["fallback_used"],
        "degraded_fallback": fallback_used,
        "warnings": list(view["warnings"]) + warnings,
        "gate_evidence": _gate_evidence(considered),
    }
    receipt["receipt_id"] = sha256_hex(canonical_json({
        "request_digest": receipt["request_digest"],
        "resolved_at": resolved_at,
        "valid_until": valid_until,
    }))
    # H1-F6: the key identity is digest-covered, so it is stamped BEFORE the
    # digest and the MAC. Loading it here (create=False) is also what makes an
    # unbootstrapped/stale/insecure key a refusal instead of a receipt.
    key = load_receipt_key(create=False)
    receipt["key_id"] = receipt_key_id(key)
    receipt["receipt_digest"] = receipt_digest(receipt)
    return sign_receipt(receipt, key=key)


def _error_doc(code: str, error: str, retryable: bool, problems: list,
               warnings: list | None = None) -> dict:
    doc = {"version": RECEIPT_VERSION, "error": error, "code": code,
           "retryable": bool(retryable), "problems": problems}
    if warnings:
        doc["warnings"] = list(warnings)
    return doc


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def _expectation_problems(receipt: dict, expect: Any) -> list:
    """H1-F1 — bind the receipt to the step the caller is about to execute.

    A receipt carries `parent_run_id` / `step_id` / `generation` / `head`, but
    every check that read them compared them against the receipt itself (or
    against the ledger row for the same digest), so a genuine step-7 receipt
    verified happily when presented as step-8's route. Nothing compares them
    to the step in HAND — that is this function. No expectation is a refusal,
    never a silent ok (`RECEIPT_UNBOUND_EXPECTATION`)."""
    problems: list = []
    if not isinstance(expect, dict) or not expect:
        problems.append({
            "code": ERR_RECEIPT_UNBOUND_EXPECTATION,
            "problem": "verification requires the expected identity "
                       "(expect={'parent_run_id': ..., 'step_id': ...}): a "
                       "receipt proves provenance, never that it is the "
                       "receipt FOR the step being executed"})
        return problems
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        problems.append({
            "code": ERR_RECEIPT_UNBOUND_EXPECTATION,
            "problem": f"unknown expectation keys {sorted(unknown)} — allowed: "
                       f"{sorted(_EXPECT_KEYS)}"})
    missing = [k for k in _REQUIRED_EXPECT_KEYS if k not in expect]
    if missing:
        problems.append({
            "code": ERR_RECEIPT_UNBOUND_EXPECTATION,
            "problem": f"expectation must name {missing} — a partial "
                       f"expectation is not a binding"})
    for field in ("parent_run_id", "step_id", "generation"):
        if field not in expect:
            continue
        wanted = expect[field]
        seen = receipt.get(field)
        if wanted != seen:
            problems.append({
                "code": ERR_RECEIPT_IDENTITY_MISMATCH,
                "problem": f"receipt {field} is {seen!r} but the caller expects "
                           f"{wanted!r} — this receipt is not for that step"})
    if "head" in expect:
        wanted_head = expect["head"]
        seen_head = receipt.get("head")
        if wanted_head is None:
            if seen_head is not None:
                problems.append({
                    "code": ERR_RECEIPT_HEAD_MISMATCH,
                    "problem": "the caller expects no head (a refusal) but the "
                               "receipt carries one"})
        elif not isinstance(wanted_head, dict):
            problems.append({
                "code": ERR_RECEIPT_UNBOUND_EXPECTATION,
                "problem": "expected head must be a candidate identity object "
                           "({'provider': ..., 'model': ...}) or None"})
        else:
            if not isinstance(seen_head, dict):
                problems.append({
                    "code": ERR_RECEIPT_HEAD_MISMATCH,
                    "problem": f"expected head {wanted_head!r} but the receipt "
                               f"head is {seen_head!r}"})
            else:
                for field in ("provider", "model", "hop"):
                    if field in wanted_head and wanted_head[field] != seen_head.get(field):
                        problems.append({
                            "code": ERR_RECEIPT_HEAD_MISMATCH,
                            "problem": f"expected head {field} is "
                                       f"{wanted_head[field]!r} but the receipt "
                                       f"carries {seen_head.get(field)!r}"})
    return problems


def _signature_problem_text(code: str) -> str:
    """Human text for a signature-layer refusal (the code carries the meaning)."""
    return {
        ERR_RECEIPT_UNSIGNED:
            "receipt carries no signature",
        ERR_RECEIPT_SIGNATURE_INVALID:
            "receipt signature is malformed or does not verify against this "
            "server's key",
        ERR_RECEIPT_KEY_UNAVAILABLE:
            "this router has no usable signing key (not bootstrapped, wrong "
            "length, or unreadable) — receipts cannot be authenticated here",
        ERR_RECEIPT_KEY_EXPIRED:
            "this router's signing key is older than RECEIPT_KEY_MAX_AGE_S and "
            "must be rotated before it can authenticate anything",
        ERR_RECEIPT_KEY_INSECURE_MODE:
            "this router's signing key file is readable by group/other; it must "
            "be 0600 before it is used",
        ERR_RECEIPT_KEY_ID_MISMATCH:
            "receipt names a different signing key than the one this router "
            "holds — the key was rotated (or the receipt is from another "
            "install); the receipt is not authority here",
    }.get(code, "receipt signature does not verify")


def verify_receipt(receipt: Any, now: Any = None, expect: Any = None) -> dict:
    """Verify a route receipt: integrity, provenance, BINDING, and expiry.

    Three independent things must all hold for `ok=true`:

    * INTEGRITY — the canonical digest covers every authority-bearing field
      (candidate order, exclusions, source, requirements, chosen head,
      expiry), so any mutation is `DIGEST_MISMATCH`;
    * AUTHENTICATION — the server signature (`RECEIPT_*_SIGNATURE_*`,
      `RECEIPT_UNSIGNED`) plus a matching durable issuance record
      (`RECEIPT_NOT_ISSUED`, `RECEIPT_ISSUANCE_MISMATCH`);
    * BINDING (`expect=`, H1-F1) — the receipt must be FOR the step in hand.
      `expect` is `{"parent_run_id": ..., "step_id": ...}` (mandatory) plus
      optional `"generation"` and `"head"` (`{"provider", "model"[, "hop"]}`,
      or `None` for a refusal). A mismatch is `RECEIPT_IDENTITY_MISMATCH` /
      `RECEIPT_HEAD_MISMATCH`. **Calling without an expectation is refused**
      with `RECEIPT_UNBOUND_EXPECTATION` rather than returning a silent ok:
      a receipt that is not bound to a step authenticates the issuer, not the
      route.

    CLOCK (H1-F2). Verification ignores the ambient `ROUTER_DECISION_NOW`
    replay knob entirely (`decision_clock(..., allow_env=False)`) and is
    evaluated against BOTH clocks: the decision clock (an explicit `now=` is
    honoured — programmatic input) and the host WALL clock, which no caller
    can supply. The receipt is expired if EITHER clock has reached
    `valid_until`, so an injected clock can only make a receipt MORE expired,
    never less. The single exception is the explicit test-only
    `ROUTER_DECISION_TEST_CLOCK=1`, which collapses the check to the injected
    clock so the hermetic suite can pin a 2026 clock; it is never honoured
    implicitly and is not documented as a replay knob.
    """
    problems: list = []
    try:
        clock = decision_clock(now, allow_env=False)
    except ValueError as exc:
        return {"version": RECEIPT_VERSION, "ok": False, "checked_at": None,
                "wall_checked_at": None,
                "receipt_digest": None, "receipt_id": None, "valid_until": None,
                "problems": [{"code": ERR_INVALID_REQUEST, "problem": str(exc)}]}
    checked_at = _canonical_ts(clock)
    # An injected clock is a caller-supplied opinion; the wall clock is the
    # trusted one. Only the hermetic suite may collapse the two.
    wall = None
    if now is not None and not test_clock_armed():
        wall = wall_clock()
    if not isinstance(receipt, dict):
        return {"version": RECEIPT_VERSION, "ok": False, "checked_at": checked_at,
                "wall_checked_at": _canonical_ts(wall) if wall else None,
                "receipt_digest": None, "receipt_id": None, "valid_until": None,
                "problems": [{"code": "MALFORMED_RECEIPT",
                              "problem": "receipt must be a JSON object"}]}
    digest_value = receipt.get("receipt_digest")
    out = {"version": RECEIPT_VERSION, "ok": False, "checked_at": checked_at,
           "wall_checked_at": _canonical_ts(wall) if wall else None,
           "receipt_digest": digest_value if isinstance(digest_value, str) else None,
           "receipt_id": receipt.get("receipt_id"),
           "valid_until": receipt.get("valid_until"),
           "problems": problems}
    if receipt.get("version") != RECEIPT_VERSION:
        problems.append({"code": "VERSION_UNSUPPORTED",
                         "problem": f"receipt version {receipt.get('version')!r} "
                                    f"is not {RECEIPT_VERSION}"})
    if not isinstance(digest_value, str) or not _match(_DIGEST_RE, digest_value):
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": "receipt_digest must be 64 lowercase hex chars"})
    else:
        recomputed = receipt_digest(receipt)
        if recomputed != digest_value:
            problems.append({"code": "DIGEST_MISMATCH",
                             "problem": "receipt_digest does not match the "
                                        "canonical digest of the receipt — an "
                                        "authority-bearing field changed "
                                        "(candidate order, exclusions, source, "
                                        "requirements, head, expiry, ...)"})
    valid_until_raw = receipt.get("valid_until")
    parsed_until = None
    if isinstance(valid_until_raw, str):
        try:
            parsed_until = datetime.datetime.fromisoformat(
                valid_until_raw.replace("Z", "+00:00"))
        except ValueError:
            parsed_until = None
    if parsed_until is None:
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": f"valid_until {valid_until_raw!r} is not an "
                                    "RFC3339 timestamp"})
    else:
        until_utc = parsed_until.astimezone(datetime.timezone.utc)
        expired_by = None
        if clock >= until_utc:
            expired_by = f"decision clock {checked_at}"
        elif wall is not None and wall >= until_utc:
            expired_by = (f"wall clock {_canonical_ts(wall)} — the supplied "
                          f"decision clock {checked_at} is not an authority on "
                          f"whether this receipt is still live")
        if expired_by:
            problems.append({"code": ERR_RECEIPT_EXPIRED,
                             "problem": f"receipt expired at {valid_until_raw} "
                                        f"({expired_by}) — re-resolve"})

    # --- binding: is this receipt FOR the step in hand? (H1-F1) ------------
    problems.extend(_expectation_problems(receipt, expect))


    # --- authentication: signature + durable issuance -----------------------
    # The canonical digest is unkeyed, so it can be recomputed by anyone over a
    # receipt they invented. `ok` therefore ALSO requires the server signature
    # and a matching issuance record for this exact receipt.
    sig_ok, sig_problem = verify_signature(receipt)
    out["signature_ok"] = sig_ok
    if not sig_ok:
        problems.append({"code": sig_problem,
                         "problem": _signature_problem_text(sig_problem)})
    issued_ok, issued_problem, _issued_row = verify_issuance(receipt)
    out["issued"] = issued_ok
    if not issued_ok:
        problems.append({"code": issued_problem,
                         "problem": "no matching issuance record: this server "
                                    "did not issue this receipt (forged, or "
                                    "replayed under a different identity)"})

    # --- completeness / consistency ----------------------------------------
    unknown = set(receipt) - set(RECEIPT_KEYS)
    if unknown:
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": f"unknown receipt keys: {sorted(unknown)}"})
    missing = set(RECEIPT_KEYS) - set(receipt)
    if missing:
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": f"missing receipt keys: {sorted(missing)}"})
    resolved_raw = receipt.get("resolved_at")
    if isinstance(resolved_raw, str) and parsed_until is not None:
        try:
            parsed_from = datetime.datetime.fromisoformat(
                resolved_raw.replace("Z", "+00:00"))
        except ValueError:
            parsed_from = None
        if parsed_from is not None:
            from_utc = parsed_from.astimezone(datetime.timezone.utc)
            if parsed_until.astimezone(datetime.timezone.utc) <= from_utc:
                problems.append({"code": "MALFORMED_RECEIPT",
                                 "problem": "valid_until is not after resolved_at"})
            elif (parsed_until.astimezone(datetime.timezone.utc) - from_utc
                  > datetime.timedelta(seconds=POLICY_MAX_TTL_S)):
                problems.append({"code": "MALFORMED_RECEIPT",
                                 "problem": f"valid_until exceeds the policy "
                                            f"ceiling ({POLICY_MAX_TTL_S}s)"})

    eligible = receipt.get("eligible")
    head = receipt.get("head")
    candidates = receipt.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": "candidates must be a list"})
    if eligible is True:
        if not candidates:
            problems.append({"code": "ELIGIBILITY_MISMATCH",
                             "problem": "eligible=true but candidates is empty"})
        if not isinstance(head, dict):
            problems.append({"code": "HEAD_MISMATCH",
                             "problem": "eligible=true but head is not a candidate identity"})
        elif candidates and isinstance(candidates[0], dict) and (
                head.get("provider"), head.get("model")) != (
                candidates[0].get("provider"), candidates[0].get("model")):
            problems.append({"code": "HEAD_MISMATCH",
                             "problem": "head is not candidates[0] — the chosen "
                                        "head changed"})
    elif eligible is False:
        if head is not None:
            problems.append({"code": "HEAD_MISMATCH",
                             "problem": "eligible=false but head is set"})
        if not isinstance(receipt.get("refusal"), dict):
            problems.append({"code": "ELIGIBILITY_MISMATCH",
                             "problem": "eligible=false but refusal is missing"})
    else:
        problems.append({"code": "MALFORMED_RECEIPT",
                         "problem": "eligible must be a boolean"})

    order = [c.get("hop") for c in candidates if isinstance(c, dict)]
    if any(isinstance(h, int) for h in order):
        ints = [h for h in order if isinstance(h, int)]
        if ints != sorted(ints):
            problems.append({"code": "HEAD_MISMATCH",
                             "problem": "candidate order is not the resolver "
                                        "order (hop numbers out of sequence)"})

    out["ok"] = not problems
    return out


# ---------------------------------------------------------------------------
# HTTP status mapping (shared by the server and the tests)
# ---------------------------------------------------------------------------

def http_status_for(doc: dict) -> int:
    """Map a decision / verification document to an HTTP status (H1-F3).

    A DECISION is a 200 even when it refuses the route (`eligible=false`): the
    decision was computed, and the caller asked a question that has an answer.

    A VERIFICATION is not a question, it is an authentication check, so
    `ok=false` is a failed precondition, not a computed answer — it maps to
    **409 Conflict** (RFC 9110 §15.5.10: the request conflicts with the
    current state of the target resource: expired, not issued here, bound to a
    different step). 200 is reserved for `ok=true`. 409 rather than 403
    (which would claim a *policy* denial by the authenticated caller) or 422
    (which would claim the receipt itself is semantically unprocessable — it
    is well-formed; it is simply not valid for this request). Only a refused
    REQUEST is a 4xx request error.
    """
    if not isinstance(doc, dict):
        return 500
    code = doc.get("code")
    if code in (ERR_INVALID_REQUEST, ERR_DEADLINE_PASSED):
        return 400
    if code in (ERR_RECEIPT_KEY_UNAVAILABLE, ERR_RECEIPT_KEY_EXPIRED,
                ERR_RECEIPT_KEY_INSECURE_MODE):
        # H1-F4/F6: the install cannot issue (no operator-bootstrapped key, an
        # over-age key, or a key file whose permissions were loosened). That is
        # a server-side precondition, not a client error and not a computed
        # answer — 503, never a 200 and never a silent key mint.
        return 503
    if code in (ERR_PREVIOUS_ROUTE_UNKNOWN, ERR_PREVIOUS_ROUTE_MISMATCH,
                ERR_RECEIPT_ISSUANCE_CONFLICT):
        # H1-F5: the request conflicts with what this router actually issued.
        return 409
    if isinstance(doc.get("ok"), bool):
        return 200 if doc["ok"] else 409
    if doc.get("error") is None and "receipt_digest" in doc:
        return 200
    return 500


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_json(path: str, label: str) -> tuple:
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path) as handle:
                text = handle.read()
    except OSError as exc:
        return None, f"{label} unreadable: {exc}"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"{label} is not valid JSON: {exc}"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="router_decision.py",
        description="just-in-time execution decision: route receipt from a "
                    "closed v1 request (read-only; never routes a "
                    "requirements-unmet fallback)")
    ap.add_argument("--request", metavar="PATH",
                    help="decision request JSON ('-' = stdin)")
    ap.add_argument("--verify", metavar="PATH",
                    help="verify a route receipt JSON ('-' = stdin)")
    ap.add_argument("--now", metavar="RFC3339",
                    help="inject the decision clock (deterministic replay and "
                         "tests; default: ROUTER_DECISION_NOW, else the system "
                         "clock). Not accepted from an HTTP request body. On "
                         "--verify it can only make a receipt MORE expired: the "
                         "host wall clock is always checked too.")
    ap.add_argument("--expect-parent-run", metavar="RUN_ID",
                    help="--verify: the parent_run_id the receipt must be for "
                         "(required: an unbound verification is refused)")
    ap.add_argument("--expect-step", metavar="STEP_ID",
                    help="--verify: the step_id the receipt must be for "
                         "(required: an unbound verification is refused)")
    ap.add_argument("--expect-generation", metavar="N", type=int,
                    help="--verify: the generation the receipt must carry")
    ap.add_argument("--expect-head", metavar="PROVIDER/MODEL",
                    help="--verify: the head the caller already knows it will "
                         "execute; a different head is refused")
    ap.add_argument("--init-receipt-key", action="store_true",
                    help="operator bootstrap: create the receipt signing key "
                         "(32 bytes, mode 0600) if it does not exist. Idempotent "
                         "and non-destructive; a decision NEVER creates it.")
    ap.add_argument("--format", choices=["json"], default="json")
    args = ap.parse_args(argv)

    if args.init_receipt_key:
        if args.request or args.verify:
            ap.error("--init-receipt-key cannot be combined with --request/--verify")
        try:
            path = bootstrap_receipt_key()
        except (OSError, ValueError) as exc:
            print(json.dumps(_error_doc(ERR_RECEIPT_KEY_UNAVAILABLE, str(exc),
                                        False, [{"field": "signing_key",
                                                 "problem": str(exc)}]), indent=1))
            return 1
        print(json.dumps({"version": RECEIPT_VERSION, "ok": True,
                          "key_path": path,
                          "key_mode": "0600",
                          "max_age_s": RECEIPT_KEY_MAX_AGE_S}, indent=1))
        return 0

    if bool(args.request) == bool(args.verify):
        ap.error("exactly one of --request / --verify is required")

    if args.verify:
        receipt, err = _read_json(args.verify, "--verify")
        if err:
            print(json.dumps(_error_doc(ERR_INVALID_REQUEST, err, False,
                                        [{"field": "--verify", "problem": err}]),
                             indent=1))
            return 2
        expect: dict = {}
        if args.expect_parent_run:
            expect["parent_run_id"] = args.expect_parent_run
        if args.expect_step:
            expect["step_id"] = args.expect_step
        if args.expect_generation is not None:
            expect["generation"] = args.expect_generation
        if args.expect_head:
            text = args.expect_head
            if "/" not in text:
                ap.error("--expect-head must be PROVIDER/MODEL")
            provider, _, model = text.partition("/")
            expect["head"] = {"provider": provider, "model": model}
        result = verify_receipt(receipt, now=args.now,
                               expect=expect or None)
        print(json.dumps(result, indent=1))
        return 0 if result.get("ok") else 1

    request, err = _read_json(args.request, "--request")
    if err:
        print(json.dumps(_error_doc(ERR_INVALID_REQUEST, err, False,
                                    [{"field": "--request", "problem": err}]),
                         indent=1))
        return 2
    doc = decide(request, now=args.now)
    print(json.dumps(doc, indent=1))
    code = doc.get("code")
    if "receipt_digest" in doc:
        return 0
    if code in (ERR_INVALID_REQUEST, ERR_DEADLINE_PASSED):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

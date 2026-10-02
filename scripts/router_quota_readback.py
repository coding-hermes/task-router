#!/usr/bin/env python3
"""router_quota_readback.py — quota L1 rung-1 header readback adapters (TR-207).

WHY: the quota-layers spec (docs/quota-layers-spec.md §1, §3) puts response
headers on calls we already make at rung 1 of the readback ladder — free,
exact, per-request. This module turns a recorded response-header mapping into
spec-shaped observations WITHOUT any network access: callers hand us headers
they already have; we extract, we never probe. One adapter per header
MECHANISM, never per provider (spec §1 L1 row: "adapters, no store"), and the
recognized header-name set is loaded from the L0 declaration table
(data/tables/provider_quota.jsonl, readback_kind == "header" rows) — no
provider lists are hard-coded here. If a name is not in the table (or in the
spec-named mechanism vocabularies below) it is IGNORED, never guessed into an
observation; a response with no recognized quota header produces an explicit
fall-through result naming the next rungs (rung 2 endpoint, rung 4 derivation
from the L2 ledger, basis "derived-from-ledger") — never a silent skip.

Honesty contract (spec §6) as applied here:
  - remaining/limit are numbers or None; unknown is NEVER 0 and never
    fabricated. Utilization-style headers carry a fraction of the window, not
    a remaining count, so those observations keep remaining/limit NULL and
    preserve the raw utilization in source_detail.
  - confidence travels with the number: "verified" only when the headers give
    an absolute remaining AND limit pair; "partial" for anything less
    (utilization-only, remaining-only, limit-only, retry-after-only).
  - the rung is stamped on every observation (source_rung == "header") and the
    exact header names SEEN are recorded in source_detail["headers"].

An adapter returns one observation PER WINDOW the headers describe — a single
response legitimately carries several (requests window + tokens window, 5h +
7d, primary + weekly) and one observation cannot honestly hold two windows.
read_observation() returns the flat list from every matching mechanism, or the
explicit fall-through result when nothing matched.

Usage (manual verification CLI; the module itself is a library):
  router_quota_readback.py [--provider ID] [--account ACC]
                           [--quota-table PATH] [--file HEADERS.json]
  Headers are read as a JSON object {name: value} from --file or stdin.
  Prints one JSON object: {"observations": [...], "fall_through": {...}}.
  Exit 0 = parsed (observations or explicit fall-through are both fine);
  exit 2 = bad input (unparseable JSON or not a JSON object).

No network calls anywhere: stdlib only (argparse/datetime/json/os/re/sys).
"""

import argparse
import datetime
import json
import os
import re
import sys

DEFAULT_QUOTA_TABLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tables",
    "provider_quota.jsonl",
)

SPEC_OBSERVATION_KEYS = (
    "provider_id",
    "account",
    "window_kind",
    "remaining",
    "limit",
    "unit",
    "observed_at",
    "source_rung",
    "source_detail",
    "confidence",
)

SOURCE_RUNG = "header"

# Header-name vocabularies, one per MECHANISM. Every name is quoted verbatim
# from the design authority (docs/quota-layers-spec.md §3 rung 1) or from the
# readback_exact fields of data/tables/provider_quota.jsonl
# (readback_kind == "header" rows) — none is invented here.
ANTHROPIC_UNIFIED_HEADERS = frozenset(
    {
        # spec §3: anthropic-ratelimit-unified-5h-utilization, -7d-utilization,
        # -status, -reset
        "anthropic-ratelimit-unified-5h-utilization",
        "anthropic-ratelimit-unified-7d-utilization",
        "anthropic-ratelimit-unified-status",
        "anthropic-ratelimit-unified-reset",
    }
)

XRATELIMIT_HEADERS = frozenset(
    {
        # per_minute requests / tokens pair (spec §3; groq + meta-model rows)
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
        # day-window variant (sambanova row: -requests-day trio)
        "x-ratelimit-limit-requests-day",
        "x-ratelimit-remaining-requests-day",
        "x-ratelimit-reset-requests-day",
        # limit-only variants (fireworks row; no remaining/reset documented)
        "x-ratelimit-limit-tokens-prompt",
        "x-ratelimit-limit-tokens-cache-adjusted-prompt",
        "x-ratelimit-limit-tokens-generated",
    }
)

X_CODEX_HEADERS = frozenset(
    {
        # openai-codex rows: percent-style windows + credits + context headers
        "x-codex-primary-used-percent",
        "x-codex-primary-window-minutes",
        "x-codex-primary-reset-at",
        "x-codex-secondary-used-percent",
        "x-codex-secondary-window-minutes",
        "x-codex-secondary-reset-at",
        "x-codex-credits-has-credits",
        "x-codex-credits-unlimited",
        "x-codex-credits-balance",
        "x-codex-rate-limit-reached-type",
        "x-codex-promo-message",
    }
)

RETRY_AFTER_HEADERS = frozenset({"retry-after"})

MECHANISMS = (
    ("anthropic-ratelimit-unified", ANTHROPIC_UNIFIED_HEADERS),
    ("x-ratelimit", XRATELIMIT_HEADERS),
    ("x-codex-percent", X_CODEX_HEADERS),
    ("retry-after", RETRY_AFTER_HEADERS),
)

# primary/secondary positional windows for the x-codex family, as documented by
# the openai-codex L0 rows (primary = 5h rolling, secondary = 7d/weekly).
X_CODEX_POSITIONAL_WINDOWS = (
    ("primary", "rolling_5h"),
    ("secondary", "weekly"),
)

# Explicit fall-through (spec §3 rungs 2/4; §7: "the ladder must not skip a
# rung silently"). Copied per call; headers_present_unrecognized is filled in.
HEADER_FALL_THROUGH = {
    "fall_through": True,
    "reason": "no-recognized-quota-headers",
    "next_rungs": [
        "rung-2: usage/balance endpoint (spec §3.2)",
        "rung-4: derivation from the L2 ledger",
    ],
    "basis": "derived-from-ledger",
}

_PROSE_HEADER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+")


def _ci_headers(headers):
    """Case-insensitive view: {lower_name: (original_name, stripped_value)}."""
    view = {}
    for name, value in (headers or {}).items():
        key = str(name).strip().lower()
        if key not in view:
            view[key] = (str(name), "" if value is None else str(value).strip())
    return view


def _num(raw):
    """Parse a header value as int-or-float; None when absent/unparseable."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _flag(raw):
    """Parse a true/false header value; None otherwise."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _observation(
    provider_id,
    account,
    window_kind,
    remaining,
    limit,
    unit,
    confidence,
    mechanism,
    seen_headers,
    detail,
    observed_at,
):
    """Build one spec §3 observation. Keys are exactly SPEC_OBSERVATION_KEYS."""
    source_detail = {"mechanism": mechanism, "headers": list(seen_headers)}
    source_detail.update(detail)
    return {
        "provider_id": provider_id,
        "account": account,
        "window_kind": window_kind,
        "remaining": remaining,
        "limit": limit,
        "unit": unit,
        "observed_at": observed_at,
        "source_rung": SOURCE_RUNG,
        "source_detail": source_detail,
        "confidence": confidence,
    }


def read_anthropic_unified(headers, provider_id="unknown", account="default", observed_at=None):
    """anthropic-ratelimit-unified-* adapter (spec §3 rung 1).

    The -utilization headers carry a fraction of the window USED, not a
    remaining count, and no absolute limit is published on the wire — so
    remaining/limit stay None (never fabricated) and the raw utilization,
    status and reset values are preserved verbatim in source_detail. One
    observation per utilization window present (5h, 7d). The utilization
    scale is not documented on the header itself, so unit is "unknown" rather
    than a guessed "percent".
    """
    if observed_at is None:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ci = _ci_headers(headers)
    observations = []
    for window_key, window_kind in (
        ("anthropic-ratelimit-unified-5h-utilization", "rolling_5h"),
        ("anthropic-ratelimit-unified-7d-utilization", "rolling_daily"),
    ):
        if window_key not in ci:
            continue
        orig, raw = ci[window_key]
        seen = [orig]
        detail = {"utilization": {window_key: raw}}
        for extra, field in (
            ("anthropic-ratelimit-unified-status", "status"),
            ("anthropic-ratelimit-unified-reset", "reset"),
        ):
            if extra in ci:
                seen.append(ci[extra][0])
                detail[field] = ci[extra][1]
        observations.append(
            _observation(
                provider_id,
                account,
                window_kind,
                None,  # utilization is a fraction used — no remaining count exists here
                None,  # no absolute limit is published on these headers
                "unknown",
                "partial",
                "anthropic-ratelimit-unified",
                seen,
                detail,
                observed_at,
            )
        )
    return observations


def read_x_ratelimit(headers, provider_id="unknown", account="default", observed_at=None):
    """x-ratelimit-* family adapter (OpenAI/Groq/Fireworks-style).

    Emits one observation per window the present headers describe:
      - per_minute requests (remaining/limit/reset-requests),
      - per_minute tokens (remaining/limit/reset-tokens, plus the fireworks
        limit-only variants, which carry an effective-limit figure and no
        remaining header — remaining stays None),
      - rolling_daily requests (-requests-day trio).
    confidence is "verified" only when an absolute remaining AND limit are
    both present; reset headers are opaque duration strings preserved raw.
    """
    if observed_at is None:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ci = _ci_headers(headers)
    observations = []

    def window(specs, window_kind, unit, extra_detail=None):
        """One observation from the present subset of a named header set.

        specs: (header_name, detail_label, kind) with kind "num" (parsed
        int/float; unparseable values land as <label>_unparsed) or "raw"
        (opaque string preserved verbatim, e.g. reset durations).
        """
        seen, values = [], {}
        for key, label, kind in specs:
            if key not in ci:
                continue
            seen.append(ci[key][0])
            if kind == "num":
                parsed = _num(ci[key][1])
                if parsed is None:
                    values[label + "_unparsed"] = ci[key][1]
                else:
                    values[label] = parsed
            else:
                values[label] = ci[key][1]
        if not seen:
            return None
        remaining = values.get("remaining")
        limit = values.get("limit")
        confidence = "verified" if remaining is not None and limit is not None else "partial"
        detail = dict(values)
        if extra_detail:
            detail.update(extra_detail)
        return _observation(
            provider_id,
            account,
            window_kind,
            remaining,
            limit,
            unit,
            confidence,
            "x-ratelimit",
            seen,
            detail,
            observed_at,
        )

    obs = window(
        (
            ("x-ratelimit-remaining-requests", "remaining", "num"),
            ("x-ratelimit-limit-requests", "limit", "num"),
            ("x-ratelimit-reset-requests", "reset_requests", "raw"),
        ),
        "per_minute",
        "requests",
    )
    if obs:
        observations.append(obs)

    fireworks_limits = {
        key: _num(ci[key][1])
        for key in (
            "x-ratelimit-limit-tokens-prompt",
            "x-ratelimit-limit-tokens-cache-adjusted-prompt",
            "x-ratelimit-limit-tokens-generated",
        )
        if key in ci and _num(ci[key][1]) is not None
    }
    tokens_present = any(
        key in ci
        for key in ("x-ratelimit-remaining-tokens", "x-ratelimit-limit-tokens")
    )
    if tokens_present or fireworks_limits:
        extra_detail = (
            {
                "limit_only_family": True,
                "limit_tokens_prompt": fireworks_limits.get(
                    "x-ratelimit-limit-tokens-prompt"
                ),
                "limit_tokens_cache_adjusted_prompt": fireworks_limits.get(
                    "x-ratelimit-limit-tokens-cache-adjusted-prompt"
                ),
                "limit_tokens_generated": fireworks_limits.get(
                    "x-ratelimit-limit-tokens-generated"
                ),
            }
            if fireworks_limits
            else None
        )
        obs = window(
            (
                ("x-ratelimit-remaining-tokens", "remaining", "num"),
                ("x-ratelimit-limit-tokens", "limit", "num"),
                ("x-ratelimit-reset-tokens", "reset_tokens", "raw"),
            ),
            "per_minute",
            "tokens_per_minute",
            extra_detail=extra_detail,
        )
        if obs is not None:
            if (
                obs["limit"] is None
                and "x-ratelimit-limit-tokens-prompt" in fireworks_limits
            ):
                # limit-only family (fireworks rows): surface the prompt-tier
                # figure as the limit; every variant stays in source_detail.
                obs["limit"] = fireworks_limits["x-ratelimit-limit-tokens-prompt"]
                obs["confidence"] = "partial"
            observations.append(obs)
        elif fireworks_limits:
            # Limit-ONLY response: the family carries effective-limit figures
            # and no remaining/reset header at all. The observation is built
            # from the family alone — remaining stays None (never 0, never
            # guessed), confidence stays "partial".
            seen = [
                ci[key][0]
                for key in (
                    "x-ratelimit-limit-tokens-prompt",
                    "x-ratelimit-limit-tokens-cache-adjusted-prompt",
                    "x-ratelimit-limit-tokens-generated",
                )
                if key in fireworks_limits
            ]
            observations.append(
                _observation(
                    provider_id,
                    account,
                    "per_minute",
                    None,
                    fireworks_limits["x-ratelimit-limit-tokens-prompt"],
                    "tokens_per_minute",
                    "partial",
                    "x-ratelimit",
                    seen,
                    {
                        "limit_only_family": True,
                        "limit_tokens_prompt": fireworks_limits.get(
                            "x-ratelimit-limit-tokens-prompt"
                        ),
                        "limit_tokens_cache_adjusted_prompt": fireworks_limits.get(
                            "x-ratelimit-limit-tokens-cache-adjusted-prompt"
                        ),
                        "limit_tokens_generated": fireworks_limits.get(
                            "x-ratelimit-limit-tokens-generated"
                        ),
                    },
                    observed_at,
                )
            )

    obs = window(
        (
            ("x-ratelimit-remaining-requests-day", "remaining", "num"),
            ("x-ratelimit-limit-requests-day", "limit", "num"),
            ("x-ratelimit-reset-requests-day", "reset_requests_day", "raw"),
        ),
        "rolling_daily",
        "requests",
    )
    if obs:
        observations.append(obs)

    return observations


def read_x_codex(headers, provider_id="unknown", account="default", observed_at=None):
    """x-codex-* percent-style adapter (recorded live mechanism, openai-codex rows).

    Each positional window (primary, secondary) carries a used-percent on a
    documented 0-100 scale plus window-minutes and a reset-at epoch. remaining
    is the percent COMPLEMENT (100 - used) in the same percent unit — the one
    arithmetic step the header itself supports; no absolute message/token
    count is invented. Credits headers, when present, yield a pool-scope
    observation (balance is absolute; unlimited keeps remaining None).
    """
    if observed_at is None:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ci = _ci_headers(headers)
    observations = []
    for position, window_kind in X_CODEX_POSITIONAL_WINDOWS:
        used_key = "x-codex-%s-used-percent" % position
        if used_key not in ci:
            continue
        seen = [ci[used_key][0]]
        used = _num(ci[used_key][1])
        detail = {"used_percent": used, "position": position}
        if used is None:
            detail["used_percent_raw"] = ci[used_key][1]
        minutes_key = "x-codex-%s-window-minutes" % position
        if minutes_key in ci:
            seen.append(ci[minutes_key][0])
            detail["window_minutes"] = _num(ci[minutes_key][1])
        reset_key = "x-codex-%s-reset-at" % position
        if reset_key in ci:
            seen.append(ci[reset_key][0])
            epoch = _num(ci[reset_key][1])
            detail["reset_at_raw"] = ci[reset_key][1]
            if epoch is not None:
                detail["resets_at"] = datetime.datetime.fromtimestamp(
                    epoch, tz=datetime.timezone.utc
                ).isoformat()
        remaining = (100 - used) if used is not None else None
        observations.append(
            _observation(
                provider_id,
                account,
                window_kind,
                remaining,
                None,  # percent-of-window only; no absolute cap on the wire
                "percent",
                "partial",
                "x-codex-percent",
                seen,
                detail,
                observed_at,
            )
        )

    credit_keys = ("x-codex-credits-has-credits", "x-codex-credits-unlimited", "x-codex-credits-balance")
    if any(key in ci for key in credit_keys):
        seen, detail = [], {}
        balance = None
        for key in credit_keys:
            if key not in ci:
                continue
            seen.append(ci[key][0])
            if key == "x-codex-credits-balance":
                balance = _num(ci[key][1])
                if balance is None:
                    detail["balance_raw"] = ci[key][1]
                else:
                    detail["balance"] = balance
            else:
                flag = _flag(ci[key][1])
                field = "has_credits" if "has-credits" in key else "unlimited"
                detail[field] = flag
        unlimited = detail.get("unlimited")
        if unlimited is True or (balance is None and unlimited is not True):
            remaining = None  # unlimited (or unreadable balance) is None, never 0
        else:
            remaining = balance
        confidence = "verified" if (balance is not None and unlimited is not True) else "partial"
        if detail.get("has_credits") is False:
            detail["reason"] = "no-credits-on-account"
        observations.append(
            _observation(
                provider_id,
                account,
                "pool",
                remaining,
                None,
                "credits",
                confidence,
                "x-codex-percent",
                seen,
                detail,
                observed_at,
            )
        )
    return observations


def read_retry_after(headers, provider_id="unknown", account="default", observed_at=None):
    """retry-after adapter — the single throttle header (spec §3 rung 1).

    An observation may carry ONLY retry-after with a reset hint: the 429 does
    not name the window that caused it, so window_kind is None and unit is
    "unknown" (both honest NULLs, never a guessed window). Seconds are parsed
    when numeric; an HTTP-date value is preserved raw.
    """
    if observed_at is None:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ci = _ci_headers(headers)
    if "retry-after" not in ci:
        return []
    orig, raw = ci["retry-after"]
    seconds = _num(raw) if re.fullmatch(r"\d+", raw) else None
    detail = {"retry_after_raw": raw}
    if seconds is not None:
        detail["retry_after_seconds"] = seconds
    else:
        detail["reason"] = "non-numeric retry-after preserved verbatim"
    return [
        _observation(
            provider_id,
            account,
            None,  # the throttled window is not named by the header
            None,
            None,
            "unknown",
            "partial",
            "retry-after",
            [orig],
            detail,
            observed_at,
        )
    ]


MECHANISM_ADAPTERS = (
    ("anthropic-ratelimit-unified", read_anthropic_unified),
    ("x-ratelimit", read_x_ratelimit),
    ("x-codex-percent", read_x_codex),
    ("retry-after", read_retry_after),
)


def load_known_header_names(quota_table=DEFAULT_QUOTA_TABLE):
    """Header names recognized as quota signals, from the L0 table.

    Reads every readback_kind == "header" row of provider_quota.jsonl and
    collects the exact header names it declares. readback_exact is either a
    JSON list of names (groq/sambanova/fireworks rows) or prose naming the
    headers (meta-model / openai-codex rows) — prose is scanned for
    header-shaped tokens and filtered to the known quota-header families
    (contains "ratelimit", equals retry-after, or starts with x-codex-).
    Nothing outside the table and the spec-named mechanism vocabularies is
    ever recognized. Returns a frozenset of lowercased names.
    """
    names = set()
    if quota_table and os.path.exists(quota_table):
        with open(quota_table, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("readback_kind") != "header":
                    continue
                exact = row.get("readback_exact") or ""
                declared = None
                if isinstance(exact, list):
                    declared = [str(x) for x in exact]
                else:
                    try:
                        parsed = json.loads(exact)
                        if isinstance(parsed, list):
                            declared = [str(x) for x in parsed]
                    except ValueError:
                        declared = None
                if declared is None:
                    declared = []
                    for token in _PROSE_HEADER_RE.findall(str(exact)):
                        low = token.lower()
                        if (
                            "ratelimit" in low
                            or low == "retry-after"
                            or low.startswith("x-codex-")
                        ):
                            declared.append(low)
                for name in declared:
                    names.add(name.strip().lower())
    return frozenset(names)


_KNOWN_NAMES_CACHE = {}


def known_header_names(quota_table=DEFAULT_QUOTA_TABLE):
    """Mechanism vocabularies (spec/table) UNION table-declared names, cached."""
    key = os.path.realpath(quota_table or "")
    if key not in _KNOWN_NAMES_CACHE:
        known = set()
        for _, names in MECHANISMS:
            known.update(names)
        known.update(load_known_header_names(quota_table))
        _KNOWN_NAMES_CACHE[key] = frozenset(known)
    return _KNOWN_NAMES_CACHE[key]


def read_observation(
    provider_id,
    headers,
    account="default",
    known_names=None,
    now=None,
    quota_table=DEFAULT_QUOTA_TABLE,
):
    """Ladder rung 1: try every header mechanism against this response.

    Returns (observations, next_rung):
      observations — list of spec §3 observation dicts (empty when no
        mechanism matched; a matched response can still yield several, one
        per window).
      next_rung — None when observations were read; otherwise the explicit
        fall-through result naming rung 2 / rung 4 with basis
        "derived-from-ledger" (never a silent skip).

    Unknown header names present in the response are IGNORED — they never
    enter an observation; when the response falls through they are reported
    verbatim in headers_present_unrecognized so the miss is auditable.

    now: inject a datetime (tz-aware) for tests; default is
    datetime.now(timezone.utc). known_names: override the recognized set;
    default loads mechanism vocabularies + the L0 header rows.
    """
    if known_names is None:
        known_names = known_header_names(quota_table)
    if now is None:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    else:
        observed_at = now.isoformat()
    ci = _ci_headers(headers)
    observations = []
    for _, adapter in MECHANISM_ADAPTERS:
        observations.extend(
            adapter(headers, provider_id=provider_id, account=account, observed_at=observed_at)
        )
    if observations:
        return observations, None
    unrecognized = sorted(
        ci[key][0] for key in ci if key not in known_names
    )
    fall_through = dict(HEADER_FALL_THROUGH)
    fall_through["headers_present_unrecognized"] = unrecognized
    return [], fall_through


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Quota L1 rung-1 header readback: observations from recorded response headers."
    )
    parser.add_argument("--provider", default="unknown", help="provider_id for the observations")
    parser.add_argument("--account", default="default", help="account label (default: default)")
    parser.add_argument(
        "--quota-table",
        default=DEFAULT_QUOTA_TABLE,
        help="path to provider_quota.jsonl (recognized header names)",
    )
    parser.add_argument(
        "--file",
        dest="headers_file",
        default=None,
        help="JSON file with {header: value}; stdin when omitted",
    )
    args = parser.parse_args(argv)
    try:
        if args.headers_file:
            with open(args.headers_file, encoding="utf-8") as fh:
                headers = json.load(fh)
        else:
            headers = json.load(sys.stdin)
    except (ValueError, OSError) as exc:
        print("error: headers must be a JSON object: %s" % exc, file=sys.stderr)
        return 2
    if not isinstance(headers, dict):
        print("error: headers must be a JSON object", file=sys.stderr)
        return 2
    observations, next_rung = read_observation(
        args.provider, headers, account=args.account, quota_table=args.quota_table
    )
    out = {"provider_id": args.provider, "observations": observations}
    if next_rung is not None:
        out["fall_through"] = next_rung
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
"""validate_provider_quota.py — L0 declaration validator (TR-206).

Validates data/tables/provider_quota.jsonl against the quota-layers-spec L0
contract (docs/quota-layers-spec.md). Every consumer of the table reads it
through these guarantees; nothing in code hard-codes a limit — if it is not in
this table, it is unknown.

Checks (fail = exit 1, machine-readable issues on stdout):
  schema      — every row: required keys present, types correct, closed
                vocabularies (window_kind, unit, reset_kind, source.kind,
                reason, confidence), no zero/negative limits (unknown is NULL,
                never 0), limit present with unit when numeric.
  coverage    — every LIVE registry provider (providers.jsonl, archive=false,
                valid_to open) has >=1 window row, OR at least one row with
                limit NULL carrying a valid reason.
  sources     — every row carries source.kind in {docs, observed}; a
                docs-sourced row names a URL; a row with NO source url at all
                must at least declare source.kind (a window row with neither
                source.url nor source.kind is rejected).
  reasons     — any row with limit NULL carries a reason from the closed set
                {not-published, no-readback, plan-not-disclosed}; a row with a
                numeric limit must NOT carry a reason.
  aliases     — a row for a gateway-alias provider (providers.jsonl
                tos_class == "gateway-alias") carries alias_of naming an
                existing registry parent; the parent exists and the alias row
                itself declares no own window (window_kind == "none").
  pools       — scope "pool:<name>" rows carry pool == <name>;
                pool rows coexist with the account-scope rows (a pool is its
                own scope, never a replacement for the account window).
  duplicates  — no two identical rows (one row per
                provider+account+window+scope identity).

Usage:
  validate_provider_quota.py [--tables DIR] [--json]
Exit 0 = all checks pass; exit 1 = issues found (validator never repairs).
"""

import argparse
import json
import os
import sys

WINDOW_KINDS = {
    "rolling_5h",
    "rolling_daily",
    "weekly",
    "monthly",
    "per_minute",
    "per_request",
    "pool",
    "none",
}
UNITS = {
    "tokens",
    "tokens_per_minute",
    "usd",
    "credits",
    "requests",
    "messages",
    "energy",
    "percent",
    "unknown",
}
RESET_KINDS = {"rolling", "fixed_interval", "calendar_aligned", "unknown"}
SOURCE_KINDS = {"docs", "observed"}
REASONS = {"not-published", "no-readback", "plan-not-disclosed"}
CONFIDENCE = {"verified", "partial", "unknown"}

REQUIRED = ("provider_id", "account", "scope", "window_kind", "unit", "limit")
STR_FIELDS = (
    "provider_id",
    "account",
    "scope",
    "window_kind",
    "unit",
    "reset_kind",
    "confidence",
)


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                rows.append({"_error": f"line {lineno}: invalid JSON: {exc}"})
                continue
            row["_line"] = lineno
            rows.append(row)
    return rows


def validate(tables_dir):
    quota_path = os.path.join(tables_dir, "provider_quota.jsonl")
    prov_path = os.path.join(tables_dir, "providers.jsonl")
    issues = []

    if not os.path.exists(quota_path):
        return [f"provider_quota.jsonl not found at {quota_path}"]
    rows = load_jsonl(quota_path)

    providers = {}
    if os.path.exists(prov_path):
        for p in load_jsonl(prov_path):
            if "_error" not in p:
                providers[p["id"]] = p

    def registry_providers():
        """Live registry providers (archive=false, valid_to open)."""
        out = {}
        for pid, p in providers.items():
            if p.get("archive"):
                continue
            if p.get("valid_to"):
                continue
            out[pid] = p
        return out

    seen_identity = {}
    rows_by_provider = {}

    # ------------------------------------------------------------- schema --
    for r in rows:
        line = r.get("_line", "?")
        if "_error" in r:
            issues.append(f"schema L{line}: {r['_error']}")
            continue
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: missing keys {missing}"
            )
        bad_type = [k for k in STR_FIELDS if k in r and not isinstance(r[k], str)]
        if bad_type:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: non-string {bad_type}"
            )
        if r.get("window_kind") not in WINDOW_KINDS:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"window_kind {r.get('window_kind')!r} not in closed set"
            )
        if r.get("unit") not in UNITS:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"unit {r.get('unit')!r} not in closed set"
            )
        if "reset_kind" in r and r.get("reset_kind") not in RESET_KINDS:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"reset_kind {r.get('reset_kind')!r} not in closed set"
            )
        if "confidence" in r and r.get("confidence") not in CONFIDENCE:
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"confidence {r.get('confidence')!r} not in closed set"
            )
        limit = r.get("limit")
        if isinstance(limit, bool) or not (
            limit is None or isinstance(limit, (int, float))
        ):
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"limit must be numeric or NULL, got {type(limit).__name__}"
            )
        elif isinstance(limit, (int, float)) and limit <= 0:
            # honesty contract: unknown is NULL + reason, never 0
            issues.append(
                f"schema L{line} {r.get('provider_id')}: "
                f"limit {limit} — a quota limit is never zero/negative; "
                f"use NULL + reason for unknown"
            )
        src = r.get("source")
        if src is not None:
            if not isinstance(src, dict):
                issues.append(
                    f"schema L{line} {r.get('provider_id')}: "
                    f"source must be an object, got {type(src).__name__}"
                )
            elif src.get("kind") not in SOURCE_KINDS:
                issues.append(
                    f"schema L{line} {r.get('provider_id')}: "
                    f"source.kind {src.get('kind')!r} not in closed set"
                )

    # ------------------------------------------------------------ reasons --
    for r in rows:
        if "_error" in r:
            continue
        line = r.get("_line", "?")
        pid = r.get("provider_id")
        reason = r.get("reason")
        if r.get("limit") is None:
            if reason is None:
                issues.append(
                    f"reasons L{line} {pid}: limit is NULL with no reason "
                    f"(silent unknown — needs one of {sorted(REASONS)})"
                )
            elif reason not in REASONS:
                issues.append(
                    f"reasons L{line} {pid}: reason {reason!r} "
                    f"not in closed set {sorted(REASONS)}"
                )
        else:
            if reason is not None:
                issues.append(
                    f"reasons L{line} {pid}: numeric limit carries a "
                    f"reason {reason!r} — reasons belong to NULL limits"
                )

    # ------------------------------------------------------------ sources --
    for r in rows:
        if "_error" in r:
            continue
        line = r.get("_line", "?")
        pid = r.get("provider_id")
        src = r.get("source")
        if src is None:
            issues.append(
                f"sources L{line} {pid}: no source block at all "
                f"(window row with neither source.url nor source.kind)"
            )
            continue
        if not isinstance(src, dict):
            continue
        url = src.get("url")
        if not url and src.get("kind") == "docs":
            issues.append(f"sources L{line} {pid}: docs-sourced row names no URL")
        if url and not str(url).startswith(("http://", "https://")):
            issues.append(
                f"sources L{line} {pid}: source.url is not an http(s) URL: {url!r}"
            )

    # ------------------------------------------------------------ aliases --
    for r in rows:
        if "_error" in r:
            continue
        line = r.get("_line", "?")
        pid = r.get("provider_id")
        prov = providers.get(pid)
        if prov is not None and prov.get("tos_class") == "gateway-alias":
            alias_of = r.get("alias_of")
            if not alias_of:
                issues.append(
                    f"aliases L{line} {pid}: alias provider row without "
                    f"alias_of — an alias does not own quota"
                )
            elif alias_of not in providers:
                issues.append(
                    f"aliases L{line} {pid}: alias_of {alias_of!r} "
                    f"is not a known provider"
                )
            elif r.get("window_kind") != "none":
                issues.append(
                    f"aliases L{line} {pid}: alias row declares its own "
                    f"window {r.get('window_kind')!r} — aliases inherit "
                    f"the parent's windows and never declare their own"
                )

    # -------------------------------------------------------------- pools --
    for r in rows:
        if "_error" in r:
            continue
        line = r.get("_line", "?")
        pid = r.get("provider_id")
        scope = r.get("scope", "")
        pool = r.get("pool")
        if scope.startswith("pool:"):
            name = scope[len("pool:") :]
            if not name:
                issues.append(f"pools L{line} {pid}: scope 'pool:' with empty name")
            if pool != name:
                issues.append(
                    f"pools L{line} {pid}: scope pool:{name!r} but "
                    f"pool field is {pool!r}"
                )
        elif pool is not None:
            issues.append(
                f"pools L{line} {pid}: pool field set ({pool!r}) but scope is {scope!r}"
            )
        if r.get("window_kind") == "pool" and not str(scope).startswith("pool:"):
            issues.append(
                f"pools L{line} {pid}: window_kind 'pool' but scope "
                f"is {scope!r} — a pool window must be pool-scoped, "
                f"not merged into the account scope"
            )

    # ---------------------------------------------------------- duplicates --
    for r in rows:
        if "_error" in r:
            continue
        line = r.get("_line", "?")
        identity = (
            r.get("provider_id"),
            r.get("account"),
            r.get("window_kind"),
            r.get("scope"),
            r.get("pool"),
            r.get("variant"),
            r.get("unit"),
        )
        if identity in seen_identity:
            issues.append(
                f"duplicates L{line} {r.get('provider_id')}: duplicate "
                f"identity {identity[1:]} — first at L{seen_identity[identity]}"
                f" (one row per provider+account+window+scope; a tier "
                f"variant needs its own variant/pool discriminator)"
            )
        else:
            seen_identity[identity] = line
        rows_by_provider.setdefault(r.get("provider_id"), []).append(line)

    # ----------------------------------------------------------- coverage --
    for pid, prov in sorted(registry_providers().items()):
        lines = rows_by_provider.get(pid, [])
        if not lines:
            issues.append(
                f"coverage: provider {pid} has no quota rows and no "
                f"declared reason — every provider needs >=1 window row "
                f"or a NULL-limit row with a reason"
            )
            continue
        has_window = any(
            r.get("window_kind") not in (None, "none") and "_error" not in r
            for r in rows
            if r.get("provider_id") == pid
        )
        has_reasoned_null = any(
            r.get("limit") is None and r.get("reason") in REASONS and "_error" not in r
            for r in rows
            if r.get("provider_id") == pid
        )
        if not has_window and not has_reasoned_null:
            issues.append(
                f"coverage: provider {pid} rows {lines} declare no "
                f"window and no reasoned NULL"
            )

    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_tables = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tables"
    )
    ap.add_argument(
        "--tables",
        default=default_tables,
        help="directory holding provider_quota.jsonl + providers.jsonl",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="machine-readable JSON output",
    )
    args = ap.parse_args()

    issues = validate(args.tables)
    if args.as_json:
        print(
            json.dumps(
                {"valid": not issues, "issues": issues, "tables_dir": args.tables},
                indent=2,
            )
        )
    else:
        for issue in issues:
            print(f"FAIL {issue}")
        print(
            f"provider_quota: {len(issues)} issue(s)"
            + ("" if issues else " — all checks pass")
        )
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())

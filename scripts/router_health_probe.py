#!/usr/bin/env python3
"""router_health_probe.py — canary-grade probe for the routing control plane.

TR-REVIEW-001. One HTTP GET against a router instance's ``/health``, judged
against the three facts that endpoint exists to publish, with an exit code a
cron/canary can branch on:

1. the process answers and its ``commit`` is a real revision (not "unknown"),
2. the ``router validate`` gate RAN and passed (``gate.valid is true`` —
   ``null`` means the checks could not run, which is a different failure),
3. the registry is not stale (``registry_age.stale is false``).

Exit codes, chosen to match the fleet's fail-open/fail-loud split:

  0  PASS  — every assertion held
  1  FAIL  — the router ANSWERED and is unhealthy (stale registry, red gate,
             unknown commit). This is the loud case: the control plane is up
             and wrong, so a silent WARN would let it drift.
  2  CANNOT RUN — no answer (connection refused, timeout, non-JSON, non-200).
             The caller decides whether an unreachable router is fatal: the
             canary WARNs (a probe run off-host must not red a repo), while an
             on-host watchdog should FAIL.

Judgement is deliberately NOT "the endpoint returned 200". A server that
happily reports ``stale: true`` or ``valid: false`` is exactly the state this
probe has to catch, and the registry-freshness breakage it guards against
(TR-108) produced a healthy-looking HTTP surface.

Usage:
  router_health_probe.py [--url URL] [--json] [--timeout S] [--max-age-h H]

  --url        base URL (default http://127.0.0.1:9092; env ROUTER_HEALTH_URL)
  --json       emit one JSON object on stdout (default: key=value lines)
  --timeout    per-request timeout in seconds (default 10)
  --max-age-h  additionally FAIL when registry_age.age_h exceeds H hours —
               an absolute cap on top of the relative staleness verdict, for
               callers that want "the registry must be reseeded daily".

Env for hermetic tests: ROUTER_HEALTH_URL overrides the default URL.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("ROUTER_HEALTH_URL", "http://127.0.0.1:9092")

PASS, FAIL, CANNOT_RUN = 0, 1, 2


def fetch_health(url, timeout):
    """-> (payload, None) or (None, reason). Never raises."""
    target = url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {target}"
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 — a probe never crashes the caller
        return None, f"{type(exc).__name__}: {exc}"
    if status != 200:
        return None, f"HTTP {status} from {target}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, f"non-JSON body from {target}: {exc}"
    if not isinstance(payload, dict):
        return None, f"unexpected body shape from {target}: {type(payload).__name__}"
    return payload, None


def judge(payload, max_age_h=None):
    """-> (failures, observations). Pure function; the whole verdict lives here."""
    failures = []
    obs = {}

    commit = payload.get("commit")
    obs["commit"] = commit
    if not commit or commit == "unknown":
        failures.append("commit unresolved (got %r) — build identity is unknown"
                        % (commit,))
    elif len(str(commit)) < 7:
        failures.append("commit %r is not a full revision" % (commit,))

    gate = payload.get("gate")
    if not isinstance(gate, dict):
        failures.append("gate block missing or not an object: %r" % (gate,))
        obs["gate_valid"] = None
    else:
        obs["gate_valid"] = gate.get("valid")
        obs["gate_failed_checks"] = gate.get("failed_checks")
        if gate.get("error"):
            failures.append("gate could not run: %s" % gate["error"])
        elif gate.get("valid") is None:
            failures.append("gate verdict is null (checks did not run)")
        elif gate.get("valid") is not True:
            failed = ", ".join(gate.get("failed_checks") or []) or "unknown"
            failures.append("gate INVALID — failed checks: %s" % failed)

    age = payload.get("registry_age")
    if not isinstance(age, dict):
        failures.append("registry_age block missing or not an object: %r" % (age,))
        obs["stale"] = None
    else:
        obs["stale"] = age.get("stale")
        obs["age_h"] = age.get("age_h")
        obs["registry_path"] = age.get("path")
        if age.get("error"):
            failures.append("registry_age error: %s" % age["error"])
        elif age.get("exists") is not True:
            failures.append("registry missing at %s" % age.get("path"))
        elif age.get("stale") is True:
            failures.append(
                "registry STALE (lag %ss vs %s, content_match=%s)"
                % (age.get("lag_s"), age.get("newest_table"),
                   age.get("content_match")))
        if max_age_h is not None:
            hours = age.get("age_h")
            if hours is None:
                failures.append("cannot enforce --max-age-h: age_h unavailable")
            elif hours > max_age_h:
                failures.append("registry age %.2fh exceeds the %.2fh cap"
                                % (hours, max_age_h))

    return failures, obs


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Probe a task-router /health surface and exit non-zero "
                    "when the control plane is unhealthy")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"base URL of the router (default {DEFAULT_URL})")
    ap.add_argument("--json", action="store_true",
                    help="emit one JSON object on stdout")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-request timeout seconds (default 10)")
    ap.add_argument("--max-age-h", type=float, default=None,
                    help="also fail when registry_age.age_h exceeds this")
    args = ap.parse_args(argv)

    payload, reason = fetch_health(args.url, args.timeout)
    if payload is None:
        result = {"status": "cannot_run", "url": args.url, "reason": reason}
        if args.json:
            print(json.dumps(result))
        else:
            print(f"cannot_run url={args.url}")
            print(f"reason={reason}")
        return CANNOT_RUN

    failures, obs = judge(payload, max_age_h=args.max_age_h)
    result = {
        "status": "fail" if failures else "pass",
        "url": args.url,
        "commit": obs.get("commit"),
        "gate_valid": obs.get("gate_valid"),
        "gate_failed_checks": obs.get("gate_failed_checks"),
        "registry_stale": obs.get("stale"),
        "registry_age_h": obs.get("age_h"),
        "registry_path": obs.get("registry_path"),
        "failures": failures,
    }
    if args.json:
        print(json.dumps(result))
    else:
        for key in ("commit", "gate_valid", "gate_failed_checks",
                    "registry_stale", "registry_age_h", "registry_path"):
            print(f"{key}={result[key]}")
        for f in failures:
            print(f"FAIL {f}")
    return FAIL if failures else PASS


if __name__ == "__main__":
    sys.exit(main())

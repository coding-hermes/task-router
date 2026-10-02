#!/usr/bin/env python3
"""TR-255: consume /health's code.stale flag — the alert side of the deploy contract.

TR-141 gave every serving instance an honest deploy-parity verdict: /health's
`code` block reports what THIS process loaded (commit + source sha) versus what
the repo/tree holds now. The 2026-09-25 proxy test plan declared the contract
"deploy = sync_runtime + restart, verified by /health" — and nothing consumed
the signal, so stale instances served for days (measured 2026-10-02: :9092 and
:9391 had been up since 2026-09-30 with code.stale=true and no alert anywhere).

What this does, per base URL (default: the three fleet listeners; env
ROUTER_STALE_URLS overrides, comma-separated):

    fetch <base>/health  ->  one verdict per server:

    OK           code.stale is false and every identity field agrees
    STALE        code.stale is true, or the identity fields disagree while
                 the flag claims fresh (a lying flag must not pass)
    UNREACHABLE  no answer, HTTP error, malformed/non-object JSON payload,
                 a missing `code` block, or an indeterminate verdict —
                 code.stale=null is never silence; its `error` reason is
                 printed (a null-with-no-reason is exactly the TR-235 lesson)

One line per server, then: exit 0 all OK, 1 any STALE (the actionable
deploy-contract signal), 2 any UNREACHABLE with no STALE. --report-only is
the human form: same lines, always exit 0.

Fail-open like every read path in this repo: a malformed payload prints its
line and moves on; this script never crashes on a bad server and never sends
anything but a GET. Stdlib-only (urllib/json/argparse) — it must run anywhere,
including a cron with no venv.

Note on the default list: the :9093 web UI (router_web.py) serves no /health
today, so it reports UNREACHABLE "HTTP 404" on purpose — that is a true
statement about the fleet (a listener without a health surface), not a bug in
this script. Either give the UI a health route or set ROUTER_STALE_URLS to the
instances that speak the contract. `make restart-router` verifies only the
health-speaking instances (:9092, :9391) for the same reason.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

#: The three fleet listeners (docs/health-plane.md). :9093 is the web UI.
DEFAULT_URLS = (
    "http://127.0.0.1:9092",
    "http://127.0.0.1:9093",
    "http://127.0.0.1:9391",
)

#: Comma-separated base-URL override, for hosts whose ports differ.
ENV_URLS = "ROUTER_STALE_URLS"

#: Don't read more than this from one /health body — it is a small dict.
MAX_BODY_BYTES = 1_000_000

EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNREACHABLE = 2


def configured_urls(flag_urls):
    """Resolution order: --url flags win, then ROUTER_STALE_URLS, then defaults.

    Blank entries (trailing commas, stray spaces) are dropped rather than
    turned into fake unreachable servers.
    """
    if flag_urls:
        urls = list(flag_urls)
    else:
        raw = os.environ.get(ENV_URLS, "").strip()
        urls = [part.strip() for part in raw.split(",")] if raw else list(DEFAULT_URLS)
    return [u for u in urls if u]


def evaluate_code(code):
    """Verdict from the payload's `code` identity block.

    Returns (verdict, detail). The payload's own `stale` flag is primary —
    it is computed by the server from the same comparison at request time —
    but when the identity fields are present they are re-derived here, and a
    disagreement is resolved CONSERVATIVELY: the stale side wins, because the
    cost of an extra restart is trivial next to days of unnoticed drift.

    Only a real comparison yields OK/STALE; a missing half or a null verdict
    is UNREACHABLE with the reason spelled out (never a fabricated pass).
    """
    loaded_commit = code.get("loaded_commit")
    loaded_sha = code.get("loaded_source_sha")
    repo_commit = code.get("repo_commit")
    live_sha = code.get("live_source_sha")
    flag = code.get("stale")
    err = code.get("error")
    loaded_at = code.get("loaded_at") or "?"

    derivable = all(v is not None for v in (loaded_commit, loaded_sha, repo_commit, live_sha))
    derived = (live_sha != loaded_sha or repo_commit != loaded_commit) if derivable else None
    ident = f"loaded {loaded_commit or '?'}/{loaded_sha or '?'} vs repo {repo_commit or '?'}/{live_sha or '?'}"

    if flag is True or derived is True:
        if flag is False:
            detail = f"UNDER-REPORTED: {ident} disagrees while code.stale=false; restart needed (loaded_at {loaded_at})"
        else:
            detail = f"{ident}; restart needed (loaded_at {loaded_at})"
            if flag is None:
                detail = f"code.stale=null but fields disagree — treated stale; {detail}"
        return "STALE", detail
    if derivable:
        if flag is False:
            return "OK", f"{ident} (loaded_at {loaded_at})"
        reason = f", error: {err}" if err else ""
        return "UNREACHABLE", f"verdict indeterminate (code.stale={flag!r}{reason}); {ident}"
    # No comparable identity fields — fall back to the bare flag only if it is
    # an explicit false; anything else (null/absent) is honest silence, which
    # here means: cannot verify -> unreachable with the reason.
    if flag is False:
        return "OK", "code.stale=false (no identity fields to cross-check)"
    reason = f", error: {err}" if err else ""
    return "UNREACHABLE", f"no identity fields to compare and code.stale={flag!r}{reason}"


def check(base_url, timeout_s):
    """Fetch one /health and classify it. Never raises — fail-open by contract."""
    url = base_url.rstrip("/") + "/health"
    result = {"url": base_url, "verdict": "UNREACHABLE", "detail": "", "code": None}
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        why = " (no /health surface on this listener)" if exc.code == 404 else ""
        result["detail"] = f"HTTP {exc.code} from {url}{why}"
        exc.close()
        return result
    except Exception as exc:  # noqa: BLE001 — any fetch failure is a line, not a crash
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        result["detail"] = f"HTTP {status} but the body is not JSON"
        return result
    if not isinstance(payload, dict):
        result["detail"] = f"HTTP {status} but the payload is a {type(payload).__name__}, not an object"
        return result
    code = payload.get("code")
    if not isinstance(code, dict):
        keys = ", ".join(sorted(payload))[:120]
        result["detail"] = f"HTTP {status} but the payload has no `code` identity block (keys: {keys})"
        return result
    result["code"] = code
    verdict, detail = evaluate_code(code)
    result["verdict"] = verdict
    result["detail"] = detail
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Watch the fleet router instances' /health code.stale flag (TR-255). "
                    "Exit 0 all OK, 1 any STALE, 2 any UNREACHABLE (unless --report-only).")
    parser.add_argument("--url", action="append", default=[], metavar="BASE",
                        help=f"base URL to check (repeatable). Default: the {ENV_URLS} env value, "
                             f"then {' '.join(DEFAULT_URLS)}")
    parser.add_argument("--timeout", type=float, default=5.0, metavar="SEC",
                        help="per-request timeout in seconds (default 5)")
    parser.add_argument("--report-only", action="store_true",
                        help="print the verdicts but always exit 0 (report, not alert)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object on stdout instead of one line per server")
    args = parser.parse_args(argv)

    urls = configured_urls(args.url)
    if not urls:
        parser.error(f"no URLs configured: pass --url or set {ENV_URLS}")

    results = [check(u, args.timeout) for u in urls]
    if args.json:
        print(json.dumps({"results": results, "report_only": bool(args.report_only)}, indent=2))
    else:
        for r in results:
            print(f"{r['verdict']:<11} {r['url']}  {r['detail']}")

    if args.report_only:
        return EXIT_OK
    if any(r["verdict"] == "STALE" for r in results):
        return EXIT_STALE
    if any(r["verdict"] == "UNREACHABLE" for r in results):
        return EXIT_UNREACHABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

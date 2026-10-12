#!/usr/bin/env python3
"""Guarded graceful deploy for the local task-router services.

Default is a DRY RUN. Use --apply to restart. Each service is restarted
sequentially; after restart, /readyz and /health must prove a new PID and the
expected loaded source digest before the next service is touched. The server
itself drains in-flight HTTP handlers on SIGTERM. This tool never edits unit
files or restarts the scheduler.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import router_drain  # noqa: E402

SERVICES = {
    "task-router-server": {"unit": "task-router-server.service", "port": 9092},
    "task-router-proxy": {"unit": "task-router-proxy.service", "port": 9391},
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually restart the selected service(s); without this, only print the plan")
    ap.add_argument("--service", dest="services", action="append",
                    choices=sorted(SERVICES),
                    help="restart one service; repeatable; default is both, sequentially")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow deployment from a dirty scripts/ tree (explicitly stamps that fact)")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="readiness deadline per service in seconds (default: 90)")
    args = ap.parse_args(argv)
    if args.services is None:
        args.services = ["task-router-server", "task-router-proxy"]
    return args


def verify_deployment(ready_status, ready_body, health_status, health_body,
                      *, old_pid, expected_digest):
    """Pure post-restart verdict; require readiness, a new PID, and exact source."""
    checks = []
    if ready_status != 200 or not isinstance(ready_body, dict) or ready_body.get("ready") is not True:
        checks.append("not ready (HTTP %s)" % ready_status)
    runtime = health_body.get("runtime") if isinstance(health_body, dict) else None
    if health_status != 200 or not isinstance(runtime, dict):
        checks.append("health/runtime missing (HTTP %s)" % health_status)
        runtime = runtime or {}
    pid = runtime.get("pid")
    if pid is None or str(pid) == str(old_pid):
        checks.append("pid did not change")
    if runtime.get("source_digest") != expected_digest:
        checks.append("source digest mismatch")
    if runtime.get("draining") is True:
        checks.append("process is draining")
    return {"ok": not checks, "checks": checks, "runtime": runtime}


def _http_json(url, timeout=2.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return exc.code, body


def _systemctl(*args, timeout=100):
    return subprocess.run(["systemctl", "--user", *args], cwd=REPO,
                          capture_output=True, text=True, timeout=timeout)


def _old_pid(unit):
    result = _systemctl("show", unit, "-p", "MainPID", "--value")
    if result.returncode != 0:
        raise RuntimeError("systemctl show failed: %s" % (result.stderr.strip() or result.stdout.strip()))
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("invalid MainPID from systemd: %r" % result.stdout.strip()) from exc


def _wait_ready(port, old_pid, expected_digest, timeout_s):
    deadline = time.monotonic() + max(0.0, timeout_s)
    last = (0, {}, 0, {})
    while time.monotonic() < deadline:
        try:
            ready_status, ready = _http_json("http://127.0.0.1:%d/readyz" % port)
            health_status, health = _http_json("http://127.0.0.1:%d/health" % port)
            last = (ready_status, ready, health_status, health)
            verdict = verify_deployment(*last, old_pid=old_pid,
                                        expected_digest=expected_digest)
            if verdict["ok"]:
                return verdict
        except (OSError, TimeoutError, ValueError):
            pass
        time.sleep(0.5)
    verdict = verify_deployment(*last, old_pid=old_pid,
                                expected_digest=expected_digest)
    verdict["ok"] = False
    verdict["checks"].append("readiness deadline expired")
    return verdict


def _preflight(repo):
    return subprocess.run(
        [sys.executable, "-m", "py_compile", *map(str, sorted((repo / "scripts").glob("*.py")))],
        cwd=repo, capture_output=True, text=True,
    )


def deploy(services=None, *, apply=False, allow_dirty=False, timeout_s=90.0,
           repo=REPO, systemctl=_systemctl):
    """Execute or plan a sequential deploy. Service manager injectable for tests."""
    names = services or ["task-router-server", "task-router-proxy"]
    unknown = sorted(set(names) - set(SERVICES))
    if unknown:
        return 2, {"ok": False, "error": "unknown service(s): %s" % ", ".join(unknown)}
    identity = router_drain.runtime_identity(repo)
    if identity.get("source_dirty") and not allow_dirty:
        return 2, {"ok": False, "error": "scripts/ worktree is dirty; commit first or pass --allow-dirty",
                   "source": identity}
    preflight = _preflight(Path(repo))
    if preflight.returncode:
        return 2, {"ok": False, "error": "Python compile preflight failed",
                   "detail": preflight.stderr[-2000:]}
    plan = [{"service": name, **SERVICES[name]} for name in names]
    if not apply:
        return 0, {"ok": True, "dry_run": True, "plan": plan,
                   "source": identity, "note": "no service restarted; pass --apply to execute"}

    results = []
    for item in plan:
        unit = item["unit"]
        try:
            old_pid = _old_pid(unit)
        except Exception as exc:
            return 1, {"ok": False, "partial": bool(results), "results": results,
                       "failed_service": item["service"], "error": str(exc)}
        result = systemctl("restart", unit, timeout=max(100, timeout_s + 10))
        if result.returncode != 0:
            return 1, {"ok": False, "partial": bool(results), "results": results,
                       "failed_service": item["service"], "error": result.stderr.strip()}
        verdict = _wait_ready(item["port"], old_pid, identity["source_digest"], timeout_s)
        results.append({"service": item["service"], "old_pid": old_pid, **verdict})
        if not verdict["ok"]:
            return 1, {"ok": False, "partial": True, "results": results,
                       "failed_service": item["service"],
                       "error": "restart returned but readiness/source verification failed; later services untouched"}
    return 0, {"ok": True, "dry_run": False, "results": results,
               "source": identity}


def main(argv=None):
    args = parse_args(argv)
    code, report = deploy(args.services, apply=args.apply,
                          allow_dirty=args.allow_dirty, timeout_s=args.timeout)
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

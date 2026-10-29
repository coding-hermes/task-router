#!/usr/bin/env python3
"""router_degraded_wall_falsifier.py — does a DEGRADED fallback hop get a short leash? (TR-355)

WHY THIS EXISTS
---------------
Measured 2026-10-29 on the live fleet: 18 of 32 scheduler failures in 24h were
`stalled: no progress for 30m` on a gateway POST. The gateway's own `_router`
envelope for one of them (mischief-chaos-2026-10-29-02-40-57) says why:

    terminal_reason = hop-wall-timeout
    exhausted       = true
    ladder[0]       = deepseek-foreman/deepseek-flash
    latency_s       = 600.1
    exclusions      = every other hop health-DOWN / circuit-OPEN
    FALLBACK: all 6 eligible hops gated — always-run lane; DEGRADED —
              requirements_unmet: [('terminal', 1, 0), ('tool_use', 1, 0)]

A lane that does not meet the task's requirements cannot succeed at it; letting
it run the full 600s hop wall converts a certain failure into a ten-minute one,
and the scheduler has no earlier signal with which to re-admit the project.

THE CLAIM THIS FALSIFIES
------------------------
When the hop a request is about to ride is a DEGRADED fallback (the ladder row
carries `requirements_unmet`), its wall budget must be at most
ROUTER_DEGRADED_WALL_S (default 120s). Healthy hops are unaffected — this is a
leash on a lane that is already known-unfit, not a shorter budget for the fleet.

WHAT IT READS
-------------
A captured `_router` envelope (JSON) — from a scheduler journal line, a ledger
row, or the fixture committed beside this script. Nothing is re-derived: the
envelope is the evidence, and the script only judges it.

EXIT
----
0  the degraded hop was bounded (or no degraded hop rode a request)
1  VIOLATION — a degraded hop consumed more than the degraded wall
2  the evidence could not be read (never a pass)
"""
from __future__ import annotations

import argparse
import json
import sys

DEFAULT_WALL_S = 120.0
DEGRADED_MARKERS = ("requirements_unmet", "DEGRADED", "always-run lane")


def degraded_hops(envelope: dict) -> list[dict]:
    """Ladder entries that rode the exhausted-chain fallback (a DEGRADED lane)."""
    ladder = envelope.get("ladder") or []
    out = []
    for hop in ladder:
        if not isinstance(hop, dict):
            continue
        blob = json.dumps(hop, sort_keys=True)
        if any(m in blob for m in DEGRADED_MARKERS):
            out.append(hop)
    # the fallback is also named on the envelope's gate_reasons, for envelopes
    # whose ladder row does not repeat the requirement matrix
    reasons = " ".join(envelope.get("gate_reasons") or [])
    if not out and any(m in reasons for m in DEGRADED_MARKERS):
        out = [h for h in ladder if isinstance(h, dict)]
    return out


def judge(envelope: dict, wall_s: float) -> tuple[int, list[str]]:
    hops = degraded_hops(envelope)
    findings = []
    if not hops:
        return 0, ["no DEGRADED fallback hop rode a request in this envelope"]
    worst = 0.0
    for hop in hops:
        latency = hop.get("latency_s")
        if latency is None:
            findings.append(
                f"hop {hop.get('hop')} {hop.get('provider')}/{hop.get('model')} "
                f"rode the DEGRADED fallback with NO measured latency — unprovable")
            continue
        latency = float(latency)
        worst = max(worst, latency)
        if latency > wall_s:
            findings.append(
                f"hop {hop.get('hop')} {hop.get('provider')}/{hop.get('model')} "
                f"rode the DEGRADED fallback for {latency:.1f}s > degraded wall {wall_s:g}s "
                f"(reason={hop.get('reason')!r}, outcome={hop.get('outcome')!r})")
    if findings:
        return 1, findings
    return 0, [f"degraded fallback hop(s) bounded: worst {worst:.1f}s <= {wall_s:g}s"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("envelope", help="captured _router envelope (JSON), or - for stdin")
    ap.add_argument("--degraded-wall-s", type=float, default=DEFAULT_WALL_S,
                    help=f"the leash a DEGRADED hop must respect (default {DEFAULT_WALL_S:g})")
    ns = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if ns.envelope == "-" else open(ns.envelope).read()
        envelope = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - evidence unreadable is a distinct verdict
        print(f"UNREADABLE: {type(exc).__name__}: {exc}")
        return 2
    if isinstance(envelope, dict) and "_router" in envelope:
        envelope = envelope["_router"]
    code, findings = judge(envelope, ns.degraded_wall_s)
    label = {0: "BOUNDED", 1: "VIOLATION"}[code]
    print(f"{label}: {findings[0] if len(findings) == 1 else ''}".rstrip())
    for f in findings if len(findings) > 1 else []:
        print(f"  - {f}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

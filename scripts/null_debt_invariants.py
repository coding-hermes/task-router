#!/usr/bin/env python3
"""null_debt_invariants.py — null-debt PREVENTION invariants (TR-201).

TR-197 built the census (classify every null MEANINGFUL vs UNEXPLAINED and
count the debt). TR-198 fixed one wipe. TR-201 adds the PREVENTION side: a
gate that fails on the SHAPES that indicate null debt is being created,
before the census ever has to count it. Three invariants over
data/tables/*.jsonl (liveness reuses data_null_census.is_live: a row is live
when it is not archive, not disabled, and carries no valid_to):

  INV1  no LIVE lane is >=50% null across the table's measured columns
        (the TR-198 wipe-shape: a lane whose values were replaced with
        nulls). A value counts as null when the key is present in the row
        with value None or "" — the same rule the census uses, so absent
        keys are not nulls (most tables are sparse by design).
  INV2  no column is 100% null (every row that carries the key carries a
        null value) unless the (table, column) pair is in the explicit
        NULL_OK allow-list WITH a reason. Again present-key based: a column
        nobody writes is absent-key sparse, not wiped.
  INV3  no row in a lifecycle_gate.TABLE_KEYS table carries a non-empty
        valid_to/available_from without a non-empty lifecycle_source. The
        predicate is lifecycle_gate's (TR-199 R4 "no anonymous dates") —
        this gate DELEGATES to it and does not re-implement it.

Why INV1/INV2 measure over NON-STRUCTURAL columns (the one deliberate
deviation from a naive reading of the brief): TR-197's STRUCTURAL registry
(data_null_census.STRUCTURAL) declares, per table, the columns whose null IS
the designed state ("no sample yet", "not retired", "not published by
upstream"). Measured on the committed tables (2026-10-07): under a naive
all-columns fraction, 724 live models.jsonl lanes land at 0.50-0.77 — but
every one of those nulls sits in STRUCTURAL columns (perf_*, thinking,
vision, ...), i.e. they are the designed-absence vocabulary the census
already blesses, not wipe-shape debt. The real TR-198 wipe (sync mirror
nulling populated values) hit columns whose absence is NOT designed. So
INV1/INV2 exclude STRUCTURAL columns from the measurement; a future writer
that starts nulling a previously-populated NON-structural column still
trips the gate, which is the debt this gate exists to prevent. The census
(TR-197) remains the authority for the stamped/meaningful vocabulary; this
gate adds the aggregate-shape checks the census cannot express.

Exit code: 0 when all three invariants hold, 1 on any violation (each
violation names table, lane, field and measured fraction), 2 on internal
errors. --json emits {"inv1"|"inv2"|"inv3": {"violations": [...]}}.
--publish PATH writes the watch-hook state file (timestamp, git sha, the
data_null_census.py --json live-UNEXPLAINED count, per-invariant violation
counts). No cron here — the foreman wires the schedule.

Usage:
  python3 scripts/null_debt_invariants.py                 # gate (text)
  python3 scripts/null_debt_invariants.py --json          # machine report
  python3 scripts/null_debt_invariants.py --publish data/state/null-census-latest.json
"""

import argparse
import collections
import datetime
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
T = os.environ.get("ROUTING_DATA_DIR", os.path.join(_REPO, "data", "tables"))

sys.path.insert(0, _HERE)
import data_null_census as census  # noqa: E402  (is_live + STRUCTURAL vocabulary)
import lifecycle_gate  # noqa: E402  (INV3 delegates to the R4 predicate)

#: INV1 default: a live lane may not be >=50% null across the measured
#: (non-structural) columns of its table. Per-table relaxations live in
#: ALLOWED with a reason; every table not listed must clear 0.50.
DEFAULT_MAX = 0.50

#: table -> max allowed live-lane null fraction (INV1). Every entry is a
#: deliberate, reasoned relaxation; an unlisted table gets DEFAULT_MAX.
ALLOWED = {
    # models.dev sync mirror: upstream simply does not publish many fields;
    # the router never reads this table for routing decisions (TR-197
    # STRUCTURAL already covers its declared-absent columns; the headroom
    # absorbs the residual unclassified mirror sparsity).
    "model_catalog.jsonl": 0.60,
}

#: (table, column) pairs allowed to be 100% null (INV2) — each with a reason.
#: Keys are table filenames + column names, matching how the gate reads them.
NULL_OK = {
    ("fallback_lanes.jsonl", "valid_to"):
        "fallback-lane mirror rows keep valid_to explicitly null until a "
        "retirement date exists (TR-197 STRUCTURAL 'lane not retired'); "
        "the carry-the-key-with-null shape is the sync's row contract",
}


def load(tables_dir):
    """{filename: [rows]} for every *.jsonl table (tolerant, like the census)."""
    out = {}
    if not os.path.isdir(tables_dir):
        return out
    for fn in sorted(os.listdir(tables_dir)):
        if not fn.endswith(".jsonl"):
            continue
        rows = []
        with open(os.path.join(tables_dir, fn), errors="replace") as f:
            for line in f:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        out[fn] = rows
    return out


def _is_null(v):
    return v is None or v == ""


def _lane(r):
    return "%s/%s" % (r.get("provider"), r.get("model"))


def check_inv1(tables, allowed=None):
    """INV1 violations: live lanes >= threshold null over measured columns.

    Measured columns = the union of keys carried by the table's LIVE rows,
    minus the table's STRUCTURAL (designed-absence) columns. A cell counts
    null only when the key is present with value None or "" (census rule).
    """
    allowed = ALLOWED if allowed is None else allowed
    out = []
    for fn, rows in sorted(tables.items()):
        structural = set(census.STRUCTURAL.get(fn, {}))
        live = [r for r in rows if isinstance(r, dict) and census.is_live(r)]
        if not live:
            continue
        cols = set()
        for r in live:
            cols |= set(r.keys())
        cols -= structural
        if not cols:
            # every column's null is designed absence — nothing to measure
            continue
        thr = allowed.get(fn, DEFAULT_MAX)
        for r in live:
            null_fields = sorted(k for k in cols if k in r and _is_null(r[k]))
            frac = len(null_fields) / len(cols)
            if frac >= thr:
                out.append({
                    "table": fn,
                    "lane": _lane(r),
                    "provider": r.get("provider"),
                    "model": r.get("model"),
                    "null_fields": null_fields,
                    "null_cells": len(null_fields),
                    "measured_cols": len(cols),
                    "fraction": round(frac, 4),
                    "max_allowed": thr,
                })
    return out


def check_inv2(tables, null_ok=None):
    """INV2 violations: columns 100% null across their carrying rows.

    A column is 'dead' when at least one row carries the key and EVERY
    carrying row's value is None or "". Columns whose null is the designed
    state (TR-197 STRUCTURAL) are exempt; anything else must be in NULL_OK
    with a reason or the gate fails.
    """
    null_ok = NULL_OK if null_ok is None else null_ok
    out = []
    for fn, rows in sorted(tables.items()):
        structural = set(census.STRUCTURAL.get(fn, {}))
        carriers = collections.Counter()
        nulls = collections.Counter()
        for r in rows:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                carriers[k] += 1
                if _is_null(v):
                    nulls[k] += 1
        for k in sorted(carriers):
            if nulls[k] != carriers[k]:
                continue  # at least one real value -> not a dead column
            if k in structural:
                continue  # designed absence (TR-197), not wipe-shape debt
            reason = null_ok.get((fn, k))
            if reason:
                continue  # explicitly allowed, reason on record
            out.append({
                "table": fn,
                "column": k,
                "carriers": carriers[k],
                "null_carriers": nulls[k],
                "fraction": 1.0,
            })
    return out


def check_inv3(tables):
    """INV3 violations: anonymous lifecycle dates — delegated entirely to
    lifecycle_gate.find_offenders (the TR-199 R4 predicate)."""
    out = []
    for fn, rows in sorted(tables.items()):
        stem = fn[: -len(".jsonl")]
        if stem not in lifecycle_gate.TABLE_KEYS:
            continue
        for i, row in lifecycle_gate.find_offenders(stem, rows):
            out.append({
                "table": fn,
                "lane": _lane(row),
                "dated_fields": {k: row.get(k)
                                 for k in lifecycle_gate.DATE_KEYS if row.get(k)},
                "lifecycle_source": lifecycle_gate.extract_source(row),
                "detail": lifecycle_gate.describe_offender(stem, row, i),
            })
    return out


def census_unexplained(tables_dir):
    """Live-lane UNEXPLAINED count, from data_null_census.py --json itself
    (a subprocess, so the published number IS the census's number)."""
    env = dict(os.environ, ROUTING_DATA_DIR=str(tables_dir))
    p = subprocess.run(
        [sys.executable, os.path.join(_HERE, "data_null_census.py"), "--json"],
        capture_output=True, text=True, env=env, cwd=_REPO, timeout=300,
    )
    if p.returncode != 0:
        raise RuntimeError(
            "data_null_census.py --json failed (rc=%d): %s"
            % (p.returncode, (p.stderr or p.stdout)[-500:])
        )
    return json.loads(p.stdout)["live_unexplained_count"]


def git_sha():
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=_REPO, timeout=30,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return "<unknown>"


def publish_state(path, tables_dir, v1, v2, v3):
    """Write the watch-hook state file (AC4). Returns the doc written."""
    doc = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "unexplained": census_unexplained(tables_dir),
        "inv1": len(v1),
        "inv2": len(v2),
        "inv3": len(v3),
    }
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="null-debt PREVENTION invariants (TR-201)")
    ap.add_argument("--json", action="store_true",
                    help="machine report: {invariant: {violations: [...]}}")
    ap.add_argument("--publish", metavar="PATH",
                    help="write the watch-hook state doc to PATH (AC4)")
    args = ap.parse_args(argv)

    tables = load(T)
    violations = {
        "inv1": check_inv1(tables),
        "inv2": check_inv2(tables),
        "inv3": check_inv3(tables),
    }

    if args.publish:
        doc = publish_state(args.publish, T, violations["inv1"],
                            violations["inv2"], violations["inv3"])
        print("published: %s" % json.dumps(doc, ensure_ascii=False))

    if args.json:
        print(json.dumps(
            {k: {"violations": v} for k, v in violations.items()},
            indent=1, ensure_ascii=False))
    else:
        names = {
            "inv1": "INV1 live-lane >=50% null across measured columns",
            "inv2": "INV2 100%-null column without a NULL_OK reason",
            "inv3": "INV3 lifecycle date without lifecycle_source (R4)",
        }
        for k in ("inv1", "inv2", "inv3"):
            v = violations[k]
            print("=== %s: %s ===" % (k, names[k]))
            if not v:
                print("  PASS (0 violations)")
            for x in v:
                if k == "inv1":
                    print("  FAIL %s lane %s: %d/%d measured columns null "
                          "(%.3f >= %.2f); fields: %s"
                          % (x["table"], x["lane"], x["null_cells"],
                             x["measured_cols"], x["fraction"],
                             x["max_allowed"], ", ".join(x["null_fields"])))
                elif k == "inv2":
                    print("  FAIL %s column %s: %d/%d carrying rows null "
                          "(fraction %.2f), no NULL_OK reason"
                          % (x["table"], x["column"], x["null_carriers"],
                             x["carriers"], x["fraction"]))
                else:
                    print("  FAIL %s" % x["detail"])

    bad = sum(1 for v in violations.values() if v)
    if bad:
        print("FAIL: %d invariant(s) violated" % bad, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

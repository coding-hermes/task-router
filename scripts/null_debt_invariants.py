#!/usr/bin/env python3
"""null_debt_invariants.py — null-debt PREVENTION invariants (TR-201).

TR-197 built the census (classify every null MEANINGFUL vs UNEXPLAINED and
count the debt). TR-198 fixed one wipe. TR-201 adds the PREVENTION side: a
gate that fails on the SHAPES that indicate null debt is being created,
before the census ever has to count it. Four invariants over
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
  INV4  (TR-362) no models.jsonl lane is routable (live, per is_live) while
        carrying NO perf_* evidence for ANY of the ten perf categories,
        unless the reason is STATED: every perf field stamped for that
        (provider, model) in null_reasons.jsonl with a reason from the
        AC2 vocabulary, or disabled=true with a non-empty disabled_reason.
        INV4 runs in TWO modes over the same predicate:
          whole-tree (default): count + report only, NEVER fails the gate —
            ~1900 pre-existing perf-less lanes live in the committed table
            and the guard/CI suites run this mode on the whole tree.
          --inv4-diff --base R1 --head R2: the COMMIT gate (wired into
            .githooks/pre-push — this repo has NO pre-commit hook, so
            pre-push is the commit gate). Fails when the range adds or
            changes a violating lane; missing file on either side, or no
            added/changed lane, exits 0.

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
errors. INV4 is EXEMPT from the whole-tree exit code (report-only; the
diff mode is what fails). --json emits
{"inv1"|"inv2"|"inv3"|"inv4": {"violations": [...]}} — inv4 is additive
(TR-362); existing consumers reading inv1/inv2/inv3 are unaffected.
--publish PATH writes the watch-hook state file (timestamp, git sha, the
data_null_census.py --json live-UNEXPLAINED count, per-invariant violation
counts; unchanged shape — inv1/inv2/inv3 only). No cron here — the foreman
wires the schedule.

Usage:
  python3 scripts/null_debt_invariants.py                 # gate (text)
  python3 scripts/null_debt_invariants.py --json          # machine report
  python3 scripts/null_debt_invariants.py --publish data/state/null-census-latest.json
  python3 scripts/null_debt_invariants.py --inv4-diff --base R1 --head R2
                                                          # TR-362 commit gate
"""
from __future__ import annotations

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
from typing import Any

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
    # TR-200 merge: models.available_from KEPT with recorded reason —
    # readers live (router_spawn coming_soon, router_server /v1/models,
    # router_lifecycle digest); no source invents an availability date,
    # null = no announcement exists (census allowlist decision).
    ("models.jsonl", "available_from"):
        "no model availability announcement was ever recorded (0/1906); "
        "readers live (spawn coming_soon / server /v1/models / lifecycle "
        "digest) — null means 'no announcement', populating would invent "
        "dates (TR-200 allowlist decision)",
    # TR-200 merge: providers.valid_to KEPT — retirement readers live
    # (modelsdev skip-retired, release_backfill retire stamps,
    # validate_provider_quota live-roster); null = provider not retired.
    ("providers.jsonl", "valid_to"):
        "no provider retirement was ever recorded (0/29); readers live — "
        "null means 'not retired', populating would invent dates "
        "(TR-200 allowlist decision)",
    ("fallback_lanes.jsonl", "valid_to"):
        "fallback-lane mirror rows keep valid_to explicitly null until a "
        "retirement date exists (TR-197 STRUCTURAL 'lane not retired'); "
        "the carry-the-key-with-null shape is the sync's row contract",
}


def load(tables_dir: Any):
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


def _is_null(v: Any) -> bool:
    return v is None or v == ""


def _lane(r: Any) -> str:
    return "%s/%s" % (r.get("provider"), r.get("model"))


def check_inv1(tables: Any, allowed: Any=None):
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


def check_inv2(tables: Any, null_ok: Any=None):
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


def check_inv3(tables: Any):
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


#: TR-362 — the ten perf categories on models.jsonl. A lane "has perf
#: evidence" when ANY of these carries a non-null, non-empty value; a lane
#: with none of them measured is INV4's subject.
PERF_FIELDS = (
    "perf_agent_tick",
    "perf_long_doc",
    "perf_debug",
    "perf_schema",
    "perf_e2e_vision",
    "perf_review",
    "perf_delegation",
    "perf_guard",
    "perf_mock",
    "perf_reasoning",
)

#: models.jsonl path inside the repo, as handed to `git show <ref>:<path>`.
MODELS_TABLE_PATH = os.path.join("data", "tables", "models.jsonl")


def _canonical_row(r: Any) -> str:
    """Stable text form of a parsed row, for base/head equality."""
    return json.dumps(r, sort_keys=True, ensure_ascii=False)


def _git_show_rows(repo: Any, ref: Any, rel_path: Any):
    """Rows of rel_path AT ref, or None when the path does not exist there.
    Other git failures (bad ref, not a repo) raise RuntimeError -> exit 2."""
    p = subprocess.run(
        ["git", "show", "%s:%s" % (ref, rel_path)],
        capture_output=True, text=True, cwd=repo, timeout=120,
    )
    if p.returncode != 0:
        err = p.stderr or ""
        # git wordings for "path absent AT this ref": "does not exist in
        # '<ref>'" (not on disk either) and "exists on disk, but not in
        # '<ref>'" (present in the working tree, absent at the ref — the
        # common pre-push case for a base that predates the table). Both
        # name the path; anything else (bad ref, not a repo) is an error.
        if (("does not exist" in err or "exists on disk, but not in" in err)
                and rel_path in err):
            return None
        raise RuntimeError(
            "git show %s:%s failed (rc=%d): %s"
            % (ref, rel_path, p.returncode, (p.stderr or p.stdout)[-500:])
        )
    rows = []
    for line in p.stdout.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _inv4_lane_violation(r: Any, stamps: Any):
    """The INV4 predicate for ONE models.jsonl row -> violation dict or None.

    A lane passes when any of:
      * it is not routable (not live: disabled / archived / valid_to),
      * it carries perf evidence (any PERF_FIELDS value non-null),
      * disabled=true WITH a non-empty disabled_reason (stated lane reason),
      * EVERY perf field is stamped for (provider, model) in
        null_reasons.jsonl with a reason from the AC2 vocabulary
        (no-sample-yet and unknown-by-design are legitimate members — the
        gate's job is that the reason is STATED, not inferred).
    A partial stamp set is a violation naming the unstamped fields.
    """
    if not census.is_live(r):
        return None  # not routable -> exempt
    prov, mod = r.get("provider"), r.get("model")
    if any(not _is_null(r.get(f)) for f in PERF_FIELDS):
        return None  # measured capability data exists
    if r.get("disabled") and r.get("disabled_reason"):
        return None  # stated lane-state reason
    unstamped = [f for f in PERF_FIELDS if (prov, mod, f) not in stamps]
    if not unstamped:
        return None  # fully stamped with meaningful reasons
    return {
        "provider": prov,
        "model": mod,
        "lane": _lane(r),
        "missing_fields": unstamped,
    }


def check_inv4(tables: Any, stamps: Any=None):
    """INV4 whole-tree mode: violating LIVE models lanes, count + report.

    NEVER fatal: the committed table holds ~1900 pre-existing perf-less
    lanes ("no sample yet" is exempt in the census), and the guard/CI
    suites run this mode — failing here would red every gate without a
    single new violation. The per-commit enforcement is check_inv4_diff.
    """
    stamps = census.load_stamps() if stamps is None else stamps
    out = []
    for r in tables.get("models.jsonl", []):
        if isinstance(r, dict):
            v = _inv4_lane_violation(r, stamps)
            if v:
                out.append(v)
    return out


def check_inv4_diff(base: Any, head: Any, repo: Any=None,
                    stamps: Any=None, rel_path: Any=MODELS_TABLE_PATH):
    """INV4 diff mode (TR-362 commit gate): violations among lanes ADDED or
    CHANGED between base and head (rows compared canonically by
    (provider, model)). Missing file on either side, or no added/changed
    lane, yields no violations (exit 0)."""
    repo = repo or _REPO
    stamps = census.load_stamps() if stamps is None else stamps
    base_rows = _git_show_rows(repo, base, rel_path)
    head_rows = _git_show_rows(repo, head, rel_path)
    if base_rows is None and head_rows is None:
        return []
    base_map = {
        (r.get("provider"), r.get("model")): r
        for r in (base_rows or []) if isinstance(r, dict)
    }
    head_map = {
        (r.get("provider"), r.get("model")): r
        for r in (head_rows or []) if isinstance(r, dict)
    }
    out = []
    for key, r in head_map.items():
        if key in base_map and _canonical_row(base_map[key]) == _canonical_row(r):
            continue  # unchanged lane — not this commit's debt
        v = _inv4_lane_violation(r, stamps)
        if v:
            v["change"] = "added" if key not in base_map else "changed"
            out.append(v)
    return out


def census_unexplained(tables_dir: Any):
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


def publish_state(path: Any, tables_dir: Any, v1: Any, v2: Any, v3: Any):
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


def main(argv: Any=None) -> int:
    ap = argparse.ArgumentParser(
        description="null-debt PREVENTION invariants (TR-201; INV4 TR-362)")
    ap.add_argument("--json", action="store_true",
                    help="machine report: {invariant: {violations: [...]}}")
    ap.add_argument("--publish", metavar="PATH",
                    help="write the watch-hook state doc to PATH (AC4)")
    ap.add_argument("--inv4-diff", action="store_true",
                    help="TR-362 commit gate: fail (exit 1) when base..head "
                         "adds or changes a routable models.jsonl lane that "
                         "carries no perf evidence and no stated reason")
    ap.add_argument("--base", metavar="REF",
                    help="base ref for --inv4-diff (required with it)")
    ap.add_argument("--head", metavar="REF",
                    help="head ref for --inv4-diff (required with it)")
    ap.add_argument("--repo", metavar="DIR",
                    help="git repo to read base/head from "
                         "(default: this checkout; used by tests)")
    args = ap.parse_args(argv)

    if args.inv4_diff:
        if not args.base or not args.head:
            ap.error("--inv4-diff requires --base REF and --head REF")
        try:
            v4 = check_inv4_diff(args.base, args.head, repo=args.repo)
        except Exception as e:
            print("ERROR: INV4 diff mode failed: %s" % e, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"inv4": {"violations": v4}},
                             indent=1, ensure_ascii=False))
        else:
            print("=== inv4 (TR-362): diff gate %s..%s ==="
                  % (args.base[:12], args.head[:12]))
            if not v4:
                print("  PASS (0 routable perf-less lanes added/changed)")
            for x in v4:
                print("  FAIL %s lane %s: no perf evidence for any category; "
                      "unstamped fields: %s"
                      % (x["change"], x["lane"], ", ".join(x["missing_fields"])))
        if v4:
            print("FAIL: %d routable perf-less lane(s) added/changed; stamp "
                  "every perf field in null_reasons.jsonl (REASONS vocabulary) "
                  "or disable the lane with a disabled_reason."
                  % len(v4), file=sys.stderr)
            return 1
        return 0

    tables = load(T)
    violations = {
        "inv1": check_inv1(tables),
        "inv2": check_inv2(tables),
        "inv3": check_inv3(tables),
        "inv4": check_inv4(tables),  # whole-tree INV4 is REPORT-ONLY
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
            "inv4": "INV4 (TR-362) routable lane, no perf evidence, no stated reason",
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
        v4 = violations["inv4"]
        print("=== inv4: %s ===" % names["inv4"])
        print("  REPORT-ONLY: %d routable perf-less lane(s) in the tree "
              "(TR-362; per-commit enforcement is --inv4-diff, wired into "
              ".githooks/pre-push — this repo has NO pre-commit hook)."
              % len(v4))

    # INV4 is deliberately EXEMPT here: whole-tree counts ~1900 pre-existing
    # perf-less lanes and must not red the guard/CI suites. Only the diff
    # mode (above) fails.
    bad = sum(1 for k in ("inv1", "inv2", "inv3") if violations[k])
    if bad:
        print("FAIL: %d invariant(s) violated" % bad, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

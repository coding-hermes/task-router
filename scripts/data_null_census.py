#!/usr/bin/env python3
"""data_null_census.py — the NULL census (TR-197): every null on a LIVE lane
must carry a reason from the finite MEANINGFUL vocabulary.

AC1  per table + per column: how many values are null, and each null is
     classified MEANINGFUL (with the reason) or UNEXPLAINED.
AC2  MEANINGFUL is a finite, named set (matching data/null_reasons.jsonl):
       disabled / archived / retired-by-date / not-published-by-provider /
       unpriced-pending-sticker / no-sample-yet / unknown-by-design
AC3  every UNEXPLAINED null on a live lane must be filled from a real source
     or stamped — no third option, no invented values.
AC4  the metric is the UNEXPLAINED count on LIVE lanes (must reach 0); the
     census is committed so the number stays re-derivable.

Classification (live lanes, strict — the census mirrors the stamp registry,
it does not re-derive reasons):
  1. STAMPED     data/null_reasons.jsonl names (lane, field) with a reason
                 (the fill script writes the price-class, meta-route and
                 no-source stamps there).
  2. STRUCTURAL  the column's designed absence (STRUCTURAL[table][field]).
  3. UNEXPLAINED -> must be filled or stamped (AC3; the gate fails, AC4).
Non-live lanes classify MEANINGFUL with their lane-state reason
(archived / disabled / retired-by-date).

Exit code is 0 when the live-lane UNEXPLAINED count is 0, 1 otherwise (so CI
and guard lanes can gate on it). --json emits the machine census.

Usage:
  python3 scripts/data_null_census.py            # text report
  python3 scripts/data_null_census.py --json     # machine census
  python3 scripts/data_null_census.py --fail-on-unexplained   # gate mode
"""

import argparse
import collections
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
T = os.environ.get("ROUTING_DATA_DIR", os.path.join(_REPO, "data", "tables"))

#: AC2 — the finite MEANINGFUL vocabulary. data/null_reasons.jsonl 'reason'
#: values must be members of this set.
REASONS = (
    "disabled",
    "archived",
    "retired-by-date",
    "not-published-by-provider",
    "unpriced-pending-sticker",
    "no-sample-yet",
    "unknown-by-design",
)

#: null_reasons.jsonl lives next to the tables dir
STAMP_PATH = os.path.join(os.path.dirname(T), "null_reasons.jsonl")

# --------------------------------------------------------------------------
# per-column classification
#
# STRUCTURAL: the null IS the schema's designed state for that column —
# absence carries its own meaning, the same on live and dead lanes.
STRUCTURAL = {
    # models.jsonl lifecycle/provenance: no date = no retirement/announcement
    # exists; no perf value = no sample measured yet for that category
    "models.jsonl": {
        "available_from": "no availability announcement exists",
        "valid_from": "no import-announcement date exists",
        "valid_to": "lane not retired",
        "replaced_by": "no successor named",
        "disabled": "lane not disabled",
        "disabled_reason": "lane not disabled",
        "note": "no fold/provenance note",
        "perf_agent_tick": "no sample yet",
        "perf_long_doc": "no sample yet",
        "perf_debug": "no sample yet",
        "perf_schema": "no sample yet",
        "perf_e2e_vision": "no sample yet",
        "perf_review": "no sample yet",
        "perf_delegation": "no sample yet",
        "perf_guard": "no sample yet",
        "perf_mock": "no sample yet",
        "perf_reasoning": "no sample yet",
        "thinking": "capability unresearched (never guessed)",
        "vision": "capability unresearched (never guessed)",
        "training_model_level": "training status unresearched (training gate treats "
        "NULL as not-training; never guessed)",
        "training_provider_level": "training status unresearched (training gate "
        "treats NULL as not-training; never guessed)",
        "release_date": "launch date not yet researched (never guessed)",
        "lifecycle_source": "no lifecycle event yet",
        "lifecycle_checked_at": "no lifecycle event yet",
        "plan_tier": "not plan-bucketed (PAYG)",
        "public_cache_read_per_m": "provider does not publish cache rates",
        "public_cache_write_per_m": "provider does not publish cache rates",
    },
    # model_catalog is the models.dev sync mirror: NULL = the upstream doc does
    # not publish the field (never guessed; the router never reads this table
    # for routing decisions)
    "model_catalog.jsonl": {
        "vision": "not published by models.dev for this model",
        "modality": "not published by models.dev for this model",
        "knowledge_cutoff": "not published by models.dev for this model",
        "fetched_at": "record predates timestamped syncs",
        "family": "not published by models.dev for this model",
        "cost_input": "no sticker published",
        "cost_output": "no sticker published",
        "context_window": "not published by models.dev for this model",
        "reasoning": "not published by models.dev for this model",
        "tool_call": "not published by models.dev for this model",
    },
    "archetypes.jsonl": {"notes": "no notes"},
    "benchmarks.jsonl": {
        "valid_from": "pre-provenance battery rows (2026-09-16 "
        "live-battery and earlier imports)"
    },
    "category_levels.jsonl": {"notes": "no notes"},
    "fallback_lanes.jsonl": {"valid_to": "lane not retired"},
    "level_defs.jsonl": {"notes": "no notes"},
    "model_aliases.jsonl": {"note": "no notes"},
    "model_notes.jsonl": {
        "valid_from": "note predates provenance fields",
        "source": "note predates source field",
    },
    "model_perf.jsonl": {"notes": "no notes"},
    "model_tier.jsonl": {"notes": "no notes"},
    "plan_terms.jsonl": {
        "requests": "billing model does not use request counts",
        "rate_per_minute": "billing model has no published rate limit",
        "tokens_per_minute": "billing model has no published rate limit",
        "tokens_per_request": "billing model does not use per-request buckets",
        "included_models": "plan has no published included-models list",
        "plan_cost": "PAYG / no subscription cost",
        "usage_multiplier": "no usage multiplier (1.0)",
        "interval": "not published by the provider",
    },
    "probe_excludes.jsonl": {"note": "no notes"},
    "probe_fixes.jsonl": {"fix_to": "unfixed probe (not-worked yet)"},
    "probe_gaps.jsonl": {
        "candidate": "no replacement candidate identified for this gap",
        "error": "probe never returned an error payload",
    },
    "probe_providers.jsonl": {"note": "no notes"},
    "projects.jsonl": {
        "sensitivity": "project never classified (open by default)",
        "board_type": "no board detected for this project",
        "stack": "stack not inventoried",
    },
    "provider_mappings.jsonl": {
        "replacement": "pattern-map rule: strip/normalize, no replacement string",
    },
    "provider_quota.jsonl": {
        "limit": "limit not published / not numeric",
        "reset_anchor": "reset schedule not published",
        "limit_text": "no prose limit on record",
        "window_notes": "no window notes",
        "readback_docs_url": "no docs readback on record",
        "readback_exact": "no exact readback on record",
        "readback_reset_field": "no reset-field readback on record",
        "readback_auth": "no auth readback on record",
    },
    "provider_rules.jsonl": {
        "valid_to": "rule not retired",
        "explains_probe": "rule not tied to a probe story",
    },
    "providers.jsonl": {
        "valid_to": "provider not retired",
        "api_base_url": "base URL private to the operator (key-based config)",
        "api_key_env": "credential handled outside the registry",
        "concurrency": "concurrency limit not published",
        "quota_unit": "no quota on record",
        "trains_on_hosted": "not published by the provider",
    },
    "quality_estimates.jsonl": {
        "guard": "no guard sample yet",
        "mock": "no mock sample yet",
    },
    # quality_ladder (TR-267): a stage target is either SET or not yet
    # declared — the design says "seeded at 0 pending TR-187/TR-282", so a
    # missing target/definition_cmd is the declared seed state, not debt
    "quality_ladder.jsonl": {
        "stage": "metric rows carry one stage each; a missing stage is the declared seed state",
        "target": "stage seeded without a target yet (TR-187/TR-282 pending)",
        "definition_cmd": "stage seeded without a measuring command yet",
        "note": "stage seeded without a note yet",
        "block": "stage seeded without a blocking rule yet",
        "priority": "stage seeded without a priority yet",
    },
    "sample-outcomes.jsonl": {
        "complexity": "sample row predates the complexity field",
        "profile_id": "sample row predates profile tagging",
        "required_categories": "sample row predates profile tagging",
        "cost_usd": "sample without a cost reading",
        "wall_time_s": "sample without a wall-time reading",
        "success": "sample without a success verdict",
    },
    "task_profiles.jsonl": {
        "max_consecutive_per_provider": "no per-provider streak cap configured",
        "max_total_per_provider": "no per-provider total cap configured",
        "allow_slow": "default (not slow-allowed)",
    },
    "task_profile_requirements.jsonl": {},
    "temporary_discounts.jsonl": {"valid_to": "discount not ended"},
}


def load(name, d=None):
    d = d or T
    out = []
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return out
    with open(p, errors="replace") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def load_stamps():
    """(lane, field) -> reason, validated against the AC2 vocabulary."""
    out = {}
    if not os.path.exists(STAMP_PATH):
        return out
    with open(STAMP_PATH, errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            reason = r.get("reason")
            if reason not in REASONS:
                raise SystemExit(
                    "null_reasons.jsonl carries a reason outside the "
                    "AC2 vocabulary: %r for %s/%s/%s"
                    % (reason, r.get("provider"), r.get("model"), r.get("field"))
                )
            out[(r.get("provider"), r.get("model"), r.get("field"))] = reason
    return out


def is_live(r):
    """live = enabled, not archived, not retired (the TR-197 metric surface)."""
    return not r.get("archive") and not r.get("disabled") and not r.get("valid_to")


def lane_state_reason(r):
    if r.get("archive"):
        return "archived"
    if r.get("disabled"):
        return "disabled"
    if r.get("valid_to"):
        return "retired-by-date"
    return None


def classify(fn, rows, stamps):
    """-> list of dicts, one per null:
    {table, provider, model, field, live, class, reason}

    Live-lane order of precedence (strict — the census mirrors the stamp
    registry, it does not re-derive reasons):
      1. STAMPED      data/null_reasons.jsonl names (lane, field)
      2. STRUCTURAL   the column's designed absence (STRUCTURAL[table][field])
      3. UNEXPLAINED  -> must be filled or stamped (AC3; gate fails, AC4)
    Non-live lanes classify MEANINGFUL with their lane-state reason.
    """
    out = []
    structural = STRUCTURAL.get(fn, {})
    for r in rows:
        live = is_live(r)
        state = lane_state_reason(r)
        prov, mod = r.get("provider"), r.get("model")
        for k, v in r.items():
            if v is not None and v != "":
                continue
            rec = {
                "table": fn,
                "provider": prov,
                "model": mod,
                "field": k,
                "live": live,
                "class": None,
                "reason": None,
            }
            stamp = stamps.get((prov, mod, k))
            if stamp:
                rec["class"] = "MEANINGFUL"
                rec["reason"] = stamp
            elif not live:
                rec["class"] = "MEANINGFUL"
                rec["reason"] = state
            elif k in structural:
                rec["class"] = "MEANINGFUL"
                rec["reason"] = structural[k]
            else:
                rec["class"] = "UNEXPLAINED"
                rec["reason"] = None
            out.append(rec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="the NULL census (TR-197)")
    ap.add_argument("--json", action="store_true", help="machine census on stdout")
    ap.add_argument(
        "--fail-on-unexplained",
        action="store_true",
        help="exit 1 when the live-lane UNEXPLAINED count is not 0",
    )
    args = ap.parse_args(argv)

    stamps = load_stamps()
    all_recs = []
    tables = {}
    for fn in sorted(os.listdir(T)):
        if not fn.endswith(".jsonl"):
            continue
        rows = load(fn)
        if not rows:
            continue
        recs = classify(fn, rows, stamps)
        all_recs.extend(recs)
        cols = collections.Counter(r["field"] for r in recs)
        unexp = sum(1 for r in recs if r["class"] == "UNEXPLAINED")
        tables[fn] = {
            "rows": len(rows),
            "nulls": len(recs),
            "by_column": dict(cols.most_common()),
            "unexplained": unexp,
        }

    live_recs = [r for r in all_recs if r["live"]]
    live_unexp = [r for r in live_recs if r["class"] == "UNEXPLAINED"]
    by_reason = collections.Counter(
        r["reason"] for r in live_recs if r["class"] == "MEANINGFUL"
    )
    unexp_by_col = collections.Counter((r["table"], r["field"]) for r in live_unexp)

    doc = {
        "metric": "live-lane UNEXPLAINED nulls (AC4)",
        "live_unexplained_count": len(live_unexp),
        "meaningful_reasons_vocabulary": list(REASONS),
        "live_nulls_by_reason": dict(by_reason.most_common()),
        "unexplained_by_table_column": {
            "%s/%s" % k: v for k, v in unexp_by_col.most_common()
        },
        "tables": tables,
    }

    if args.json:
        doc["unexplained_rows"] = live_unexp
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        print("=== NULL census (TR-197): meaningful vs unexplained ===")
        print("MEANINGFUL vocabulary (AC2): %s" % ", ".join(REASONS))
        print()
        for fn in sorted(tables):
            t = tables[fn]
            mark = (
                ""
                if not t["unexplained"]
                else "  <<< UNEXPLAINED %d" % t["unexplained"]
            )
            print("  %-34s rows=%-5d nulls=%-6d %s" % (fn, t["rows"], t["nulls"], mark))
            for col, n in t["by_column"].items():
                print("      %-30s %d" % (col, n))
        print()
        print("=== AC4 metric: live-lane UNEXPLAINED nulls ===")
        print("  count: %d" % len(live_unexp))
        for (fn, col), n in unexp_by_col.most_common():
            print("    %s / %s : %d" % (fn, col, n))
        print()
        print("=== live-lane nulls by MEANINGFUL reason (stamped + classified) ===")
        for reason, n in by_reason.most_common():
            print("  %-28s %d" % (reason, n))

    if args.fail_on_unexplained and live_unexp:
        print("FAIL: %d live-lane UNEXPLAINED nulls" % len(live_unexp), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

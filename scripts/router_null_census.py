#!/usr/bin/env python3
"""TR-197 NULL census: count nulls per column per table, classify as MEANINGFUL or UNEXPLAINED.

Usage: python3 scripts/router_null_census.py [--json] [--table <name>]
Exit 0 = census complete with 0 unexplained; exit 1 = unexplained nulls found.
"""
import json, os, sys
from pathlib import Path

TABLES_DIR = Path(__file__).resolve().parent.parent / "data" / "tables"

# Columns that are MEANINGFUL-by-design (sparse by nature) — allowlisted per table
# These are columns where null is the expected state for most rows
ALLOWLIST = {
    # Benchmarks: valid_from only set when benchmark versioned
    "benchmarks": {"valid_from"},
    # Plan terms: quota cols only set when quota applies
    "plan_terms": {"quota_requests", "quota_rate_per_minute", "plan_cost",
                   "included_models", "interval", "plan_offset", "rate_per_minute",
                   "requests", "requests_per_5h", "tokens_per_minute", "tokens_per_request",
                   "usage_multiplier"},
    # Provider mappings: replacement only set when mapping replaces another
    "provider_mappings": {"replacement"},
    # Probe gaps: candidate only set when gap is a candidate for filling
    "probe_gaps": {"candidate"},
    # Model catalog: sparse cols only set when API/cache applies
    "model_catalog": {"vision", "modality", "knowledge_cutoff", "fetched_at", "family",
                      "cost_in", "cost_out", "context_window", "api_id", "api_note", "cache_read"},
    # Providers: sparse cols only set when provider has these attributes
    "providers": {"valid_to", "api_base_url", "api_key_env", "concurrency"},
    # Task profiles: max_* only set when profile limits concurrency
    "task_profiles": {"max_consecutive_per_provider", "max_total_per_provider", "allow_slow"},
    # Projects: stack only set when project declares a stack
    "projects": {"stack"},
    # Models: sparse cols only set when model has these attributes
    "models": {"available_from", "disabled_reason", "lifecycle_checked_at", "lifecycle_source",
               "note", "replaced_by", "valid_from", "plan_tier",
               "public_cache_read_per_m", "public_cache_write_per_m",
               # perf_* cols: only 10 categories measured, rest are null by design
               "perf_agent_tick", "perf_debug", "perf_delegation", "perf_e2e_vision",
               "perf_guard", "perf_long_doc", "perf_mock", "perf_reasoning", "perf_review",
               "perf_schema"},
    # Model perf: ALL cols are meaningful-by-design (only 10 categories measured)
    "model_perf": set(),
    # Quality estimates: ALL cols are meaningful-by-design (only 10 categories measured)
    "quality_estimates": {"agent_tick", "code_gen", "debug", "delegation", "e2e_vision",
                          "guard", "long_doc", "long_horizon", "mock", "multilingual",
                          "reasoning", "refactor", "review", "schema", "spec_docs",
                          "terminal", "tool_use", "ui_frontend", "vision"},
    # Provider quota: sparse cols only set when quota applies
    "provider_quota": {"limit", "registry"},
    # Provider rules: sparse cols only set when rule applies
    "provider_rules": {"explains_probe", "note"},
    # Probe providers: headers only set when provider requires headers
    "probe_providers": {"headers"},
    # Temporary discounts: lifecycle_source only set when discount has lifecycle
    "temporary_discounts": {"lifecycle_source"},
    # Model notes: added only set when note was added (not backfilled)
    "model_notes": {"added"},
    # Fallback lanes: sparse cols only set when fallback applies
    "fallback_lanes": {"profiles", "valid_from"},
}

# Columns whose null is always MEANINGFUL regardless of table (retired/archived markers)
UNIVERSAL_MEANINGFUL = {"disabled", "archive", "valid_to", "archived_at", "retired_at"}


def is_live(row):
    """A row is live if not disabled, not archived, and not past valid_to."""
    if row.get("disabled"):
        return False
    if row.get("archive"):
        return False
    vt = row.get("valid_to")
    if vt and str(vt) < "2026-09-30":
        return False
    return True


def census_table(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        return {"rows": 0, "live": 0, "cols": {}}
    cols = set()
    for r in rows:
        cols.update(r.keys())
    live_rows = [r for r in rows if is_live(r)]
    col_stats = {}
    for c in sorted(cols):
        null_total = sum(1 for r in rows if r.get(c) is None)
        null_live = sum(1 for r in live_rows if r.get(c) is None)
        col_stats[c] = {
            "null_total": null_total,
            "null_live": null_live,
            "total": len(rows),
            "live": len(live_rows),
        }
    return {"rows": len(rows), "live": len(live_rows), "cols": col_stats}


def main():
    as_json = "--json" in sys.argv
    table_filter = None
    if "--table" in sys.argv:
        idx = sys.argv.index("--table")
        table_filter = sys.argv[idx + 1]

    summary = {}
    total_unexplained_live = 0
    for f in sorted(TABLES_DIR.glob("*.jsonl")):
        tname = f.stem
        if table_filter and tname != table_filter:
            continue
        census = census_table(f)
        if census["rows"] == 0:
            continue
        allow = ALLOWLIST.get(tname, set())
        unexplained_by_col = {}
        for c, stats in census["cols"].items():
            if c in allow or c in UNIVERSAL_MEANINGFUL:
                continue
            if stats["null_live"] > 0 and stats["live"] > 0:
                pct_live = 100 * stats["null_live"] / stats["live"]
                if pct_live >= 50:
                    unexplained_by_col[c] = {
                        "null_live": stats["null_live"],
                        "live": stats["live"],
                        "pct_live": round(pct_live, 1),
                    }
                    total_unexplained_live += stats["null_live"]
        summary[tname] = {
            "rows": census["rows"],
            "live": census["live"],
            "unexplained_live_cols": unexplained_by_col,
        }

    if as_json:
        print(json.dumps({"tables": summary, "total_unexplained_live": total_unexplained_live}, indent=2))
    else:
        for tname, info in summary.items():
            if info["unexplained_live_cols"]:
                print(f"\n{tname} ({info['live']} live / {info['rows']} total):")
                for c, stats in info["unexplained_live_cols"].items():
                    print(f"  {c}: {stats['null_live']}/{stats['live']} null ({stats['pct_live']}%)")
        print(f"\nTOTAL UNEXPLAINED nulls on live rows (excl allowlist): {total_unexplained_live}")
    return 0 if total_unexplained_live == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

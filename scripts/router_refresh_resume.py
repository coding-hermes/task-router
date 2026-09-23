#!/usr/bin/env python3
"""TR-086: resume helper for the model-router-refresh cron (a26c4522cfb1).

WHY: the cron is ONE long agent turn (reconfigure → research → seed → export
→ tests → commit). When the turn dies mid-run (2026-09-22 05:51: "Interrupted
by shutdown before terminal completion", root-caused: fallback-chain
exhaustion → turn ended with a pending tool result → local fire fence timed
out → run marked failed), the partial registry edits stay uncommitted on disk
and the next day's run starts from scratch — or worse, commits on top of an
untriaged dirty tree.

CONTRACT (fail-open, like every router tool):
  exit 0 ALWAYS. Prints a JSON resume plan: what is already done (committed
  steps are idempotent — every pipeline step is a full-rebuild sync, so
  re-running never duplicates), what is dirty-but-uncommitted (the partial
  state to finish/commit), and what remains. The cron prompt gains a RESUME
  block built from this output, so an interrupted run continues instead of
  restarting.

Usage:
  python3 scripts/router_refresh_resume.py            # resume plan (JSON)
  python3 scripts/router_refresh_resume.py --commit   # commit the partial
                                                      # deterministic state
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

REGISTRY_TABLES = [
    "data/tables/models.jsonl",
    "data/tables/model_catalog.jsonl",
    "data/tables/providers.jsonl",
    "data/tables/probe_fixes.jsonl",
    "data/tables/probe_gaps.jsonl",
    "data/tables/plan_terms.jsonl",
    "data/tables/provider_rules.jsonl",
    "data/tables/model_notes.jsonl",
    "data/tables/quality_estimates.jsonl",
    "data/tables/fallback_lanes.jsonl",
]

PIPELINE_STEPS = [
    ("phase0_reconfigure", ["python3", "/home/kara/.hermes/scripts/reconfigure.py"]),
    ("phase1_data_quality", ["bash", "scripts/router-data-quality.sh"]),
    ("phase2_seed", ["$PY", "scripts/router_seed.py"]),
    ("phase3_export", ["$PY", "scripts/router_maintain.py", "export"]),
    ("phase4_tests", ["$PY", "-m", "pytest", "-q", "tests/"]),
]


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def snapshot_state():
    """Classify the working tree into resume-relevant buckets."""
    status = _git("status", "--porcelain")
    dirty, untracked = [], []
    for line in status.stdout.splitlines():
        code, path = line[:2], line[3:].strip()
        if code.strip() == "??" or code.startswith("??"):
            untracked.append(path)
        elif path.startswith(".gitreins/"):
            continue  # harness artefacts, not registry state
        else:
            dirty.append({"path": path, "code": code})
    registry_dirty = [d for d in dirty if d["path"] in REGISTRY_TABLES]
    return {"dirty": dirty, "untracked": untracked,
            "registry_dirty": registry_dirty}


def head_info():
    log = _git("log", "-1", "--format=%H %cI %s").stdout.strip()
    parts = log.split(" ", 2)
    return {"commit": parts[0][:12] if parts else None,
            "committed_at": parts[1] if len(parts) > 1 else None,
            "subject": parts[2] if len(parts) > 2 else None}


def last_good_refresh():
    """The last commit that touched the registry tables = last good snapshot."""
    log = _git("log", "-1", "--format=%H %cI %s", "--",
               "data/tables/models.jsonl").stdout.strip()
    parts = log.split(" ", 2)
    return {"commit": parts[0][:12] if parts else None,
            "at": parts[1] if len(parts) > 1 else None,
            "subject": parts[2] if len(parts) > 2 else None}


def build_plan(commit_partial=False):
    state = snapshot_state()
    plan = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "head": head_info(),
        "last_good_registry_commit": last_good_refresh(),
        "resume": {"registry_dirty": state["registry_dirty"],
                   "other_dirty": [d for d in state["dirty"]
                                   if d["path"] not in REGISTRY_TABLES]},
        "steps": {},
    }
    # Which pipeline steps are already satisfied?
    #  - phase1 (data-quality sync) is IDEMPOTENT: every run is a full rebuild
    #    of the catalog/pricing tables; re-running is the resume mechanism.
    #  - phase2/3 (seed+export) likewise regenerate deterministically.
    #  - the ONLY state that must not be lost is the uncommitted registry diff.
    if state["registry_dirty"]:
        plan["steps"]["partial_state"] = (
            "UNCOMMITTED registry rows present — finish the run: re-run "
            "scripts/router-data-quality.sh (idempotent full-rebuild), seed, "
            "export, test, then commit. Never restart from a clean checkout: "
            "that discards the interrupted run's work.")
        if commit_partial:
            add = _git("add", *REGISTRY_TABLES)
            commit = _git("commit", "-m",
                          "data: rescue partial registry state from an "
                          "interrupted refresh (TR-086 resume)")
            plan["rescue_commit"] = {
                "rc": commit.returncode,
                "out": (commit.stdout or commit.stderr).strip()[:200],
            }
    else:
        plan["steps"]["partial_state"] = (
            "clean — nothing to rescue; a fresh run of the pipeline is the "
            "resume (all steps are idempotent full rebuilds)")
    plan["steps"]["idempotent_steps"] = [name for name, _ in PIPELINE_STEPS]
    plan["steps"]["note"] = (
        "Every pipeline step rebuilds from source; a re-run converges to the "
        "same state and cannot duplicate rows. Interruptions lose only the "
        "agent's in-turn research narration, never registry rows: they are "
        "written to data/tables/*.jsonl on disk as the run proceeds.")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="commit uncommitted registry-table changes")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(commit_partial=args.commit)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 0  # fail-open
    print(json.dumps(plan, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
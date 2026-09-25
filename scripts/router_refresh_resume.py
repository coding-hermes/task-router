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

  TR-108: the plan also reports `registry_freshness` (the SAME freshness
  predicate `router validate` enforces, imported — never re-derived) and a
  `self_heal` block {needed, command}; `--commit` additionally EXECUTES the
  re-seed when the tree is the interrupted-cron shape (tables newer than the
  registry beyond the slack, no content match), so the follow-up test phase
  cannot trip the validate gate the way it did on 2026-09-22.

Usage:
  python3 scripts/router_refresh_resume.py            # resume plan (JSON)
  python3 scripts/router_refresh_resume.py --commit   # commit the partial
                                                      # deterministic state
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# TR-108: reuse the validate module's canonical freshness predicate instead of
# re-deriving mtime math here (the FRESHNESS_SLACK_S slack and the content
# tiebreak are part of the definition of stale — re-deriving them drifts).
# Works both as a script (scripts/ is sys.path[0]) and via the test import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import router_validate as rv  # noqa: E402

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


def registry_staleness():
    """The validate module's freshness verdict for the repo tree (TR-108).

    Returns rv.freshness_check()'s dict (ok/detail/lag_s/content_match/stale)
    so the plan reports the SAME predicate `router validate` enforces. Returns
    None when the check itself cannot run (missing tree) — the plan then just
    carries the heal command without a verdict.
    """
    reg, data = str(REPO / 'registry.json'), str(REPO / 'data' / 'tables')
    if not os.path.isdir(data):
        return None
    if not os.path.exists(reg):
        # freshness_check would raise on getmtime; report the missing-registry
        # break explicitly instead (same exit-1 family as validate).
        return {'ok': False, 'detail': f'missing: {reg}', 'lag_s': None,
                'content_match': None, 'stale': None}
    try:
        return rv.freshness_check(reg, data)
    except Exception:  # fail-open: a broken verdict must not kill the plan
        return None


def registry_freshness_blocks_tests():
    """True when the tree is the 2026-09-22 shape: table(s) newer than the
    registry beyond FRESHNESS_SLACK_S with no content match — the shape that
    makes the doctrine test fail. Missing registry.json also blocks (same
    `router validate` exit-1 family)."""
    fresh = registry_staleness()
    if fresh is None:
        return False
    return not fresh.get('ok')


def registry_freshness_detail():
    fresh = registry_staleness()
    if fresh is None:
        return None
    return {'ok': fresh.get('ok'), 'detail': fresh.get('detail'),
            'lag_s': fresh.get('lag_s'),
            'content_match': fresh.get('content_match')}


def self_heal_command():
    """The one-liner the cron RESUME block runs to heal before validate."""
    return ('ROUTER_VALIDATE_HEAL=1 python3 scripts/router_validate.py --heal '
            '--json')


def run_self_heal_seed():
    """Execute the heal: re-seed the repo registry from the committed tables.

    Used by --commit rescue mode (the operator/cron has already decided to act
    on this tree). Fail-open like everything else here; the seed is a
    deterministic full rebuild, so re-running it is safe by construction.
    """
    seed = REPO / 'scripts' / 'router_seed.py'
    if not seed.exists():
        return {'ok': False, 'detail': f'seed script missing: {seed}'}
    try:
        proc = subprocess.run(
            [sys.executable, str(seed)], cwd=str(REPO),
            capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return {'ok': False, 'detail': f'self-heal seed could not run: {exc}'}
    ok = proc.returncode == 0
    return {'ok': ok,
            'detail': ('self-heal seed completed'
                       if ok else
                       f'self-heal seed failed (rc={proc.returncode}): '
                       f'{(proc.stderr or proc.stdout or "")[-300:]}')}


def build_plan(commit_partial=False):
    state = snapshot_state()
    heal_needed = registry_freshness_blocks_tests()
    plan = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "head": head_info(),
        "last_good_registry_commit": last_good_refresh(),
        "registry_freshness": registry_freshness_detail(),
        "self_heal": {
            "needed": heal_needed,
            "command": self_heal_command(),
        },
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
    # TR-108: when the tree is the interrupted-cron shape (tables newer than
    # the registry beyond the freshness slack), the rescue run heals FIRST so
    # the follow-up test phase passes instead of tripping `router validate`.
    if commit_partial and heal_needed:
        plan["self_heal"]["executed"] = run_self_heal_seed()
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
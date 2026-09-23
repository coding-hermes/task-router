"""REVIEW-TR-003 — repo hygiene: guard-log class rule + ledger retention policy.

Two halves of the review row, both pinned executably:

1. Guard logs are OUTPUT. `.gitreins/logs/` must be covered by a CLASS rule in
   the repo-root `.gitignore` and must never be tracked, so a guard run cannot
   dirty the tree. The same goes for the other runtime artifacts GitReins
   manages for itself (the tasks flock file, QA ledger, usage telemetry) — a
   tracked copy of local task state dirties the tree on every task transition.
2. The metrics ledger (`data/metrics.jsonl`) is 1.4 GB of operational telemetry
   whose growth is NOT intended to be unbounded: the retention policy must be
   stated in-repo, and the live ledger itself must stay gitignored.

Why `git check-ignore --no-index`: plain `check-ignore` reports 1 for a path git
already tracks even when a pattern MATCHES it, which would make a
tracked-dirt-file assertion silently vacuous. `--no-index` evaluates the
patterns themselves, so the assertion tests the RULE rather than today's index.

Why the matching SOURCE is asserted: a nested `.gitignore` (or a future
`tests/.gitignore`) could satisfy a bare "is it ignored" check while leaving the
repo-root class rule absent, which is the actual defect this row names.
"""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = shutil.which("git")

#: Retention policy statement required by REVIEW-TR-003 acceptance criterion 3.
POLICY_DOC = os.path.join(REPO, "docs", "decisions",
                          "review-tr-003-metrics-ledger-retention.md")

pytestmark = pytest.mark.skipif(GIT is None, reason="git not available")

# ---------------------------------------------------------------------------
# GitReins runtime artifacts — the class this row is about.
# ---------------------------------------------------------------------------

#: Paths GitReins treats as its own runtime state. Upstream `gitreins install`
#: writes exactly this set into .gitignore (cli.py GITREINS_GITIGNORE_ENTRIES).
GITREINS_RUNTIME_PATHS = (
    ".gitreins/logs/guard-20260923T212431.003041Z.log",  # one file per guard run
    ".gitreins/logs/archive/guard-20260919T120728.526960Z.log.gz",
    ".gitreins/tasks.yaml.lock",       # flock file, every task transition
    ".gitreins/usage.jsonl",           # judge usage telemetry
    ".gitreins/qa-ledger.jsonl",       # QA run ledger (DF-GITREINS-POC-31)
    ".gitreins/config.yaml.bak",       # config rewrite backup
)

#: Runtime/state stores that must be ignored but are NOT gitreins-specific.
RUNTIME_STATE_PATHS = (
    "data/metrics.jsonl",              # TR-021 resolve metrics ledger
    "data/state/outcomes.jsonl",       # TR-049 outcomes store
)

#: Tracked on purpose — a rule broad enough to swallow these is a bug.
TRACKED_BY_DESIGN_PATHS = (
    ".gitreins/config.yaml",           # shared guard/evaluator config
    "data/tables/providers.jsonl",     # committed catalog
    "docs/decisions/review-tr-003-metrics-ledger-retention.md",
)


def _git(*argv):
    return subprocess.run([GIT, *argv], cwd=REPO, capture_output=True,
                          text=True, timeout=60)


def _matching_gitignore(path):
    """Return the .gitignore file whose pattern matches `path`, or None.

    Uses --no-index so a TRACKED path is still evaluated against the patterns.
    """
    p = _git("check-ignore", "--no-index", "-v", "--", path)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    # "<source>:<lineno>:<pattern>\t<pathname>"
    return p.stdout.split("\t", 1)[0].split(":", 1)[0]


def _tracked_paths():
    p = _git("ls-files")
    assert p.returncode == 0, f"git ls-files failed: {p.stderr}"
    return set(p.stdout.splitlines())


def test_gitignore_has_gitreins_log_class_rule():
    """AC2: `.gitreins/logs/` is ignored BY CLASS from the repo-root .gitignore.

    Matches any present or future guard log and the compressed archive under it
    without naming individual run stamps.
    """
    for path in GITREINS_RUNTIME_PATHS[:2]:
        src = _matching_gitignore(path)
        assert src is not None, f"{path} is not ignored — guard runs dirty the tree"
        assert os.path.abspath(src) == os.path.join(REPO, ".gitignore"), (
            f"{path} is ignored by {src}, not the repo-root .gitignore — the class "
            "rule is missing where a fresh clone needs it"
        )


def test_all_gitreins_runtime_artifacts_ignored():
    """The whole managed set, not just the logs dir this row started from."""
    missing = [p for p in GITREINS_RUNTIME_PATHS if _matching_gitignore(p) is None]
    assert not missing, f"GitReins runtime artifacts still ignored-free: {missing}"


def test_runtime_state_stores_ignored():
    """The metrics ledger and the other runtime stores stay out of the repo."""
    missing = [p for p in RUNTIME_STATE_PATHS if _matching_gitignore(p) is None]
    assert not missing, f"runtime state stores not ignored: {missing}"


def test_tracked_files_are_not_ignored():
    """Guard the guard: the class rules must not swallow tracked content."""
    swallowed = [p for p in TRACKED_BY_DESIGN_PATHS
                 if _matching_gitignore(p) is not None]
    assert not swallowed, (
        f"tracked-by-design paths now match an ignore rule: {swallowed}"
    )


def test_no_guard_log_or_ledger_is_tracked():
    """AC1 (the 'shown deleted' half): git holds no guard log or live ledger.

    TR-089/TR-113 untracked `.gitreins/logs/*` on 2026-09-22; a re-add would
    restore the permanent-dirt defect this row reports.
    """
    tracked = _tracked_paths()
    offending = sorted(
        p for p in tracked
        if p.startswith(".gitreins/logs/") or p == "data/metrics.jsonl"
    )
    assert offending == [], (
        f"guard logs / metrics ledger are tracked again: {offending[:5]}"
    )


def test_local_task_state_flock_file_not_tracked():
    """The flock file is runtime state; committing it is a churn source."""
    assert ".gitreins/tasks.yaml.lock" not in _tracked_paths()


# ---------------------------------------------------------------------------
# Metrics ledger retention policy (acceptance criterion 3)
# ---------------------------------------------------------------------------

def test_metrics_retention_policy_is_stated_in_repo():
    """A size/rotation policy decision exists and is anchored in-repo."""
    assert os.path.isfile(POLICY_DOC), (
        "metrics ledger retention policy is not stated anywhere in the repo "
        f"(expected {POLICY_DOC})"
    )
    text = open(POLICY_DOC, encoding="utf-8").read()
    for anchor in ("Retention window: 30 days",
                   "Size ceiling: 2 GiB",
                   "Fail-open"):
        assert anchor in text, f"policy doc is missing its {anchor!r} statement"


def test_metrics_retention_window_covers_documented_query_windows():
    """The window must not be narrower than the windows the CLI documents.

    README documents 24h/7d, and tests/test_metrics.py exercises 30d — a policy
    window below any of those would silently truncate a supported query.
    """
    text = open(POLICY_DOC, encoding="utf-8").read()
    assert "30 days" in text
    for documented in ("24h", "7d", "30d"):
        assert documented in text, (
            f"policy does not reconcile the documented --since {documented} window"
        )

"""TR-175 — the guard graded a failing tree (and then stopped grading at all).

Two defects, one regression file:

1. AMBIENT-REGISTRY tests. Two tests read the generated <repo>/registry.json
   through the scripts' fallback chain (ROUTING_REGISTRY unset ->
   <repo>/registry.json). That file is GITIGNORED machine state
   (`.gitignore: registry.json`) — present only on machines that had seeded
   the checkout — so the tests passed on the dev box and in CI (which runs
   `python3 -m scripts.router_seed` as its own step) but FAILED under the
   bare guard run in a fresh clone or worktree. The guard failed a tree whose
   tests were fine, and the fix is in the tests: the expected values come
   from the COMMITTED tables via the session-seeded fixture, passed as an
   explicit path (the test_selection_evidence.py convention: "hermetic and
   worktree-safe").

2. BUDGET / FAIL-OPEN. The suite grew to 1443 collected tests (917s in CI,
   2045-2244s on the dev box under load) while the guard script ran pytest
   (`-x`, no clock of its own) under gitreins budgets sized for the ~220s
   suite of 2026-09-17 (test_timeout 900, hook_timeout 700 — an inverted
   ladder). The hook clock expired mid-suite and the guard FAILED OPEN
   ("commit allowed to proceed"): commits landed on trees nobody graded.
   The fix: the script owns a clock (coreutils `timeout`, loud exit 1) and
   the config budgets form a monotone ladder around it
   (script 2400 < test_timeout 3000 < hook_timeout 3600), so the innermost
   clock always fires first and a timeout is a graded FAILURE, never a
   silent pass.

Why a script test lives in pytest: the guard is this repo's commit gate —
gitreins runs scripts/gitreins-guard-tests.sh verbatim (a custom,
non-pytest command is never narrowed by diff mode). The only place a
regression can pin the gate itself is the suite the gate runs.
"""
import os
import re
import stat
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "gitreins-guard-tests.sh")
CONFIG = os.path.join(REPO, ".gitreins", "config.yaml")


# ---------------------------------------------------------------------------
# Part 1 — the ambient-registry tests are worktree-safe
# ---------------------------------------------------------------------------

def _test_source(name):
    with open(os.path.join(REPO, "tests", name), encoding="utf-8") as f:
        return f.read()


def test_outcomes_profile_signature_test_is_worktree_safe():
    """The test must not assert through the <repo>/registry.json fallback."""
    src = _test_source("test_outcomes.py")
    body = src.split("def test_profile_signature_is_declared_category_levels", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert "seeded_registry_path" in body, (
        "test_profile_signature_is_declared_category_levels regressed to an "
        "ambient-registry read: it must take the seeded_registry_path fixture")
    # no bare profile_signature(...) call without the explicit path
    for call in re.findall(r"ro\.profile_signature\(([^)]*)\)", body):
        assert "registry_path" in call, f"bare fallback call: profile_signature({call})"


def test_proxy_classify_categories_test_is_worktree_safe():
    """The vocabulary assertion must not read the generated checkout file."""
    src = _test_source("test_proxy_classify.py")
    body = src.split("def test_categories_are_data_driven_not_hardcoded", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert "seeded_registry_path" in body, (
        "test_categories_are_data_driven_not_hardcoded regressed to an "
        "ambient-registry read: it must take the seeded_registry_path fixture")
    assert "os.path.join(rc.REPO, 'registry.json')" not in body, (
        "the live-vocabulary assertion reads the gitignored generated "
        "registry.json — absent in a fresh clone/worktree, so the assertion "
        "degrades to `in []` exactly as before TR-175")


def test_conftest_ships_the_seeded_registry_fixture():
    """The fixture both fixed tests consume must exist and be explicit-path."""
    with open(os.path.join(REPO, "tests", "conftest.py"), encoding="utf-8") as f:
        src = f.read()
    assert "@pytest.fixture(scope=\"session\")" in src
    assert "def seeded_registry_path()" in src
    # the fixture passes the registry as a PATH argument — that is the whole fix
    assert re.search(r"def seeded_registry_path\(\):\n(.*\n)*?.*return\b.*path",
                     src), "fixture must return the registry path"


def test_seeded_registry_carries_the_committed_levels(seeded_registry_path):
    """The values the fixed outcomes-test asserts come from committed data.

    RED without the fixture (and without an ambient registry): the call
    resolves the <repo>/registry.json fallback, missing in a worktree ->
    profile_signature returns None -> assert fires.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import router_outcomes as ro
    sig = ro.profile_signature("P4_SECURITY", registry_path=seeded_registry_path)
    assert sig == {"guard": 0, "review": 0, "security": 2}


# ---------------------------------------------------------------------------
# Part 2 — the script owns a clock, loudly
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def script_exec():
    st = os.stat(SCRIPT)
    assert st.st_mode & stat.S_IXUSR, f"guard script lost its exec bit: {SCRIPT}"
    return SCRIPT


def _run_script(env=None, timeout=120):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                          env=e, timeout=timeout)


def test_script_enforces_its_own_timeout_arm(script_exec):
    """A hung suite dies by the script's clock with a LOUD graded failure.

    RED on the pre-TR-175 script: no `timeout` invocation, so pytest ran
    unbounded and the gitreins HOOK clock was the first thing to fire —
    which fails OPEN (guard_manager.py: every stage checks the hook clock
    and returns "commit allowed to proceed (fail-open)"). The env arm also
    pins that GUARD_SUITE_TIMEOUT is the knob.
    """
    src = open(SCRIPT, encoding="utf-8").read()
    assert re.search(r"\btimeout\b", src), (
        "the script runs pytest unbounded again: the next budget blowout "
        "fails open at the hook layer (commits land ungraded)")
    assert "--kill-after=30s" in src, "timeout must escalate to SIGKILL"
    assert "GUARD_SUITE_TIMEOUT" in src, "the budget must be an overridable knob"

    # BEHAVIORAL RED arm: with a tiny budget the script must exit 1 (graded
    # failure), NOT 124/137 (a timeout code an outer layer may swallow) and
    # NOT 0 (fail-open). Pre-fix this never returned at all — pytest ran to
    # completion regardless of any budget.
    p = _run_script(env={"GUARD_SUITE_TIMEOUT": "20"})
    assert "GUARD_SUITE_TIMEOUT=20s" in (p.stdout + p.stderr), (
        "the timeout message must name the budget that fired")
    assert p.returncode == 1, (
        f"script timeout must be a graded failure (exit 1), got {p.returncode}")


def test_script_rejects_a_non_integer_budget(script_exec):
    """A garbage knob must be refused before anything runs (exit 2)."""
    p = _run_script(env={"GUARD_SUITE_TIMEOUT": "soon"})
    assert p.returncode == 2
    assert "GUARD_SUITE_TIMEOUT" in (p.stdout + p.stderr)


def test_script_runs_the_full_suite_without_stop_at_first_failure(script_exec):
    """The guard command is CI-shaped: full tests/ tree, no `-x`.

    `-x` made the first failure hide how many tests a change actually broke
    (the TR-175 census: 2 worktree failures, 19 raw ones once -x was off).
    """
    src = open(SCRIPT, encoding="utf-8").read()
    m = re.search(r"timeout\s+--kill-after=30s\s+\"\$GUARD_SUITE_TIMEOUT\"\s+\"\$PY\"\s+-m\s+pytest\s+(.*)$", src, re.M)
    assert m, "script must run pytest under its own timeout clock"
    args = m.group(1)
    assert "tests/" in args, "guard must run the tests/ tree (CI parity)"
    assert "-x" not in args, "-x hides the real failure count from the guard output"
    assert "-q" in args


def test_config_budget_ladder_is_monotone():
    """script 2400 < gitreins test_timeout < gitreins hook_timeout.

    RED on the pre-TR-175 config: hook_timeout (700) sat BELOW
    test_timeout (900) — the inverted ladder that let the hook clock kill a
    test lane that still had its own budget left, failing the whole guard
    OPEN. The ladder must stay strictly increasing or the innermost-clock
    contract breaks again.
    """
    with open(CONFIG, encoding="utf-8") as f:
        text = f.read()

    def _int(key):
        m = re.search(rf"^\s*{key}:\s*(\d+)\s*$", text, re.M)
        assert m, f"{key} missing from .gitreins/config.yaml"
        return int(m.group(1))

    script_m = re.search(r'^GUARD_SUITE_TIMEOUT="\$\{GUARD_SUITE_TIMEOUT:-(\d+)\}"',
                         open(SCRIPT, encoding="utf-8").read(), re.M)
    assert script_m, "script default budget missing"
    script_budget = int(script_m.group(1))
    test_timeout = _int("test_timeout")
    hook_timeout = _int("hook_timeout")

    assert script_budget < test_timeout < hook_timeout, (
        f"budget ladder must be strictly increasing so the innermost clock "
        f"fires first: script {script_budget} < test_timeout {test_timeout} "
        f"< hook_timeout {hook_timeout}")


# ---------------------------------------------------------------------------
# Part 3 — the previously-failing tests pass in this very process
# ---------------------------------------------------------------------------

def test_the_two_tr175_tests_pass_on_this_tree():
    """The exact defect the brief names: guard-red, standalone-green.

    RED pre-fix on a worktree (no ambient registry): these two modules'
    ambient tests failed under ANY invocation from a fresh checkout. GREEN
    post-fix: they take the seeded fixture. Running them in-process here
    also proves the session fixture wires up for every consumer.
    """
    for module, test in (
        ("test_outcomes", "test_profile_signature_is_declared_category_levels"),
        ("test_proxy_classify", "test_categories_are_data_driven_not_hardcoded"),
    ):
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"tests/{module}.py::{test}", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=REPO, timeout=600)
        assert p.returncode == 0, (
            f"{module}::{test} fails on this tree:\n{p.stdout[-2000:]}\n{p.stderr[-500:]}")
        assert "1 passed" in p.stdout

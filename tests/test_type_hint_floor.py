"""TR-268 — the type-hint regression floor, wired into the suite.

Reads the committed baseline data/tables/type_hint_baseline.jsonl (generated
by scripts/type_hint_score.py itself — never hand-typed), recomputes the live
score with the instrument's own logic (imported, not copied), and FAILS when
either overall score drops more than 0.2 percentage points below the baseline.

Floor semantics (the law this test pins): the comparison basis is the
ACHIEVED baseline, never an aspirational target. An unreached ladder target
(e.g. "we want 20%") must NEVER fail this suite — only a real regression
below the level the codebase already reached does. The instrument's
check_floor() carries the 0.2pp grace band; the semantic tests below pin it
from both sides so the direction of the grace can never silently flip.

Fast by construction: one AST scan per pytest session (module-scoped
fixtures), zero subprocesses, zero imports of the scanned code.
"""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO / "data" / "tables" / "type_hint_baseline.jsonl"
INSTRUMENT_PATH = REPO / "scripts" / "type_hint_score.py"

#: percentage points of grace below the baseline before the floor bites
FLOOR_GRACE_PP = 0.2


def _load_instrument():
    spec = importlib.util.spec_from_file_location(
        "type_hint_score", INSTRUMENT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def instrument():
    return _load_instrument()


@pytest.fixture(scope="module")
def baseline_rows():
    lines = [
        ln for ln in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    rows = [json.loads(ln) for ln in lines]
    overall = [r for r in rows if r.get("module") == "_overall"]
    assert len(overall) == 1, (
        "type_hint_baseline.jsonl must carry exactly one _overall row"
    )
    return rows, overall[0]


@pytest.fixture(scope="module")
def baseline_overall(baseline_rows):
    return baseline_rows[1]


@pytest.fixture(scope="module")
def live_overall(instrument):
    """ONE scan of the live tree for the whole module."""
    files = instrument.discover_scope(REPO)
    assert files, "scope discovery found no runtime modules"
    modules = instrument.scan_tree(files, REPO)
    return instrument.compute_overall(modules)


# ---------------------------------------------------------------------------
# the floor itself
# ---------------------------------------------------------------------------

def test_fn_score_does_not_regress(live_overall, baseline_overall):
    base = baseline_overall["fn_score"]
    live = live_overall["fn_score"]
    assert live >= base - FLOOR_GRACE_PP, (
        f"fn_score regressed: live {live:.4f}% is more than "
        f"{FLOOR_GRACE_PP}pp below the committed baseline {base:.4f}% "
        f"(measured {baseline_overall.get('measured_at')}). Re-add the "
        "annotations, or regenerate the baseline via "
        "scripts/type_hint_score.py if the drop is intentional."
    )


def test_arg_score_does_not_regress(live_overall, baseline_overall):
    base = baseline_overall["arg_score"]
    live = live_overall["arg_score"]
    assert live >= base - FLOOR_GRACE_PP, (
        f"arg_score regressed: live {live:.4f}% is more than "
        f"{FLOOR_GRACE_PP}pp below the committed baseline {base:.4f}% "
        f"(measured {baseline_overall.get('measured_at')}). Re-add the "
        "annotations, or regenerate the baseline via "
        "scripts/type_hint_score.py if the drop is intentional."
    )


# ---------------------------------------------------------------------------
# pinned floor semantics
# ---------------------------------------------------------------------------

def test_floor_semantics_grace_band(instrument):
    """The 0.2pp grace runs the right direction: at-floor and just-below
    pass, past-grace breaches, and EITHER score breaching alone is enough."""
    at_floor = {"fn_score": 10.0, "arg_score": 10.0}
    ok, _ = instrument.check_floor(at_floor, 10.0)
    assert ok

    inside_grace = {"fn_score": 9.85, "arg_score": 9.85}
    ok, _ = instrument.check_floor(inside_grace, 10.0)
    assert ok

    fn_breach = {"fn_score": 9.75, "arg_score": 10.0}
    ok, failures = instrument.check_floor(fn_breach, 10.0)
    assert not ok
    assert any("fn_score" in f for f in failures)

    arg_breach = {"fn_score": 10.0, "arg_score": 9.70}
    ok, failures = instrument.check_floor(arg_breach, 10.0)
    assert not ok
    assert any("arg_score" in f for f in failures)


def test_baseline_is_measured_not_aspirational(instrument, baseline_overall):
    """The floor's basis must be a MEASURED level: the _overall row's scores
    must be exactly what its own counts imply. A hand-typed aspirational
    number (a ladder target like 20%) would break this — and must never
    ship as the thing the suite enforces."""
    counts = {
        "functions": baseline_overall["functions"],
        "return_annotated": baseline_overall["return_annotated"],
        "args": baseline_overall["args"],
        "args_annotated": baseline_overall["args_annotated"],
    }
    expected = instrument.with_scores(counts)
    assert baseline_overall["fn_score"] == pytest.approx(
        expected["fn_score"], abs=1e-9
    )
    assert baseline_overall["arg_score"] == pytest.approx(
        expected["arg_score"], abs=1e-9
    )
    # the achieved level must be a real denominator, not a vacuous 100.0
    assert baseline_overall["functions"] > 0
    assert baseline_overall["args"] > 0
    assert baseline_overall["tool"] == "ast"


def test_baseline_modules_match_live_scope(instrument, baseline_rows):
    """Every module the baseline measured must still be discovered live —
    a rename/move must not let a module silently escape the floor's sight.
    (New live modules are deliberately allowed: their annotations move the
    overall scores, which the score checks above police.)"""
    rows, _ = baseline_rows
    baseline_modules = {r["module"] for r in rows if r["module"] != "_overall"}
    files = instrument.discover_scope(REPO)
    live_modules = {
        p.resolve().relative_to(REPO).as_posix() for p in files
    }
    escaped = sorted(baseline_modules - live_modules)
    assert not escaped, (
        f"baseline modules no longer in the live scan scope: {escaped}. "
        "Regenerate the baseline via scripts/type_hint_score.py if the "
        "rename/delete is intentional."
    )

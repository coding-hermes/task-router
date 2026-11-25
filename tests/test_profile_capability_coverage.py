"""CF-246 — capability coverage for the code-factory profile plane.

A capability a code-factory-lane profile needs must never reach chain
selection unresolved. Two failure modes are forbidden, and both arms of this
module were proven RED on the pre-fix table (P0_FORE without the
long_horizon row, in a scratch copy — never a committed broken tree):

  ARM 1 (blank-default resolution): a profile requirement row whose category
  NO lane measures falls to the resolver's blank default tier -1
  (router_spawn.py, Bane 2026-08-27) on every lane; a required level above
  that default is unsatisfiable by declaration and the tick dies at chain
  selection. P0_FORE's `math >= -5` row is deliberately NOT this case: the
  level sits at/below the blank default, so the blank default itself serves
  it (foreman resolution 3, CF-246: a declared negative requirement with no
  routable supply is not a default-resolution defect).

  ARM 2 (declared-or-documented): the categories the complexity scorer has
  demonstrably demanded of foreman ticks at tick time must be accounted for
  by the clamp reference profile — declared with a satisfiable level, or
  covered by a documented-unrequired disposition. The red class is
  long_horizon on the pre-fix table: demanded (code-factory tick
  2026-11-05), undeclared, undocumented — the tick hard-failed
  requirements_unmet. tool_use is the documented-unrequired exception: owner
  regime lock, Bane 2026-08-27 (2nd correction), enforced by
  tests/test_regression.py::test_p0_fore_has_no_tool_use_bar.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import router_satisfiability as sat  # noqa: E402


#: The clamp reference profile — scripts/router_server.py::_infer_reference_profile
#: defaults ROUTER_INFER_PROFILE to P0_FORE, "the documented default profile,
#: and the one whose declarations define what a foreman tick actually needs".
REFERENCE_PROFILE = "P0_FORE"

#: Owner regime lock (Bane 2026-08-27, 2nd correction): tool-calling is table
#: stakes in 2026; the foreman profile must not gate on a cliff-artifact
#: category. P0_FORE deliberately does NOT declare tool_use; the lock is
#: enforced by tests/test_regression.py::test_p0_fore_has_no_tool_use_bar.
DOCUMENTED_UNREQUIRED = frozenset({"tool_use"})

#: Where that disposition is documented (must stay truthful on disk).
DISPOSITION_DOC = REPO / "docs" / "p0-fore-tool-use-disposition.md"

#: Categories the scorer has DEMONSTRABLY demanded of foreman ticks at tick
#: time (live evidence, scheduler.db):
#:   long_horizon — code-factory tick 2026-11-05: the inferred-matrix clamp
#:     wanted long_horizon=2 and the tick hard-failed requirements_unmet
#:     because P0_FORE never declared it (CF-246).
#:   tool_use — 2026-10-20: an emitted tool_use>=2 excluded the whole 0
#:     cluster and 503'd the gateway
#:     (router_outcomes.clamp_inferred_levels docstring).
SCORER_DEMANDS_ON_FOREMAN_TICKS = ("long_horizon", "tool_use")


def _code_factory_profiles() -> list:
    """The profiles the code-factory lanes name in data/tables/projects.jsonl.

    Derived from the file (never hand-enumerated): every project id under the
    code-factory family contributes its `profile`.
    """
    rows = []
    with open(REPO / "data" / "tables" / "projects.jsonl") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    profiles = sorted({r.get("profile") for r in rows
                       if str(r.get("id") or "").startswith("code-factory")
                       and r.get("profile")})
    assert profiles, ("no code-factory lanes found in projects.jsonl — the "
                      "file moved or was renamed; fix this derivation, do "
                      "not hand-enumerate the profiles")
    return profiles


def _tables():
    tables = sat.load_tables(str(REPO))
    missing = [k for k in ("models", "tiers", "domain", "requirements")
               if tables.get(k) is None]
    if missing:
        pytest.skip(f"committed tables unreadable in this checkout: {missing}")
    return tables


def test_profile_requirements_never_fall_to_the_blank_default():
    """ARM 1 (CF-246 AC3): a requirement row of a code-factory-lane profile
    whose category no lane measures (capability_index holders empty — every
    lane serves the blank default tier -1) at a required level ABOVE the
    blank default is unsatisfiable by declaration. Levels at or below the
    blank default are served by having no tier row at all and are not a
    defect (the P0_FORE math=-5 case)."""
    tables = _tables()
    idx = sat.capability_index(tables)
    profiles = set(_code_factory_profiles())
    checked = 0
    for row in tables["requirements"]:
        if row.get("task_id") not in profiles:
            continue
        cat, lvl = row.get("category"), row.get("level")
        checked += 1
        ent = idx.get(cat) or {}
        holders = ent.get("holders") or []
        if not holders and lvl > sat.MISSING_TIER_DEFAULT:
            pytest.fail(
                f"{row['task_id']} requires {cat}>={lvl} but no routable lane "
                f"MEASURES {cat}: every lane falls to the blank default tier "
                f"{sat.MISSING_TIER_DEFAULT}, so the rating resolves "
                f"unsatisfiable by declaration. Either declare a level at/below "
                f"the blank default, measure a lane, or document the category "
                f"as intentionally unrequired (CF-246).")
    assert checked, ("no requirement rows matched the code-factory profiles "
                     f"{sorted(profiles)} — requirements table drifted?")


def test_scorer_demanded_categories_are_declared_or_documented_unrequired():
    """ARM 2 (CF-246 AC2): every category the scorer demonstrably demands of
    foreman ticks is either declared by the clamp reference profile with a
    satisfiable level, or covered by the documented-unrequired disposition.
    RED control: on the pre-fix table (P0_FORE without long_horizon) this
    test fails for long_horizon."""
    tables = _tables()
    idx = sat.capability_index(tables)
    declared = {r.get("category"): r.get("level")
                for r in tables["requirements"]
                if r.get("task_id") == REFERENCE_PROFILE}

    # The disposition doc must exist and carry the lock it cites — a constant
    # whose documentation rotted away is the silent gap this module exists to
    # prevent.
    assert DISPOSITION_DOC.is_file(), (
        f"the tool_use disposition doc is missing: {DISPOSITION_DOC}")
    doc_text = DISPOSITION_DOC.read_text()
    assert "2026-08-27" in doc_text and "tool_use" in doc_text, (
        f"{DISPOSITION_DOC} no longer cites the Bane 2026-08-27 tool_use "
        f"regime lock — restore the disposition note")

    for cat in SCORER_DEMANDS_ON_FOREMAN_TICKS:
        if cat in declared:
            lvl = declared[cat]
            best = (idx.get(cat) or {}).get("best_tier")
            satisfiable = ((best is not None and best >= lvl)
                           or lvl <= sat.MISSING_TIER_DEFAULT)
            assert satisfiable, (
                f"{REFERENCE_PROFILE} declares {cat}>={lvl} but the best "
                f"routable tier is {best} — the declaration is unsatisfiable "
                f"and foreman ticks die requirements_unmet (CF-246)")
            continue
        assert cat in DOCUMENTED_UNREQUIRED, (
            f"{REFERENCE_PROFILE} does not declare {cat} and no "
            f"documented-unrequired disposition exists for it — a scorer tick "
            f"demanding {cat} resolves unsatisfiable and dies requirements_"
            f"unmet (the CF-246 red class; see "
            f"tests/test_regression.py::test_p0_fore_has_no_tool_use_bar for "
            f"the tool_use precedent)")

    # The disposition set and the declared set must never overlap: declaring
    # tool_use on P0_FORE would re-arm the cliff-artifact gate the regime
    # lock removed.
    assert "tool_use" not in declared, (
        f"{REFERENCE_PROFILE} must NOT declare tool_use (Bane 2026-08-27 2nd "
        f"correction, regime lock) — see "
        f"tests/test_regression.py::test_p0_fore_has_no_tool_use_bar")

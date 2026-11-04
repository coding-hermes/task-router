"""TR-383 — an unsatisfiable rating must be diagnosable, at seed time and at
request time.

The row, measured 2026-11-03 19:18:49: four code-factory satellite ticks died
with

    HTTP 503  failure_reason=unservable-rating  chain_length=0  ladder=[]
    error: "no chain — no eligible model and no fallback lane could serve"

Nothing in that body named the capability nobody holds or the lane that came
closest, so the operator could not tell "we are one model away" from "an entire
capability class is missing".

Two halves, both proven here:

  REGISTRY (seed time)  every rating the classifier can emit must be servable by
  at least one ROUTABLE lane; the ones that are not are named, with the profiles
  that demand them, and the seed run must not exit 0.

  REQUEST (tick time)   a rating that cannot be served is refused with a
  diagnosis naming the missing capability and the closest lane.

The data below is synthetic on purpose: these tests must not break when the live
registry changes, and the live registry's current `math` gap is covered by the
CLI test at the end which reads the committed tables.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import router_satisfiability as sat  # noqa: E402


# --------------------------------------------------------------------------
# fixtures: a small registry where `math` is held only by an unroutable lane
# --------------------------------------------------------------------------

def _tables(*, math_holder_disabled=True, math_holder_priced=True):
    return {
        "models": [
            # the only lane with a math tier — and it is off/unpriced
            {"provider": "ollama-cloud", "model": "qwen3.5:397b",
             "normalized_price": 1.0 if math_holder_priced else None,
             "disabled": math_holder_disabled},
            # a healthy general lane with no math tier at all
            {"provider": "deepseek", "model": "deepseek-v4-flash",
             "normalized_price": 0.15},
        ],
        "tiers": [
            {"model": "qwen3.5:397b", "category": "math", "tier": 5},
            {"model": "deepseek-v4-flash", "category": "code_gen", "tier": 4},
            {"model": "deepseek-v4-flash", "category": "math", "tier": -5},
        ],
        "domain": [
            {"category": "math", "level": -5},
            {"category": "math", "level": 0},
            {"category": "math", "level": 5},
            {"category": "code_gen", "level": 3},
            {"category": "code_gen", "level": 4},
        ],
        "requirements": [
            {"task_id": "P0_FORE", "category": "math", "level": -5},
            {"task_id": "P0_FORE", "category": "code_gen", "level": 3},
        ],
    }


# --------------------------------------------------------------------------
# REGISTRY half
# --------------------------------------------------------------------------

def test_routability_is_what_makes_a_capability_count():
    """The disabled holder must NOT satisfy the rating — this is the whole row.

    On the live registry the only `math` tier belonged to a row with
    `disabled: true` and `normalized_price: null`. Counting it would have hidden
    the gap forever.
    """
    tables = _tables()
    idx = sat.capability_index(tables)
    # the floor is the blank default (-1): a routable lane with no math row still
    # clears -1, so the MEASURED -5 is not the ceiling anyone feels
    assert idx["math"]["best_tier"] == -1
    # …and the tier-5 holder is reported as BLOCKED, with the reasons named.
    blocked = {(b["model"], tuple(sorted(b["why"])))
               for b in idx["math"]["blocked_holders"]}
    assert ("qwen3.5:397b", ("disabled",)) in blocked, blocked


def test_unsatisfiable_levels_follow_the_resolver_blank_default():
    """Missing tier = -1 (the resolver's documented BLANK default), so `math -5`
    IS served by every lane that simply has no math tier — only levels above the
    default are unservable. A check that treated a missing tier as "no
    capability at all" would report ratings the router serves every day."""
    tables = _tables()
    findings = sat.satisfiability_findings(tables)
    pairs = {(f["category"], f["level"]) for f in findings}
    # deepseek holds math at -5 (measured), and the blank default is -1, so
    # levels 0 and 5 exceed it => unservable; -5 is served
    assert ("math", 0) in pairs and ("math", 5) in pairs
    assert ("math", -5) not in pairs
    # code_gen 3/4 are served by deepseek's measured tier 4
    assert ("code_gen", 3) not in pairs and ("code_gen", 4) not in pairs
    math0 = [f for f in findings if f["category"] == "math" and f["level"] == 0][0]
    assert math0["best_tier"] == -1             # the blank default floor
    assert math0["required_by"] == []           # no profile asks for math 0


def test_summary_names_each_category_once_with_blocked_holders():
    tables = _tables(math_holder_disabled=True, math_holder_priced=False)
    groups = sat.summarise(sat.satisfiability_findings(tables))
    assert len(groups) == 1
    g = groups[0]
    assert g["category"] == "math"
    # -5 stays servable (blank default -1 >= -5), so unservable levels start at 0
    assert g["levels"] == [0, 5]
    # the report names the capability AND why the holder cannot be used
    assert "qwen3.5:397b" in g["reason"]
    assert "disabled" in g["reason"] and "unpriced" in g["reason"]
    assert g["required_by"] == []


def test_a_fully_servable_domain_yields_no_findings():
    tables = _tables(math_holder_disabled=False, math_holder_priced=True)
    assert sat.satisfiability_findings(tables) == []


# --------------------------------------------------------------------------
# REQUEST half
# --------------------------------------------------------------------------

def test_diagnosis_names_the_missing_capability_and_the_closest_lane():
    tables = _tables()
    diag = sat.diagnose_request([("math", 5), ("code_gen", 3)], tables)
    assert diag["missing"] == [{"category": "math", "need": 5, "best_tier": -1}]
    closest = diag["closest"]
    assert closest is not None
    # deepseek-v4-flash meets code_gen and misses only math (its OWN math tier is
    # the measured -5) — the closest miss
    assert closest["model"] == "deepseek-v4-flash"
    assert [(c, lvl, have) for c, lvl, have in closest["unmet"]] == [("math", 5, -5)]
    assert closest["met_categories"] == 1
    summary = diag["summary"]
    assert "math needs 5" in summary and "best routable tier is -1" in summary
    assert "closest lane deepseek/deepseek-v4-flash" in summary


def test_blank_default_serves_a_lenient_bar_even_with_no_math_tier():
    """The negative half of the default rule: with NO math tier anywhere, a
    `math >= -1` bar is still served by every lane (blank default), and only a
    bar above the default is unservable. This is exactly the case that makes a
    naive check cry wolf."""
    tables = _tables()
    tables["tiers"] = [t for t in tables["tiers"] if t["category"] != "math"]
    diag_lenient = sat.diagnose_request([("math", -1)], tables)
    assert diag_lenient["missing"] == []
    diag_strict = sat.diagnose_request([("math", 2)], tables)
    assert diag_strict["missing"] == [{"category": "math", "need": 2, "best_tier": -1}]
    assert "math needs 2" in diag_strict["summary"]
    assert "best routable tier is -1" in diag_strict["summary"]


def test_satisfiable_rating_produces_no_missing_entries():
    tables = _tables()
    diag = sat.diagnose_request([("code_gen", 3)], tables)
    assert diag["missing"] == []


# --------------------------------------------------------------------------
# CLI half — against the COMMITTED tables, which is the live registry shape
# --------------------------------------------------------------------------

#: the module's internal table keys -> the FILES the resolver actually reads.
_TABLE_FILES = {
    "models": "models.jsonl",
    "tiers": "model_tier.jsonl",
    "domain": "category_levels.jsonl",
    "requirements": "task_profile_requirements.jsonl",
    "profiles": "task_profiles.jsonl",
}


def _write_tables(root: Path, tables: dict) -> Path:
    base = root / "data" / "tables"
    base.mkdir(parents=True)
    for name, rows in tables.items():
        with open(base / _TABLE_FILES[name], "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return root


def test_cli_exits_non_zero_and_names_the_gap(tmp_path, capsys):
    root = _write_tables(tmp_path, _tables(math_holder_disabled=True,
                                           math_holder_priced=False))
    rc = sat._main(["--data-home", str(root)])
    out = capsys.readouterr().out
    assert rc == 1, "an unsatisfiable rating must NOT exit 0"
    assert "SATISFIABILITY FAIL" in out
    assert "math" in out and "qwen3.5:397b" in out


def test_cli_exits_zero_on_a_clean_registry(tmp_path, capsys):
    root = _write_tables(tmp_path, _tables(math_holder_disabled=False,
                                           math_holder_priced=True))
    rc = sat._main(["--data-home", str(root)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "SATISFIABILITY OK" in out


def test_cli_refuses_when_tables_are_unreadable(tmp_path, capsys):
    rc = sat._main(["--data-home", str(tmp_path / "nowhere")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot read table" in err


def test_seed_refuses_by_default_and_the_env_var_is_the_only_opt_out(tmp_path):
    """The AC's teeth: running the real seed with NO flag and NO env var must
    exit non-zero and name the gap. Proven as a subprocess so nothing in this
    process (conftest sets the opt-out session-wide) can leak in."""
    import subprocess
    # A MINIMAL environment on purpose. This test runs the real seed subprocess,
    # and inheriting the suite's environment let it reach the user's own data
    # home (~/.local/share/task-router) — which then changed the head some later
    # resolve picked (measured: test_quota_gate's head flipped openai-codex ->
    # deepseek in a full run, while passing in isolation). Everything it needs
    # points at tmp_path, and HOME/XDG point there too so nothing can leak out.
    data = tmp_path / "data" / "tables"
    data.parent.mkdir()
    import shutil
    shutil.copytree(REPO / "data" / "tables", data)
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
                    "PYTHONHOME", "SYSTEMROOT")}
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "TASK_ROUTER_HOME": str(tmp_path / "home"),
        "ROUTER_STATE_DIR": str(tmp_path / "state"),
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
        "ROUTING_NS": str(tmp_path / "ns"),
    })
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    p = subprocess.run([sys.executable,
                        str(REPO / "scripts" / "router_seed.py")],
                       capture_output=True, text=True, env=env, timeout=900)
    assert p.returncode == 1, (
        f"seed without the opt-out must refuse; rc={p.returncode}\n"
        f"stdout tail:\n{p.stdout[-1500:]}\nstderr tail:\n{p.stderr[-800:]}")
    assert "SATISFIABILITY FAIL" in p.stdout and "math" in p.stdout
    assert "Seeding REFUSED" in p.stdout
    # …and the SAME run with the env opt-out seeds loudly and exits 0.
    env["ROUTING_ALLOW_UNSATISFIABLE"] = "1"
    p2 = subprocess.run([sys.executable,
                         str(REPO / "scripts" / "router_seed.py")],
                        capture_output=True, text=True, env=env, timeout=900)
    assert p2.returncode == 0, p2.stderr[-800:]
    assert "seeding anyway" in p2.stdout


def test_committed_registry_state_is_reported_consistently(capsys):
    """The shipped tables are graded, not assumed.

    This asserts the CHECK runs and agrees with itself; it deliberately does not
    assert the fleet is satisfiable, because on 2026-11-03 it is not (`math` has
    one tier row, held by a disabled, unpriced lane). When the registry gains a
    routable math lane this test keeps passing — only the CLI exit code changes.
    """
    tables = sat.load_tables(str(REPO))
    if not tables.get("domain"):
        pytest.skip("no committed category_levels.jsonl in this checkout")
    findings = sat.satisfiability_findings(tables)
    groups = sat.summarise(findings)
    # every finding must be attributable to something concrete
    for g in groups:
        assert g["levels"], g
        assert g["reason"], g
    capsys.readouterr()

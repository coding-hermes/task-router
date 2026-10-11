"""TR-185 — per-category perf-evidence source attribution in router_tier_coverage.py.

The coverage tool reported "terminal: 0/3681 models have perf" style lines
without saying WHERE the evidence comes from. TR-185 joins the tier table to
its EVIDENCE (tables.model_perf, mirrored at data/tables/model_perf.jsonl,
seed-deduped with max-perf-wins and the winning row's `source`):

Hermetic: drives the script against SCRATCH registry/data (the TR-005 /
TR-197 env-override pattern), never the live registry.json. Pins:

  AC1  every ASKED category shows a source breakdown
       (measured=/bench=/family=/estimate=), including zero-evidence ones
  AC2  the WHY-THIN statements are measurements: a category with evidence
       names its measured=/bench= counts; one with NO model_perf rows says
       so with the full zero breakdown (never a guess)
  AC3  proposals are TYPED — EXPANDS EVIDENCE (land benchmarks via
       data/tables/benchmarks.jsonl + router_seed.py) vs RELAXES REQUIREMENT
       (lower the asked level; owner decision); no invented perf values
  AC4  before/after unsatisfiable rate with sample size n: the simulated
       "after" equals an independent recomputation of eligibility under the
       capped requirement, and a POSITIVE case shows a nonzero delta
  AC5  --relax off suppresses the before/after block; --json carries the
       evidence/proposals/relax objects and leaks no private (underscore)
       measure state
  AC6  seed-dedup winner contract: max perf wins and the WINNING row's
       source is reported
"""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
TOOL = os.path.join(SCRIPTS, "router_tier_coverage.py")
PY = sys.executable


def _scratch_registry(tmp_path):
    """Minimal registry tables: 3 lanes, terminal + code_gen evidence only."""
    tables = {
        "models": [
            {"model": "alpha", "archive": None, "available_from": "2020-01-01",
             "disabled": None, "normalized_price": 1.0},
            {"model": "beta", "archive": None, "available_from": "2020-01-01",
             "disabled": None, "normalized_price": 2.0},
            {"model": "gamma", "archive": None, "available_from": "2020-01-01",
             "disabled": None, "normalized_price": 3.0},
        ],
        "model_tier": [
            {"model": "alpha", "category": "terminal", "tier": 4},
            {"model": "alpha", "category": "code_gen", "tier": 1},
            {"model": "beta", "category": "code_gen", "tier": 2},
            {"model": "gamma", "category": "code_gen", "tier": 0},
        ],
        # evidence: alpha/terminal MEASURED 0.95; beta/code_gen BENCH 0.8;
        # gamma/code_gen FAMILY 0.5; two rows for alpha/code_gen where the
        # ESTIMATE (0.9) outranks a bench (0.3) — seed dedup keeps the
        # ESTIMATE row as the winner's source (AC6).
        "model_perf": [
            {"model": "alpha", "category": "terminal", "perf": 0.95,
             "source": "measured", "source_ref": "live-probe"},
            {"model": "beta", "category": "code_gen", "perf": 0.8,
             "source": "bench", "source_ref": "swe-bench"},
            {"model": "gamma", "category": "code_gen", "perf": 0.5,
             "source": "family", "source_ref": "alias:gamma-base"},
            {"model": "alpha", "category": "code_gen", "perf": 0.3,
             "source": "bench", "source_ref": "x"},
            {"model": "alpha", "category": "code_gen", "perf": 0.9,
             "source": "estimate", "source_ref": "CATEGORY_ESTIMATES"},
        ],
        "task_profiles": [],
        "task_profile_requirements": [],
    }
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps({"version": 3, "tables": tables}))
    return str(reg_path)


def _ledger(tmp_path, reqs_rows):
    """outcomes.jsonl with router-proxy rows carrying the given ratings."""
    lines = []
    for i, reqs in enumerate(reqs_rows):
        lines.append(json.dumps({
            "ts": 1700000000 + i, "source_system": "router-proxy",
            "session_id": f"s{i}", "required_categories": reqs,
            "hops_attempted": 0, "provider": "p", "model": "alpha",
            "success": True,
        }))
    p = tmp_path / "outcomes.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _run(tmp_path, registry, ledger, *args):
    env = dict(os.environ)
    env["ROUTING_REGISTRY"] = registry
    env["ROUTER_OUTCOMES"] = ledger
    return subprocess.run(
        [PY, TOOL, *args], capture_output=True, text=True,
        timeout=120, env=env, cwd=REPO,
    )


def _fixtures(tmp_path):
    reg = _scratch_registry(tmp_path)
    # alpha has terminal=4: a rating asking terminal>=4 is served by alpha;
    # one asking terminal>=5 is unsatisfiable (nobody has tier 5).
    ledger = _ledger(tmp_path, [
        {"terminal": 4, "code_gen": 1},   # satisfiable (alpha)
        {"terminal": 5, "code_gen": 3},   # unsatisfiable (terminal 5 > 4)
        {"terminal": 5, "code_gen": 3},   # unsatisfiable
        {"terminal": 5, "code_gen": 3},   # unsatisfiable
    ])
    return reg, ledger


def _parse_evidence(out):
    """{category: {rows, models, measured, bench, family, estimate}} from the
    evidence table (header + rows), parsed positionally."""
    lines = out.splitlines()
    res = {}
    inside = False
    for i, l in enumerate(lines):
        if "evidence sources per category" in l:
            inside = True
            continue
        if not inside:
            continue
        if l.startswith("  model_perf rows"):
            continue
        if l.strip().startswith("category "):      # column header row
            continue
        if not l.startswith("  ") or not l.strip():
            if res:
                break
            continue
        tok = l.split()
        res[tok[0]] = dict(zip(("rows", "models", "measured", "bench",
                                "family", "estimate"),
                               (int(x) for x in tok[1:])))
    return res


def test_ac1_source_breakdown_per_asked_category(tmp_path):
    reg, ledger = _fixtures(tmp_path)
    r = _run(tmp_path, reg, ledger)
    assert r.returncode == 0, r.stderr
    ev = _parse_evidence(r.stdout)
    assert ev["terminal"] == {"rows": 1, "models": 1, "measured": 1,
                              "bench": 0, "family": 0, "estimate": 0}
    assert ev["code_gen"] == {"rows": 4, "models": 3, "measured": 0,
                              "bench": 1, "family": 1, "estimate": 1}


def test_ac2_why_thin_is_measurement(tmp_path):
    """code_gen as the BLOCKER: its why-thin line must state the measured
    counts (no lane live-measured on it, bench rows exist)."""
    reg = _scratch_registry(tmp_path)
    # nobody has code_gen tier 3 -> the row is unsatisfiable, code_gen blocks
    ledger = _ledger(tmp_path, [{"code_gen": 3}])
    r = _run(tmp_path, reg, ledger)
    out = r.stdout
    thin = next(l for l in out.splitlines()
                if "code_gen:" in l and "carry evidence" in l)
    assert "measured=0" in thin and "bench=1" in thin
    assert "no lane has been live-measured on code_gen" in thin
    # positive relax control: capping code_gen 3 -> 1 lets alpha (tier 1) serve
    assert "Lower code_gen tier requirement from 3 to 1" in out
    assert "reduces unsatisfiable by 1 rows" in out
    ba = next(l for l in out.splitlines() if "after :" in l)
    assert "0/1" in ba


def test_ac2b_zero_evidence_category_states_zeros(tmp_path):
    """A category asked but absent from model_perf gets the measured all-zero
    why-thin phrasing (the fleet's 'a NULL carries its reason' law)."""
    reg = _scratch_registry(tmp_path)
    ledger = _ledger(tmp_path, [{"security": 1}])
    r = _run(tmp_path, reg, ledger)
    out = r.stdout
    assert "zero perf evidence" in out
    assert "measured=0, bench=0, family=0, estimate=0" in out
    # and the evidence table shows the category at all zeros
    ev = _parse_evidence(out)
    assert ev["security"] == {"rows": 0, "models": 0, "measured": 0,
                              "bench": 0, "family": 0, "estimate": 0}


def test_ac3_proposals_typed(tmp_path):
    reg, ledger = _fixtures(tmp_path)
    r = _run(tmp_path, reg, ledger)
    out = r.stdout
    # the unsatisfiable rows are blocked by terminal: EXPANDS proposal names
    # the landing path; RELAXES proposal states the owner decision + delta
    exp = [l for l in out.splitlines() if "EXPANDS EVIDENCE" in l]
    rel = [l for l in out.splitlines() if "RELAXES REQUIREMENT" in l]
    assert exp, "at least one EXPANDS EVIDENCE proposal"
    assert rel, "at least one RELAXES REQUIREMENT proposal"
    assert any("benchmarks.jsonl" in l for l in exp)
    assert any("owner decision" in l for l in rel)
    # no invented perf values: a RELAXES line never claims a perf number
    for l in rel:
        assert "perf=" not in l.split("owner decision")[1]


def test_ac4_before_after_matches_independent_recompute(tmp_path):
    """The reported before/after counts equal a fresh, non-memoized eligibility
    walk over the same sample with the capped requirement."""
    reg, ledger = _fixtures(tmp_path)
    r = _run(tmp_path, reg, ledger, "--relax", "terminal:2")
    out = r.stdout
    assert "before/after unsatisfiable rate" in out
    assert "n=4" in out  # sample size stated on the block header
    before_line = next(l for l in out.splitlines() if l.strip().startswith("before:"))
    after_line = next(l for l in out.splitlines() if l.strip().startswith("after :"))
    assert "3/4" in before_line
    assert "3/4" in after_line
    # ask-level distribution among unsat rows is shown as measurement
    assert "L5=3" in out

    # independent recomputation via --json
    rj = _run(tmp_path, reg, ledger, "--relax", "terminal:2", "--json")
    doc = json.loads(rj.stdout)
    assert doc["metric"]["n"] == 4
    assert doc["relax"]["before"]["unsat"] == 3
    assert doc["relax"]["after"]["unsat"] == 3
    assert doc["relax"]["targets"] == {"terminal": 2}


def test_ac4b_drop_category_bound_is_measured(tmp_path):
    """--relax terminal:-1 (drop the category) flips rows blocked ONLY by
    terminal: the simulation must SHOW that effect, not a hardcoded zero."""
    reg = _scratch_registry(tmp_path)
    # blocked purely by terminal 5 (code_gen 1 is served by alpha)
    ledger = _ledger(tmp_path, [
        {"terminal": 5, "code_gen": 1},
        {"terminal": 5, "code_gen": 1},
    ])
    r = _run(tmp_path, reg, ledger, "--relax", "terminal:-1")
    out = r.stdout
    after_line = next(l for l in out.splitlines() if l.strip().startswith("after :"))
    assert "0/2" in after_line
    assert "+2 rows" in out
    rj = _run(tmp_path, reg, ledger, "--relax", "terminal:-1", "--json")
    doc = json.loads(rj.stdout)
    assert doc["relax"]["before"]["unsat"] == 2
    assert doc["relax"]["after"]["unsat"] == 0
    assert doc["relax"]["delta_rows"] == 2


def test_ac5_relax_off_and_json_shape(tmp_path):
    reg, ledger = _fixtures(tmp_path)
    r = _run(tmp_path, reg, ledger, "--relax", "off")
    assert r.returncode == 0
    assert "before/after unsatisfiable rate" not in r.stdout
    rj = _run(tmp_path, reg, ledger, "--json")
    doc = json.loads(rj.stdout)
    for key in ("metric", "coverage", "evidence", "evidence_totals",
                "proposals", "relax"):
        assert key in doc
    assert not [k for k in doc["metric"] if k.startswith("_")]
    assert doc["evidence"]["terminal"]["sources"]["measured"] == 1
    assert doc["evidence"]["code_gen"]["sources"]["estimate"] == 1
    assert doc["evidence_totals"] == {"bench": 2, "estimate": 1,
                                      "family": 1, "measured": 1}


def test_ac6_dedup_winner_source_pinned(tmp_path):
    """Seed dedup contract: max perf wins and the WINNING row's source is
    reported — alpha/code_gen has bench 0.3 vs estimate 0.9; the estimate
    row must be the counted winner, never the generic first row."""
    reg = _scratch_registry(tmp_path)
    ledger = _ledger(tmp_path, [{"code_gen": 1}])
    rj = _run(tmp_path, reg, ledger, "--json")
    doc = json.loads(rj.stdout)
    src = doc["evidence"]["code_gen"]["sources"]
    assert src.get("estimate") == 1  # 0.9 estimate outranks 0.3 bench
    assert src.get("bench") == 1     # beta's 0.8 bench stands alone
    assert src.get("family") == 1    # gamma's 0.5 family stands alone

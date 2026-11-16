"""Declared-unmeasured categories — the honest shape of "we cannot serve this yet".

THE ROW THIS CLOSES (t_math_unservable, 2026-11-15). The seed refused to run on
origin/main: 6 of 924 ratings in the emit domain had no routable lane — all of
`math` levels 0..5. Every evidence card that must seed to propagate its rows was
therefore blocked, and both umbrella cards were sequenced after that wave. The
deadlock was circular: the card that owned the gap had been closed as
"superseded by granular cards", and the granular cards were blocked on it.

The gap itself is real and not a bug in the bands: `math` is a FROZEN absolute
ladder whose lowest band is -5 at min_perf 0.99208, authored against a population
that still contained ollama-cloud/qwen3.5:397b (AIME26 1.0). That lane was
retired 2026-09-25 (disabled, unpriced), leaving a ONE-VALUE population — glm-5.2
at 0.992, 8e-05 below the lowest band. A one-value population cannot be re-derived
by quantile, so re-pinning the bands is not mechanically available.

The three dishonest escapes, and what this module does instead:

  * delete the math ladder        -> hands math to the quantile fallback in the
                                     seed, which rebuilds 11 levels from the same
                                     one-value population;
  * hand-edit category_levels     -> the next seed silently reverts it;
  * --allow-unsatisfiable forever -> a gate that only ever warns.

So the withdrawal is a DECLARED INPUT: data/tables/unmeasured_categories.jsonl,
one row per range, `reason` REQUIRED. The seed withdraws the declared levels from
`category_levels` (the published emit domain) before building model_tier, the
satisfiability gate grades the withdrawn domain and prints the declaration on
every run, and /health publishes it. A declared gap is a decision; an undeclared
one is still a defect and still fails the seed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import router_satisfiability as sat  # noqa: E402

DECL = "unmeasured_categories.jsonl"


def _decl_row(**over):
    row = {"category": "math", "min_level": 0, "max_level": 5,
           "reason": "test: no routable lane measures this", "declared_at": "2026-11-15",
           "declared_by": "test_unmeasured_categories",
           "recheck": "withdraw when a routable lane measures >= 0.996"}
    row.update(over)
    return row


def _write(root: Path, name: str, rows) -> None:
    """Write a table under <root>/data/tables (the data-home shape)."""
    base = root / "data" / "tables"
    base.mkdir(parents=True, exist_ok=True)
    _write_in(base, name, rows)


def _write_in(tables_dir: Path, name: str, rows) -> None:
    """Write a table straight into an existing tables directory."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    with open(tables_dir / name, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _tables(declared=True, *, reason="test: no routable lane measures this"):
    """A registry where math >= 0 is unservable and (maybe) declared."""
    data = {
        "models": [
            {"provider": "ollama-cloud", "model": "qwen3.5:397b",
             "normalized_price": None, "disabled": True},
            {"provider": "deepseek", "model": "deepseek-v4-flash",
             "normalized_price": 0.15},
        ],
        "tiers": [
            {"model": "deepseek-v4-flash", "category": "code_gen", "tier": 4},
            {"model": "deepseek-v4-flash", "category": "math", "tier": -5},
        ],
        "domain": [
            {"category": "math", "level": -5},
            {"category": "math", "level": 0},
            {"category": "math", "level": 5},
            {"category": "code_gen", "level": 4},
        ],
        "requirements": [
            {"task_id": "P0_FORE", "category": "math", "level": -5},
        ],
        "profiles": [],
    }
    if declared:
        data["unmeasured"] = [_decl_row(min_level=0, max_level=5, reason=reason)]
    return data


# --------------------------------------------------------------------------
# the gate: a declared gap is a decision, an undeclared one is a defect
# --------------------------------------------------------------------------

def test_undeclared_gap_is_still_a_finding():
    findings = sat.satisfiability_findings(_tables(declared=False))
    assert {(f["category"], f["level"]) for f in findings} == {("math", 0), ("math", 5)}


def test_declared_gap_is_not_a_finding_but_is_still_reported():
    tables = _tables()
    assert sat.satisfiability_findings(tables) == []
    # …and the withdrawal is visible, not silent: the residue names the pairs
    # still sitting in a domain that predates the declaration.
    residue = sat.declared_gaps(tables)
    assert [(r["category"], r["level"]) for r in residue] == [("math", 0), ("math", 5)]
    assert residue[0]["reason"].startswith("test:")
    assert residue[0]["declared_at"] == "2026-11-15"


def test_a_declaration_never_hides_a_category_it_does_not_cover():
    tables = _tables()
    tables["domain"].append({"category": "code_gen", "level": 5})
    findings = sat.satisfiability_findings(tables)
    assert [(f["category"], f["level"]) for f in findings] == [("code_gen", 5)]


def test_a_servable_level_is_not_reported_as_a_declared_gap():
    """A stale declaration must not hide levels the fleet CAN serve: the tier
    check runs BEFORE the declaration, so math -5 (served at the blank default)
    never shows up as a residue."""
    tables = _tables()
    assert [r["level"] for r in sat.declared_gaps(tables)] == [0, 5]


def test_declared_covers_honours_an_open_ended_range():
    decls = sat.declared_unmeasured({"unmeasured": [_decl_row(min_level=2, max_level=None)]})
    assert sat.declared_covers(decls, "math", 1) is None
    assert sat.declared_covers(decls, "math", 2) is not None
    assert sat.declared_covers(decls, "math", 5) is not None
    assert sat.declared_covers(decls, "code_gen", 5) is None


def test_absent_declaration_file_reads_as_nothing_declared(tmp_path):
    """The pre-declaration behaviour must be byte-identical: no file, no change."""
    _write(tmp_path, "models.jsonl", _tables()["models"])
    _write(tmp_path, "model_tier.jsonl", _tables()["tiers"])
    _write(tmp_path, "category_levels.jsonl", _tables()["domain"])
    tables = sat.load_tables(str(tmp_path))
    assert tables["unmeasured"] is None
    assert sat.declared_unmeasured(tables) == []
    assert len(sat.satisfiability_findings(tables)) == 2


# --------------------------------------------------------------------------
# the CLI says it out loud
# --------------------------------------------------------------------------

def test_cli_prints_the_declaration_even_when_it_passes(tmp_path, capsys):
    _write(tmp_path, "models.jsonl", _tables()["models"])
    _write(tmp_path, "model_tier.jsonl", _tables()["tiers"])
    _write(tmp_path, "category_levels.jsonl", _tables()["domain"])
    _write(tmp_path, "task_profile_requirements.jsonl", _tables()["requirements"])
    _write(tmp_path, "task_profiles.jsonl", [])
    _write(tmp_path, DECL, _tables()["unmeasured"])
    rc = sat._main(["--data-home", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "SATISFIABILITY OK" in out
    assert "DECLARED UNMEASURED: math levels 0..5" in out
    assert "test: no routable lane measures this" in out


def test_cli_json_carries_both_halves(tmp_path, capsys):
    _write(tmp_path, "models.jsonl", _tables()["models"])
    _write(tmp_path, "model_tier.jsonl", _tables()["tiers"])
    _write(tmp_path, "category_levels.jsonl", _tables()["domain"])
    _write(tmp_path, DECL, _tables()["unmeasured"])
    rc = sat._main(["--data-home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["findings"] == []
    assert payload["declared_unmeasured"][0]["category"] == "math"
    assert len(payload["declared_gaps"]) == 2


# --------------------------------------------------------------------------
# /health publishes it
# --------------------------------------------------------------------------

def test_health_publishes_the_declaration(tmp_path):
    import router_health
    _write(tmp_path, "models.jsonl", _tables()["models"])
    _write(tmp_path, DECL, _tables()["unmeasured"])
    h = router_health.health(data_dir=tmp_path / "data" / "tables")
    block = h["unmeasured_categories"]
    assert block["count"] == 1
    assert block["declared"][0]["category"] == "math"
    assert block["declared"][0]["max_level"] == 5
    assert "no routable lane" in block["declared"][0]["reason"]


def test_health_reports_zero_declarations_without_a_file(tmp_path):
    import router_health
    _write(tmp_path, "models.jsonl", _tables()["models"])
    h = router_health.health(data_dir=tmp_path / "data" / "tables")
    assert h["unmeasured_categories"]["count"] == 0
    assert h["unmeasured_categories"]["declared"] == []


def test_health_survives_a_torn_declaration_line(tmp_path):
    import router_health
    _write(tmp_path, "models.jsonl", _tables()["models"])
    base = tmp_path / "data" / "tables"
    with open(base / DECL, "w") as f:
        f.write(json.dumps(_decl_row()) + "\n")
        f.write('{"category": "math", "reason": tor')  # torn line
    h = router_health.health(data_dir=tmp_path / "data" / "tables")
    assert h["unmeasured_categories"]["count"] == 1  # the good row survives


# --------------------------------------------------------------------------
# the SEED: the withdrawal is real, atomic and reasonless-proof
# --------------------------------------------------------------------------

def _seed_env(tmp_path: Path, data: Path):
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
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    return env


def _copy_tables(tmp_path: Path) -> Path:
    data = tmp_path / "data" / "tables"
    data.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / "data" / "tables", data)
    return data


def test_seed_withdraws_declared_levels_and_exits_zero(tmp_path):
    """The whole point: the registry seeds, and the published domain loses
    exactly the declared levels — no --allow-unsatisfiable, no hand-edit."""
    data = _copy_tables(tmp_path)
    with open(data / "category_levels.jsonl") as f:
        before = [json.loads(l) for l in f if l.strip()]
    # Declare a range that IS currently publishable so the withdrawal has teeth
    # whatever the live corpus happens to hold. APPEND to the shipped
    # declaration (replacing it would put the live math gap back and the seed
    # would refuse for the right reason but the wrong test).
    with open(data / DECL) as f:
        decls = [json.loads(l) for l in f if l.strip()]
    decls.append(_decl_row(category="code_gen", min_level=-5, max_level=-5,
                           reason="test: withdraw one code_gen band"))
    _write_in(data, DECL, decls)
    env = _seed_env(tmp_path, data)
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "router_seed.py")],
                       capture_output=True, text=True, env=env, timeout=1800)
    assert p.returncode == 0, (
        f"seed must pass with a declared gap; rc={p.returncode}\n"
        f"stdout tail:\n{p.stdout[-2000:]}\nstderr tail:\n{p.stderr[-1000:]}")
    assert "WITHDRAWN from the emit domain" in p.stdout
    assert "DECLARED UNMEASURED: code_gen level -5" in p.stdout
    assert "SATISFIABILITY OK" in p.stdout
    with open(data / "category_levels.jsonl") as f:
        after = [json.loads(l) for l in f if l.strip()]
    # one row per reasoning_effort variant of that band — every one withdrawn
    gone = [r for r in before
            if r.get("category") == "code_gen" and r.get("level") == -5]
    assert gone, "the fixture band must exist before the run"
    assert len(after) == len(before) - len(gone)
    assert not [r for r in after
                if r.get("category") == "code_gen" and r.get("level") == -5]
    # …the declaration itself is republished as a sidecar, so the published
    # registry and the gate agree about WHY the level is gone.
    with open(data / DECL) as f:
        republished = [json.loads(l) for l in f if l.strip()]
    assert any(r.get("category") == "code_gen"
               and r.get("reason") == "test: withdraw one code_gen band"
               for r in republished), republished


def test_seed_refuses_a_reasonless_declaration(tmp_path):
    """A withdrawal nobody can explain is the silent gap this input exists to
    prevent — so the seed refuses it rather than obeying it."""
    data = _copy_tables(tmp_path)
    _write_in(data, DECL, [{"category": "math", "min_level": 0, "max_level": 5,
                         "reason": ""}])
    env = _seed_env(tmp_path, data)
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "router_seed.py")],
                       capture_output=True, text=True, env=env, timeout=1800)
    assert p.returncode != 0, p.stdout[-1500:]
    msg = p.stdout + p.stderr
    assert "NO reason" in msg and "math" in msg


def test_committed_declaration_matches_the_committed_domain():
    """The shipped corpus must not disagree with itself: every declared level is
    absent from category_levels, and the declaration says why."""
    tables = sat.load_tables(str(REPO))
    if not tables.get("domain"):
        pytest.skip("no committed category_levels.jsonl in this checkout")
    decls = sat.declared_unmeasured(tables)
    if not decls:
        pytest.skip("this checkout declares nothing unmeasured")
    for d in decls:
        assert d["reason"].strip(), d
        assert d["recheck"], d
        covered = [r for r in tables["domain"]
                   if r.get("category") == d["category"]
                   and r.get("level") is not None
                   and r["level"] >= d["min_level"]
                   and (d["max_level"] is None or r["level"] <= d["max_level"])]
        assert not covered, f"{d['category']} levels {d['min_level']}..{d['max_level']} " \
                            f"are declared unmeasured but still published: {covered}"
    # and no tier may be published inside a withdrawn range
    for d in decls:
        stray = [t for t in (tables.get("tiers") or [])
                 if t.get("category") == d["category"]
                 and t.get("tier") is not None
                 and t["tier"] >= d["min_level"]
                 and (d["max_level"] is None or t["tier"] <= d["max_level"])]
        assert not stray, stray[:3]

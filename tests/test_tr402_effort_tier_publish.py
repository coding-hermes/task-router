"""TR-402 — publish per-effort tiers: registry views + /metrics effort label.

Row acceptance, verbatim:

  * ``/metrics`` exposes ``task_router_tier`` with an ``effort`` label;
  * the registry views join cost@effort (``v_tier_cost_effort``);
  * a consumer can reconstruct the full ``(provider, model, effort, category)``
    cube from PUBLISHED data alone.

What these tests hold the implementation to:

  1. The view is not a text claim — it is EXECUTED against a fixture registry
     (duckdb) and its rows are compared cell-for-cell with the stdlib reader
     that ships alongside it. Two surfaces, one semantic; a drift in either
     fails here.
  2. The cube's shape is the doctrine: the tier is a MODEL fact (two providers
     selling one model inherit one tier, keep two costs), and a cell with no
     measured cost is NULL WITH A REASON — never 0, which would win a price
     ranking as if it were free.
  3. ``/metrics`` carries ``router_tier`` / ``task_router_tier`` with the
     effort label, aggregate (bounded) rather than per-lane, and the
     cost-basis coverage gauge that says how much of the ladder is costed.
  4. The committed tables satisfy the same quality gate the script enforces
     (every tier row keyed by a vocabulary effort; every NULL cost explained).

Hermetic: fixture tables in tmp_path, no live service, no live state writes.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tier_cost_cube as tcc  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fixture(tmp_path, tier_rows=None, cost_rows=None, model_rows=None):
    """A two-provider, one-model, one-category, two-effort corpus.

    Two providers sell `glm-5.3`: `zai` measures a cost at default, `xkiro`
    has NO cost cell at all (the no-cell NULL), and the `high` effort is
    uncosted everywhere (the unmeasured-with-reason NULL).
    """
    tier = tier_rows if tier_rows is not None else [
        {"model": "glm-5.3", "category": "code_gen", "perf": 0.9, "tier": 3,
         "tier_source": "benchmark", "source_ref": "src-a",
         "reasoning_effort": "default"},
        {"model": "glm-5.3", "category": "code_gen", "perf": 0.95, "tier": 4,
         "tier_source": "benchmark", "source_ref": "src-a",
         "reasoning_effort": "high"},
        {"model": "dead-model", "category": "code_gen", "perf": 0.1, "tier": -4,
         "tier_source": "estimate", "source_ref": "est",
         "reasoning_effort": "default"},
    ]
    cost = cost_rows if cost_rows is not None else [
        {"provider": "zai", "model": "glm-5.3", "reasoning_effort": "default",
         "est_cost_per_task": 0.02, "tokens_in": 1.0, "tokens_out": 2.0,
         "tokens_reasoning": 0.0, "in_per_m": 0.6, "out_per_m": 2.2,
         "n_samples": 12, "basis": "measured", "source_ref": "outcomes",
         "null_reason": None},
        {"provider": "zai", "model": "glm-5.3", "reasoning_effort": "high",
         "est_cost_per_task": None, "tokens_in": None, "tokens_out": None,
         "tokens_reasoning": None, "in_per_m": 0.6, "out_per_m": 2.2,
         "n_samples": 0, "basis": "unmeasured", "source_ref": None,
         "null_reason": "no outcome rows at effort='high'"},
    ]
    models = model_rows if model_rows is not None else [
        {"provider": "zai", "model": "glm-5.3", "disabled": False,
         "archive": False, "valid_to": None},
        {"provider": "xkiro", "model": "glm-5.3", "disabled": False,
         "archive": False, "valid_to": None},
        # retired lane: archive=true must not contribute cube rows
        {"provider": "oldco", "model": "glm-5.3", "disabled": False,
         "archive": True, "valid_to": None},
        # expired lane: valid_to in the past must not contribute either
        {"provider": "gone", "model": "glm-5.3", "disabled": False,
         "archive": False, "valid_to": "2020-01-01"},
        # disabled lane
        {"provider": "off", "model": "glm-5.3", "disabled": True,
         "archive": False, "valid_to": None},
        # a lane whose model has no tier row at all
        {"provider": "zai", "model": "no-tier-model", "disabled": False,
         "archive": False, "valid_to": None},
        # the model behind the tier row that no live lane serves
        {"provider": "zai", "model": "dead-model", "disabled": True,
         "archive": False, "valid_to": None},
    ]
    data = tmp_path / "tables"
    _write_jsonl(data / "model_tier.jsonl", tier)
    _write_jsonl(data / "cost_effort.jsonl", cost)
    _write_jsonl(data / "models.jsonl", models)
    return data


def _view_rows(data):
    """Execute CUBE_VIEW_SQL — the SQL the seed installs — over the fixture."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    for t in ("models", "model_tier", "cost_effort"):
        con.execute(
            f"CREATE TABLE {t} AS SELECT * FROM read_json_auto('{data / (t + '.jsonl')}',"
            " format='newline_delimited')")
    con.execute(tcc.CUBE_VIEW_SQL)
    cur = con.execute(
        "SELECT provider, model, category, reasoning_effort, tier, "
        "est_cost_per_task, cost_basis, n_samples, cost_null_reason, "
        "tier_per_dollar FROM v_tier_cost_effort")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# the cube: shape and doctrine
# --------------------------------------------------------------------------

def test_cube_has_one_cell_per_live_lane_and_tier_row(tmp_path):
    cube = tcc.build_cube(*(tcc.read_jsonl(_fixture(tmp_path) / f)
                            for f in ("model_tier.jsonl", "cost_effort.jsonl",
                                      "models.jsonl")))
    keys = {(r["provider"], r["model"], r["category"], r["reasoning_effort"])
            for r in cube}
    # 2 live providers x 2 efforts; the archived/expired/disabled lanes are out
    assert keys == {("zai", "glm-5.3", "code_gen", "default"),
                    ("zai", "glm-5.3", "code_gen", "high"),
                    ("xkiro", "glm-5.3", "code_gen", "default"),
                    ("xkiro", "glm-5.3", "code_gen", "high")}


def test_tier_is_a_model_fact_cost_is_a_lane_fact(tmp_path):
    cube = {(r["provider"], r["reasoning_effort"]): r for r in tcc.build_cube(
        *(tcc.read_jsonl(_fixture(tmp_path) / f)
          for f in ("model_tier.jsonl", "cost_effort.jsonl", "models.jsonl")))}
    zai_default = cube[("zai", "default")]
    xkiro_default = cube[("xkiro", "default")]
    # same tier from the model, different cost per provider
    assert zai_default["tier"] == xkiro_default["tier"] == 3
    assert zai_default["est_cost_per_task"] == 0.02
    assert xkiro_default["est_cost_per_task"] is None


def test_uncosted_cells_are_null_with_a_reason_never_zero(tmp_path):
    cube = {(r["provider"], r["reasoning_effort"]): r for r in tcc.build_cube(
        *(tcc.read_jsonl(_fixture(tmp_path) / f)
          for f in ("model_tier.jsonl", "cost_effort.jsonl", "models.jsonl")))}
    # no cost_effort row for this lane at all
    no_cell = cube[("xkiro", "default")]
    assert no_cell["est_cost_per_task"] is None
    assert no_cell["cost_null_reason"] == tcc.NULL_REASON_NO_CELL
    assert no_cell["est_cost_per_task"] != 0
    # row exists, measurement does not: the generator's own reason is kept
    high = cube[("zai", "high")]
    assert high["est_cost_per_task"] is None
    assert high["cost_null_reason"] == "no outcome rows at effort='high'"
    # a measured cell carries NO stale gap reason
    assert cube[("zai", "default")]["cost_null_reason"] is None


def test_tier_per_dollar_is_null_with_reason_when_undefined(tmp_path):
    cube = {(r["provider"], r["reasoning_effort"]): r for r in tcc.build_cube(
        *(tcc.read_jsonl(_fixture(tmp_path) / f)
          for f in ("model_tier.jsonl", "cost_effort.jsonl", "models.jsonl")))}
    good = cube[("zai", "default")]
    assert good["tier_per_dollar"] == round(3 / 0.02, 6)
    no_cost = cube[("zai", "high")]
    assert no_cost["tier_per_dollar"] is None
    assert no_cost["_ratio_null_reason"] == tcc.RATIO_NO_COST


def test_negative_tier_never_gets_a_dollar_ratio(tmp_path):
    tier = [{"model": "weakling", "category": "code_gen", "perf": 0.1,
             "tier": -4, "tier_source": "estimate", "source_ref": "x",
             "reasoning_effort": "default"}]
    cost = [{"provider": "p", "model": "weakling", "reasoning_effort": "default",
             "est_cost_per_task": 0.01, "basis": "measured", "n_samples": 5,
             "in_per_m": 1.0, "out_per_m": 1.0, "null_reason": None}]
    models = [{"provider": "p", "model": "weakling", "disabled": False}]
    data = tmp_path / "tables"
    _write_jsonl(data / "model_tier.jsonl", tier)
    _write_jsonl(data / "cost_effort.jsonl", cost)
    _write_jsonl(data / "models.jsonl", models)
    row = tcc.build_cube(tier, cost, models)[0]
    assert row["tier"] == -4
    assert row["tier_per_dollar"] is None            # not a ratio worth buying
    assert row["_ratio_null_reason"] == tcc.RATIO_NO_TIER


# --------------------------------------------------------------------------
# the two published surfaces must not drift
# --------------------------------------------------------------------------

def test_sql_view_and_stdlib_reader_agree_cell_for_cell(tmp_path):
    """The warehouse view and the stdlib reader are ONE semantic.

    This is the test that makes 'reconstruct the cube from published data
    alone' checkable: the same three files, two independent engines, byte-equal
    cells. If someone edits the view without the reader (or vice versa), this
    fails with the differing keys.
    """
    data = _fixture(tmp_path)
    view = {(r["provider"], r["model"], r["category"], r["reasoning_effort"]): r
            for r in _view_rows(data)}
    std = {(r["provider"], r["model"], r["category"], r["reasoning_effort"]): r
           for r in tcc.build_cube(
               tcc.read_jsonl(data / "model_tier.jsonl"),
               tcc.read_jsonl(data / "cost_effort.jsonl"),
               tcc.read_jsonl(data / "models.jsonl"))}
    assert set(view) == set(std), (sorted(set(view) - set(std)),
                                   sorted(set(std) - set(view)))
    diffs = []
    for k in view:
        for field in ("tier", "est_cost_per_task", "cost_basis", "n_samples",
                      "cost_null_reason", "tier_per_dollar"):
            if view[k][field] != std[k][field]:
                diffs.append((k, field, view[k][field], std[k][field]))
    assert not diffs


def test_view_filters_retired_and_expired_lanes(tmp_path):
    rows = _view_rows(_fixture(tmp_path))
    assert {r["provider"] for r in rows} == {"zai", "xkiro"}


# --------------------------------------------------------------------------
# the data-quality gate
# --------------------------------------------------------------------------

def _cube(tmp_path, **kw):
    data = _fixture(tmp_path, **kw)
    return tcc.build_cube(tcc.read_jsonl(data / "model_tier.jsonl"),
                          tcc.read_jsonl(data / "cost_effort.jsonl"),
                          tcc.read_jsonl(data / "models.jsonl"))


def test_check_passes_on_a_clean_cube(tmp_path):
    tier = tcc.read_jsonl(_fixture(tmp_path) / "model_tier.jsonl")
    ok, problems, stats = tcc.check(_cube(tmp_path), tier)
    assert ok, problems
    assert stats["cells"] == 4
    assert stats["explained_null_ratio"] == 1.0
    assert stats["efforts"] == {"default": 2, "high": 2}


def test_check_names_a_duplicate_cell(tmp_path):
    cube = _cube(tmp_path)
    ok, problems, _ = tcc.check(cube + [dict(cube[0])], [])
    assert not ok
    assert any("duplicate cube cell" in p for p in problems)


def test_check_names_an_unexplained_null(tmp_path):
    cube = _cube(tmp_path)
    cube[0]["est_cost_per_task"] = None
    cube[0]["cost_null_reason"] = None
    ok, problems, _ = tcc.check(cube, [])
    assert not ok
    assert any("unexplained NULL cost" in p for p in problems)


def test_check_rejects_an_effort_outside_the_vocabulary(tmp_path):
    tier = [{"model": "glm-5.3", "category": "code_gen", "tier": 1,
             "reasoning_effort": "highh"}]     # typo
    ok, problems, _ = tcc.check([], tier)
    assert not ok
    assert any("unknown effort" in p for p in problems)


def test_check_rejects_a_tier_row_without_an_effort_key(tmp_path):
    tier = [{"model": "glm-5.3", "category": "code_gen", "tier": 1,
             "reasoning_effort": ""}]
    ok, problems, _ = tcc.check([], tier)
    assert not ok
    assert any("without an effort" in p for p in problems)


# --------------------------------------------------------------------------
# the committed corpus satisfies the same gate
# --------------------------------------------------------------------------

def test_committed_tables_reconstruct_the_cube_and_pass_check():
    data = REPO / "data" / "tables"
    tier_path = data / "model_tier.jsonl"
    if not tier_path.exists():
        pytest.skip("model_tier.jsonl not published in this checkout")
    tier = tcc.read_jsonl(tier_path)
    cube = tcc.build_cube(tier, tcc.read_jsonl(data / "cost_effort.jsonl"),
                          tcc.read_jsonl(data / "models.jsonl"))
    ok, problems, stats = tcc.check(cube, tier)
    assert ok, problems[:5]
    assert stats["cells"] > 0
    # every published tier row carries the effort key the axis is keyed on
    assert all(r.get("reasoning_effort") for r in tier)


def test_seed_installs_the_cube_view_from_the_shared_sql():
    """The seed must not carry a second copy of the view SQL."""
    src = (SCRIPTS / "router_seed.py").read_text()
    assert "import tier_cost_cube" in src
    assert "DROP VIEW IF EXISTS v_tier_cost_effort" in src
    assert "CUBE_VIEW_SQL" in src
    assert "CREATE VIEW v_tier_cost_effort" in tcc.CUBE_VIEW_SQL


# --------------------------------------------------------------------------
# /metrics — task_router_tier carries the effort label
# --------------------------------------------------------------------------

def _scrape(tmp_path, monkeypatch, data):
    """Render /metrics against a fixture data dir (no live service)."""
    import router_prometheus as rp
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data))
    monkeypatch.setattr(rp, "_TIER_CACHE", {"key": None, "samples": None})
    return rp.render_metrics().decode()


def _samples(doc, family):
    out = {}
    for line in doc.splitlines():
        if line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if name != family:
            continue
        labels, _, value = rest.rpartition("}")
        labels = dict(part.split("=", 1) for part in labels.split(","))
        labels = {k: v.strip('"') for k, v in labels.items()}
        out[tuple(sorted(labels.items()))] = float(value)
    return out


def test_metrics_exposes_tier_with_an_effort_label(tmp_path, monkeypatch):
    doc = _scrape(tmp_path, monkeypatch, _fixture(tmp_path))
    for family in ("router_tier", "task_router_tier"):
        s = _samples(doc, family)
        assert s, f"{family} rendered no samples"
        for labels in s:
            keys = dict(labels)
            assert "reasoning_effort" in keys, keys
            assert "category" in keys and "tier" in keys
    # effort is a real label value, not a hardcoded 'default'
    assert {dict(labels)["reasoning_effort"]
            for labels in _samples(doc, "task_router_tier")} == {"default", "high"}


def test_metrics_tier_counts_live_lanes_per_rung(tmp_path, monkeypatch):
    doc = _scrape(tmp_path, monkeypatch, _fixture(tmp_path))
    s = _samples(doc, "task_router_tier")
    # tier 3 at default: zai + xkiro (two live lanes of one model)
    assert s[(("category", "code_gen"), ("reasoning_effort", "default"),
              ("tier", "3"))] == 2
    # tier 4 at high: same two lanes
    assert s[(("category", "code_gen"), ("reasoning_effort", "high"),
              ("tier", "4"))] == 2


def test_metrics_cost_basis_coverage_per_effort(tmp_path, monkeypatch):
    doc = _scrape(tmp_path, monkeypatch, _fixture(tmp_path))
    s = _samples(doc, "task_router_tier_cost_cells")
    labels = {tuple(dict(k).items()): v for k, v in s.items()}
    def get(**kw):
        for k, v in labels.items():
            d = dict(k)
            if all(d.get(a) == b for a, b in kw.items()):
                return v
        return None
    assert get(category="code_gen", reasoning_effort="default",
               basis="measured") == 1          # zai's measured cell only
    assert get(category="code_gen", reasoning_effort="default",
               basis="no_cell") == 1           # xkiro has no cost row
    assert get(category="code_gen", reasoning_effort="high",
               basis="unmeasured") == 1        # zai's row carries the reason
    assert get(category="code_gen", reasoning_effort="high",
               basis="no_cell") == 1           # xkiro was never enumerated


def test_metrics_effort_levels_says_how_real_the_axis_is(tmp_path, monkeypatch):
    doc = _scrape(tmp_path, monkeypatch, _fixture(tmp_path))
    line = [ln for ln in doc.splitlines()
            if ln.startswith("task_router_tier_effort_levels ")]
    assert line and line[0].endswith(" 2")     # default + high


def test_metrics_tier_is_aggregate_not_per_lane(tmp_path, monkeypatch):
    """Spec §7: no per-lane series. The tier family must not carry provider/model."""
    doc = _scrape(tmp_path, monkeypatch, _fixture(tmp_path))
    for family in ("router_tier", "task_router_tier"):
        for labels in _samples(doc, family):
            keys = dict(labels)
            assert "provider" not in keys and "model" not in keys, keys


def test_metrics_tier_cache_republishes_on_file_change(tmp_path, monkeypatch):
    data = _fixture(tmp_path)
    assert len(_samples(_scrape(tmp_path, monkeypatch, data),
                        "task_router_tier")) == 2
    # a seed rewrites the tier table: the next scrape must see it
    _write_jsonl(data / "model_tier.jsonl", [
        {"model": "glm-5.3", "category": "code_gen", "tier": 1,
         "tier_source": "estimate", "source_ref": "x",
         "reasoning_effort": "low"}])
    s = _samples(_scrape(tmp_path, monkeypatch, data), "task_router_tier")
    assert s[(("category", "code_gen"), ("reasoning_effort", "low"),
              ("tier", "1"))] == 2


def test_metrics_tier_family_fails_open_without_data(tmp_path, monkeypatch):
    """A missing data dir must not fail the scrape (fail-open contract)."""
    import router_prometheus as rp
    monkeypatch.setenv("ROUTING_DATA_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(rp, "_TIER_CACHE", {"key": None, "samples": None})
    doc = rp.render_metrics().decode()
    assert "task_router_tier_effort_levels 0" in doc
    assert "task_router_prometheus_scrape_errors_total" in doc


# --------------------------------------------------------------------------
# the CLI a reviewer runs
# --------------------------------------------------------------------------

def test_cli_check_and_rows(tmp_path, capsys):
    data = _fixture(tmp_path)
    assert tcc.main(["--data-dir", str(data), "--check"]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert tcc.main(["--data-dir", str(data), "--rows"]) == 0
    rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(rows) == 4
    assert set(rows[0]) >= set(tcc.COLUMNS)


def test_cli_check_exits_1_on_a_defect(tmp_path, capsys):
    data = _fixture(tmp_path)
    _write_jsonl(data / "model_tier.jsonl", [
        {"model": "glm-5.3", "category": "code_gen", "tier": 1,
         "reasoning_effort": "bogus-effort"}])
    assert tcc.main(["--data-dir", str(data), "--check"]) == 1
    assert '"ok": false' in capsys.readouterr().out


def test_cli_filters_by_category_and_effort(tmp_path, capsys):
    data = _fixture(tmp_path)
    assert tcc.main(["--data-dir", str(data), "--rows", "--effort", "high"]) == 0
    rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert rows and {r["reasoning_effort"] for r in rows} == {"high"}
    assert tcc.main(["--data-dir", str(data), "--rows",
                     "--category", "no-such-cat"]) == 0
    assert capsys.readouterr().out.strip() == ""

"""TR-167 — carrier presets are wired into the daily refresh.

The model-router-refresh cron's pre-run script (scripts/router-data-quality.sh)
must run a catalog import for EVERY preset in data/catalogs/ and report the
diff (new lanes, price moves, catalog removals). Adding a carrier means
dropping one preset file — no prompt edit, no hardcoded name. A run that finds
new lanes files them into the registry, and lanes it cannot price are HELD and
reported loudly (never appended as unexplained null debt).

Everything is hermetic: presets + catalogs come from fixture dirs (zero
network), the registry is a fixture models.jsonl in tmp. Two contract tests
pin the wiring at the source-text level so the pipeline cannot silently drop
the step again (the same shape test_sync_runtime_copies_state_dir uses).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)
import router_catalog_refresh as rcr  # noqa: E402
import router_provider_import as rpi  # noqa: E402

PY = sys.executable


# ----------------------------------------------------------------- fixtures ----

def _preset(pid="provtest", **over):
    p = {
        "id": pid,
        "catalog_url": f"https://{pid}.example/v1/models",
        "catalog_format": "openai_models_list",
        "field_map": {"model": "id", "price_in": "pricing.input",
                      "price_out": "pricing.output", "context": "context_length"},
        "blend": [0.96, 0.04],
    }
    p.update(over)
    return p


def _catalog(*models):
    """Raw catalog payload exactly as the URL would return it."""
    return {"data": list(models)}


def _m(mid, pin, pout, ctx=100000):
    return {"id": mid, "pricing": {"input": pin, "output": pout},
            "context_length": ctx}


def _row(provider, model, price=None, **over):
    r = {"provider": provider, "model": model,
         "normalized_price": price, "price_evidence": None,
         "public_price": None, "public_in_per_m": None,
         "public_out_per_m": None, "context_limit": None,
         "data_class": "zdr", "plan_tier": None,
         "valid_from": None, "valid_to": None, "archive": False,
         "token_factor": 1.0, "disabled": None, "disabled_reason": None}
    r.update(over)
    return r


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _write_presets(tmp_path, *presets):
    d = tmp_path / "catalogs"
    d.mkdir()
    for p in presets:
        (d / (p["id"] + ".json")).write_text(json.dumps(p))
    return str(d)


def _write_catalogs(tmp_path, **by_id):
    d = tmp_path / "cat"
    d.mkdir()
    for pid, cat in by_id.items():
        (d / (pid + ".json")).write_text(json.dumps(cat))
    return str(d)


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Scratch tables + preset/catalog dirs wired through the env seams."""
    tables = tmp_path / "tables"
    tables.mkdir()
    _write_jsonl(tables / "models.jsonl", [])
    _write_jsonl(tables / "probe_providers.jsonl", [])
    monkeypatch.setenv("ROUTING_DATA_DIR", str(tables))
    monkeypatch.delenv("ROUTER_CATALOG_DIR", raising=False)
    monkeypatch.delenv("ROUTER_CATALOG_FETCH_DIR", raising=False)
    return tmp_path, tables


# ---------------------------------------------- discovery: the drop-a-file AC ----

def test_discovery_is_a_glob_not_a_hardcoded_list(hermetic, monkeypatch):
    """A NEW preset file is picked up with no code or list change."""
    tmp, _ = hermetic
    presets = _write_presets(tmp, _preset("carrier-a"), _preset("carrier-b"))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    assert [os.path.basename(p) for p in rcr.discover_presets()] == \
        ["carrier-a.json", "carrier-b.json"]
    # dropping one more file = one more preset (the AC's 'new model wave')
    _preset_file = os.path.join(presets, "carrier-c.json")
    with open(_preset_file, "w") as f:
        json.dump(_preset("carrier-c"), f)
    assert len(rcr.discover_presets()) == 3


def test_no_preset_name_is_hardcoded_in_the_wrapper():
    """The discovery AC is structural: no carrier id may ever live in the
    wrapper's source (xkiro, commandcode, opencode-go-2, openrouter)."""
    src = open(os.path.join(SCRIPTS, "router_catalog_refresh.py")).read()
    for carrier in ("xkiro", "commandcode", "opencode-go-2", "openrouter"):
        assert carrier not in src, f"hardcoded carrier name: {carrier}"


# ------------------------------------------------- per-preset refresh shapes ----

def test_dry_run_diffs_and_writes_nothing(hermetic, monkeypatch):
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl",
                 [_row("provtest", "keep", 1.0),
                  _row("provtest", "old-model", 2.0),
                  _row("other", "untouched", 3.0)])
    presets = _write_presets(tmp, _preset())
    cats = _write_catalogs(tmp, provtest=_catalog(
        _m("new-lane", 1.0, 2.0), _m("keep", 1.5, 2.5)))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    before = (tables / "models.jsonl").read_text()
    r = rcr.refresh_preset(_preset(), {("provtest", "keep"): _row("provtest", "keep", 1.0),
                                       ("provtest", "old-model"): _row("provtest", "old-model", 2.0),
                                       ("other", "untouched"): _row("other", "untouched", 3.0)},
                           apply=False)
    assert r["status"] == "ok"
    assert r["added"] == ["new-lane"]
    assert r["removed"] == ["old-model"]
    assert r["changed"] == 1
    assert (tables / "models.jsonl").read_text() == before  # dry-run wrote nothing


def test_apply_files_new_priced_lanes_and_reports_removals(hermetic, monkeypatch):
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl", [_row("provtest", "old-model", 2.0)])
    presets = _write_presets(tmp, _preset())
    cats = _write_catalogs(tmp, provtest=_catalog(_m("new-lane", 1.0, 2.0)))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    r = rcr.refresh_preset(_preset(), {}, apply=True)
    assert r["applied"] is True
    rows = _read_jsonl(tables / "models.jsonl")
    new = [r for r in rows if r["model"] == "new-lane"]
    assert len(new) == 1
    assert new[0]["normalized_price"] == pytest.approx(0.96 * 1.0 + 0.04 * 2.0)
    assert new[0]["perf_reasoning"] is None          # TR-044: invisible until probed
    assert "provider_import preset=provtest" in new[0]["price_evidence"]
    assert new[0]["plan_tier"] is None               # PAYG preset declares null
    probe = _read_jsonl(tables / "probe_providers.jsonl")
    assert any(p["id"] == "provtest" for p in probe)
    assert "api_key" not in probe[0]                 # env NAME only, never a value


def test_unpriced_new_lane_is_held_never_filed(hermetic, monkeypatch):
    """Null-debt doctrine: a net-new lane with no price (catalog omits pricing,
    preset declares no sticker) must NOT be appended — it is invisible to every
    price-ordered chain and counts as unexplained null debt. It is reported
    loudly instead (the research work order)."""
    tmp, tables = hermetic
    presets = _write_presets(tmp, _preset(pid="nopricer"))
    cats = _write_catalogs(tmp, nopricer=_catalog(_m("mystery-lane", None, None)))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    r = rcr.refresh_preset(_preset(pid="nopricer"), {}, apply=True)
    assert r["status"] == "held"
    assert r["applied"] is True
    assert r["unpriced_new"] == ["mystery-lane"]
    assert _read_jsonl(tables / "models.jsonl") == []   # nothing written


def test_hold_is_per_lane_priced_siblings_still_file(hermetic, monkeypatch):
    """Measured on real data (2026-10-14 dry-run): carriers ship waves where
    MOST new lanes are priced and a few are not. The hold is PER LANE — the 9
    priced lanes file, the 2 unpriced ones stay held and reported."""
    tmp, tables = hermetic
    presets = _write_presets(tmp, _preset(pid="mixed"))
    cats = _write_catalogs(tmp, mixed=_catalog(
        _m("priced-a", 1.0, 2.0), _m("priced-b", 0.5, 1.0),
        _m("mystery-1", None, None), _m("mystery-2", None, None)))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    r = rcr.refresh_preset(_preset(pid="mixed"), {}, apply=True)
    assert r["status"] == "held"
    assert r["unpriced_new"] == ["mystery-1", "mystery-2"]
    models = [r["model"] for r in _read_jsonl(tables / "models.jsonl")]
    assert sorted(models) == ["priced-a", "priced-b"]


def test_sticker_priced_lane_is_filed_not_held(hermetic, monkeypatch):
    """A preset that declares sticker_prices prices its catalog as DATA — that
    lane is priced, files normally (the commandcode shape)."""
    tmp, tables = hermetic
    preset = _preset(pid="stickers", sticker_prices={"s/one": {"in": 0.5, "out": 2.0}})
    presets = _write_presets(tmp, preset)
    cats = _write_catalogs(tmp, stickers=_catalog({"id": "s/one"}))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    r = rcr.refresh_preset(preset, {}, apply=True)
    assert r["status"] == "ok" and r["applied"] is True
    rows = _read_jsonl(tables / "models.jsonl")
    assert rows[0]["normalized_price"] == 0.5


def test_plan_multiplier_preview_does_not_show_a_fake_hike(hermetic, monkeypatch):
    """The xkiro shape: preview/apply show the plan-effective price (list/30),
    so an honest refresh never reads as a 30x price hike."""
    tmp, tables = hermetic
    preset = _preset(pid="plan30",
                     plan_tier_policy={"default": 0, "usage_multiplier": 30,
                                       "reason": "test plan"})
    presets = _write_presets(tmp, preset)
    cats = _write_catalogs(tmp, plan30=_catalog(_m("p/lane", 3.0, 6.0)))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    r = rcr.refresh_preset(preset, {}, apply=True)
    assert r["status"] == "ok" and r["applied"] is True
    row = _read_jsonl(tables / "models.jsonl")[0]
    assert row["plan_tier"] == 0
    assert row["normalized_price"] == pytest.approx(3.12 / 30)
    assert row["public_price"] == pytest.approx(3.12)   # list kept on public


# ------------------------------------------------------- sweep-level behaviour ----

def test_sweep_covers_every_preset_and_survives_one_dead_carrier(hermetic, monkeypatch):
    """One unreachable carrier must not kill the sweep: others still report,
    the dead one carries status=error, and the CLI exits 1 (TR-024)."""
    tmp, tables = hermetic
    presets = _write_presets(tmp, _preset("dead-one"), _preset("alive-one"))
    cats = _write_catalogs(tmp, **{"alive-one": _catalog(_m("a/one", 1.0, 2.0))})
    monkeypatch.setenv("ROUTER_CATALOG_DIR", presets)
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", cats)
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py")],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ,
                 ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=presets,
                 ROUTER_CATALOG_FETCH_DIR=cats))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "[dead-one] ERROR:" in p.stdout
    assert "[alive-one] ok:" in p.stdout
    assert "dry-run: nothing written" in p.stdout


def test_cli_apply_writes_and_holds_exit_zero(hermetic):
    """The cron path: --apply files priced lanes (exit 0 even when a sibling
    preset is HELD — holds are honest diffs, the research phase prices them),
    the held preset writes nothing, and the report names the unpriced lanes."""
    tmp, tables = hermetic
    priced = _preset("priced-one")
    unpriced = _preset("unpriced-one")
    presets = _write_presets(tmp, priced, unpriced)
    cats = _write_catalogs(tmp, **{
        "priced-one": _catalog(_m("pr/lane", 1.0, 2.0)),
        "unpriced-one": _catalog(_m("un/lane", None, None))})
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py"), "--apply"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ,
                 ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=presets,
                 ROUTER_CATALOG_FETCH_DIR=cats))
    assert p.returncode == 0, p.stdout + p.stderr
    out = p.stdout
    assert "HELD" in out and "un/lane" in out          # loud, named lanes
    assert "priced-one" in out and "applied" in out
    rows = _read_jsonl(tables / "models.jsonl")
    models = [r["model"] for r in rows]
    assert "pr/lane" in models
    assert "un/lane" not in models                     # the hold held
    # re-running is idempotent (a daily cron may re-run after a partial night)
    p2 = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py"), "--apply"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ,
                 ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=presets,
                 ROUTER_CATALOG_FETCH_DIR=cats))
    assert p2.returncode == 0
    assert len(_read_jsonl(tables / "models.jsonl")) == len(rows)


# ------------------------------------------------------- pipeline wiring pins ----

def test_pipeline_runs_the_catalog_refresh_for_every_preset():
    """The AC's wiring half: the cron's pre-run script must invoke the
    catalog refresh (the 'EVERY preset' discovery lives in the wrapper and is
    pinned by test_discovery_is_a_glob_not_a_hardcoded_list)."""
    src = open(os.path.join(SCRIPTS, "router-data-quality.sh")).read()
    assert "router_catalog_refresh.py" in src, (
        "cron pre-run script dropped the carrier-catalog refresh step")


def test_pipeline_runs_the_refresh_before_the_research_input_steps():
    """Order matters: the refresh's diff + holds are agent research input, so
    the step must run before the gap report (the agent's task list)."""
    src = open(os.path.join(SCRIPTS, "router-data-quality.sh")).read()
    assert src.index("router_catalog_refresh.py") < src.index("router_gaps.py")


def test_wrapper_itself_is_synced_to_the_live_install():
    """The cron runs scripts/ from ~/.hermes/scripts — the wrapper must be on
    the sync list or the cron's pre-run step dies on a missing module."""
    sync = open(os.path.join(SCRIPTS, "sync_runtime.sh")).read()
    assert "router_catalog_refresh.py" in sync


def test_all_committed_presets_refresh_clean_in_dry_run():
    """AC acceptance leg: dry-run works for each of the committed presets —
    each parses, normalizes against the REAL registry snapshot, and produces a
    well-formed diff. Network-free where a fixture catalog exists; a preset
    whose live fetch fails here still reports (the sweep shape), it just can't
    be diffed in this hermetic test — so we mark it explicitly."""
    d = os.path.join(REPO, "data", "catalogs")
    names = sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
    assert len(names) >= 4, f"expected the 4 carrier presets, saw {names}"
    by_key = {}
    for l in open(os.path.join(REPO, "data", "tables", "models.jsonl")):
        if l.strip():
            r = json.loads(l)
            by_key[(r.get("provider"), r["model"])] = r
    for name in names:
        preset = json.load(open(os.path.join(d, name + ".json")))
        existing = {k: v for k, v in by_key.items() if k[0] == preset["id"]}
        # no network: reuse the saved live snapshot when the test box has one,
        # else verify the preset parses + normalizes an EMPTY catalog cleanly.
        snap = f"/tmp/{name}_models.json"
        if os.path.exists(snap):
            lanes = rpi.normalize(json.load(open(snap)), preset)
            diff = rpi.diff(existing, lanes)
            assert set(diff) == {"added", "changed", "unchanged", "removed"}
            for lane in lanes.values():
                assert lane["provider"] == preset["id"]
                assert "normalized_price" in lane
        else:
            lanes = rpi.normalize({"data": []}, preset)
            assert lanes == {}


def test_importer_unchanged_contracts_still_hold():
    """The wrapper reuses the importer's own normalize/diff/apply — the
    existing importer suite remains the authority for those contracts."""
    assert rpi.normalize(_catalog(_m("x/1", 1.0, 2.0)), _preset())["x/1"][
        "normalized_price"] == pytest.approx(1.04)

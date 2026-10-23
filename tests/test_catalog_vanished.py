"""XK-404-001-R2 — vanished carrier lanes cannot stay active.

scripts/router_catalog_refresh.py already diffed and reported `removed`
(the registry ids a carrier's catalog no longer serves) but never retired
anything: a row with archive:false / valid_to:null stayed eligible in every
router_spawn chain walk forever, and every hop that ranked it burned
HTTP 404 "Model does not exist" first — the exact XK-404-001 burn, now
three times. These tests pin the class fix:

  * a non-archived row absent from a real (list-carrying) catalog fixture is
    reported as vanished and, with --archive-vanished, archived with a dated
    valid_to + provenance (the R4 gate refuses anonymous dates);
  * an already-archived row is never vanished (idempotent re-runs: zero);
  * the default run writes NOTHING (archive_vanished off; --apply without
    the flag also writes nothing);
  * a list-less/empty catalog payload (`data: []`) is ambiguous — the lanes
    are skipped, never archived off a guess (a wipe-my-tables archive would
    be the XK-404-001 failure with the polarity flipped);
  * all fixtures are SYNTHETIC presets/carriers — no carrier name is
    hardcoded anywhere (discovery-not-enumeration invariant).

Zero network: catalogs come from the ROUTER_CATALOG_FETCH_DIR seam, tables
from ROUTING_DATA_DIR (the same hermetic shape as test_catalog_refresh.py).
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)
import router_catalog_refresh as rcr  # noqa: E402

PY = sys.executable


# ----------------------------------------------------------------- fixtures ----

def _preset(pid="synthcar", url=None, **over):
    p = {
        "id": pid,
        "catalog_url": url or f"https://{pid}.example/v1/models",
        "catalog_format": "openai_models_list",
        "field_map": {"model": "id", "price_in": "pricing.input",
                      "price_out": "pricing.output",
                      "context": "context_length"},
        "blend": [0.96, 0.04],
    }
    p.update(over)
    return p


def _catalog(*models):
    return {"data": list(models)}


def _m(mid, pin=1.0, pout=2.0):
    return {"id": mid, "pricing": {"input": pin, "output": pout},
            "context_length": 100000}


def _row(provider, model, **over):
    r = {"provider": provider, "model": model,
         "normalized_price": 1.0, "price_evidence": "fixture",
         "public_price": None, "public_in_per_m": None,
         "public_out_per_m": None, "context_limit": None,
         "data_class": "zdr", "plan_tier": None,
         "valid_from": "2026-01-01", "valid_to": None, "archive": False,
         "token_factor": 1.0, "disabled": None, "disabled_reason": None,
         "lifecycle_source": "fixture", "note": None}
    r.update(over)
    return r


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    return [json.loads(l) for l in open(path) if l.strip()]


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    tables = tmp_path / "tables"
    tables.mkdir()
    _write_jsonl(tables / "models.jsonl", [])
    _write_jsonl(tables / "probe_providers.jsonl", [])
    monkeypatch.setenv("ROUTING_DATA_DIR", str(tables))
    monkeypatch.delenv("ROUTER_CATALOG_DIR", raising=False)
    monkeypatch.delenv("ROUTER_CATALOG_FETCH_DIR", raising=False)
    return tmp_path, tables


def _wire(tmp, monkeypatch, preset, catalog):
    presets = tmp / "catalogs"
    presets.mkdir(exist_ok=True)
    (presets / (preset["id"] + ".json")).write_text(json.dumps(preset))
    cats = tmp / "cat"
    cats.mkdir(exist_ok=True)
    (cats / (preset["id"] + ".json")).write_text(json.dumps(catalog))
    monkeypatch.setenv("ROUTER_CATALOG_DIR", str(presets))
    monkeypatch.setenv("ROUTER_CATALOG_FETCH_DIR", str(cats))
    return preset


def _rows_by_key(tables):
    out = {}
    with open(tables / "models.jsonl") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[(r["provider"], r["model"])] = r
    return out


# ------------------------------------------ a: vanished reported + archived ----

def test_vanished_lane_is_reported_and_archived_with_flag(hermetic, monkeypatch):
    """(a) A non-archived row absent from a REAL catalog fixture: reported
    as vanished in the dry run, and with the flag archived with a dated
    valid_to + lifecycle provenance (R4: no anonymous dates)."""
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl",
                 [_row("synthcar", "alive/lane"),
                  _row("synthcar", "dead/lane")])
    preset = _preset(pid="synthcar", url="https://synthcar.example/v1/models")
    _wire(tmp, monkeypatch, preset, _catalog(_m("alive/lane")))

    # dry run: vanished is REPORTED, nothing is written
    r = rcr.refresh_preset(preset, _rows_by_key(tables), apply=False)
    assert r["status"] == "ok"
    assert r["vanished"] == ["dead/lane"]
    assert (tables / "models.jsonl").read_text().count("dead/lane") == 1
    assert _rows_by_key(tables)["synthcar", "dead/lane"]["archive"] is False

    # CLI shape: the default sweep prints the vanished lanes loudly even
    # without --apply
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py")],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=os.path.join(str(tmp), "catalogs"),
                 ROUTER_CATALOG_FETCH_DIR=os.path.join(str(tmp), "cat")))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "dead/lane" in p.stdout and "VANISHED" in p.stdout
    assert "--archive-vanished" in p.stdout        # the loud how-to-fix hint
    # --json carries the machine payload
    p2 = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py"), "--json",
         "--preset", "synthcar"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=os.path.join(str(tmp), "catalogs"),
                 ROUTER_CATALOG_FETCH_DIR=os.path.join(str(tmp), "cat")))
    assert p2.returncode == 0, p2.stdout + p2.stderr
    payload = json.loads(p2.stdout)
    (rp,) = payload["presets"]
    assert rp["vanished"] == ["dead/lane"]
    assert rp["vanished_archived"] == []

    # apply + flag: archived with a dated valid_to and provenance
    preset2 = _preset(pid="synthcar")
    r2 = rcr.refresh_preset(preset2, _rows_by_key(tables), apply=True,
                            archive_vanished=True)
    assert r2["vanished"] == ["dead/lane"]
    assert r2["vanished_archived"] == [("synthcar", "dead/lane")]
    row = _rows_by_key(tables)["synthcar", "dead/lane"]
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    assert row["archive"] is True
    assert row["valid_to"] == today
    src = row["lifecycle_source"]
    assert "synthcar.example" in src and today in src
    assert "absent from GET https://synthcar.example/v1/models" in src
    note = row["note"]
    assert "retired" in note and "synthcar.example" in note
    # every other fingerprint field is preserved
    assert row["normalized_price"] == 1.0
    assert row["price_evidence"] == "fixture"
    assert row["valid_from"] == "2026-01-01"


def test_apply_archive_vanished_via_cli(hermetic, monkeypatch):
    """The full CLI arm: --apply --archive-vanished flips the row; the byte
    shape of every OTHER line is untouched (line-wise rewrite discipline)."""
    tmp, tables = hermetic
    keep_line = json.dumps(_row("synthcar", "alive/lane"), sort_keys=True)
    _write_jsonl(tables / "models.jsonl",
                 [_row("synthcar", "alive/lane"), _row("synthcar", "dead/lane"),
                  _row("othercar", "unrelated/lane")])
    preset = _preset(pid="synthcar")
    _wire(tmp, monkeypatch, preset, _catalog(_m("alive/lane")))
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py"),
         "--apply", "--archive-vanished", "--preset", "synthcar"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=os.path.join(str(tmp), "catalogs"),
                 ROUTER_CATALOG_FETCH_DIR=os.path.join(str(tmp), "cat")))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "archived vanished lanes: 1" in p.stdout
    assert "'dead/lane'" in p.stdout
    lines = (tables / "models.jsonl").read_text().splitlines(True)
    assert len(lines) == 3
    alive = [json.loads(l) for l in lines if "alive/lane" in l]
    other = [json.loads(l) for l in lines if "unrelated/lane" in l]
    dead = [json.loads(l) for l in lines if "dead/lane" in l]
    assert len(alive) == len(other) == len(dead) == 1
    assert alive[0]["archive"] is False           # untouched row: still active
    assert other[0]["provider"] == "othercar"     # other presets untouched
    today = dead[0]["valid_to"]
    import datetime
    assert today == datetime.datetime.now(
        datetime.timezone.utc).date().isoformat()
    assert dead[0]["lifecycle_source"]            # R4: dated row carries source


# --------------------------------------------- b: already-archived not vanished ----

def test_already_archived_row_is_not_vanished(hermetic, monkeypatch):
    """(b) An archived row absent from the catalog is not a vanished lane —
    idempotent re-runs report zero and write nothing."""
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl",
                 [_row("synthcar", "dead/lane", archive=True,
                       valid_to="2026-09-01",
                       lifecycle_source="fixture-archive")])
    preset = _preset(pid="synthcar")
    _wire(tmp, monkeypatch, preset, _catalog(_m("alive/lane")))
    before_line = [l for l in (tables / "models.jsonl").read_text().splitlines(True)
                   if "dead/lane" in l]
    assert len(before_line) == 1
    r = rcr.refresh_preset(preset, _rows_by_key(tables), apply=True,
                           archive_vanished=True)
    assert r["vanished"] == []
    assert r["vanished_archived"] == []
    after_line = [l for l in (tables / "models.jsonl").read_text().splitlines(True)
                  if "dead/lane" in l]
    assert after_line == before_line        # archived row itself untouched
    assert _rows_by_key(tables)["synthcar", "dead/lane"]["archive"] is True


# --------------------------------------------- c: default writes nothing ----

def test_default_flow_writes_nothing(hermetic, monkeypatch):
    """(c) archive_vanished OFF (default): dry-run reports the lanes and
    writes nothing; --apply WITHOUT the flag files only net-new lanes and
    still does not archive."""
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl",
                 [_row("synthcar", "dead/lane")])
    preset = _preset(pid="synthcar")
    _wire(tmp, monkeypatch, preset, _catalog(_m("alive/lane")))

    # default dry-run: no write
    before = (tables / "models.jsonl").read_text()
    r1 = rcr.refresh_preset(preset, _rows_by_key(tables), apply=False)
    assert r1["vanished"] == ["dead/lane"]
    assert r1["applied"] is None
    assert (tables / "models.jsonl").read_text() == before

    # apply WITHOUT the flag: nothing archived
    r2 = rcr.refresh_preset(preset, _rows_by_key(tables), apply=True,
                            archive_vanished=False)
    assert r2["applied"] is True
    assert r2["vanished"] == ["dead/lane"]
    assert r2["vanished_archived"] == []
    assert _rows_by_key(tables)["synthcar", "dead/lane"]["archive"] is False
    # the apply DID append the catalog's net-new priced lane (unchanged
    # import behaviour), but the stale row itself is untouched
    assert ("synthcar", "alive/lane") in _rows_by_key(tables)


# --------------------------------- fail-safe: ambiguous catalog archives nothing ----

def test_list_less_or_empty_catalog_never_archives(hermetic, monkeypatch):
    """A payload with NO model list (data:[] — the wipe-everything vs
    paginated/failed ambiguity) must not archive the preset's rows: the
    lanes are surfaced as vanished_skipped and marked skipped."""
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl",
                 [_row("synthcar", "dead/lane"),
                  _row("synthcar", "second/lane")])
    preset = _preset(pid="synthcar")
    _wire(tmp, monkeypatch, preset, {"data": []})
    r = rcr.refresh_preset(preset, _rows_by_key(tables), apply=True,
                           archive_vanished=True)
    assert r["vanished"] == []
    assert r["vanished_archived"] == []
    assert r["vanished_skipped"] == ["dead/lane", "second/lane"]
    rows = _rows_by_key(tables)
    for k in (("synthcar", "dead/lane"), ("synthcar", "second/lane")):
        assert rows[k]["archive"] is False


def test_error_preset_fails_open_and_archives_nothing(hermetic, monkeypatch):
    """A preset whose FETCH raises is caught at the SWEEP level (main()) — the
    one place the try/except lives — reported as status=error, and archives
    nothing for that preset (fail-open: other carriers still run)."""
    tmp, tables = hermetic
    _write_jsonl(tables / "models.jsonl", [_row("synthcar", "dead/lane")])
    preset = _preset(pid="synthcar")
    _wire(tmp, monkeypatch, preset, _catalog(_m("alive/lane")))
    # corrupt the catalog payload so the fetch/parse raises inside the sweep
    (tmp / "cat" / "synthcar.json").write_text("{not json")
    before = (tables / "models.jsonl").read_text()
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_catalog_refresh.py"),
         "--apply", "--archive-vanished", "--preset", "synthcar"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, ROUTING_DATA_DIR=str(tables),
                 ROUTER_CATALOG_DIR=os.path.join(str(tmp), "catalogs"),
                 ROUTER_CATALOG_FETCH_DIR=os.path.join(str(tmp), "cat")))
    out = p.stdout + p.stderr
    assert p.returncode != 0, out            # the sweep reports the failure
    assert "ERROR" in out and "synthcar" in out
    assert (tables / "models.jsonl").read_text() == before
    assert _rows_by_key(tables)["synthcar", "dead/lane"]["archive"] is False


# ------------------------------------------- no carrier names hardcoded ----

def test_no_carrier_name_is_hardcoded():
    """Discovery-not-enumeration: the vanished-lane logic must key on the
    PRESET id + catalog shape, never on a carrier name (synthetic fixtures
    prove the logic is generic; this pins the source invariant)."""
    src = open(os.path.join(SCRIPTS, "router_catalog_refresh.py")).read()
    for carrier in ("xkiro", "xkiro-2", "commandcode", "opencode-go-2",
                    "openrouter"):
        assert carrier not in src, f"hardcoded carrier name: {carrier}"

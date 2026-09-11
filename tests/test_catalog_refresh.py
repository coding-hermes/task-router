"""TR-035 lane 1 — catalog-driven capability refresh on EXISTING registry rows.

router_modelsdev.py sync used to add capability fields only on BRAND-NEW rows;
existing rows never saw catalog updates (24 live rows carried
context_limit: null on 2026-09-10). These tests lock in the refresh behaviour:
context_limit / vision / thinking filled when null, overwritten when the
catalog differs, retired rows (valid_to / archive) frozen, price/plan fields
byte-identical, --dry-run detecting without writing.

Hermetic: fixture JSONL tables under tmp_path + a SYNTHETIC models.dev payload
written to a scratch MODELSDEV_CACHE — ZERO network, never touches the real
data/ directory (ROUTING_DATA_DIR override, same pattern as
test_provider_mapping.py).
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import router_modelsdev as md  # noqa: E402


# ----------------------------------------------------------------- fixtures ----

def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _model_row(provider, model, **over):
    """Registry row carrying the fields this path may touch or must not."""
    row = {
        "provider": provider, "model": model,
        "normalized_price": None, "price_evidence": None,
        "public_price": None, "public_in_per_m": None, "public_out_per_m": None,
        "data_class": "zdr", "plan_tier": None,
        "context_limit": None, "vision": None, "thinking": None,
        "valid_from": None, "valid_to": None, "archive": False,
        "token_factor": 1.0, "disabled": None, "disabled_reason": None,
    }
    row.update(over)
    return row


def _meta(context=None, vision=None, reasoning=None):
    """Synthetic models.dev per-model metadata block."""
    meta = {"tool_call": True, "cost": {"input": 0.1, "output": 0.2}}
    if context is not None:
        meta["limit"] = {"context": context}
    if vision is not None:
        meta["vision"] = vision
    if reasoning is not None:
        meta["reasoning"] = reasoning
    return meta


def _payload(**models_by_name):
    """Synthetic models.dev payload for our provider id 'prov'."""
    return {"prov": {"models": models_by_name}}


@pytest.fixture
def fixture_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _write_jsonl(data / "providers.jsonl", [{"id": "prov", "archive": False}])
    _write_jsonl(data / "provider_mappings.jsonl", [])
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data))
    md.DATA_DIR = str(data)  # module constant re-pointed (tests reload helpers)
    yield data
    md.DATA_DIR = os.path.join(REPO, "data", "tables")


# ------------------------------------------------------- refresh: fill / overwrite ----

def test_fill_null_context_limit_from_catalog(fixture_env):
    """AC 1: existing row with context_limit: null gets the catalog value."""
    rows = [_model_row("prov", "m1")]
    api = _payload(m1=_meta(context=200000))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["context_limit"] == 200000
    assert summary["refreshed"]["context_limit"] == 1
    assert summary["refreshed_rows"] == [
        {"provider": "prov", "model": "m1", "field": "context_limit",
         "old": None, "new": 200000}]


def test_overwrite_stale_context_limit(fixture_env):
    """AC 2: catalog is the live source of truth — a differing context_limit
    is overwritten (not just null-filled)."""
    rows = [_model_row("prov", "m1", context_limit=128000)]
    api = _payload(m1=_meta(context=262144))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["context_limit"] == 262144
    assert summary["refreshed"]["context_limit"] == 1
    assert summary["refreshed_rows"] == [
        {"provider": "prov", "model": "m1", "field": "context_limit",
         "old": 128000, "new": 262144}]


def test_matching_values_are_not_reported(fixture_env):
    """No-diff rows produce no refresh noise (old == new -> skip)."""
    rows = [_model_row("prov", "m1", context_limit=200000,
                       vision=True, thinking=True)]
    api = _payload(m1=_meta(context=200000, vision=True, reasoning=True))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert summary["refreshed"] == {"context_limit": 0, "vision": 0, "thinking": 0}
    assert summary["refreshed_rows"] == []


def test_vision_and_thinking_fill_from_catalog(fixture_env):
    """AC 3: vision <- catalog vision, thinking <- catalog reasoning."""
    rows = [_model_row("prov", "m1", context_limit=200000)]
    api = _payload(m1=_meta(context=200000, vision=True, reasoning=True))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["vision"] is True
    assert rows[0]["thinking"] is True
    assert summary["refreshed"] == {"context_limit": 0, "vision": 1, "thinking": 1}
    fields = {(r["field"], r["old"], r["new"]) for r in summary["refreshed_rows"]}
    assert fields == {("vision", None, True), ("thinking", None, True)}


def test_catalog_absent_field_never_nulls_a_known_value(fixture_env):
    """Catalog carries no value for a field -> ours is left alone (never
    overwrite a known value with null)."""
    rows = [_model_row("prov", "m1", context_limit=128000,
                       vision=True, thinking=True)]
    api = _payload(m1=_meta())  # no limit/vision/reasoning keys at all
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["context_limit"] == 128000
    assert rows[0]["vision"] is True and rows[0]["thinking"] is True
    assert summary["refreshed_rows"] == []


# --------------------------------------------------------------- retired rows ----

def test_retired_rows_are_frozen(fixture_env):
    """AC 4: valid_to set or archive true -> NOT refreshed."""
    rows = [
        _model_row("prov", "m1", context_limit=1000, valid_to="2026-01-01"),
        _model_row("prov", "m2", context_limit=1000, archive=True),
    ]
    api = _payload(m1=_meta(context=999999, vision=True, reasoning=True),
                   m2=_meta(context=999999, vision=True, reasoning=True))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["context_limit"] == 1000
    assert rows[1]["context_limit"] == 1000
    assert rows[0]["vision"] is None and rows[1]["vision"] is None
    assert summary["refreshed"] == {"context_limit": 0, "vision": 0, "thinking": 0}
    assert summary["refreshed_rows"] == []


def test_live_sibling_row_refreshed_while_retired_twin_frozen(fixture_env):
    """A (provider, model) with both a live and a retired row: the live row
    refreshes, the retired row stays byte-frozen."""
    rows = [
        _model_row("prov", "m1", context_limit=1000, valid_to="2026-01-01"),
        _model_row("prov", "m1", context_limit=None),
    ]
    api = _payload(m1=_meta(context=262144))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    assert rows[0]["context_limit"] == 1000   # retired twin untouched
    assert rows[1]["context_limit"] == 262144  # live row filled
    assert len(summary["refreshed_rows"]) == 1
    assert summary["refreshed_rows"][0]["old"] is None


# ------------------------------------------------------- price path untouched ----

def test_price_fields_byte_identical_through_real_write(fixture_env):
    """AC 5: the refresh is capability-ONLY — normalized_price,
    price_evidence, public_price, public_in_per_m, public_out_per_m are
    byte-identical before and after, on disk, through the real write path."""
    rows = [_model_row("prov", "m1",
                       normalized_price=3.33, price_evidence="research",
                       public_price="sub", public_in_per_m=0.5,
                       public_out_per_m=1.5)]
    api = _payload(m1=_meta(context=262144, vision=True, reasoning=True))
    summary = md.run_sync(api, rows, [], md.load_mappings())
    wrote_cat, wrote_models, n_added = md._apply_writes(summary, rows, verbose=False)
    assert wrote_models is True
    assert n_added == 0  # refresh-only: no adds
    on_disk = _read_jsonl(os.path.join(str(fixture_env), "models.jsonl"))
    assert len(on_disk) == 1
    r = on_disk[0]
    # explicit price-path proof
    assert r["normalized_price"] == 3.33
    assert r["price_evidence"] == "research"
    assert r["public_price"] == "sub"
    assert r["public_in_per_m"] == 0.5
    assert r["public_out_per_m"] == 1.5
    # ...while the capability fields DID change on disk
    assert r["context_limit"] == 262144
    assert r["vision"] is True and r["thinking"] is True


# ------------------------------------------------------------- write-path gate ----

def test_models_jsonl_written_on_refresh_without_adds(fixture_env):
    """TR-035 write-path regression: today models.jsonl is written only under
    `if adds:` — a refresh-only sync (catalog pre-seeded, zero adds) must
    still land the refreshed values on disk."""
    _write_jsonl(os.path.join(str(fixture_env), "model_catalog.jsonl"),
                 [{"provider": "prov", "model": "m1"}])
    catalog = md.load_catalog()
    rows = [_model_row("prov", "m1")]
    api = _payload(m1=_meta(context=262144))
    summary = md.run_sync(api, rows, catalog, md.load_mappings())
    assert summary["catalog_new"] == 0 and summary["new_models"] == []
    assert summary["refreshed"]["context_limit"] == 1
    wrote_cat, wrote_models, n_added = md._apply_writes(summary, rows, verbose=False)
    assert (wrote_cat, wrote_models, n_added) == (False, True, 0)
    on_disk = _read_jsonl(os.path.join(str(fixture_env), "models.jsonl"))
    assert on_disk[0]["context_limit"] == 262144


# ----------------------------------------------------------- dry-run + --json ----

def test_dry_run_same_counts_and_models_jsonl_byte_identical(fixture_env, tmp_path):
    """AC 6: `sync --json --dry-run` reports the SAME refresh counts as a real
    run_sync and leaves models.jsonl byte-identical on disk. Also proves
    stdout stays PURE machine-parseable JSON (repo doctrine)."""
    models_rows = [
        _model_row("prov", "m1", context_limit=128000),
        _model_row("prov", "m2"),  # null context -> fill; no vision/reasoning in catalog
    ]
    _write_jsonl(os.path.join(str(fixture_env), "models.jsonl"), models_rows)
    before = open(os.path.join(str(fixture_env), "models.jsonl"), "rb").read()

    cache = tmp_path / "modelsdev-cache.json"
    cache.write_text(json.dumps(_payload(
        m1=_meta(context=262144, vision=True, reasoning=True),
        m2=_meta(context=400000))))

    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(fixture_env)
    env["MODELSDEV_CACHE"] = str(cache)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "router_modelsdev.py"),
         "sync", "--all", "--json", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=60, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[:400]
    data = json.loads(proc.stdout)  # raises -> stdout is not pure JSON
    assert data["dry_run"] is True
    assert data["refreshed"] == {"context_limit": 2, "vision": 1, "thinking": 1}
    assert len(data["refreshed_rows"]) == 4
    assert {(r["provider"], r["model"], r["field"]) for r in data["refreshed_rows"]} == {
        ("prov", "m1", "context_limit"), ("prov", "m1", "vision"),
        ("prov", "m1", "thinking"), ("prov", "m2", "context_limit")}

    # parity: an in-process run_sync against the same state reports identical counts
    rows2 = [json.loads(json.dumps(r)) for r in models_rows]
    summary = md.run_sync(json.loads(cache.read_text()), rows2, [],
                          md.load_mappings())
    assert summary["refreshed"] == data["refreshed"]
    assert len(summary["refreshed_rows"]) == len(data["refreshed_rows"])

    after = open(os.path.join(str(fixture_env), "models.jsonl"), "rb").read()
    assert after == before, "--dry-run must not write models.jsonl"

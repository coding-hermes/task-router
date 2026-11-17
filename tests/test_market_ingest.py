"""Tests for the keyless OpenRouter market benchmark ingest."""
import importlib.util
import json

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "router_market_ingest.py"
FIXTURE = ROOT / "tests" / "fixtures" / "openrouter_models_market.json"


def _module():
    spec = importlib.util.spec_from_file_location("router_market_ingest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_feed_parses_benchmark_models_without_http():
    mod = _module()
    feed = json.loads(FIXTURE.read_text())
    models = mod.parse_feed(feed)
    assert [m["id"] for m in models] == [
        "vendor/ranked-model", "vendor/other-model", "vendor/not-registered"
    ]
    assert len(models[0]["benchmarks"]["design_arena"]) == 2


def test_percentile_uses_rank_over_population_size():
    mod = _module()
    assert mod.market_percentile(rank=1, n=2) == 0.5
    assert mod.market_percentile(rank=2, n=2) == 0.0


def test_category_mapping_is_explicit_and_conservative():
    mod = _module()
    assert mod.map_category("models", "codecategories") == "code_gen"
    assert mod.map_category("agents", "pdf") == "long_doc"
    assert mod.map_category("models", "unlisted-category") is None
    assert ("models", "unlisted-category") in mod.UNMAPPED_REASONS


def test_unmapped_category_is_reported_and_skipped():
    mod = _module()
    payload = {"data": [{
        "id": "vendor/ranked-model",
        "benchmarks": {"design_arena": [{
            "arena": "models", "category": "unlisted-category", "elo": 1000, "rank": 1,
        }]},
    }]}
    rows, report = mod.build_rows(payload, {"vendor/ranked-model"}, "2026-11-16")
    assert rows == []
    assert report["skipped_categories"] == {"models/unlisted-category": 1}
    assert report["unmapped_reasons"]["models/unlisted-category"]


def test_build_rows_contains_market_percentiles_and_raw_elo():
    mod = _module()
    feed = json.loads(FIXTURE.read_text())
    rows, report = mod.build_rows(
        feed, {"vendor/ranked-model", "vendor/other-model"}, "2026-11-16"
    )
    code_rows = [r for r in rows if r["category"] == "code_gen"]
    assert [r["score"] for r in code_rows] == [round(2 / 3, 12), round(1 / 3, 12)]
    assert all(r["max_score"] == 1.0 and r["source"] == "market" for r in code_rows)
    assert "raw_elo=1400" in code_rows[0]["source_ref"]
    aa = [r for r in rows if r["category"] == "artificial_analysis_intelligence"]
    assert aa[0]["score"] == 0.725 and aa[0]["max_score"] == 1.0
    assert any(r["category"] == "artificial_analysis_coding" for r in rows) is False
    assert "raw_score=72.5" in aa[0]["source_ref"]
    assert report["skipped_models"]["vendor/not-registered"] == "not in openrouter registry"


def test_market_rows_are_accepted_by_effort_gate():
    mod = _module()
    gate_spec = importlib.util.spec_from_file_location(
        "model_data_commit_gate", ROOT / "scripts" / "model_data_commit_gate.py"
    )
    gate = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(gate)
    rows = [{
        "model": "vendor/ranked-model", "category": "code_gen", "score": 0.5,
        "max_score": 1.0, "source": "market", "source_ref": "openrouter-models-api:2026-11-16:raw_elo=1400",
        "reasoning_effort": "default",
    }]
    assert gate.check_efforts([], rows) == []
    assert mod.map_category("models", "codecategories") == "code_gen"


def test_refresh_converges_and_purges_provenance_less_market_rows(tmp_path):
    """A second run must REPLACE the cells it emits, not append a second copy.

    The first landed pass wrote source=market rows with no source_ref, so a
    date-prefix filter could never retire them; a refresh would then leave two
    rows per cell and the provenance-less one would keep feeding the bridge.
    """
    mod = _module()
    feed = json.loads(FIXTURE.read_text())
    registry = tmp_path / "models.jsonl"
    registry.write_text("\n".join(json.dumps({"provider": "openrouter", "model": m})
                                  for m in ("vendor/ranked-model", "vendor/other-model")) + "\n")
    output = tmp_path / "benchmarks.jsonl"
    stale = {"model": "vendor/ranked-model", "category": "code_gen", "score": 0.5,
             "max_score": 1.0, "source": "market", "valid_from": "2026-11-16",
             "reasoning_effort": "default"}
    keep = {"model": "vendor/ranked-model", "category": "terminal", "score": 0.9,
            "max_score": 1.0, "source": "Terminal-Bench research-bench-2026-11-01",
            "valid_from": "2026-11-01", "reasoning_effort": "default"}
    output.write_text(json.dumps(stale) + "\n" + json.dumps(keep) + "\n")

    rows, report = mod.build_rows(feed, mod.load_registry_ids(str(registry)), "2026-11-17")
    mod.write_outputs(rows, report, str(output), str(tmp_path / "queue"), [])

    written = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    market = [r for r in written if r.get("source") == "market"]
    assert market, "refresh emitted no market rows"
    assert all(r.get("source_ref") for r in market), "refreshed rows must carry provenance"
    cells = [(r["model"], r["category"]) for r in market]
    assert len(cells) == len(set(cells)), "refresh left duplicate market cells"
    assert not any(r.get("source_ref") is None and r["category"] == "code_gen" for r in market)
    assert keep in written, "non-market evidence must never be touched by a market refresh"

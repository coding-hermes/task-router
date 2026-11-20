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


def test_percentile_population_is_arena_scoped_not_category_scoped():
    """rank and n must come from the SAME population.

    The feed ranks a model inside its ARENA, so a category with few entries can
    carry ranks far above its own entry count. Normalizing an arena rank by the
    category's entry count produced negative percentiles that the clamp turned
    into 0.0 — "top of class" stored as "worst in class".
    """
    mod = _module()
    payload = {"data": [
        {"id": "vendor/a", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "graphicdesign", "elo": 1251, "rank": 1}]}},
        {"id": "vendor/b", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "image", "elo": 1225, "rank": 2}]}},
        {"id": "vendor/c", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "image", "elo": 1200, "rank": 3}]}},
        {"id": "vendor/d", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "image", "elo": 1190, "rank": 4}]}},
    ]}
    rows, report = mod.build_rows(payload, {"vendor/a", "vendor/b", "vendor/c", "vendor/d"}, "2026-11-18")
    assert report["population_violations"] == {}
    by_model = {r["model"]: r for r in rows}
    # arena population is 4 distinct ranked models, not the 1-entry category:
    # the old category-scoped denominator stored 1 - 1/1 = 0.0 for vendor/a.
    assert by_model["vendor/a"]["score"] == round(1 - 1 / 4, 12)
    assert by_model["vendor/d"]["score"] == 0.0
    assert "n=4" in by_model["vendor/a"]["source_ref"]
    assert "population=arena" in by_model["vendor/a"]["source_ref"]


def test_rank_beyond_arena_population_is_reported_and_skipped():
    """A rank outside its own population is refused, never clamped to 0.0."""
    mod = _module()
    payload = {"data": [
        {"id": "vendor/a", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "imageediting", "elo": 1227, "rank": 3}]}},
        {"id": "vendor/b", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "imageediting", "elo": 1200, "rank": 1}]}},
    ]}
    rows, report = mod.build_rows(payload, {"vendor/a", "vendor/b"}, "2026-11-18")
    assert rows == []
    assert report["population_violations"]["models"] == "max rank 3 exceeds arena population 2"
    assert report["skipped_categories"]["models/imageediting"] == 2


def test_image_arena_ranks_are_not_pinned_to_zero():
    """Regression for the 2026-11-17 finding: image arenas stored 0.0 at top rank.

    google/gemini-2.5-flash-image carried raw_elo 1167/1168/1169 at rank 10/10/11
    with n=5 per category, so every row clamped to 0.0 while the model ranked
    mid-pack of the 186-model arena.
    """
    mod = _module()
    arena = []
    for rank in range(1, 12):
        arena.append({"id": f"vendor/m{rank}", "benchmarks": {"design_arena": [
            {"arena": "models", "category": "graphicdesign", "elo": 1200 - rank, "rank": rank}]}})
    rows, report = mod.build_rows({"data": arena}, {f"vendor/m{rank}" for rank in range(1, 12)}, "2026-11-18")
    assert report["population_violations"] == {}
    scores = {r["model"]: r["score"] for r in rows}
    assert scores["vendor/m10"] == round(1 - 10 / 11, 12)
    assert scores["vendor/m10"] > 0.0
    assert min(scores.values()) == 0.0  # only the last-ranked model is 0.0


def test_corpus_market_rows_never_carry_a_rank_beyond_their_population():
    """Ratchet on the landed corpus, not just on the builder.

    A stored percentile is only meaningful if its rank and its population come
    from the same list. Any market row whose source_ref encodes rank > n is a
    clamped negative — the exact defect that stored 0.0 for the image arenas.
    """
    import re

    bench = ROOT / "data" / "tables" / "benchmarks.jsonl"
    offenders = []
    for line in bench.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") != "market":
            continue
        ref = row.get("source_ref") or ""
        match = re.search(r"rank=(\d+):n=(\d+)", ref)
        if not match:
            continue
        rank, n = int(match.group(1)), int(match.group(2))
        if rank > n:
            offenders.append((row.get("model"), row.get("category"), rank, n))
        if row.get("score") == 0.0 and rank > 1:
            offenders.append((row.get("model"), row.get("category"), rank, n))
    assert offenders == [], f"market rows with rank beyond population: {offenders[:5]}"


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


def test_unrepublished_market_cells_are_carried_forward_with_provenance(tmp_path):
    """A cell the feed stops ranking must be EXPLAINED, not silently kept bare.

    The pre-provenance pass wrote source=market rows with no source_ref. A
    refresh only replaces the cells it emits, so a cell the feed no longer
    carries would keep an unverifiable row forever. The refresh must stamp it
    with its origin date, the stored score and the fact that this feed does not
    republish it — and the stamp must be idempotent, so a second run is
    byte-identical.
    """
    mod = _module()
    feed = json.loads(FIXTURE.read_text())
    registry = tmp_path / "models.jsonl"
    registry.write_text("\n".join(json.dumps({"provider": "openrouter", "model": m})
                                  for m in ("vendor/ranked-model", "vendor/absent-model")) + "\n")
    output = tmp_path / "benchmarks.jsonl"
    stale = {"model": "vendor/absent-model", "category": "artificial_analysis_coding",
             "score": 0.715, "max_score": 1.0, "source": "market",
             "valid_from": "2026-11-17", "reasoning_effort": "default"}
    output.write_text(json.dumps(stale) + "\n")

    rows, report = mod.build_rows(feed, mod.load_registry_ids(str(registry)), "2026-11-20")
    mod.write_outputs(rows, report, str(output), str(tmp_path / "queue"), [])
    first = output.read_text()

    written = [json.loads(line) for line in first.splitlines() if line.strip()]
    carried = [r for r in written if r.get("model") == "vendor/absent-model"]
    assert len(carried) == 1, "the unrepublished cell must survive as one row"
    ref = carried[0]["source_ref"]
    assert ref.startswith("openrouter-models-api:2026-11-17:"), ref
    assert "carried-forward" in ref, ref
    assert "not-republished-2026-11-20" in ref, ref
    assert "category=artificial_analysis_coding" in ref, ref
    assert "raw_score=0.715" in ref, ref
    assert report["carried_forward_rows"] == 1, report["carried_forward_rows"]

    # Idempotent: the carried row already carries provenance, so a re-run is a
    # byte-identical no-op (this is the property the card is about).
    rows2, report2 = mod.build_rows(feed, mod.load_registry_ids(str(registry)), "2026-11-20")
    mod.write_outputs(rows2, report2, str(output), str(tmp_path / "queue"), [])
    assert output.read_text() == first, "a second refresh must not rewrite carried rows"
    assert report2["carried_forward_rows"] == 0, report2["carried_forward_rows"]

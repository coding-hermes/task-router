"""Tests for the per-model market-verify card tool (scripts/market_verify_model.py).

The card contract is ONE MODEL PER CARD, so this tool — not the wholesale ingest —
is what a market-verify card runs. Its two write paths are pinned here:

  * a model the feed still ranks: arena cells are replaced with provenance;
  * a model the feed no longer ranks at all (every AA-only lane): the prior cells
    are CARRIED FORWARD with a source_ref that says so.

The second path is the regression this file exists for: the carry-forward was
computed and reported but the file rewrite was gated on `replaced`, so an
AA-only model's provenance-less rows stayed on disk while the report claimed
they had been carried.
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "market_verify_model.py"


def _module():
    # The tool resolves its sibling import relative to the CWD ("scripts"), which
    # the tests move to a tmp repo; put the real scripts dir on the path first.
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("market_verify_model", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _feed(entries):
    return {"data": entries}


def _entry(model, *, arena=None, aa=None):
    entry = {
        "id": model,
        "created": 1700000000,
        "context_length": 1000,
        "top_provider": {"context_length": 1000, "max_completion_tokens": 100},
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["max_tokens"],
        "hugging_face_id": None,
        "knowledge_cutoff": None,
        "benchmarks": {},
    }
    if arena:
        entry["benchmarks"]["design_arena"] = arena
    if aa:
        entry["benchmarks"]["artificial_analysis"] = aa
    return entry


def _write_repo(tmp_path, model, bench_rows):
    (tmp_path / "data" / "tables").mkdir(parents=True)
    (tmp_path / "data" / "null_reasons.jsonl").write_text("", encoding="utf-8")
    models = tmp_path / "data" / "tables" / "models.jsonl"
    models.write_text(
        json.dumps({
            "provider": "openrouter",
            "model": model,
            "context_limit": 1000,
            "vision": False,
            "thinking": False,
            "cap_image_generation": False,
            "cap_audio": False,
            "cap_video": False,
            "release_date": "2023-11-14",
        }) + "\n",
        encoding="utf-8",
    )
    bench = tmp_path / "data" / "tables" / "benchmarks.jsonl"
    bench.write_text(
        "".join(json.dumps(r) + "\n" for r in bench_rows), encoding="utf-8"
    )
    return models, bench


def test_aa_only_model_carries_prior_cells_forward_with_provenance(tmp_path, monkeypatch):
    """A model the feed no longer ranks must still get its provenance written."""
    model = "vendor/aa-only"
    models, bench = _write_repo(tmp_path, model, [{
        "model": model,
        "category": "artificial_analysis_coding",
        "score": 0.114,
        "max_score": 1.0,
        "source": "market",
        "valid_from": "2026-11-17",
        "reasoning_effort": "default",
    }])
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(_feed([_entry(model)])), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    mod = _module()
    rc = mod.main([
        "--model", model, "--date", "2026-11-20", "--input", str(feed),
        "--models", str(models), "--output", str(bench), "--write",
    ])
    assert rc == 0

    rows = [json.loads(line) for line in bench.read_text().splitlines() if line.strip()]
    mine = [r for r in rows if r["model"] == model and r["source"] == "market"]
    assert len(mine) == 1, "the carried cell must survive the rewrite"
    ref = mine[0].get("source_ref") or ""
    assert "carried-forward" in ref, ref
    assert "not-republished-2026-11-20" in ref, ref
    assert "category=artificial_analysis_coding" in ref, ref
    # the value itself is preserved, not recomputed
    assert mine[0]["score"] == 0.114


def test_ranked_model_gets_arena_provenance(tmp_path, monkeypatch):
    """A model the feed still ranks gets fresh rows carrying arena/rank/n."""
    model = "vendor/ranked"
    models, bench = _write_repo(tmp_path, model, [{
        "model": model,
        "category": "code_gen",
        "score": 0.5,
        "max_score": 1.0,
        "source": "market",
        "valid_from": "2026-11-17",
        "reasoning_effort": "default",
    }])
    feed = tmp_path / "feed.json"
    feed.write_text(
        json.dumps(_feed([
            _entry(model, arena=[{
                "arena": "models", "category": "codecategories",
                "elo": 1000, "win_rate": 50.0, "rank": 1,
            }]),
            _entry("vendor/other", arena=[{
                "arena": "models", "category": "codecategories",
                "elo": 900, "win_rate": 40.0, "rank": 2,
            }]),
        ])),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    mod = _module()
    rc = mod.main([
        "--model", model, "--date", "2026-11-20", "--input", str(feed),
        "--models", str(models), "--output", str(bench), "--write",
    ])
    assert rc == 0

    rows = [json.loads(line) for line in bench.read_text().splitlines() if line.strip()]
    mine = [r for r in rows if r["model"] == model and r["source"] == "market"]
    assert len(mine) == 1
    ref = mine[0]["source_ref"]
    assert "arena=models" in ref and "rank=1" in ref and "n=2" in ref, ref
    assert mine[0]["score"] == 0.5  # rank 1 of 2 -> 1 - 1/2


def test_carried_cells_are_mirrored_onto_bare_id_lanes(tmp_path, monkeypatch):
    """TR-350 for carried cells: the bare-id twin must not keep the stale row.

    The 2026-11-17 ingest wrote the same cell under both the feed id and the bare
    id. Re-stamping only the feed id leaves the bare lane's row provenance-less,
    which is the split TR-350 exists to prevent.
    """
    model = "vendor/aa-only"
    bare = "aa-only"
    models, bench = _write_repo(tmp_path, model, [
        {
            "model": model,
            "category": "artificial_analysis_coding",
            "score": 0.114,
            "max_score": 1.0,
            "source": "market",
            "valid_from": "2026-11-17",
            "reasoning_effort": "default",
        },
        {
            "model": bare,
            "category": "artificial_analysis_coding",
            "score": 0.114,
            "max_score": 1.0,
            "source": "market",
            "valid_from": "2026-11-17",
            "reasoning_effort": "default",
        },
    ])
    # register the bare-id lane so the mirror set is non-empty
    with models.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "provider": "commandcode",
            "model": bare,
            "context_limit": 1000,
        }) + "\n")
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(_feed([_entry(model)])), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    mod = _module()
    rc = mod.main([
        "--model", model, "--date", "2026-11-20", "--input", str(feed),
        "--models", str(models), "--output", str(bench), "--write",
    ])
    assert rc == 0

    rows = [json.loads(line) for line in bench.read_text().splitlines() if line.strip()]
    for lane in (model, bare):
        mine = [r for r in rows if r["model"] == lane and r["source"] == "market"]
        assert len(mine) == 1, lane
        assert "carried-forward" in (mine[0].get("source_ref") or ""), (lane, mine[0])


def test_explained_null_is_only_claimed_for_a_field_the_row_leaves_null(tmp_path, monkeypatch):
    """A settled capability must not be annotated as an open NULL.

    The note is the lane's audit trail and the NULL census reads a NULL claim as
    an open gap. Two lanes serve the same model here: the live one gets its
    modality-settled fields FILLED from the feed (so it has no gap to explain),
    the disabled one keeps them null (so it does). Writing "cap_audio=NULL" on
    the live lane would invent debt that does not exist — the feed proving a
    channel ABSENT is a settled value, not a missing one.
    """
    model = "vendor/settled"
    models, bench = _write_repo(tmp_path, model, [])
    rows = [json.loads(line) for line in models.read_text().splitlines() if line.strip()]
    live = dict(rows[0])
    live["cap_video"] = None
    disabled = dict(rows[0])
    disabled.update({"provider": "commandcode", "disabled": True,
                     "disabled_reason": "test lane: never measured",
                     "cap_audio": None, "cap_image_generation": None,
                     "cap_video": None})
    models.write_text(
        "".join(json.dumps(r) + "\n" for r in (live, disabled)), encoding="utf-8"
    )
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(_feed([_entry(model)])), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    mod = _module()
    rc = mod.main([
        "--model", model, "--date", "2026-11-20", "--input", str(feed),
        "--models", str(models), "--output", str(bench), "--write",
    ])
    assert rc == 0

    by_provider = {
        r["provider"]: r
        for r in (json.loads(line) for line in models.read_text().splitlines() if line.strip())
    }
    live_note = by_provider["openrouter"].get("note") or ""
    assert live_note == "", live_note
    assert by_provider["openrouter"]["cap_video"] is False
    disabled_note = by_provider["commandcode"].get("note") or ""
    assert "cap_video=NULL" in disabled_note, disabled_note
    assert "cap_audio=NULL" in disabled_note, disabled_note

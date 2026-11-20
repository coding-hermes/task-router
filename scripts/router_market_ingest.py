#!/usr/bin/env python3
"""Ingest ranked market evidence from OpenRouter's public models endpoint.

The endpoint is keyless, but the evidence is not guessed: only model ids already
registered as provider=openrouter are joined. Design Arena categories are mapped
through the explicit table below; an absent mapping is reported and skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from market_category_vocab import CATEGORY_MAP, UNMAPPED_REASONS, AA_INDEX_CATEGORIES
except ModuleNotFoundError:  # direct execution from outside scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_category_vocab import CATEGORY_MAP, UNMAPPED_REASONS, AA_INDEX_CATEGORIES

API_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_OUTPUT = "data/tables/benchmarks.jsonl"
DEFAULT_QUEUE = "data/state/backfill_queue"



def parse_feed(payload: Any) -> list[dict[str, Any]]:
    """Return valid model objects from an API payload or captured fixture."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("OpenRouter payload has no data list")
    return [row for row in data if isinstance(row, dict) and isinstance(row.get("id"), str)]


def market_percentile(rank: int, n: int) -> float:
    """Compute the requested rank percentile, bounded to [0, 1]."""
    if n <= 0 or rank < 1:
        raise ValueError("rank and population must be positive")
    return round(max(0.0, min(1.0, 1.0 - (rank / n))), 12)


def map_category(arena: str, category: str) -> str | None:
    key = (str(arena), str(category))
    value = CATEGORY_MAP.get(key)
    if value is None:
        UNMAPPED_REASONS.setdefault(key, "no defensible mapping to the 24-category vocabulary")
    return value


def _source_ref(date: str, **fields: Any) -> str:
    suffix = ":".join(f"{key}={fields[key]}" for key in fields)
    return f"openrouter-models-api:{date}" + (f":{suffix}" if suffix else "")


def _frozen_source_ref(row: dict[str, Any], date: str) -> str:
    """Provenance for a market row this run's feed does not republish.

    The pre-provenance pass recorded no arena, rank or population, so the
    original measurement cannot be reconstructed — inventing one would be a
    fabrication. What IS known is stated: the row's own valid_from, the fact
    that the current feed carries no cell for it, and the stored score. The
    shape is the one scripts/market_verify_model.py already uses for carried
    cells (2026-11-20c precedent), so the corpus has ONE vocabulary for
    "real evidence, not republished" rather than two.
    """
    origin = str(row.get("valid_from") or "unknown")
    return (
        f"openrouter-models-api:{origin}"
        f":carried-forward:not-republished-{date}"
        f":category={row.get('category')}:raw_score={row.get('score')}"
    )


def build_rows(
    payload: Any, registry_ids: set[str], date: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build market rows and an auditable report without writing either."""
    UNMAPPED_REASONS.clear()
    models = parse_feed(payload)
    joined = {m["id"] for m in models if m["id"] in registry_ids}
    benchmark_model_ids = {
        m["id"] for m in models if m["id"] in joined and m.get("benchmarks")
    }
    skipped_models: dict[str, str] = {
        m["id"]: "not in openrouter registry" for m in models if m["id"] not in registry_ids
    }
    arena_entries: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    # The feed's `rank` is ARENA-scoped, not category-scoped: one leaderboard per
    # arena, and a model carries the same rank in every category it is listed
    # under (openai/gpt-5.2 is rank 84 in codecategories and rank 72 in website
    # of the same 186-model `models` arena). The percentile population must
    # therefore be the arena's ranked-model count, never the category's entry
    # count — mixing the two populations is what pinned every image-arena row to
    # 0.0 (graphicdesign/image/logo carry n=5 entries but ranks 4..11, and
    # imageediting carries n=1 with rank 3, so 1 - rank/n went negative and the
    # clamp turned "top of class" into "worst in class").
    arena_population: dict[str, set[str]] = defaultdict(set)
    for model in models:
        for entry in (model.get("benchmarks") or {}).get("design_arena", []) or []:
            if not isinstance(entry, dict):
                continue
            arena = str(entry.get("arena") or "")
            category = str(entry.get("category") or "")
            if isinstance(entry.get("rank"), int) and entry["rank"] > 0:
                # The percentile population is the complete feed arena; unjoined
                # ids are excluded only at emission, not denominator.
                arena_entries[(arena, category)].append({"model": model, "entry": entry})
                arena_population[arena].add(model["id"])

    rows: list[dict[str, Any]] = []
    skipped_categories: Counter[str] = Counter()
    rows_by_category: Counter[str] = Counter()
    population_violations: dict[str, str] = {}
    for (arena, source_category), entries in sorted(arena_entries.items()):
        n = len(arena_population[arena])
        category = map_category(arena, source_category)
        if category is None:
            skipped_categories[f"{arena}/{source_category}"] += n
            continue
        max_rank = max(item["entry"]["rank"] for item in entries)
        if max_rank > n:
            # A rank outside its own population cannot be normalized: refuse the
            # arena loudly instead of clamping a negative percentile to 0.0.
            population_violations[arena] = (
                f"max rank {max_rank} exceeds arena population {n}"
            )
            skipped_categories[f"{arena}/{source_category}"] += len(entries)
            continue
        for item in sorted(entries, key=lambda x: (x["entry"]["rank"], x["model"]["id"])):
            if item["model"]["id"] not in joined:
                continue
            entry = item["entry"]
            row = {
                "model": item["model"]["id"],
                "category": category,
                "score": market_percentile(entry["rank"], n),
                "max_score": 1.0,
                "source": "market",
                "source_ref": _source_ref(
                    date, arena=arena, source_category=source_category,
                    raw_elo=entry.get("elo"), rank=entry["rank"], n=n,
                    population="arena",
                ),
                "valid_from": date,
                "reasoning_effort": "default",
            }
            rows.append(row)
            rows_by_category[category] += 1

    # AA indexes are broad composites, so each lives in its own documented
    # category. Normalize the feed's 0-100 values to the requested 0-1 score.
    for model in models:
        if model["id"] not in joined:
            continue
        aa = (model.get("benchmarks") or {}).get("artificial_analysis") or {}
        for index_name, category in AA_INDEX_CATEGORIES.items():
            value = aa.get(index_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append({
                    "model": model["id"],
                    "category": category,
                    "score": round(float(value) / 100.0, 12),
                    "max_score": 1.0,
                    "source": "market",
                    "source_ref": _source_ref(date, index=index_name, raw_score=value),
                    "valid_from": date,
                    "reasoning_effort": "default",
                })
                rows_by_category[category] += 1

    report = {
        "source": API_URL,
        "date": date,
        "feed_models": len(models),
        "joined_models": len(joined),
        "join_rate": len(joined) / len(models) if models else 0.0,
        "benchmark_models_joined": len(benchmark_model_ids),
        "rows_added_by_category": dict(sorted(rows_by_category.items())),
        "skipped_models": skipped_models,
        "skipped_categories": dict(sorted(skipped_categories.items())),
        "population_violations": dict(sorted(population_violations.items())),
        "unmapped_reasons": {
            f"{arena}/{category}": reason
            for (arena, category), reason in sorted(UNMAPPED_REASONS.items())
        },
        "rows_emitted": len(rows),
    }
    return rows, report


def fetch_payload(url: str = API_URL) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter response is not a JSON object")
    return payload


def load_registry_ids(path: str) -> set[str]:
    ids: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("provider") == "openrouter" and row.get("model"):
                ids.add(row["model"])
    return ids


def load_registry_bare_id_map(path: str) -> dict[str, set[str]]:
    """Map each registry model id to its BARE form (vendor prefix stripped).

    Lanes may serve a model under an id that differs from the OpenRouter feed
    id: the feed says 'openai/gpt-6.1-sol' while commandcode serves the same
    underlying model as 'gpt-6.1-sol'. Market evidence describes the MODEL, not
    the lane, so it must reach every lane serving that model (TR-350 defect,
    found 2026-11-16 by the P5_VISION_E2E corpus case: the tier derivation
    never saw commandcode's vision evidence because the market rows were keyed
    under the feed id only).
    """
    bare: dict[str, set[str]] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            model = row.get("model")
            if not model:
                continue
            # every id maps to its bare form — INCLUDING ids that are already
            # bare (a lane serving 'gpt-6.1-sol' unprefixed is the same model
            # the feed lists as 'openai/gpt-6.1-sol'); without the unprefixed
            # arm the mirror never fires for prefix-less lane ids (TR-350).
            bare_form = model.rsplit("/", 1)[1] if "/" in model else model
            bare.setdefault(bare_form, set()).add(model)
    return bare


def load_live_registry_ids(path: str) -> set[str]:
    """Return live OpenRouter model ids for the explicit NULL coverage report."""
    ids: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("provider") != "openrouter" or not row.get("model"):
                continue
            if not row.get("archive") and not row.get("disabled") and not row.get("valid_to"):
                ids.add(row["model"])
    return ids


def emitted_model_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {row["model"] for row in rows if row.get("source") == "market"}


def write_outputs(
    rows: list[dict[str, Any]], report: dict[str, Any], output: str, queue_dir: str,
    missing_registry_models: list[str],
) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if output_path.exists():
        with output_path.open(encoding="utf-8") as handle:
            existing = [json.loads(line) for line in handle if line.strip()]
    # A refresh must CONVERGE, not accumulate. The first landed pass (2026-11-16)
    # wrote 4,914 source=market rows with NO source_ref at all, so a date-prefix
    # filter could never retire them: every later run would append a second,
    # better-shaped copy of the same cells and the file would grow forever while
    # the provenance-less rows kept feeding the tier bridge. Market rows are
    # derived evidence, so the ingest owns them wholesale: drop every prior
    # source=market row for the (model, category) cells this run emits, then
    # write the fresh rows. Cells the feed no longer ranks are dropped too —
    # a stale percentile is worse than an absent one.
    emitted_cells = {(row["model"], row["category"]) for row in rows}
    existing = [
        r for r in existing
        if not (r.get("source") == "market" and (r.get("model"), r.get("category")) in emitted_cells)
    ]
    # Cells the feed no longer ranks are NOT re-emitted, so the filter above
    # leaves their prior rows in place. The pre-provenance pass (2026-11-16/17)
    # wrote those rows with no source_ref at all, which made them unverifiable
    # AND un-retirable: nothing on the row said where the number came from or
    # that the feed had stopped publishing it. Carry them forward instead of
    # dropping them — the evidence is real, it is just not republished — with
    # the same stamp scripts/market_verify_model.py uses. The stamp is
    # idempotent: a carried row already carries a source_ref, so a second run
    # leaves it byte-identical.
    carried = 0
    for row in existing:
        if row.get("source") != "market" or row.get("source_ref"):
            continue
        row["source_ref"] = _frozen_source_ref(row, report["date"])
        carried += 1
    report["carried_forward_rows"] = carried
    with output_path.open("w", encoding="utf-8") as handle:
        for row in existing + rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    queue = Path(queue_dir)
    queue.mkdir(parents=True, exist_ok=True)
    # Coverage is explicit: missing registry ids are listed, never represented
    # by fabricated NULL benchmark rows.
    missing_path = queue / f"market_missing_{report['date']}.txt"
    missing_path.write_text("\n".join(missing_registry_models) + ("\n" if missing_registry_models else ""), encoding="utf-8")
    (queue / f"market_ingest_{report['date']}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="data/tables/models.jsonl")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--queue-dir", default=DEFAULT_QUEUE)
    parser.add_argument("--input", help="captured JSON payload; omit to fetch the live endpoint")
    parser.add_argument("--date", help="evidence date (UTC YYYY-MM-DD)")
    args = parser.parse_args(argv)
    date = args.date or dt.datetime.now(dt.timezone.utc).date().isoformat()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else fetch_payload()
    registry_ids = load_registry_ids(args.models)
    live_registry_ids = load_live_registry_ids(args.models)
    rows, report = build_rows(payload, registry_ids, date)
    # TR-350 fix (2026-11-16): mirror market rows to BARE id variants — lanes
    # may serve the model under a prefixed or unprefixed form that differs from
    # the feed id; the evidence belongs to the model, not one lane's spelling.
    bare_map = load_registry_bare_id_map(args.models)
    mirrored: list[dict[str, Any]] = []
    for row in rows:
        model = row.get("model", "")
        bare_form = model.rsplit("/", 1)[1] if "/" in model else model
        if bare_form == model or bare_form not in bare_map:
            continue
        serving_variants = bare_map[bare_form] - {model}
        if not serving_variants:
            continue
        for variant in sorted(serving_variants):
            alt = dict(row)
            alt["model"] = variant
            mirrored.append(alt)
    rows = rows + mirrored
    feed_ids = {m["id"] for m in parse_feed(payload)}
    missing = sorted(live_registry_ids - emitted_model_ids(rows))
    report["missing_registry_models"] = missing
    report["missing_registry_count"] = len(missing)
    write_outputs(rows, report, args.output, args.queue_dir, missing)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

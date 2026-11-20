#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20f — card t_model_openai_gpt-5.2.

Model: openai/gpt-5.2 (text+image+file->text, 400,000 ctx, thinking-capable).

What this wave changes:

1. PROVENANCE on all thirteen market cells. The rows on disk came from the
   2026-11-17 pre-provenance ingest and carry no source_ref, so the tier bridge
   could not audit a single percentile. They are recomputed from the live feed
   with the current shape (arena, source_category, raw_elo, rank, n,
   population=arena). The recomputation also corrects the pre-02153035
   category-scoped percentiles: the feed's rank is ARENA-scoped (n=188 for the
   `models` arena, n=110 for `agents`), so e.g. code_gen moves 0.5260 -> 0.5532
   and agent_tick 0.6154 -> 0.8091.

2. NO AA CELL. The feed publishes exactly one artificial_analysis value for this
   id — multilingual_index 0.898 — and the vocabulary deliberately maps only
   coding_index / agentic_index / intelligence_index (AA_INDEX_CATEGORIES in
   scripts/market_category_vocab.py). multilingual_index has no router category,
   so nothing is emitted and nothing is invented; the published-but-unmapped
   value is recorded here instead of being silently dropped.

3. PRICE + SPEC verified clean, no change: carrier sticker 1.75/14/cache-read
   0.175 matches the stored public_in/out/cache exactly; models.dev api.json
   agrees (cost 1.75/14/0.175, limit.context 400000, reasoning true,
   text+image->text). Independent web read (openrouter.ai/openai/gpt-5.2 and two
   price aggregators, 2026-11-20) also says 1.75/14. The feed carries a separate
   web_search price (0.01/call) that the registry does not model — recorded in
   price_evidence so the omission is visible rather than silent. release_date
   stays 2025-12-10 (feed created 1765389775 = 2025-12-10T18:02:55Z); models.dev
   and canonical_slug openai/gpt-5.2-20251211 both say 2025-12-11, a one-day
   UTC-vs-publisher-date difference recorded rather than silently rewritten.
   Lane retired 2026-09-26 (replaced_by openrouter/openai/gpt-5.5).

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "openai/gpt-5.2"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120b.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 1.75/14/cache-read 0.175 - "
    "matches the stored public_in/out/cache exactly, so no drift; models.dev "
    "api.json agrees (cost 1.75/14/0.175, limit.context 400000, reasoning true, "
    "text+image->text) and an independent web read of openrouter.ai/openai/"
    "gpt-5.2 plus two price aggregators on 2026-11-20 also says 1.75/14. The "
    "feed also publishes a separate web_search price (0.01 per call) that the "
    "registry does not model - recorded here so the omission is visible rather "
    "than silent. Spec confirmed against the feed: 400,000 ctx (top_provider "
    "400,000), text+image+file->text (stored vision=true is correct), thinking "
    "via supported_parameters.reasoning/reasoning_effort, release_date "
    "2025-12-10 = feed created 1765389775 (2025-12-10T18:02:55Z); models.dev "
    "and canonical_slug openai/gpt-5.2-20251211 both say 2025-12-11, a one-day "
    "UTC-vs-publisher-date difference recorded rather than rewritten. Lane "
    "retired 2026-09-26 (replaced_by openrouter/openai/gpt-5.5). The feed "
    "publishes exactly one artificial_analysis value for this id "
    "(multilingual_index 0.898) and the vocabulary maps only coding/agentic/"
    "intelligence indexes, so no AA cell exists for this model."
)


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)
    fresh = [r for r in rows if r["model"] == MODEL]
    if not fresh:
        print(f"REFUSING: feed emitted no market rows for {MODEL}", file=sys.stderr)
        return 1
    print(f"recomputed {len(fresh)} market cells for {MODEL}")

    lines = BENCH.read_text().splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") == MODEL:
            dropped += 1
            continue
        kept.append(line)

    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in fresh]
    BENCH.write_text("\n".join(kept + new_lines) + "\n", encoding="utf-8")
    print(f"benchmarks.jsonl: dropped {dropped} provenance-less rows, wrote {len(new_lines)}")

    mlines = MODELS.read_text().splitlines()
    mout: list[str] = []
    touched = 0
    for line in mlines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("provider") == "openrouter" and row.get("model") == MODEL:
            row["price_evidence"] = PRICE_EVIDENCE
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-serialised {touched} row(s) with price provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

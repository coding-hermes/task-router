#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20g — card t_model_openai_gpt-5.2:batch.

Model: openai/gpt-5.2:batch (the batch transport alias of openai/gpt-5.2).

What this wave changes:

1. PROVENANCE on all thirteen market cells. The rows on disk came from the
   2026-11-17 pre-provenance ingest and carry no source_ref. They are recomputed
   from the live feed with the current shape (arena, source_category, raw_elo,
   rank, n, population=arena), which also corrects the pre-02153035
   category-scoped percentiles (code_gen 0.5260 -> 0.5532, agent_tick
   0.6154 -> 0.8091).

   CAVEAT RECORDED, NOT HIDDEN: the carrier publishes the SAME design_arena
   entries under the :batch id as under the base id — identical elo, rank and
   win_rate for all thirteen categories. The arena evidence is therefore the
   base model's, carried across the transport alias; the source_ref says
   arena/source_category/rank so a reader can see exactly what was measured.

2. SPEC GAP FILLED with provenance: vision and thinking were NULL on this row
   while the feed publishes modality text+image+file->text and
   supported_parameters reasoning/reasoning_effort. Both are now true, sourced
   from the same 2026-11-20 carrier read. data_class stays 'unknown' — the feed
   publishes no data-handling class and the batch endpoint's handling is not
   evidenced by the base lane's zdr stamp, so it is not copied across.

3. PRICE verified clean, no change: 0.875/7/cache-read 0.0875 is exactly half
   the base lane's 1.75/14/0.175, i.e. the standard 50% batch discount, and it
   matches the carrier sticker exactly. The feed's separate web_search price
   (0.01/call) is not modelled by the registry and is recorded here.

4. NO AA CELL: the only artificial_analysis value published for this id is
   multilingual_index 0.898, and the vocabulary maps only coding/agentic/
   intelligence indexes.

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "openai/gpt-5.2:batch"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120b.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 0.875/7/cache-read 0.0875 - "
    "exactly half the base lane's 1.75/14/0.175 (the standard 50% batch "
    "discount) and an exact match to the stored public_in/out/cache, so no "
    "drift. The feed also publishes a separate web_search price (0.01 per call) "
    "that the registry does not model - recorded here so the omission is "
    "visible rather than silent. Spec filled from the same read: 400,000 ctx, "
    "text+image+file->text (vision=true), reasoning/reasoning_effort in "
    "supported_parameters (thinking=true); both were NULL on this row before "
    "this wave. data_class stays 'unknown': the feed publishes no data-handling "
    "class and the batch endpoint's handling is not evidenced by the base "
    "lane's zdr stamp, so it is not copied across. The carrier publishes the "
    "SAME design_arena entries under this :batch id as under the base id "
    "(identical elo/rank/win_rate in all thirteen categories), so the arena "
    "evidence is the base model's carried across the transport alias. The feed "
    "publishes exactly one artificial_analysis value for this id "
    "(multilingual_index 0.898) and the vocabulary maps only coding/agentic/"
    "intelligence indexes, so no AA cell exists."
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
            row["vision"] = True
            row["thinking"] = True
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-serialised {touched} row(s) with price + spec provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20a — card t_model_z-ai_glm-5.

Model: z-ai/glm-5 (Z.ai GLM-5, text->text, 204,800 ctx, thinking-capable).

What this wave changes:

1. PROVENANCE on all thirteen market cells. The rows on disk came from the
   2026-11-17 pre-provenance ingest and carry no source_ref, so the tier bridge
   could not audit a single percentile. They are recomputed from the live feed
   with the current shape (arena, source_category, raw_elo, rank, n,
   population=arena).

2. The feed's artificial_analysis block for this id is ALL-NULL on the
   2026-11-20 read (intelligence/coding/agentic/finance/... all null; only
   openness_index=50 is published, which is not a capability index). No AA cell
   is emitted, and that absence is recorded here rather than left as a silent
   blank.

3. PRICE: the stored 0.6/1.92/cache 0.12 is the sticker this lane was priced at
   on 2026-09-17 (commit f792a7bc, the 359-lane catalog pass). The live carrier
   now lists 1.0/3.2/cache 0.2 — a 1.67x increase that happened AFTER the lane
   retired (valid_to 2026-09-26, replaced_by openrouter/z-ai/glm-5.3). The
   frozen sticker is therefore correct for the lane's life and no chain can rank
   a retired lane, so the price is NOT rewritten; the post-retirement carrier
   change is recorded in price_evidence so the next reader does not re-derive it.

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "z-ai/glm-5"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 1.0/3.2/cache-read 0.2 and "
    "models.dev api.json (openrouter entry) agrees 1.0/3.2/0.2. The stored "
    "0.6/1.92/cache 0.12 is the sticker this lane was priced at on 2026-09-17 "
    "(commit f792a7bc, the 359-lane catalog pass), i.e. a 1.67x/1.67x/1.67x "
    "carrier increase that landed AFTER the lane retired (valid_to 2026-09-26, "
    "replaced_by openrouter/z-ai/glm-5.3). The frozen sticker is kept because it "
    "is the lane's own retirement-era price and a retired lane cannot be ranked "
    "by any chain; rewriting it to today's carrier value would falsify the "
    "lane's history. Spec confirmed against the feed: 204,800 ctx (top_provider "
    "202,752), text->text, thinking via supported_parameters.reasoning, "
    "release_date 2026-02-11 = feed created 1770829182 / canonical_slug "
    "z-ai/glm-5-20260211 - matches the stored context_limit/vision/thinking/"
    "release_date. AA block all-null on the 2026-11-20 read (only "
    "openness_index=50 published, not a capability index), so no AA cell exists "
    "for this id."
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

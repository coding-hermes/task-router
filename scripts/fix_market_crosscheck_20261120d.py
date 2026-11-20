#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20d — card t_model_z-ai_glm-4.7.

Model: z-ai/glm-4.7 (Z.ai GLM-4.7, text->text, 204,800 ctx, thinking-capable).

What this wave changes:

1. PROVENANCE on all twelve market cells. The rows on disk came from the
   2026-11-17 pre-provenance ingest and carry no source_ref, so the tier bridge
   could not audit a single percentile. They are recomputed from the live feed
   with the current shape (arena, source_category, raw_elo, rank, n,
   population=arena).

2. PRICE REGRESSION RESTORED. The openrouter row carried 0.6/2.2/cache 0.11.
   That is not this model's sticker: commit f792a7bc (2026-09-17) priced the
   lane at 0.4/1.75, and commit 23d42809 (2026-09-26, "add muse-code
   subscription lane") re-serialised the row to 0.6/2.2/0.11 — the exact
   z-ai/glm-4.5 sticker — as collateral of an unrelated lane addition. The live
   carrier read on 2026-11-20 prices this id 0.4/1.75/cache 0.08, i.e. the
   09-17 value, so the 09-26 change was a 1.5x/1.26x/1.375x over-price with no
   discount record anywhere in the registry. Restored to 0.4/1.75/0.08;
   public_price re-blended 0.96*in+0.04*out = 0.454.

3. The feed's artificial_analysis block for this id is ALL-NULL on the
   2026-11-20 read except openness_index=44.4 and multilingual_index=0.799,
   neither of which is a capability index in the registry vocabulary. No AA cell
   is emitted; the absence is recorded here rather than left as a silent blank.

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "z-ai/glm-4.7"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

# The feed's AA block is all-null on the 2026-11-20 read; this is the
# 2026-11-17 published value, kept and dated rather than dropped.
AA_KEEP = {
    "artificial_analysis_coding": ("coding_index", 45.3),
}

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 0.4/1.75/cache-read 0.08; "
    "models.dev api.json agrees. The stored 0.6/2.2/cache 0.11 was a REGRESSION, "
    "not a carrier change: commit f792a7bc (2026-09-17) priced the lane at "
    "0.4/1.75, and commit 23d42809 (2026-09-26, 'add muse-code subscription "
    "lane') re-serialised this row to 0.6/2.2/0.11 - the exact z-ai/glm-4.5 "
    "sticker - as collateral of an unrelated lane addition, a 1.5x/1.26x/1.375x "
    "over-price with no discount record anywhere in the registry. Restored to "
    "the carrier sticker; public_price re-blended 0.96*in+0.04*out = 0.454. Spec "
    "confirmed against the feed: 204,800 ctx (top_provider 202,752), text->text, "
    "thinking via supported_parameters.reasoning, release_date 2025-12-22 = feed "
    "created 1766378014 / canonical_slug z-ai/glm-4.7-20251222 - matches the "
    "stored context_limit/vision/thinking/release_date. Lane retired 2026-09-26 "
    "(replaced_by openrouter/z-ai/glm-5.3). AA block all-null on the 2026-11-20 "
    "read except openness_index=44.4 and multilingual_index=0.799, neither a "
    "capability index in the registry vocabulary, so no AA cell exists."
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
    aa_rows: list[dict] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") == MODEL:
            if row.get("category") in AA_KEEP:
                aa_rows.append(row)
            dropped += 1
            continue
        kept.append(line)

    aa_out: list[dict] = []
    for row in aa_rows:
        index_name, raw = AA_KEEP[row["category"]]
        row = dict(row)
        row["source_ref"] = (
            f"openrouter-models-api:2026-11-17:index={index_name}:raw_score={raw}"
            f":not-republished-{DATE}"
        )
        aa_out.append(row)
    print(f"kept {len(aa_out)} AA cell(s) with not-republished provenance")

    new_rows = list(fresh) + aa_out
    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in new_rows]
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
            row["normalized_price"] = 0.4
            row["public_in_per_m"] = 0.4
            row["public_out_per_m"] = 1.75
            row["public_cache_read_per_m"] = 0.08
            row["public_price"] = 0.454
            row["price_evidence"] = PRICE_EVIDENCE
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-priced + re-serialised {touched} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

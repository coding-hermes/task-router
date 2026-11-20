#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20i — the GPT-5.1 family (4 cards) + the two
GPT-5 image lanes (2 cards).

Cards closed by this wave:
  t_model_openai_gpt-5.1            openai/gpt-5.1
  t_model_openai_gpt-5.1:batch      openai/gpt-5.1:batch
  t_model_openai_gpt-5.1-codex      openai/gpt-5.1-codex
  t_model_openai_gpt-5.1-codex-mini openai/gpt-5.1-codex-mini
  t_model_openai_gpt-5-image-mini   openai/gpt-5-image-mini
  t_model_openai_gpt-5-image        openai/gpt-5-image

What this wave changes:

1. PROVENANCE on every design_arena cell of the six lanes. The rows on disk came
   from the 2026-11-17 pre-provenance ingest and carry no source_ref, so the tier
   bridge could not audit a single percentile. They are recomputed from the live
   2026-11-20 feed with the current shape (arena, source_category, raw_elo, rank,
   n, population=arena). Percentiles move because the arena population grew, not
   because the models changed: the feed's rank is arena-scoped and the
   denominator is the arena's ranked-model count.

   CAVEAT RECORDED, NOT HIDDEN: the carrier publishes the SAME design_arena
   entries under the :batch id as under the base id — identical elo, rank and
   win_rate for every category. The arena evidence is therefore the base model's,
   carried across the transport alias; the source_ref says
   arena/source_category/rank so a reader can see exactly what was measured.

2. AA cells are PRESERVED, not dropped. The 2026-11-20 feed republishes the
   artificial_analysis block with every index null for these ids (the 2026-11-17
   read carried coding_index 49.4 for the gpt-5.1 pair), so a plain re-ingest
   would silently delete a real published measurement. The surviving cells are
   re-stamped with the read they came from and the fact that the newer read did
   not republish them. AA's public site now reports Intelligence Index v5.0.1
   values (gpt-5.1 25, gpt-5.1-codex 36) — a DIFFERENT index vintage from the
   feed's 0-100 block, so no cell is emitted from it: mixing vintages inside one
   category would corrupt the tier bridge. Recorded, not silently dropped.

3. SPEC GAP filled on openai/gpt-5.1:batch: vision/thinking were NULL while the
   feed publishes modality text+image+file->text and reasoning in
   supported_parameters. Set true from the feed, same repair as the gpt-5.2:batch
   card.

4. PRICE verified against the feed sticker for all six lanes; no drift. Two
   unmodelled rates are recorded rather than left silent: the feed's per-call
   web_search price (0.01) on the four text lanes, and the image_output rate on
   the two image lanes (gpt-5-image 40/M, gpt-5-image-mini 8/M) which the
   registry's single public_out_per_m column cannot express — OpenRouter's own
   model page quotes gpt-5-image as "$10 per million input tokens, $40 per
   million output tokens", so public_out_per_m=10 is the TEXT-output sticker and
   the 4x image-output rate is the unmodelled half.

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

DATE = "2026-11-20"
PRIOR_READ = "2026-11-17"
FEED = Path("/tmp/or_models_20261120b.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

# Per-lane evidence. `price` is the cross-check story written to price_evidence;
# `spec` is an optional dict of models.jsonl fields to fill.
LANES: dict[str, dict] = {
    "openai/gpt-5.1": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 1.25/10/cache-read 0.125 "
            "- matches the stored public_in/out/cache exactly, so no drift. "
            "Publisher corroboration: platform.openai.com/docs/models/gpt-5.1 and "
            "three independent price trackers (requesty.ai, pricepertoken.com, "
            "openmark.ai) all quote $1.25/1M in, $10/1M out, 400K context. Spec "
            "confirmed against the feed: 400,000 ctx (top_provider 400,000, "
            "max_completion_tokens 128,000), text+image+file->text (stored "
            "vision=true is correct), thinking via supported_parameters.reasoning "
            "with supported_efforts none/low/medium/high (default medium), "
            "release_date 2025-11-13 = feed created 1763060305 / canonical_slug "
            "openai/gpt-5.1-20251113. The feed also publishes a separate "
            "web_search price (0.01 per call) that the registry does not model - "
            "recorded here so the omission is visible rather than silent. Lane "
            "retired 2026-09-26 (replaced_by openrouter/openai/gpt-5.5)."
        ),
    },
    "openai/gpt-5.1:batch": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 0.625/5/cache-read "
            "0.0625 - exactly 50% of the standard gpt-5.1 sticker (1.25/10/0.125) "
            "on all three axes, which is the documented batch discount, so no "
            "drift. Spec confirmed against the feed: 400,000 ctx (top_provider "
            "400,000), text+image+file->text, thinking via "
            "supported_parameters.reasoning, release_date 2025-11-13. The feed "
            "also publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "data_class stays the explicit unknown stamp (TR-197: unknown != "
            "missing; the batch SKU's data-handling class is not published). "
            "CAVEAT: the carrier publishes the SAME design_arena entries under "
            "this :batch id as under openai/gpt-5.1 (identical elo/rank/win_rate "
            "in every category), so the arena evidence is the base model's "
            "carried across the transport alias."
        ),
        "spec": {"vision": True, "thinking": True},
    },
    "openai/gpt-5.1-codex": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 1.25/10/cache-read 0.13 "
            "- matches the stored public_in/out/cache exactly, so no drift. Spec "
            "confirmed against the feed: 400,000 ctx (top_provider 400,000), "
            "text+image->text (stored vision=true is correct), thinking via "
            "supported_parameters.reasoning, release_date 2025-11-13 = feed "
            "created 1763060305 / canonical_slug openai/gpt-5.1-codex-20251113. "
            "The feed also publishes a separate web_search price (0.01 per call) "
            "that the registry does not model - recorded here so the omission is "
            "visible. Lane retired 2026-09-26 (replaced_by "
            "openrouter/openai/gpt-5.3-codex)."
        ),
    },
    "openai/gpt-5.1-codex-mini": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 0.25/2/cache-read 0.03 "
            "- matches the stored public_in/out/cache exactly, so no drift. Spec "
            "confirmed against the feed: 400,000 ctx (top_provider 400,000), "
            "text+image->text (stored vision=true is correct), thinking via "
            "supported_parameters.reasoning, release_date 2025-11-13. The feed "
            "also publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible."
        ),
    },
    "openai/gpt-5-image-mini": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 2.5/2/cache-read 0.25 "
            "for TEXT tokens - matches the stored public_in/out/cache exactly, so "
            "no drift. UNMODELLED: the same feed publishes image_output 0.000008 "
            "= $8/1M image-output tokens, which the registry's single "
            "public_out_per_m column cannot express; public_out_per_m=2 is the "
            "text-output sticker and the image-output rate is recorded here so the "
            "gap is visible rather than silent. Spec confirmed against the feed: "
            "400,000 ctx, text+image+file->text+image (stored vision=true and "
            "cap_image_generation=true are correct), thinking via "
            "supported_parameters.reasoning, release_date 2025-10-16."
        ),
    },
    "openai/gpt-5-image": {
        "price": (
            "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
            "/api/v1/models read 2026-11-20 prices this id 10/10/cache-read 1.25 "
            "for TEXT tokens - matches the stored public_in/out/cache exactly, so "
            "no drift. UNMODELLED: the same feed publishes image_output 0.00004 = "
            "$40/1M image-output tokens, and OpenRouter's own model page quotes "
            "this id as \"$10 per million input tokens, $40 per million output "
            "tokens\" - so public_out_per_m=10 is the TEXT-output sticker and the "
            "4x image-output rate is the unmodelled half. Recorded here so the gap "
            "is visible rather than silent. Spec confirmed against the feed: "
            "400,000 ctx, text+image+file->text+image (stored vision=true and "
            "cap_image_generation=true are correct), thinking via "
            "supported_parameters.reasoning, release_date 2025-10-14. Lane retired "
            "2026-09-26 (replaced_by openrouter/openai/gpt-5.4-image-2)."
        ),
    },
}

# AA cells the 2026-11-20 feed no longer republishes, with the read they came
# from. Only cells that exist on disk today are preserved; the value is the
# raw 0-100 index reading from that read.
PRESERVED_AA: dict[str, dict[str, float]] = {
    "openai/gpt-5.1": {"artificial_analysis_coding": 49.4},
    "openai/gpt-5.1:batch": {"artificial_analysis_coding": 49.4},
}


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)

    fresh: dict[str, list[dict]] = {model: [] for model in LANES}
    for row in rows:
        if row["model"] in fresh:
            fresh[row["model"]].append(row)

    missing = [model for model, got in fresh.items() if not got]
    if missing:
        print(f"REFUSING: feed emitted no market rows for {missing}", file=sys.stderr)
        return 1

    # Preserve the AA cells the newer read dropped, re-stamped with provenance.
    for model, cells in PRESERVED_AA.items():
        for category, raw in cells.items():
            fresh[model].append({
                "model": model,
                "category": category,
                "score": round(raw / 100.0, 12),
                "max_score": 1.0,
                "source": "market",
                "source_ref": (
                    f"openrouter-models-api:{PRIOR_READ}:index=coding_index:"
                    f"raw_score={raw}:not-republished={DATE}"
                ),
                "valid_from": PRIOR_READ,
                "reasoning_effort": "default",
            })

    for model, got in fresh.items():
        print(f"recomputed {len(got)} market cells for {model}")

    lines = BENCH.read_text().splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") in LANES:
            dropped += 1
            continue
        kept.append(line)

    new_lines: list[str] = []
    for model in LANES:
        new_lines.extend(
            json.dumps(r, ensure_ascii=False, sort_keys=True) for r in fresh[model]
        )
    BENCH.write_text("\n".join(kept + new_lines) + "\n", encoding="utf-8")
    print(f"benchmarks.jsonl: dropped {dropped} provenance-less rows, wrote {len(new_lines)}")

    mlines = MODELS.read_text().splitlines()
    mout: list[str] = []
    touched = 0
    for line in mlines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("provider") == "openrouter" and row.get("model") in LANES:
            lane = LANES[row["model"]]
            row["price_evidence"] = lane["price"]
            for field, value in (lane.get("spec") or {}).items():
                if row.get(field) is None:
                    row[field] = value
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-serialised {touched} row(s) with price provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

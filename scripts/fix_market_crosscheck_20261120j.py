#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20j — the GPT-5 launch family (6 cards).

Cards closed by this wave:
  t_model_openai_gpt-5               openai/gpt-5
  t_model_openai_gpt-5:batch         openai/gpt-5:batch
  t_model_openai_gpt-5-mini          openai/gpt-5-mini
  t_model_openai_gpt-5-mini:batch    openai/gpt-5-mini:batch
  t_model_openai_gpt-5-nano          openai/gpt-5-nano
  t_model_openai_gpt-5-nano:batch    openai/gpt-5-nano:batch

What this wave changes:

1. PROVENANCE on every design_arena cell of the six lanes. The rows on disk came
   from the 2026-11-17 pre-provenance ingest and carry no source_ref, so the tier
   bridge could not audit a single percentile. They are recomputed from the live
   2026-11-20 feed with the current shape (arena, source_category, raw_elo, rank,
   n, population=arena). Percentiles move because the arena population grew
   (n=188 today), not because the models changed.

2. AA cells are PRESERVED, not dropped. The 2026-11-20 feed republishes the
   artificial_analysis block with every composite index null for these ids (the
   2026-11-17 read carried coding 37.8 for gpt-5 and coding 15.6 / agentic 6.8
   for gpt-5-mini), so a plain re-ingest would silently delete three real
   published measurements. The surviving cells are re-stamped with the read they
   came from and the fact that the newer read did not republish them. AA's public
   site now reports Intelligence Index v5.0.1 values (gpt-5-mini 21, gpt-5-nano
   13) — a DIFFERENT index vintage from the feed's 0-100 block, so no cell is
   emitted from it: mixing vintages inside one category would corrupt the tier
   bridge. Recorded, not silently dropped.

3. SPEC GAP filled on the three :batch lanes: vision, thinking and
   cap_image_generation were NULL while the feed publishes
   modality text+image+file->text (input_modalities text/image/file,
   output_modalities text) and reasoning/reasoning_effort in
   supported_parameters. Filled from the same read; same repair as the
   gpt-5.1:batch and gpt-5.2:batch cards.

4. PRICE verified against the feed sticker for all six lanes; no drift. The three
   :batch ids are exactly 50% of their base lane on all three axes
   (in/out/cache-read), which is the documented batch discount. One unmodelled
   rate is recorded rather than left silent: the feed's per-call web_search price
   (0.01) on all six lanes. cap_web_search stays NULL — a per-call price is not a
   capability measurement, and only the search-native sonar lanes carry
   cap_web_search=true.

5. EXTERNAL PUBLISHED rank recorded in the narrative, NOT emitted as a cell.
   OpenAI's launch post gives gpt-5 74.9% SWE-bench Verified, 88% Aider polyglot
   (94% with thinking). Emitting them as a new source class into benchmarks.jsonl
   would change the tier bridge while t_market_precedence (open card) still has
   the bridge taking max(score) per cell — a percentile would outrank the
   published absolute, so the cell would be inert or actively wrong. Recorded
   here, emitted after the precedence fix lands.

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
FEED = Path("/tmp/or_models_20261120c.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")
STAMPS = Path("data/null_reasons.jsonl")

# Stamps that this wave makes FALSE: the 2026-11-20 feed publishes
# output_modalities ['text'] for these lanes, so "no catalog modality or
# name-marker evidence for this cap" no longer holds. A stamp may only name a
# field the row leaves NULL (tests/test_null_census.py enforces it), so the
# stamp is dropped in the same pass that fills the field.
DROP_STAMPS: dict[str, set[str]] = {
    "openai/gpt-5:batch": {"cap_image_generation"},
    "openai/gpt-5-mini:batch": {"cap_image_generation"},
    "openai/gpt-5-nano:batch": {"cap_image_generation"},
}

_PRICE_HEAD = "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "

# Per-lane evidence. `price` is the cross-check story written to price_evidence;
# `spec` is an optional dict of models.jsonl fields to fill when NULL.
LANES: dict[str, dict] = {
    "openai/gpt-5": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 1.25/10/cache-read "
            "0.125 - matches the stored public_in/out/cache exactly, so no drift. "
            "Publisher corroboration: developers.openai.com/api/docs/models/gpt-5 "
            "quotes $1.25/1M in, $10/1M out, 400,000 context window, "
            "reasoning.effort minimal/low/medium/high; three independent price "
            "trackers (pricepertoken.com, userightai.com, benchgen.com) quote the "
            "same sticker and the same 400K context. Spec confirmed against the "
            "feed: 400,000 ctx (top_provider 400,000, max_completion_tokens "
            "128,000), text+image+file->text (stored vision=true is correct), "
            "thinking via supported_parameters.reasoning + reasoning_effort "
            "(stored thinking=true is correct), release_date 2025-08-07 = feed "
            "created 1754587413 / canonical_slug openai/gpt-5-2025-08-07. The "
            "feed also publishes a separate web_search price (0.01 per call) that "
            "the registry does not model - recorded here so the omission is "
            "visible rather than silent; cap_web_search stays NULL because a "
            "per-call price is not a capability measurement. EXTERNAL PUBLISHED, "
            "recorded not emitted: OpenAI's launch post reports 74.9% SWE-bench "
            "Verified and 88% Aider polyglot (94% with thinking) - held out of "
            "benchmarks.jsonl until t_market_precedence stops the bridge taking "
            "max(score) across source classes. Lane retired 2026-09-26 "
            "(replaced_by openrouter/openai/gpt-5.5)."
        ),
    },
    "openai/gpt-5:batch": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 0.625/5/cache-read "
            "0.0625 - exactly 50% of the standard gpt-5 sticker (1.25/10/0.125) "
            "on all three axes, which is the documented batch discount, so no "
            "drift. Spec filled from the same read: 400,000 ctx (top_provider "
            "400,000, max_completion_tokens 128,000), modality "
            "text+image+file->text with output_modalities ['text'] (vision=true "
            "and cap_image_generation=false; both were NULL on this row before "
            "this wave), reasoning/reasoning_effort in supported_parameters "
            "(thinking=true, was NULL), release_date 2025-08-07. The feed also "
            "publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "data_class stays the explicit unknown stamp (TR-197: unknown != "
            "missing; the batch SKU's data-handling class is not published and "
            "the base lane's zdr stamp is not copied across an endpoint "
            "boundary). CAVEAT: the carrier publishes the SAME design_arena "
            "entries under this :batch id as under openai/gpt-5 (identical "
            "elo/rank/win_rate in every category), so the arena evidence is the "
            "base model's carried across the transport alias."
        ),
        "spec": {"vision": True, "thinking": True, "cap_image_generation": False},
    },
    "openai/gpt-5-mini": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 0.25/2/cache-read "
            "0.025 - matches the stored public_in/out/cache exactly, so no drift. "
            "Publisher corroboration: developers.openai.com/api/docs/models/"
            "gpt-5-mini is the canonical page; OpenRouter's own compare page "
            "quotes gpt-5-mini at $0.25/1M in and $2/1M out against nano's "
            "$0.05/$0.40, and awesomeagents.ai independently quotes 400K context "
            "at the same sticker. Spec confirmed against the feed: 400,000 ctx "
            "(top_provider 400,000, max_completion_tokens 128,000), "
            "text+image+file->text (stored vision=true is correct), thinking via "
            "supported_parameters.reasoning + reasoning_effort (stored "
            "thinking=true is correct), release_date 2025-08-07 = feed created "
            "1754587407 / canonical_slug openai/gpt-5-mini-2025-08-07. The feed "
            "also publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "AA's public site reports Intelligence Index v5.0.1 21 for this id - a "
            "different vintage from the feed's 0-100 block, so no cell is "
            "emitted; the 2026-11-17 coding 15.6 / agentic 6.8 cells are preserved "
            "with provenance instead. Lane retired 2026-09-26 (replaced_by "
            "openrouter/openai/gpt-5.4-mini)."
        ),
    },
    "openai/gpt-5-mini:batch": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 0.125/1/cache-read "
            "0.0125 - exactly 50% of the standard gpt-5-mini sticker "
            "(0.25/2/0.025) on all three axes, which is the documented batch "
            "discount, so no drift. Spec filled from the same read: 400,000 ctx "
            "(top_provider 400,000, max_completion_tokens 128,000), modality "
            "text+image+file->text with output_modalities ['text'] (vision=true "
            "and cap_image_generation=false; both were NULL on this row before "
            "this wave), reasoning/reasoning_effort in supported_parameters "
            "(thinking=true, was NULL), release_date 2025-08-07. The feed also "
            "publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "data_class stays the explicit unknown stamp (TR-197: unknown != "
            "missing). CAVEAT: the carrier publishes the SAME design_arena "
            "entries under this :batch id as under openai/gpt-5-mini (identical "
            "elo/rank/win_rate in every category), so the arena evidence is the "
            "base model's carried across the transport alias."
        ),
        "spec": {"vision": True, "thinking": True, "cap_image_generation": False},
    },
    "openai/gpt-5-nano": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 0.05/0.4/cache-read "
            "0.005 - matches the stored public_in/out/cache exactly, so no drift. "
            "Publisher corroboration: OpenRouter's own compare page quotes "
            "gpt-5-nano at $0.05/1M in and $0.40/1M out against mini's "
            "$0.25/$2.00. Spec confirmed against the feed: 400,000 ctx "
            "(top_provider 400,000, max_completion_tokens 128,000), "
            "text+image+file->text (stored vision=true is correct), thinking via "
            "supported_parameters.reasoning + reasoning_effort (stored "
            "thinking=true is correct), release_date 2025-08-07 = feed created "
            "1754587402 / canonical_slug openai/gpt-5-nano-2025-08-07. The feed "
            "also publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "AA's public site reports Intelligence Index v5.0.1 13 for this id - a "
            "different vintage from the feed's 0-100 block, so no cell is "
            "emitted; this lane has no AA cell on disk and none is invented. Lane "
            "retired 2026-09-26 (replaced_by openrouter/openai/gpt-5.4-nano)."
        ),
    },
    "openai/gpt-5-nano:batch": {
        "price": (
            _PRICE_HEAD
            + "/api/v1/models read 2026-11-20 prices this id 0.025/0.2/cache-read "
            "0.0025 - exactly 50% of the standard gpt-5-nano sticker "
            "(0.05/0.4/0.005) on all three axes, which is the documented batch "
            "discount, so no drift. Spec filled from the same read: 400,000 ctx "
            "(top_provider 400,000, max_completion_tokens 128,000), modality "
            "text+image+file->text with output_modalities ['text'] (vision=true "
            "and cap_image_generation=false; both were NULL on this row before "
            "this wave), reasoning/reasoning_effort in supported_parameters "
            "(thinking=true, was NULL), release_date 2025-08-07. The feed also "
            "publishes a separate web_search price (0.01 per call) that the "
            "registry does not model - recorded here so the omission is visible. "
            "data_class stays the explicit unknown stamp (TR-197: unknown != "
            "missing). CAVEAT: the carrier publishes the SAME design_arena "
            "entries under this :batch id as under openai/gpt-5-nano (identical "
            "elo/rank/win_rate in every category), so the arena evidence is the "
            "base model's carried across the transport alias."
        ),
        "spec": {"vision": True, "thinking": True, "cap_image_generation": False},
    },
}

# AA cells the 2026-11-20 feed no longer republishes, with the read they came
# from. Only cells that exist on disk today are preserved; the value is the
# raw 0-100 index reading from that read.
PRESERVED_AA: dict[str, dict[str, float]] = {
    "openai/gpt-5": {"artificial_analysis_coding": 37.8},
    "openai/gpt-5:batch": {"artificial_analysis_coding": 37.8},
    "openai/gpt-5-mini": {
        "artificial_analysis_coding": 15.6,
        "artificial_analysis_agentic": 6.8,
    },
    "openai/gpt-5-mini:batch": {
        "artificial_analysis_coding": 15.6,
        "artificial_analysis_agentic": 6.8,
    },
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
    provenance_less = [
        r["model"] for got in fresh.values() for r in got if not r.get("source_ref")
    ]
    if provenance_less:
        print(f"REFUSING: cells without source_ref: {provenance_less}", file=sys.stderr)
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
                    f"openrouter-models-api:{PRIOR_READ}:index={category}:"
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
    filled: list[str] = []
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
                    filled.append(f"{row['model']}.{field}={value}")
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-serialised {touched} row(s) with price provenance")
    print(f"spec fields filled: {filled}")

    # Drop the stamps this wave made false, and refuse if the count is not what
    # the fill implies (a silent 0 would mean the stamp file moved).
    stamp_lines = STAMPS.read_text().splitlines()
    kept_stamps: list[str] = []
    dropped_stamps = 0
    for line in stamp_lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("model") in DROP_STAMPS and row.get("field") in DROP_STAMPS[row["model"]]:
            dropped_stamps += 1
            continue
        kept_stamps.append(line)
    expected = sum(len(v) for v in DROP_STAMPS.values())
    if dropped_stamps != expected:
        print(
            f"REFUSING: dropped {dropped_stamps} stamps, expected {expected}",
            file=sys.stderr,
        )
        return 2
    STAMPS.write_text("\n".join(kept_stamps) + "\n", encoding="utf-8")
    print(f"null_reasons.jsonl: dropped {dropped_stamps} stale stamp(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

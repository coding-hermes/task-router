#!/usr/bin/env python3
"""Market cross-check wave 2026-11-18 (cards t_model_nvidia_nemotron-3-ultra-550b-a55b:free,
t_model_qwen_qwen3.7-plus, t_model_qwen_qwen3.7-plus:batch, t_model_stepfun_step-3.7-flash,
t_model_qwen_qwen3.7-max, t_model_x-ai_grok-build-0.1).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
DATE = "2026-11-18"
FEED = "carrier:openrouter /api/v1/models read 2026-11-18 (480 ids)"
MD = "models.dev api.json read 2026-11-18 (openrouter provider, 382 ids)"

UPDATES = {
    # The :batch SKU was a thin row: thinking/vision/release_date/cap_* all NULL
    # while its own base SKU and both carriers agree. Same defect class as the
    # other thin :batch rows backfilled on 2026-10-29.
    ("openrouter", "qwen/qwen3.7-plus:batch"): {
        "thinking": True,
        "vision": True,
        "release_date": "2026-06-02",
        "cap_audio": False,
        "cap_embedding": False,
        "cap_image_generation": False,
        "cap_video": False,
    },
    # Release dates: the row carried a date 1-2 days EARLY against the canonical
    # slug, models.dev and the publisher announcement, on all three Qwen/xAI rows.
    ("openrouter", "qwen/qwen3.7-plus"): {"release_date": "2026-06-02"},
    ("openrouter", "qwen/qwen3.7-max"): {"release_date": "2026-05-20"},
    ("openrouter", "x-ai/grok-build-0.1"): {"release_date": "2026-05-20"},
}

PROVENANCE = {
    "nvidia/nemotron-3-ultra-550b-a55b:free": (
        "market cross-check 2026-11-18: no field change needed. " + FEED +
        " (id nvidia/nemotron-3-ultra-550b-a55b:free, canonical_slug "
        "nvidia/nemotron-3-ultra-550b-a55b-20260604, created 2026-06-04T05:33Z, "
        "1,000,000 ctx, 65,536 max completion, text->text, pricing 0/0, "
        "supported_parameters include reasoning + reasoning_effort, hf "
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16) and " + MD +
        " (cost 0/0, limit 1,000,000/65,536, text->text, reasoning true, "
        "tool_call true, release_date 2026-06-04, knowledge 2025-09) agree with "
        "the row on every spec field it carries."
    ),
    "qwen/qwen3.7-plus": (
        "market cross-check 2026-11-18: release_date 2026-06-01 -> 2026-06-02. "
        + FEED + " canonical_slug qwen/qwen3.7-plus-20260602 (created "
        "2026-06-03T13:03Z) and " + MD + " release_date 2026-06-02 agree; the "
        "publisher launch coverage is dated 2026-06-02 (MarkTechPost, MLQ, "
        "VentureBeat) with one outlet dating GA 2026-06-01. Price/spec verified "
        "clean: 0.32/1.28/cache 0.064/0.4 matches both carriers exactly, "
        "1,000,000 ctx, text+image->text (vision true), reasoning true."
    ),
    "qwen/qwen3.7-plus:batch": (
        "market cross-check 2026-11-18: thin row completed. " + FEED +
        " (id qwen/qwen3.7-plus:batch, canonical_slug qwen/qwen3.7-plus-20260602, "
        "262,144 ctx, 131,072 max completion, text+image->text, pricing "
        "0.23/0.92 with no cache sticker, reasoning + reasoning_effort in "
        "supported_parameters) and the base SKU's own row agree: thinking true, "
        "vision true, release_date 2026-06-02, no audio/embedding/image-gen/video. "
        "models.dev publishes no :batch entry, so the carrier is the sole source."
    ),
    "stepfun/step-3.7-flash": (
        "market cross-check 2026-11-18: no field change needed. " + FEED +
        " (id stepfun/step-3.7-flash, canonical_slug stepfun/step-3.7-flash-20260528, "
        "created 2026-05-28T16:17Z, 262,144 ctx, 230,400 max completion, "
        "text+image+video->text, pricing 0.2/1.15/cache 0.04, reasoning + "
        "reasoning_effort supported, hf stepfun-ai/Step-3.7-Flash) and " + MD +
        " (cost 0.2/1.15/cache 0.04, limit 262,144/230,400, text+image+video, "
        "reasoning true, release_date 2026-05-29) agree with the row: 0.2/1.15/"
        "cache 0.04, 262,144 ctx, vision true, cap_video true, thinking true, "
        "release 2026-05-29."
    ),
    "qwen/qwen3.7-max": (
        "market cross-check 2026-11-18: release_date 2026-05-19 -> 2026-05-20. "
        "The publisher announcement is the 2026-05-20 Alibaba Cloud Summit "
        "(codersera, digitalapplied, codersera launch guide); " + FEED +
        " canonical_slug qwen/qwen3.7-max-20260520 (created 2026-05-21T15:21Z) "
        "and " + MD + " release_date 2026-05-21 bracket it. Two outlets date it "
        "2026-05-19. Price/spec verified clean: 1.475/4.425/cache 0.295/1.84375 "
        "matches both carriers, 1,000,000 ctx, text->text (vision false), "
        "reasoning true. Lane is retired (valid_to 2026-09-26, replaced_by "
        "openrouter/qwen/qwen3.8-max-0902); the lifecycle record is left intact."
    ),
    "x-ai/grok-build-0.1": (
        "market cross-check 2026-11-18: release_date 2026-05-19 -> 2026-05-20. "
        + FEED + " canonical_slug x-ai/grok-build-0.1-20260520 (created "
        "2026-05-20T17:28Z) and two independent write-ups (basenor, "
        "awesomeagents) date the model 2026-05-20; " + MD + " says 2026-04-16, "
        "which is the outlier and is rejected. Price/spec verified clean: "
        "1.0/2.0/cache 0.2 matches both carriers, 256,000 ctx, text+image+file "
        "(vision true), reasoning true."
    ),
}

NOTE_TEXT = {
    "nvidia/nemotron-3-ultra-550b-a55b:free": (
        "market cross-check 2026-11-18: VERIFIED CLEAN, no field change. " + FEED +
        " and " + MD + " agree with the row on every spec field: 1,000,000 ctx, "
        "text->text (vision false, cap_video false), reasoning true with a "
        "reasoning_effort ladder, release 2026-06-04, hf "
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16, 65,536 max completion. "
        "PRICE: the carrier sticker is 0/0 and the row keeps public_price 0.0 "
        "(the monthly-pool truth) while normalized_price stays 1.5 under the "
        "xKiro window-cost rule (a free SKU draws the metered window at its "
        "paid-sibling list-equivalent, 0.6/2.4 = the BaseTen endpoint the paid "
        "sibling was just re-verified against on 2026-11-18). That is doctrine, "
        "not drift, and it is deliberately NOT re-priced here. EVIDENCE CLASS: "
        "the only internal rows are n=1 small-probes (agent_tick 3/4) plus the "
        "market composites (AA coding 0.493, agentic 0.201, intelligence 0.118, "
        "8 design_arena arenas) — no n>=3 measurement and no large-bench source, "
        "so under the stated precedence this lane has no head-seat-grade "
        "evidence. cap_web_search stays NULL: the feed publishes no web_search "
        "price for this id, and a carrier declaration is not a measurement."
    ),
    "qwen/qwen3.7-plus": (
        "market cross-check 2026-11-18: ONE DATE FIXED (release_date 2026-06-01 "
        "-> 2026-06-02; canonical_slug qwen/qwen3.7-plus-20260602, " + MD +
        " 2026-06-02, publisher coverage dated 2026-06-02). Everything else "
        "verified clean against both carriers: 0.32/1.28/cache 0.064/0.4, "
        "1,000,000 ctx, text+image->text (vision true), reasoning true, "
        "tool_call true, knowledge 2025-04. TWO DOCUMENTED GAPS, deliberately "
        "not changed: (1) the carrier publishes a CONTEXT TIER — above 256,000 "
        "prompt tokens the sticker is 0.96/3.84/cache 0.192/1.2, i.e. 3x — and "
        "the row carries only the base tier; the registry's convention is to "
        "rank on the base sticker (the same call made for the time-windowed hy3 "
        "pair), so a context-tier-aware price is a pricing-policy change, not a "
        "per-row edit. (2) VentureBeat reports the model accepts text, VIDEO and "
        "imagery at $0.4/$1.6 per 1M; both carriers publish text+image only and "
        "0.32/1.28, so cap_video stays false and the price stays the carrier "
        "sticker — the publisher's direct list is not the openrouter lane's "
        "price. EVIDENCE CLASS: market composites only (AA coding 0.559, agentic "
        "0.175, intelligence NULL, 8 design_arena arenas) plus a market-derived "
        "code_gen 0.78; no n>=3 internal measurement, so no head-seat-grade "
        "evidence. cap_web_search stays NULL (no web_search price published)."
    ),
    "qwen/qwen3.7-plus:batch": (
        "market cross-check 2026-11-18: THIN ROW COMPLETED — thinking NULL->true, "
        "vision NULL->true, release_date NULL->2026-06-02, cap_audio/"
        "cap_embedding/cap_image_generation/cap_video NULL->false. " + FEED +
        " publishes the :batch SKU as text+image->text with reasoning + "
        "reasoning_effort supported, 262,144 ctx, 131,072 max completion, "
        "0.23/0.92 and NO cache sticker; the base SKU's row and " + MD +
        " agree on the model-level facts (models.dev has no :batch entry, so the "
        "carrier is the sole source for the SKU's own limits). PRICE verified "
        "clean: 0.23/0.92 is exactly the carrier sticker and 0.8x the base SKU, "
        "which is the batch discount, not drift. The row's public_cache_read/"
        "write stay NULL because the carrier publishes no cache price for the "
        "batch SKU. cap_web_search and cap_computer_use stay NULL (carrier "
        "declaration, not a measurement). EVIDENCE CLASS: the market composites "
        "are inherited from the base model (AA coding 0.559, agentic 0.175, "
        "code_gen 0.78); no n>=3 internal measurement exists for the batch SKU."
    ),
    "stepfun/step-3.7-flash": (
        "market cross-check 2026-11-18: VERIFIED CLEAN, no field change. " + FEED +
        " and " + MD + " agree with the row on every spec field: 0.2/1.15/cache "
        "0.04, 262,144 ctx, text+image+VIDEO->text (vision true, cap_video true), "
        "reasoning true with a reasoning_effort ladder, release 2026-05-29, hf "
        "stepfun-ai/Step-3.7-Flash, 230,400 max completion, knowledge 2026-03-01. "
        "ONE DOCUMENTED GAP: the carrier's top_provider block advertises a "
        "256,000 input window while the model-level context_length is 262,144 "
        "(models.dev publishes both: context 262,144, input 256,000). The row "
        "keeps the model-level 262,144 — the same choice every other lane makes — "
        "so a request sized between 256k and 262k could be refused by the "
        "endpoint; that is a routing-safety question for the context guard, not "
        "a market-evidence edit. EVIDENCE CLASS: market composites only (AA "
        "coding 0.396, agentic NULL, intelligence NULL, 8 design_arena arenas) "
        "plus market-derived code_gen 0.572 and creative 0.451; no n>=3 internal "
        "measurement, so no head-seat-grade evidence. cap_web_search stays NULL."
    ),
    "qwen/qwen3.7-max": (
        "market cross-check 2026-11-18: ONE DATE FIXED (release_date 2026-05-19 "
        "-> 2026-05-20, the Alibaba Cloud Summit announcement date; canonical_slug "
        "qwen/qwen3.7-max-20260520, " + MD + " 2026-05-21, two outlets 2026-05-19). "
        "Everything else verified clean against both carriers: 1.475/4.425/cache "
        "0.295/1.84375, 1,000,000 ctx, text->text (vision false), reasoning true, "
        "tool_call true. The lane is RETIRED (valid_to 2026-09-26, replaced_by "
        "openrouter/qwen/qwen3.8-max-0902) and its lifecycle_source record is "
        "left intact — the date correction does not move it across any lifecycle "
        "threshold (both candidate dates are >90d old). EVIDENCE CLASS: market "
        "composites only (AA coding 0.66, agentic 0.225, intelligence NULL, 19 "
        "design_arena arenas) plus market-derived agent_tick rows; no n>=3 "
        "internal measurement. cap_web_search stays NULL."
    ),
    "x-ai/grok-build-0.1": (
        "market cross-check 2026-11-18: ONE DATE FIXED (release_date 2026-05-19 "
        "-> 2026-05-20; canonical_slug x-ai/grok-build-0.1-20260520, created "
        "2026-05-20T17:28Z, two independent write-ups date it 2026-05-20; " + MD +
        " says 2026-04-16 and is the rejected outlier). Everything else verified "
        "clean against both carriers: 1.0/2.0/cache 0.2, 256,000 ctx, "
        "text+image+file->text (vision true), reasoning true, tool_call true, "
        "230,400 max completion. TWO DOCUMENTED GAPS, deliberately not changed: "
        "(1) the carrier publishes a CONTEXT TIER — above 200,000 prompt tokens "
        "the sticker doubles to 2.0/4.0/cache 0.4 — and the row carries the base "
        "tier only (same convention as the other tiered lanes; a tier-aware "
        "price is a pricing-policy change). (2) the carrier prices web_search at "
        "$0.005/request, which is a per-request fee rather than a token sticker, "
        "so cap_web_search stays NULL (carrier declaration, not a measurement). "
        "EVIDENCE CLASS: market composites only (AA coding 0.515, agentic NULL, "
        "intelligence NULL, ZERO design_arena arenas) — the thinnest evidence "
        "base in this batch; no n>=3 internal measurement, so no head-seat-grade "
        "evidence."
    ),
}

LANES = [
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("openrouter", "qwen/qwen3.7-plus"),
    ("openrouter", "qwen/qwen3.7-plus:batch"),
    ("openrouter", "stepfun/step-3.7-flash"),
    ("openrouter", "qwen/qwen3.7-max"),
    ("openrouter", "x-ai/grok-build-0.1"),
]

# Rows whose lifecycle_source holds a real lifecycle-pass record must not have it
# clobbered by a market note.
KEEP_LIFECYCLE = {("openrouter", "qwen/qwen3.7-max")}


def main():
    touched = 0
    out = []
    with open(TABLES, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                out.append(line)
                continue
            row = json.loads(line)
            key = (row.get("provider"), row.get("model"))
            if key not in UPDATES:
                out.append(line)
                continue
            for field, value in UPDATES[key].items():
                if field not in row:
                    print(f"REFUSED: {key} has no field {field}", file=sys.stderr)
                    return 2
                row[field] = value
            if key not in KEEP_LIFECYCLE:
                row["lifecycle_source"] = PROVENANCE[row["model"]]
                row["lifecycle_checked_at"] = DATE
            row["note"] = NOTE_TEXT[row["model"]]
            out.append(json.dumps(row) + "\n")
            touched += 1
    if touched != len(UPDATES):
        print(f"REFUSED: touched {touched} rows, expected {len(UPDATES)}", file=sys.stderr)
        return 2
    with open(TABLES, "w", encoding="utf-8") as f:
        f.writelines(out)

    seen = set()
    with open(NOTES_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                seen.add((r.get("provider"), r.get("model")))
    added = 0
    with open(NOTES_PATH, "a", encoding="utf-8") as f:
        for provider, model in LANES:
            if (provider, model) in seen:
                print(f"note already present: {provider} {model}", file=sys.stderr)
                continue
            f.write(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model,
                        "note": NOTE_TEXT[model],
                        "source": "market cross-check 2026-11-18 (openrouter "
                        "/api/v1/models live feed + models.dev api.json + "
                        "publisher launch coverage)",
                        "valid_from": DATE,
                    }
                )
                + "\n"
            )
            added += 1

    print(f"rows updated: {touched}; notes written: {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

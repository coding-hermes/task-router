#!/usr/bin/env python3
"""Market cross-check wave 2026-11-18 (cards t_model_x-ai_grok-4.3,
t_model_x-ai_grok-4.3:batch).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json

TABLES = "data/tables/models.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
DATE = "2026-11-18"
FEED = "carrier:openrouter /api/v1/models read 2026-11-18 (480 ids)"
MD = "models.dev api.json read 2026-11-18 (openrouter provider, 382 ids)"

NOTE_BASE = (
    "market cross-check 2026-11-18: ONE DATE FIXED (release_date 2026-04-17 -> "
    "2026-04-30; canonical_slug x-ai/grok-4.3-20260430, created "
    "2026-04-30T23:30:21Z, and four independent write-ups (kie.ai, ai-tldr.dev, "
    "codersera, ai-toolbox) all date general API availability 2026-04-30; "
    "models.dev api.json read 2026-11-18 (openrouter provider, 382 ids) says "
    "2026-04-17 and is the rejected outlier). Everything else verified clean "
    "against both carriers: 1.25/2.5/cache 0.2, 1,000,000 ctx, 900,000 max "
    "completion, text+image+file->text (vision true), reasoning true with an "
    "effort ladder none/low/medium/high, tool_call true, structured_output true. "
    "PRICE: the model-level sticker and BOTH standard endpoints (tag xai and "
    "xai/zdr) read 1.25/2.5/cache 0.2 to the cent; the two status-0 endpoints in "
    "the carrier's endpoint list are the PRIORITY service tier at 2.5/5/0.4, a "
    "service-tier price and not the lane's base sticker, so the row keeps the "
    "standard number. THREE DOCUMENTED GAPS, deliberately not changed: (1) a "
    "CONTEXT TIER at 200,000 prompt tokens re-prices the whole request at "
    "2.5/5/cache 0.4 (exactly 2x) and the row carries the base tier only - the "
    "registry's convention is to rank on the base sticker, so a tier-aware price "
    "is a pricing-policy change, not a per-row edit; (2) the carrier prices "
    "web_search at $0.005/request, a per-request fee rather than a token sticker, "
    "so cap_web_search stays NULL (carrier declaration, not a measurement); "
    "(3) the AA composites are FROZEN at 2026-11-17 - the 2026-11-18 feed returns "
    "artificial_analysis=null for this id (221 of 480 ids still carry the block), "
    "so the stored artificial_analysis_coding 0.422 / agentic 0.155 rows are the "
    "last published values and cannot be refreshed from the carrier. EVIDENCE "
    "CLASS: market composites only (AA coding 0.422, agentic 0.155, code_gen "
    "0.595, 25 design_arena entries across the agents and models arenas) plus "
    "market-derived category rows; no n>=3 internal measurement and no "
    "large-bench source, so under the stated precedence this lane holds no "
    "head-seat-grade evidence. The lane is RETIRED (valid_to 2026-09-26, "
    "replaced_by openrouter/x-ai/grok-4.7) and its lifecycle_source record is "
    "left intact - the date correction moves the age from 162d to 149d, still "
    ">90d, so it crosses no lifecycle threshold."
)

NOTE_BATCH = (
    "market cross-check 2026-11-18: THIN ROW COMPLETED + TWO TRUNCATION REPAIRS. "
    "vision NULL->true, thinking NULL->true, "
    "cap_audio/cap_embedding/cap_image_generation/cap_video NULL->false, "
    "release_date 2026-04-17 -> 2026-04-30, api_type 'o' -> 'openai-chat', "
    "data_class 'z' -> 'zdr'. " + FEED + " publishes the :batch SKU as "
    "text+image+file->text with reasoning + reasoning_effort supported, "
    "1,000,000 ctx, 900,000 max completion, 1.0/2.0/cache 0.16 and a 200k "
    "override at 2.0/4.0/cache 0.32; models.dev publishes no :batch entry, so "
    "the carrier is the sole source for the SKU's own limits and the base SKU's "
    "row is the cross-check for the model-level facts. PRICE verified clean: "
    "1.0/2.0 is exactly the carrier sticker and exactly 0.8x the base SKU's "
    "1.25/2.5 - the 20% batch discount, not drift (the same 20% the grok-4.7 "
    "batch lane carries, NOT the 50% Anthropic/OpenAI batch lanes buy). The two "
    "single-character fields were TRUNCATION, not values: api_type 'o' is the "
    "truncated form of 'openai-chat' (the base SKU's value and the convention "
    "every other openrouter :batch row follows) and data_class 'z' is the "
    "truncated form of 'zdr' (the base SKU's value; the carrier tags the "
    "standard endpoints xai/zdr). Both are instances of the umbrella defect "
    "tracked by t_api_type_truncation (api_type is one character on 512 of "
    "2,173 rows) - repaired here for this row only, not generalised. "
    "DOCUMENTED GAPS, deliberately not changed: (1) the 200k context tier "
    "(2.0/4.0/cache 0.32, exactly 2x) is not carried, same base-sticker "
    "convention as the base SKU; (2) cap_web_search stays NULL - the carrier's "
    "$0.005/request web_search is a fee, not a capability measurement; "
    "(3) ENDPOINT HAZARD unchanged - the feed lists this SKU as batches-only "
    "('/api/v1/batches') while the row says openai-chat, the same mismatch the "
    "grok-4.7 batch lane carries; a router that dispatches a chat request to "
    "this id would be refused by the endpoint. EVIDENCE CLASS: the market "
    "composites are INHERITED from the base id (AA coding 0.422, agentic 0.155, "
    "code_gen 0.595, 25 design_arena entries) and were never measured for the "
    "batch SKU; no n>=3 internal measurement exists. The lane is RETIRED "
    "(valid_to 2026-09-27, replaced_by openrouter/x-ai/grok-4.7) and its "
    "lifecycle_source record is left intact."
)

UPDATES = {
    ("openrouter", "x-ai/grok-4.3"): {
        "release_date": "2026-04-30",
        "note": NOTE_BASE,
    },
    ("openrouter", "x-ai/grok-4.3:batch"): {
        "release_date": "2026-04-30",
        "api_type": "openai-chat",
        "data_class": "zdr",
        "vision": True,
        "thinking": True,
        "cap_image_generation": False,
        "cap_embedding": False,
        "cap_audio": False,
        "cap_video": False,
        "note": NOTE_BATCH,
    },
}

NOTES = {
    "x-ai/grok-4.3": NOTE_BASE,
    "x-ai/grok-4.3:batch": NOTE_BATCH,
}


def main() -> int:
    out = []
    touched = set()
    with open(TABLES, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                out.append(line)
                continue
            row = json.loads(stripped)
            key = (row.get("provider"), row.get("model"))
            if key in UPDATES:
                for field, value in UPDATES[key].items():
                    row[field] = value
                touched.add(key)
                out.append(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                out.append(line)
    missing = set(UPDATES) - touched
    if missing:
        raise SystemExit(f"target rows not found: {sorted(missing)}")
    with open(TABLES, "w", encoding="utf-8") as handle:
        handle.writelines(out)

    with open(NOTES_PATH, "a", encoding="utf-8") as handle:
        for model, note in NOTES.items():
            handle.write(json.dumps({
                "provider": "openrouter",
                "model": model,
                "note": note,
                "source": (
                    "market cross-check 2026-11-18 (openrouter /api/v1/models "
                    "live feed + /api/v1/models/x-ai/grok-4.3/endpoints + "
                    "models.dev api.json + publisher launch coverage)"
                ),
                "valid_from": DATE,
            }, ensure_ascii=False) + "\n")
    print(f"updated {len(touched)} rows, appended {len(NOTES)} notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

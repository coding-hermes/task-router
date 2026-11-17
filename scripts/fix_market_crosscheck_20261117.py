#!/usr/bin/env python3
"""Market cross-check wave 2026-11-17 (cards t_model_meta_muse-spark-1.2,
t_model_thinkingmachines_inkling-small, t_model_inclusionai_ling-3.0-flash,
t_model_google_gemini-3.6-flash, t_model_google_gemini-3.6-flash:batch,
t_model_google_gemini-3.5-flash-lite).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
STAMPS = "data/null_reasons.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
DATE = "2026-11-17"
FEED = "carrier:openrouter /api/v1/models read 2026-11-17"

# (provider, model) -> {field: value}
UPDATES = {
    # Muse Spark 1.2: feed modality text+image+video+file -> text (NO audio input).
    ("commandcode", "meta/muse-spark-1.2"): {"cap_audio": False},
    ("commandcode-2", "meta/muse-spark-1.2"): {"vision": True, "cap_audio": False},
    # Inkling Small: feed modality text+image+audio -> text.
    ("commandcode-2", "thinkingmachines/inkling-small"): {"vision": True},
    # Gemini 3.6 Flash: feed modality text+image+file+audio+video -> text, reasoning params present.
    ("commandcode", "google/gemini-3.6-flash"): {"vision": True, "thinking": True},
    ("commandcode-2", "google/gemini-3.6-flash"): {"vision": True, "thinking": True},
    # Gemini 3.6 Flash batch SKU: capability-blind row, inherits the base model family.
    ("openrouter", "google/gemini-3.6-flash:batch"): {
        "vision": True,
        "thinking": True,
        "cap_image_generation": False,
        "cap_embedding": False,
        "cap_audio": True,
        "cap_video": True,
        "training_model_level": False,
        "training_provider_level": False,
    },
    # Gemini 3.5 Flash Lite: feed modality text+image+file+audio+video -> text.
    ("commandcode-2", "google/gemini-3.5-flash-lite"): {"vision": True, "thinking": True},
}

PROVENANCE = {
    "meta/muse-spark-1.2": (
        "publisher:Meta model card via " + FEED + " (text+image+video+file -> text, "
        "1,048,576 ctx, reasoning_effort ladder, $1.25/$4.25 per M, cache read $0.15) "
        "+ carrier:api.commandcode.ai/provider/v1/models read 2026-11-17 (ctx 1048576, "
        "/chat/completions + /responses)"
    ),
    "thinkingmachines/inkling-small": (
        "publisher:huggingface.co/thinkingmachines/Inkling-Small (12B active / 276B total MoE, "
        "multimodal) + " + FEED + " (text+image+audio -> text, 524,288 ctx, reasoning params) "
        "+ carrier:api.commandcode.ai/provider/v1/models read 2026-11-17 (ctx 1000000 on the "
        "commandcode lanes — carrier-declared, wider than the openrouter lane)"
    ),
    "inclusionai/ling-3.0-flash": (
        "publisher:huggingface.co/inclusionAI/Ling-3.0-flash + " + FEED + " (text -> text, "
        "262,144 ctx, reasoning params, $0.021/$0.063 per M, cache read $0.0042)"
    ),
    "google/gemini-3.6-flash": (
        "publisher:Google Gemini API model card via " + FEED + " (text+image+file+audio+video -> "
        "text, 1,048,576 ctx, reasoning params, $0.75/$3.75 per M, cache read $0.075, cache "
        "write $0.041667) + carrier:api.commandcode.ai/provider/v1/models read 2026-11-17 "
        "(ctx 1000000)"
    ),
    "google/gemini-3.6-flash:batch": (
        "publisher:Google Gemini API model card via " + FEED + " (batch SKU of "
        "google/gemini-3.6-flash: text+image+file+audio+video -> text, 1,048,576 ctx, reasoning "
        "params, $0.375/$1.875 per M = 50% of the interactive sticker, cache read $0.0375, "
        "cache write $0.041667)"
    ),
    "google/gemini-3.5-flash-lite": (
        "publisher:Google Gemini API model card via " + FEED + " (text+image+file+audio+video -> "
        "text, 1,048,576 ctx, reasoning params, $0.30/$2.50 per M, cache read $0.03, cache "
        "write $0.083333) + carrier:api.commandcode.ai/provider/v1/models read 2026-11-17 "
        "(ctx 1000000)"
    ),
}

NOTE_TEXT = {
    "meta/muse-spark-1.2": (
        "market cross-check 2026-11-17: feed modality is text+image+video+file -> text, so the "
        "commandcode lanes' cap_audio=true was WRONG (no audio input on this model) and "
        "commandcode-2's vision=false contradicted both the feed and its own commandcode twin — "
        "both corrected. cap_video=true is right (video is an input modality). Reasoning is an "
        "effort ladder (reasoning_effort in supported_parameters), so thinking=true. The "
        "commandcode lanes carry no cache-read sticker while the feed publishes $0.15/M — a "
        "price gap, deliberately NOT filled here (pricing changes chain order, own card). "
        "AA index 31 / coding 75.9 / agentic 52.5 from the feed; no internal measurement, so no "
        "perf_* cell and no head seat (perf-bar ruling 2026-11-16)."
    ),
    "thinkingmachines/inkling-small": (
        "market cross-check 2026-11-17: feed modality text+image+audio -> text; commandcode-2's "
        "vision=false contradicted the feed and its commandcode twin — corrected. cap_audio=true "
        "is right (audio is an input modality). Context: the openrouter lane is 524,288 (feed) "
        "while both commandcode lanes declare 1,000,000 in their own keyless catalog — "
        "carrier-declared, left as-is. AA agentic 23.5 / coding 52.9 / intelligence 12.9 from the "
        "feed; no internal measurement, so no perf_* cell and no head seat."
    ),
    "inclusionai/ling-3.0-flash": (
        "market cross-check 2026-11-17: NO defect found. Feed agrees with the row on every "
        "field it publishes — text -> text (vision=false, cap_audio=false, cap_video=false), "
        "262,144 ctx, reasoning params (thinking=true), $0.021/$0.063 per M and cache read "
        "$0.0042. AA agentic 19.3 / coding 50.6 from the feed; no internal measurement, so no "
        "perf_* cell and no head seat (perf-bar ruling 2026-11-16)."
    ),
    "google/gemini-3.6-flash": (
        "market cross-check 2026-11-17: both commandcode lanes carried vision=false AND "
        "thinking=false against a feed that publishes text+image+file+audio+video -> text with "
        "reasoning params — capability-blind mirror rows, corrected. The commandcode lanes are "
        "retired (valid_to 2026-09-27, superseded by gemini-3.8-flash) but capability is a "
        "property of the MODEL, so the flags are still true. AA agentic 29 / coding 69.2 from "
        "the feed; no internal measurement, so no perf_* cell and no head seat."
    ),
    "google/gemini-3.6-flash:batch": (
        "market cross-check 2026-11-17: CAPABILITY-BLIND BATCH SKU FIXED. The row carried TR-197 "
        "explicit-unknown stamps on vision/thinking/cap_image_generation/cap_embedding/cap_audio/"
        "cap_video. TR-369 makes capability families properties of the MODEL, not the lane, so "
        "the batch SKU inherits the interactive id: text+image+file+audio+video -> text, "
        "reasoning params (thinking), no image generation, no embeddings. The four stamps this "
        "made stale are removed from data/null_reasons.jsonl (a stamp may only name a field the "
        "row leaves NULL). cap_computer_use and cap_web_search stay NULL with their stamps: the "
        "feed publishes a per-request web_search PRICE but no web_search supported_parameter, "
        "and nothing on computer use. Batch sticker is exactly 50% of the interactive rate."
    ),
    "google/gemini-3.5-flash-lite": (
        "market cross-check 2026-11-17: commandcode-2 carried vision=false AND thinking=false "
        "against a feed that publishes text+image+file+audio+video -> text with reasoning params "
        "— capability-blind mirror row, corrected. AA agentic 14.3 / coding 49.3 / intelligence "
        "11.7 from the feed; no internal measurement, so no perf_* cell and no head seat."
    ),
}

# Stamps that must go: the row no longer leaves these fields NULL.
DROP_STAMPS = {
    ("openrouter", "google/gemini-3.6-flash:batch"): {
        "cap_image_generation",
        "cap_embedding",
        "cap_audio",
        "cap_video",
    },
}


def main() -> int:
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

    # null_reasons: drop stamps for fields that are no longer NULL
    kept, dropped = [], 0
    with open(STAMPS, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                kept.append(line)
                continue
            r = json.loads(line)
            key = (r.get("provider"), r.get("model"))
            if key in DROP_STAMPS and r.get("field") in DROP_STAMPS[key]:
                dropped += 1
                continue
            kept.append(line)
    with open(STAMPS, "w", encoding="utf-8") as f:
        f.writelines(kept)

    # model_notes: one cross-check row per TOUCHED LANE, keyed (provider, model) —
    # the key every consumer (router_modelsdev, router_gaps, the commit gate) reads.
    # Existing lines are copied verbatim; only missing keys are appended.
    seen = set()
    with open(NOTES_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                seen.add((r.get("provider"), r.get("model")))
    added = 0
    with open(NOTES_PATH, "a", encoding="utf-8") as f:
        for provider, model in UPDATES:
            if (provider, model) in seen:
                continue
            f.write(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model,
                        "note": NOTE_TEXT[model],
                        "source": "market cross-check 2026-11-17 (openrouter feed + commandcode keyless catalog + publisher cards)",
                        "valid_from": DATE,
                    }
                )
                + "\n"
            )
            added += 1

    print(f"rows updated: {touched}; stamps dropped: {dropped}; notes written: {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

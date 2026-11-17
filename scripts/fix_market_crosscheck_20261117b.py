#!/usr/bin/env python3
"""Market cross-check wave 2026-11-17b (cards t_model_google_gemini-3.5-flash,
t_model_google_gemini-3.5-flash:batch, t_model_google_gemini-3.5-flash-lite:batch,
t_model_google_gemma-3-12b-it, t_model_google_gemma-3-27b-it, t_model_google_gemma-3-4b-it).

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
MD = "models.dev api.json read 2026-11-17"

# (provider, model) -> {field: value}
UPDATES = {
    # Gemini 3.5 Flash: feed modality text+image+file+audio+video -> text, reasoning_effort
    # ladder in supported_parameters. Both commandcode mirror lanes were capability-blind.
    ("commandcode", "google/gemini-3.5-flash"): {"vision": True, "thinking": True},
    ("commandcode-2", "google/gemini-3.5-flash"): {"vision": True, "thinking": True},
    # Gemini 3.5 Flash batch SKU: capability-blind row, inherits the interactive id
    # (TR-369: capability families are properties of the MODEL, not the lane).
    ("openrouter", "google/gemini-3.5-flash:batch"): {
        "vision": True,
        "thinking": True,
        "cap_image_generation": False,
        "cap_embedding": False,
        "cap_audio": True,
        "cap_video": True,
        "training_model_level": False,
        "training_provider_level": False,
    },
    # Gemini 3.5 Flash Lite batch SKU: same defect class, same fix.
    ("openrouter", "google/gemini-3.5-flash-lite:batch"): {
        "vision": True,
        "thinking": True,
        "cap_image_generation": False,
        "cap_embedding": False,
        "cap_audio": True,
        "cap_video": True,
        "training_model_level": False,
        "training_provider_level": False,
    },
}

PROVENANCE = {
    "google/gemini-3.5-flash": (
        "publisher:Google Gemini API model card via " + FEED + " (text+image+file+audio+video -> "
        "text, 1,048,576 ctx, 65,536 max completion, reasoning mandatory with a "
        "high/medium/low/minimal effort ladder, $1.50/$9.00 per M, cache read $0.15, cache write "
        "$0.083333) + " + MD + " (openrouter 1.5/9/0.15/0.083333, commandcode 1.5/9/0.15 at "
        "1,000,000 ctx) + carrier:api.commandcode.ai/provider/v1/models read 2026-11-17 "
        "(ctx 1000000, carrier-declared)"
    ),
    "google/gemini-3.5-flash:batch": (
        "publisher:Google Gemini API model card via " + FEED + " (batch SKU of "
        "google/gemini-3.5-flash: text+image+file+audio+video -> text, 1,048,576 ctx, 65,536 max "
        "completion, reasoning effort ladder, $0.75/$4.50 per M = 50% of the interactive sticker, "
        "cache read $0.075) + " + MD + " (no :batch SKU published; the interactive rate is "
        "1.5/9/0.15)"
    ),
    "google/gemini-3.5-flash-lite:batch": (
        "publisher:Google Gemini API model card via " + FEED + " (batch SKU of "
        "google/gemini-3.5-flash-lite: text+image+file+audio+video -> text, 1,048,576 ctx, 65,536 "
        "max completion, reasoning effort ladder with default_effort minimal, $0.15/$1.25 per M = "
        "50% of the interactive sticker, cache read $0.015) + " + MD + " (no :batch SKU "
        "published; the interactive rate is 0.3/2.5/0.03)"
    ),
    "google/gemma-3-12b-it": (
        "publisher:Google Gemma 3 model card via " + FEED + " (text+image -> text, 131,072 ctx, "
        "16,384 max completion, NO reasoning block, $0.05/$0.15 per M) + " + MD + " (openrouter "
        "0.05/0.15 at 131,072/16,384)"
    ),
    "google/gemma-3-27b-it": (
        "publisher:Google Gemma 3 model card via " + FEED + " (text+image -> text, 131,072 ctx, "
        "117,964 max completion, NO reasoning block, $0.08/$0.35 per M, cache read $0.04) + "
        + MD + " (openrouter 0.08/0.35/0.04 at 131,072/117,964)"
    ),
    "google/gemma-3-4b-it": (
        "publisher:Google Gemma 3 model card via " + FEED + " (text+image -> text, 131,072 ctx, "
        "16,384 max completion, NO reasoning block, $0.05/$0.10 per M) + " + MD + " (openrouter "
        "0.05/0.10 at 131,072/16,384)"
    ),
}

NOTE_TEXT = {
    "google/gemini-3.5-flash": (
        "market cross-check 2026-11-17: both commandcode mirror lanes carried vision=false AND "
        "thinking=false against a feed that publishes text+image+file+audio+video -> text with a "
        "mandatory reasoning_effort ladder (high/medium/low/minimal) — capability-blind mirror "
        "rows, corrected; the openrouter and xkiro lanes already agreed with the feed. cap_audio "
        "and cap_video true are right (both are input modalities). Context: openrouter publishes "
        "1,048,576 while the commandcode and xkiro lanes declare 1,000,000 — carrier-declared in "
        "the commandcode keyless catalog, left as-is. Price agrees on 1.50/9.00 per M; the "
        "commandcode lanes carry NO cache-read sticker although models.dev commandcode publishes "
        "$0.15/M — a price gap deliberately not filled here (pricing = own card). AA coding 70.1 / "
        "agentic 26 with 25 design_arena entries in the feed; no internal measurement, so no "
        "perf_* cell and no head seat (perf-bar ruling 2026-11-16). Lifecycle: superseded by "
        "gemini-3.8-flash, which is the lifecycle pass's call, not this card's."
    ),
    "google/gemini-3.5-flash:batch": (
        "market cross-check 2026-11-17: CAPABILITY-BLIND BATCH SKU FIXED. The row carried TR-197 "
        "explicit-unknown stamps on vision/thinking/cap_image_generation/cap_embedding/cap_audio/"
        "cap_video. TR-369 makes capability families properties of the MODEL, not the lane, so "
        "the batch SKU inherits the interactive id: text+image+file+audio+video -> text, reasoning "
        "effort ladder (thinking), no image generation, no embeddings. The four stamps this made "
        "stale are removed from data/null_reasons.jsonl (a stamp may only name a field the row "
        "leaves NULL). cap_computer_use and cap_web_search stay NULL with their stamps: the feed "
        "publishes a per-request web_search PRICE but no web_search supported_parameter, and "
        "nothing on computer use. Batch sticker is exactly 50% of the interactive rate "
        "($0.75/$4.50 vs $1.50/$9.00, cache read $0.075 vs $0.15) — row already agreed."
    ),
    "google/gemini-3.5-flash-lite:batch": (
        "market cross-check 2026-11-17: CAPABILITY-BLIND BATCH SKU FIXED, same defect class as "
        "google/gemini-3.5-flash:batch. The row carried TR-197 explicit-unknown stamps on all six "
        "capability fields. Under TR-369 the batch SKU inherits the interactive model's "
        "capabilities: text+image+file+audio+video -> text, reasoning effort ladder, no image "
        "generation, no embeddings. The four stamps this made stale (cap_image_generation, "
        "cap_embedding, cap_audio, cap_video) are removed from data/null_reasons.jsonl; "
        "cap_computer_use and cap_web_search stay NULL with their stamps for the same reasons as "
        "the interactive sibling. Batch sticker $0.15/$1.25 with cache read $0.015 = exactly 50% "
        "of the interactive $0.30/$2.50/$0.03 — row already agreed. AA intelligence 11.7 / "
        "coding 49.3 / agentic 14.3 from the feed, zero design_arena entries; no internal "
        "measurement, so no perf_* cell and no head seat."
    ),
    "google/gemma-3-12b-it": (
        "market cross-check 2026-11-17: NO defect found. The feed and models.dev openrouter both "
        "say text+image -> text, 131,072 ctx, 16,384 max completion, NO reasoning block and "
        "$0.05/$0.15 per M — the row agrees on every one of them (vision=true, thinking=false, "
        "cap_audio=false, cap_video=false, context_limit 131,072). AA coding 5.8 / agentic 0.1, "
        "zero design_arena entries. Live n=1 small-probe cells exist (agent_tick 0.75, code_gen "
        "0.333, debug 0.667) but n=1 does not hold a head seat (perf-bar ruling 2026-11-16)."
    ),
    "google/gemma-3-27b-it": (
        "market cross-check 2026-11-17: ONE PRICE DEFECT FOUND while verifying capabilities. The "
        "row carried public_out_per_m 0.45 with cache read 0.04; BOTH carriers of truth — the "
        "openrouter feed and models.dev openrouter — say $0.08/$0.35/$0.04 for this SKU. 0.45 is "
        "not this carrier's rate: it is merge-gateway's sticker for the same model "
        "(models.dev merge-gateway google/gemma-3-27b-it = 0.08/0.45/0.04), so the row carried "
        "ANOTHER provider's output price, which overstated the lane's blended cost. Corrected in "
        "its own priced commit, not folded into the capability diff (a pricing change is a "
        "separate diff by doctrine). Capabilities themselves are clean: text+image -> text, "
        "thinking=false, cap_audio=false, cap_video=false, 131,072 ctx, 117,964 max completion all "
        "agree. AA coding 10.1 / agentic 0.1, zero design_arena entries; no internal measurement, "
        "so no head seat."
    ),
    "google/gemma-3-4b-it": (
        "market cross-check 2026-11-17: NO defect found. Feed and models.dev openrouter agree on "
        "text+image -> text, 131,072 ctx, 16,384 max completion, no reasoning block, "
        "$0.05/$0.10 per M and no cache-read sticker — the row agrees on every field it publishes "
        "(vision=true, thinking=false, cap_audio=false, cap_video=false). AA coding 2.7 (agentic "
        "index absent in the feed). Live n=1 small-probe cells exist (agent_tick 0.75, code_gen "
        "0.667, debug 0.667, long_doc 1.0) but n=1 does not hold a head seat (perf-bar ruling "
        "2026-11-16)."
    ),
}

# Stamps that must go: the row no longer leaves these fields NULL.
DROP_STAMPS = {
    ("openrouter", "google/gemini-3.5-flash:batch"): {
        "cap_image_generation",
        "cap_embedding",
        "cap_audio",
        "cap_video",
    },
    ("openrouter", "google/gemini-3.5-flash-lite:batch"): {
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

    # model_notes: one cross-check row per lane, keyed (provider, model) — the key every
    # consumer (router_modelsdev, router_gaps, the commit gate) reads. Existing lines are
    # copied verbatim; only missing keys are appended.
    lanes = [(p, m) for (p, m) in UPDATES]
    lanes += [
        ("openrouter", "google/gemma-3-12b-it"),
        ("openrouter", "google/gemma-3-27b-it"),
        ("openrouter", "google/gemma-3-4b-it"),
    ]
    seen = set()
    with open(NOTES_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                seen.add((r.get("provider"), r.get("model")))
    added = 0
    with open(NOTES_PATH, "a", encoding="utf-8") as f:
        for provider, model in lanes:
            if (provider, model) in seen:
                print(f"note already present: {provider} {model}", file=sys.stderr)
                continue
            f.write(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model,
                        "note": NOTE_TEXT[model],
                        "source": "market cross-check 2026-11-17 (openrouter feed + commandcode "
                        "keyless catalog + models.dev)",
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

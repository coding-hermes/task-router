#!/usr/bin/env python3
"""Market cross-check wave 2026-11-17d (cards t_model_meta_muse-spark-1.1,
t_model_openai_gpt-5.6-luna).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
DATE = "2026-11-17"
FEED = "carrier:openrouter /api/v1/models read 2026-11-17"
MD = "models.dev api.json read 2026-11-17"
CC = "carrier:api.commandcode.ai/provider/v1/models read 2026-11-17"
XK = "carrier:api.xkiro.com/v1/models read 2026-11-17 (keyless, 69 ids)"

UPDATES = {
    # Muse Spark 1.1: both commandcode lanes carried vision=false. models.dev's
    # commandcode entry publishes modalities text+image+pdf+video (attachment=true)
    # and the openrouter feed publishes text+image+file+video -> text. Same defect
    # class as the Gemini 3.5 Flash / Inkling mirrors.
    ("commandcode", "meta/muse-spark-1.1"): {"vision": True},
    ("commandcode-2", "meta/muse-spark-1.1"): {"vision": True},
}

PROVENANCE = {
    "meta/muse-spark-1.1": (
        "market cross-check 2026-11-17: both commandcode lanes carried vision=false "
        "against " + MD + " (commandcode meta/muse-spark-1.1: attachment=true, "
        "modalities text+image+pdf+video, reasoning effort ladder low..xhigh, "
        "1,048,576 ctx, $1.25/$4.25/cache $0.15) and against " + FEED +
        " (text+image+file+video -> text, reasoning MANDATORY with an "
        "xhigh/high/medium/low/minimal ladder, 943,718 max completion). The "
        "openrouter lane already said vision=true. Corrected in place; capability "
        "families are properties of the model, not the lane."
    ),
}

NOTE_TEXT = {
    "meta/muse-spark-1.1": (
        "market cross-check 2026-11-17: ONE FALSE CAPABILITY FIXED on BOTH commandcode "
        "lanes (vision=false -> true; " + MD + " commandcode publishes text+image+pdf+"
        "video input and " + FEED + " publishes text+image+file+video -> text). "
        "Everything else agrees: 1,048,576 ctx, reasoning mandatory with an "
        "xhigh..minimal effort ladder (thinking=true), no audio input "
        "(cap_audio=false), video input (cap_video=true), no image generation, no "
        "embeddings; sticker $1.25/$4.25 with cache read $0.15 matches the feed and "
        "models.dev on the openrouter lane. " + CC + " lists the id at 1,048,576 ctx "
        "with /chat/completions + /responses endpoints and publishes NO modality, so "
        "the vendor model card is the capability authority for the mirror lanes. TWO "
        "DOCUMENTED GAPS, deliberately not changed here: (1) both commandcode lanes "
        "carry no cache-read sticker while models.dev commandcode publishes $0.15/M; "
        "(2) the feed publishes a per-request web_search PRICE ($0.0025) but no "
        "web_search supported_parameter, so cap_web_search stays NULL. Re-pricing is "
        "its own priced diff by doctrine. Feed AA: intelligence NULL, coding 71.3, "
        "agentic 25.8 plus 20 design_arena arenas — all present as market rows."
    ),
    "openai/gpt-5.6-luna": (
        "market cross-check 2026-11-17: NO defect found. " + FEED + " and " + MD +
        " openrouter agree with the row on text+image+file -> text (vision=true, "
        "cap_video=false, cap_audio=false), 1,050,000 ctx, 128,000 max completion, "
        "$0.20/$1.20 with cache read $0.02 and cache write $0.25, and a "
        "max/xhigh/high/medium/low/none effort ladder (thinking=true, "
        "default_enabled). The openrouter row is the BASE sticker on purpose: the feed "
        "carries a 272,000-prompt-token override (0.4/1.8, cache read 0.04, cache "
        "write 0.5) that doubles the rate on long prompts — a context tier, not a "
        "drift. " + XK + " independently confirms both xkiro lanes: openai/gpt-5.6-luna "
        "at $0.10/$0.60 per 1M with cache_read $0.05, 1,000,000 ctx, 65,536 max "
        "output, vision=true, reasoning=true with a none..max ladder. TWO DOCUMENTED "
        "GAPS on the xkiro lanes, deliberately not changed here: (1) the carrier "
        "publishes cache_read $0.05 and both rows leave public_cache_read_per_m NULL; "
        "(2) the carrier declares web_search via chat_completions=true while "
        "cap_web_search stays NULL (carrier declaration, not a measurement — filing "
        "it rather than promoting an unmeasured family). Feed AA: intelligence 23.8, "
        "coding 71.4, agentic 42.1, zero design_arena entries — all present as market "
        "rows."
    ),
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

    lanes = [
        ("openrouter", "meta/muse-spark-1.1"),
        ("commandcode", "meta/muse-spark-1.1"),
        ("commandcode-2", "meta/muse-spark-1.1"),
        ("openrouter", "openai/gpt-5.6-luna"),
        ("xkiro", "openai/gpt-5.6-luna"),
        ("xkiro-2", "openai/gpt-5.6-luna"),
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
                        "source": "market cross-check 2026-11-17 (openrouter feed + "
                        "commandcode keyless catalog + xkiro keyless catalog + models.dev)",
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

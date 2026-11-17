#!/usr/bin/env python3
"""Market cross-check wave 2026-11-17c (cards t_model_meituan_longcat-2.0,
t_model_thinkingmachines_inkling, t_model_moonshotai_kimi-k3,
t_model_moonshotai_kimi-k3:batch).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.

Sources of truth used for this wave (all read 2026-11-17):
  OR  = carrier:openrouter /api/v1/models (470 ids) + per-id /endpoints route table
  CC  = carrier:api.commandcode.ai/provider/v1/models (101 ids, keyless)
  MD  = models.dev api.json (324 providers)
  WEB = publisher model card / independent ranking pages (per-row source string)
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
STAMPS = "data/null_reasons.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
DATE = "2026-11-17"
FEED = "carrier:openrouter /api/v1/models read 2026-11-17"
FEED_EP = "carrier:openrouter /api/v1/models/moonshotai/kimi-k3-20260715/endpoints read 2026-11-17"
MD = "models.dev api.json read 2026-11-17"

UPDATES = {
    # Inkling on commandcode-2 is a mirror of commandcode (same vendor account family,
    # 2026-09-26). The mirror lane alone carried vision=false against a feed that
    # publishes image input (text+image+audio -> text) and a models.dev commandcode
    # entry with attachment=true. Same defect class as the Gemini 3.5 Flash mirrors.
    ("commandcode-2", "thinkingmachines/inkling"): {"vision": True},
    # Kimi K3 batch SKU: capability-blind row, inherits the interactive id
    # (TR-369: capability families are properties of the MODEL, not the lane).
    ("openrouter", "moonshotai/kimi-k3:batch"): {
        "vision": True,
        "thinking": True,
        "cap_image_generation": False,
        "cap_embedding": False,
        "cap_audio": False,
        "cap_video": True,
        "training_model_level": False,
        "training_provider_level": False,
    },
}

PROVENANCE = {
    "thinkingmachines/inkling": (
        "mirror-consistency fix 2026-11-17: commandcode-2 carried vision=false against " + FEED +
        " (thinkingmachines/inkling: text+image+audio -> text, 524,288 ctx, reasoning_effort "
        "ladder) and " + MD + " (commandcode thinkingmachines/inkling: attachment=true, "
        "modalities text+image+audio, 256,000 ctx) — the commandcode lane already said "
        "vision=true. Mirror SKUs of one vendor account must not disagree on a published "
        "capability; corrected in place."
    ),
    "moonshotai/kimi-k3:batch": (
        "TR-369 capability fill 2026-11-17: the batch SKU was capability-blind (vision, "
        "thinking and all six cap_* families NULL with TR-197 unknown stamps). " + FEED +
        " publishes moonshotai/kimi-k3:batch as text+image+video -> text with a "
        "reasoning_effort ladder (max/high/low/none) at 1,048,576 ctx, so the batch SKU "
        "inherits its interactive id's capabilities: vision=true, thinking=true, "
        "cap_video=true, cap_audio=false, cap_image_generation=false, cap_embedding=false. "
        "The four stamps this made stale (cap_image_generation, cap_embedding, cap_audio, "
        "cap_video) are removed; cap_computer_use and cap_web_search stay NULL with their "
        "stamps (the feed publishes neither). ROUTING FINDING, deliberately not changed: "
        "the id's endpoints list is ['/api/v1/batches'] ONLY — there is no chat endpoint "
        "behind this SKU, yet api_type reads openai-chat and the row is not disabled. Same "
        "finding class as the gemini-3.7-flash:batch card; api_type has no batch member in "
        "the vocabulary, so this is filed rather than guessed. Batch sticker "
        "$2.28/$11.40 with cache read $0.228 = exactly 76% of the interactive "
        "$3.00/$15.00/$0.30 — row already agreed."
    ),
}

# Stamps that must go: the row no longer leaves these fields NULL.
DROP_STAMPS = {
    ("openrouter", "moonshotai/kimi-k3:batch"): {
        "cap_image_generation",
        "cap_embedding",
        "cap_audio",
        "cap_video",
    },
}

NOTE_TEXT = {
    "meituan/longcat-2.0": (
        "market cross-check 2026-11-17: NO defect found. " + FEED + " publishes a SINGLE "
        "route for this id (AtlasCloud) at $0.30/$1.20 per M with cache read $0.006, "
        "1048756 ctx and text -> text; " + MD + " openrouter agrees on every number and on "
        "reasoning=true, attachment=false. The row agrees field-for-field (vision=false, "
        "thinking=true, cap_audio=false, cap_video=false, context_limit 1048756, "
        "normalized_price 0.30). Publisher card (meituan-longcat/LongCat-2.0, "
        "huggingface.co/meituan-longcat/LongCat-2.0) confirms 1.6T total / 48B active MoE "
        "with native 1M context; vendor-reported SWE-bench Pro 59.5, Terminal-Bench 2.1 "
        "70.8, SWE-bench Multilingual 77.3 (self-reported, not independently verified, and "
        "recorded as a note not a bench row). Feed AA: intelligence NULL, coding 45.3, "
        "agentic 14.0, zero design_arena entries — those three feed rows are present in "
        "benchmarks.jsonl (source=market, valid_from 2026-11-17)."
    ),
    "thinkingmachines/inkling": (
        "market cross-check 2026-11-17: ONE MIRROR DEFECT FOUND AND FIXED. " + FEED +
        " publishes text+image+audio -> text, 524,288 ctx, 471,859 max completion, "
        "$1.00/$4.05 with cache read $0.17, reasoning_effort ladder (max/high/medium/low/"
        "minimal/none); " + MD + " openrouter agrees exactly, and MD commandcode agrees on "
        "modalities + $1.00/$4.05 at a 256,000 ctx carrier. The openrouter and commandcode "
        "rows agree on every field. commandcode-2 (mirror of commandcode) was the only lane "
        "carrying vision=false; corrected to true in place. Publisher card "
        "(thinkingmachines.ai/model-card/inkling) confirms 975B total / 41B active, 6-of-256 "
        "experts, native text+image+audio. Feed AA: intelligence 12.5, coding 52.1, agentic "
        "22.5 plus 9 design_arena arenas — all present as market rows."
    ),
    "moonshotai/kimi-k3": (
        "market cross-check 2026-11-17: NO defect — and one deliberate NON-change. " + FEED +
        " reports moonshotai/kimi-k3 at $0.192/$16.00 with cache read $0.182, and " + MD +
        " openrouter copies that headline, which tempts a 'price drift' fix. It is not one: "
        "this openrouter id is a MULTI-ROUTE aggregator (22 endpoints) and the feed's "
        "top-level pricing is the CHEAPEST route (Wafer, a quantized route). " + FEED_EP +
        " shows the Moonshot AI route at $3.00/$15.00/cache $0.30 — exactly what this row "
        "carries, and exactly Moonshot's own published list ($3 input / $0.30 cached / $15 "
        "output, corroborated 2026-11-17 by aipricing.guru, benchlm.ai and maximem.ai). The "
        "row keeps the standard-route sticker on purpose: re-pricing to a cheapest reseller "
        "route would understate this lane's real cost and reorder chains on a single "
        "low-precision route. Capabilities clean: text+image+video -> text, vision=true, "
        "cap_video=true, cap_audio=false, thinking=true, 1048576 ctx. Feed AA: intelligence "
        "17.4, coding 72.0, agentic 29.3 plus 20 design_arena arenas, all present as market "
        "rows."
    ),
    "moonshotai/kimi-k3:batch": (
        "market cross-check 2026-11-17: capability-blind batch SKU FILLED (TR-369 "
        "inheritance from the interactive id) and one ROUTING FINDING filed: " + FEED +
        " lists moonshotai/kimi-k3:batch with endpoints ['/api/v1/batches'] ONLY, i.e. no "
        "chat endpoint behind the SKU, while api_type still reads openai-chat and the row "
        "carries no disabled flag (same class as the gemini-3.7-flash:batch card). "
        "Capabilities now vision=true, thinking=true, cap_video=true, cap_audio=false, "
        "cap_image_generation=false, cap_embedding=false; cap_computer_use and "
        "cap_web_search stay NULL with stamps. Sticker $2.28/$11.40/cache $0.228 = exactly "
        "76% of the interactive standard route $3.00/$15.00/$0.30 — row already agreed and "
        "is NOT the cheapest-route artifact described on the interactive card. AA "
        "intelligence 17.4, coding 72.0, agentic 29.3; 20 design_arena arenas — all present "
        "as market rows."
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

    # model_notes: one cross-check row per lane, keyed (provider, model).
    lanes = [
        ("openrouter", "meituan/longcat-2.0"),
        ("openrouter", "thinkingmachines/inkling"),
        ("commandcode", "thinkingmachines/inkling"),
        ("commandcode-2", "thinkingmachines/inkling"),
        ("openrouter", "moonshotai/kimi-k3"),
        ("openrouter", "moonshotai/kimi-k3:batch"),
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
                        "source": "market cross-check 2026-11-17 (openrouter feed + route "
                        "table + commandcode keyless catalog + models.dev + publisher pages)",
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

#!/usr/bin/env python3
"""Market cross-check wave 2026-11-18b (cards t_model_nvidia_nemotron-3-nano-omni-30b-a3b-reasoning:free,
t_model_qwen_qwen3.6-35b-a3b, t_model_moonshotai_kimi-k2.6, t_model_z-ai_glm-5.1,
t_model_qwen_qwen3.6-plus, t_model_z-ai_glm-5v-turbo).

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
NULLS_PATH = "data/tables/null_reasons.jsonl"
DATE = "2026-11-18"
FEED = "carrier:openrouter /api/v1/models read 2026-11-18 (480 ids)"
MD = "models.dev api.json read 2026-11-18 (openrouter provider)"

# public_price is the 0.96*in + 0.04*out blend (scripts/pricing/helpers.py
# public_from_sticker); normalized_price follows the input sticker on a PAYG lane.
UPDATES = {
    # The row carried 0.15/1.0/cache 0.05 — no source, and 1.5x/1.11x/2x the
    # carrier sticker. Both machine sources agree on 0.1/0.9/cache 0.025.
    ("openrouter", "qwen/qwen3.6-35b-a3b"): {
        "normalized_price": 0.1,
        "public_in_per_m": 0.1,
        "public_out_per_m": 0.9,
        "public_cache_read_per_m": 0.025,
        "public_price": 0.132,
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.1/0.9/cache 0.025 and "
            + MD + " (openrouter) agrees to the cent; the prior 0.15/1.0/cache 0.05 had no "
            "source and overstated the lane 1.5x/1.11x/2x"
        ),
    },
    # The row carried 0.95/4.0/cache 0.16 — the VENDOR list price (models.dev
    # ofox/tokengo), not this lane's carrier sticker.
    ("openrouter", "moonshotai/kimi-k2.6"): {
        "normalized_price": 0.65,
        "public_in_per_m": 0.65,
        "public_out_per_m": 3.41,
        "public_cache_read_per_m": 0.15,
        "public_price": 0.7604,
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.65/3.41/cache 0.15 and "
            + MD + " (openrouter) agrees to the cent; the prior 0.95/4.0/cache 0.16 was the "
            "vendor list sticker (models.dev ofox/tokengo), not the openrouter lane's price"
        ),
    },
    # The row carried 0.9646/3.0316/cache 0.17914 — a 0.689x factor of the
    # carrier sticker with no discount record anywhere in the registry.
    ("openrouter", "z-ai/glm-5.1"): {
        "normalized_price": 1.4,
        "public_in_per_m": 1.4,
        "public_out_per_m": 4.4,
        "public_cache_read_per_m": 0.26,
        "public_price": 1.52,
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 1.4/4.4/cache 0.26 and "
            + MD + " (openrouter) agrees to the cent; the prior 0.9646/3.0316/cache 0.17914 "
            "was exactly 0.689x the sticker with no temporary_discounts or plan record behind it"
        ),
    },
    # Free SKU: the carrier now lists this exact id at 0/0, so the stale
    # "no EXACT paid sibling" window-cost-pending text is retired.
    ("openrouter", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " lists this exact id at 0/0 (free SKU, no "
            "cache sticker) and " + MD + " (openrouter) agrees; supersedes the 2026-09-20 "
            "TR-070 window-cost-pending note, which said no exact sibling was listed"
        ),
    },
    # Verified clean — no field change.
    ("openrouter", "qwen/qwen3.6-plus"): {},
    ("openrouter", "z-ai/glm-5v-turbo"): {},
}

PROVENANCE = {
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": (
        "market cross-check 2026-11-18: no spec field changed. " + FEED +
        " (canonical_slug nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-20260428, 256,000 ctx, "
        "65,536 max completion, text+image+audio+video->text, pricing 0/0, supported_parameters "
        "include reasoning + tools) and " + MD + " (cost 0/0, limit 256,000/65,536, same "
        "modalities, reasoning true, tool_call true, release_date 2026-04-28) agree with the row "
        "on every spec field it carries: 256,000 ctx, vision/thinking/cap_audio/cap_video true, "
        "cap_image_generation/cap_embedding false, release 2026-04-28. PRICE: the row's 0/0 is "
        "the carrier sticker; only the price_evidence text was stale."
    ),
    "qwen/qwen3.6-35b-a3b": (
        "market cross-check 2026-11-18: PRICE REPAIRED 0.15/1.0/cache 0.05 -> 0.1/0.9/cache "
        "0.025 (normalized_price 0.1, public_price re-blended 0.96*in+0.04*out = 0.132). " + FEED +
        " (canonical_slug qwen/qwen3.6-35b-a3b-20260415, 262,144 ctx, 235,929 max completion, "
        "text+image+video->text, pricing 0.1/0.9/cache 0.025) and " + MD + " (cost 0.1/0.9/cache "
        "0.025, limit 262,144/235,929, same modalities, reasoning true) agree to the cent; the "
        "prior numbers had no source. release_date 2026-04-16 KEPT: the publisher review dates "
        "the open-weight release 2026-04-16 (dev.to czmilo review), the carrier slug says "
        "2026-04-15 and models.dev says 2026-04-17, so the row's date sits inside the bracket "
        "and is the only one with publisher support."
    ),
    "moonshotai/kimi-k2.6": (
        "market cross-check 2026-11-18: PRICE REPAIRED 0.95/4.0/cache 0.16 -> 0.65/3.41/cache "
        "0.15 (normalized_price 0.65, public_price re-blended = 0.7604). " + FEED +
        " (canonical_slug moonshotai/kimi-k2.6-20260420, 262,144 ctx, 235,929 max completion, "
        "text+image->text, pricing 0.65/3.41/cache 0.15) and " + MD + " (openrouter: cost "
        "0.65/3.41/cache 0.15, limit 262,144/235,929, text+image, reasoning true) agree to the "
        "cent; the prior numbers were the vendor list sticker (models.dev ofox/tokengo 0.95/4/"
        "cache 0.16), not this lane's price. Spec fields verified clean: 262,144 ctx, vision "
        "true, cap_video false (carrier publishes text+image only), thinking true, release "
        "2026-04-20. Lane is retired (valid_to 2026-09-26, replaced_by openrouter/moonshotai/"
        "kimi-k3); the lifecycle record is left intact."
    ),
    "z-ai/glm-5.1": (
        "market cross-check 2026-11-18: PRICE REPAIRED 0.9646/3.0316/cache 0.17914 -> "
        "1.4/4.4/cache 0.26 (normalized_price 1.4, public_price re-blended = 1.52). " + FEED +
        " (canonical_slug z-ai/glm-5.1-20260406, 204,800 ctx, 131,072 max completion, "
        "text->text, pricing 1.4/4.4/cache 0.26) and " + MD + " (openrouter: cost 1.4/4.4/cache "
        "0.26, limit 204,800/131,072, text->text, reasoning true) agree to the cent. The prior "
        "numbers were exactly 0.689x the sticker and no temporary_discounts row, plan_terms row "
        "or provider_rules discount covers this id, so the factor had no record behind it. Spec "
        "fields verified clean: 204,800 ctx, vision false, cap_video false, thinking true, "
        "release 2026-04-07. Lane is retired (valid_to 2026-09-26, replaced_by openrouter/z-ai/"
        "glm-5.3); the lifecycle record is left intact."
    ),
    "qwen/qwen3.6-plus": (
        "market cross-check 2026-11-18: no field change needed. " + FEED +
        " (canonical_slug qwen/qwen3.6-plus-04-02, 1,000,000 ctx, 65,536 max completion, "
        "text+image+video->text, pricing 0.325/1.95 with cache_write 0.40625 and NO cache_read "
        "sticker) and " + MD + " (openrouter: cost 0.325/1.95/cache_write 0.40625, limit "
        "1,000,000/65,536, same modalities, reasoning true) agree with the row exactly: "
        "0.325/1.95, cache_read NULL, cache_write 0.40625, 1,000,000 ctx, vision/cap_video true, "
        "thinking true, release 2026-04-02. Lane is retired (valid_to 2026-09-26, replaced_by "
        "openrouter/qwen/qwen3.7-plus); the lifecycle record is left intact."
    ),
    "z-ai/glm-5v-turbo": (
        "market cross-check 2026-11-18: no field change needed. " + FEED +
        " (canonical_slug z-ai/glm-5v-turbo-20260401, 202,752 ctx, 131,072 max completion, "
        "text+image+video->text, pricing 1.2/4.0/cache 0.24) and " + MD + " (openrouter: cost "
        "1.2/4/cache 0.24, limit 202,752/131,072, text+image+video, reasoning true) agree with "
        "the row exactly: 1.2/4.0/cache 0.24, 202,752 ctx, vision/cap_video true, thinking true, "
        "release 2026-04-01."
    ),
}

NOTE_TEXT = {
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": (
        "market cross-check 2026-11-18: VERIFIED CLEAN on spec, price_evidence refreshed. " + FEED +
        " and " + MD + " agree with the row: 256,000 ctx, text+image+audio+video->text (vision, "
        "cap_audio, cap_video true; cap_image_generation, cap_embedding false), reasoning true, "
        "release 2026-04-28, 65,536 max completion. PRICE: the carrier lists this exact id at "
        "0/0, so the row's 0.0 is the sticker and the 2026-09-20 TR-070 window-cost-pending note "
        "(\"no EXACT paid sibling\") is retired — the SKU itself is now listed. EVIDENCE CLASS: "
        "the only internal rows are n=1 small-probes (agent_tick 3/4, code_gen 2/3, tool_use 1/3) "
        "plus the frozen 2026-11-17 market composite (AA coding 0.138); no n>=3 measurement and "
        "no large-bench source, so no head-seat-grade evidence. The 2026-11-18 feed publishes NO "
        "AA index for this id (all null except openness_index, which has no category mapping) and "
        "ZERO design_arena entries, so no market cell could be re-ingested with provenance; the "
        "frozen 11-17 row is left in place rather than deleted. cap_web_search and "
        "cap_computer_use stay NULL by design (product features with no modality evidence class)."
    ),
    "qwen/qwen3.6-35b-a3b": (
        "market cross-check 2026-11-18: PRICE REPAIRED (0.15/1.0/cache 0.05 -> 0.1/0.9/cache "
        "0.025; normalized_price 0.1, public_price 0.132). " + FEED + " and " + MD +
        " agree to the cent on 0.1/0.9/cache 0.025; the prior numbers had no source and "
        "overstated the lane 1.5x on input, 1.11x on output and 2x on cache reads. Everything "
        "else verified clean: 262,144 ctx, text+image+video->text (vision true, cap_video true, "
        "cap_audio false), reasoning true, release 2026-04-16 (publisher review; carrier slug "
        "2026-04-15 and models.dev 2026-04-17 bracket it). EVIDENCE CLASS: n=1 small-probes "
        "(agent_tick 3/4, code_gen 1/3, delegation 3/4) plus the frozen 2026-11-17 market "
        "composites (AA coding 0.419, agentic 0.131); no n>=3 measurement, no large-bench "
        "source. The 2026-11-18 feed publishes no AA index and zero design_arena entries for "
        "this id, so the frozen market rows stay and no cell could be re-ingested with "
        "provenance. cap_web_search/cap_computer_use stay NULL by design."
    ),
    "moonshotai/kimi-k2.6": (
        "market cross-check 2026-11-18: PRICE REPAIRED (0.95/4.0/cache 0.16 -> 0.65/3.41/cache "
        "0.15; normalized_price 0.65, public_price 0.7604). The prior numbers were the VENDOR "
        "list sticker (models.dev ofox/tokengo), not this lane's price: " + FEED + " and " + MD +
        " (openrouter) both price the id 0.65/3.41/cache 0.15. Spec fields verified clean: "
        "262,144 ctx, text+image->text (vision true, cap_video false — the carrier publishes no "
        "video input for this id even though some resellers do), reasoning true, release "
        "2026-04-20. MARKET PROVENANCE: the 22 design_arena cells were re-ingested from the "
        "2026-11-18 feed with full provenance (percentile in score, raw ELO + rank + n in "
        "source_ref), which also collapsed the duplicate agent_tick cells the 11-17 pass left "
        "behind; the two frozen artificial_analysis rows (coding 0.618, agentic 0.205) stay "
        "because the 11-18 feed no longer publishes AA indexes. EVIDENCE CLASS: market "
        "composites + n=1 probes only; no n>=3 measurement. Lane is retired (valid_to "
        "2026-09-26, replaced_by openrouter/moonshotai/kimi-k3) and its lifecycle record is "
        "left intact."
    ),
    "z-ai/glm-5.1": (
        "market cross-check 2026-11-18: PRICE REPAIRED (0.9646/3.0316/cache 0.17914 -> "
        "1.4/4.4/cache 0.26; normalized_price 1.4, public_price 1.52). The prior numbers were "
        "exactly 0.689x the sticker and no temporary_discounts row, plan_terms row or "
        "provider_rules discount covers this id, so the factor had no record behind it. " + FEED +
        " and " + MD + " (openrouter) both price the id 1.4/4.4/cache 0.26. Spec fields verified "
        "clean: 204,800 ctx, text->text (vision false, cap_video false), reasoning true, release "
        "2026-04-07. MARKET PROVENANCE: the 23 design_arena cells were re-ingested from the "
        "2026-11-18 feed with full provenance (percentile in score, raw ELO + rank + n in "
        "source_ref), collapsing the duplicate agent_tick cells; the two frozen "
        "artificial_analysis rows (coding 0.558, agentic 0.239) stay because the 11-18 feed no "
        "longer publishes AA indexes. EVIDENCE CLASS: market composites + n=1 probes only; no "
        "n>=3 measurement. Lane is retired (valid_to 2026-09-26, replaced_by openrouter/z-ai/"
        "glm-5.3) and its lifecycle record is left intact."
    ),
    "qwen/qwen3.6-plus": (
        "market cross-check 2026-11-18: VERIFIED CLEAN, no field change. " + FEED + " and " + MD +
        " agree with the row exactly: 0.325/1.95 with cache_write 0.40625 and NO cache_read "
        "sticker (the row's NULL is the carrier's own silence, not a gap), 1,000,000 ctx, "
        "text+image+video->text (vision true, cap_video true, cap_audio false), reasoning true, "
        "release 2026-04-02. ONE DOCUMENTED GAP, deliberately not changed: the carrier publishes "
        "a CONTEXT TIER — above 256,000 prompt tokens the sticker rises to 1.3/3.9/cache_write "
        "1.625 — and the row carries the base tier only, which is the registry's standing "
        "convention (a tier-aware price is a pricing-policy change, not a per-row edit). MARKET "
        "PROVENANCE: the 8 design_arena cells were re-ingested from the 2026-11-18 feed with "
        "full provenance; the frozen artificial_analysis coding row (0.545) stays because the "
        "11-18 feed no longer publishes AA indexes. EVIDENCE CLASS: market composites only; no "
        "n>=3 measurement. Lane is retired (valid_to 2026-09-26, replaced_by openrouter/qwen/"
        "qwen3.7-plus) and its lifecycle record is left intact."
    ),
    "z-ai/glm-5v-turbo": (
        "market cross-check 2026-11-18: VERIFIED CLEAN, no field change. " + FEED + " and " + MD +
        " agree with the row exactly: 1.2/4.0/cache 0.24, 202,752 ctx, text+image+video->text "
        "(vision true, cap_video true, cap_audio false), reasoning true, release 2026-04-01, "
        "131,072 max completion. MARKET PROVENANCE: the 21 design_arena cells were re-ingested "
        "from the 2026-11-18 feed with full provenance (percentile in score, raw ELO + rank + n "
        "in source_ref); the 11-18 feed publishes no AA index for this id, so no AA row exists "
        "and none is fabricated. EVIDENCE CLASS: n=1 small-probes (agent_tick 3/4, code_gen 2/3, "
        "delegation 3/4) plus market composites; no n>=3 measurement, no large-bench source."
    ),
}

LANES = [
    ("openrouter", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
    ("openrouter", "qwen/qwen3.6-35b-a3b"),
    ("openrouter", "moonshotai/kimi-k2.6"),
    ("openrouter", "z-ai/glm-5.1"),
    ("openrouter", "qwen/qwen3.6-plus"),
    ("openrouter", "z-ai/glm-5v-turbo"),
]

# Rows whose lifecycle_source holds a real lifecycle-pass record must not have it
# clobbered by a market note.
KEEP_LIFECYCLE = {
    ("openrouter", "moonshotai/kimi-k2.6"),
    ("openrouter", "z-ai/glm-5.1"),
    ("openrouter", "qwen/qwen3.6-plus"),
}

# cap_web_search / cap_computer_use are product features with no modality
# evidence class (scripts/capability_vocab.py resolves them only from explicit
# name markers), so a published modality string can never settle them.
CAP_NULL_REASON = (
    "market cross-check 2026-11-18: cap_web_search and cap_computer_use are PRODUCT features "
    "with no modality evidence class, so a published modality string can never resolve them - "
    "scripts/capability_vocab.py deliberately leaves them None and resolves them only from "
    "explicit name markers. No marker on this id and no lane-level sample; the null is by "
    "design, not an oversight."
)


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

    # Explained NULLs for the two capability families the feed cannot settle.
    have = set()
    with open(NULLS_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                have.add((r.get("provider"), r.get("model"), r.get("field")))
    nulls_added = 0
    with open(NULLS_PATH, "a", encoding="utf-8") as f:
        for provider, model in LANES:
            for field in ("cap_web_search", "cap_computer_use"):
                if (provider, model, field) in have:
                    continue
                f.write(
                    json.dumps(
                        {
                            "provider": provider,
                            "model": model,
                            "field": field,
                            "reason": "unknown-by-design",
                            "source": CAP_NULL_REASON,
                            "ts": DATE,
                        }
                    )
                    + "\n"
                )
                nulls_added += 1

    print(f"rows updated: {touched}; notes written: {added}; null reasons written: {nulls_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

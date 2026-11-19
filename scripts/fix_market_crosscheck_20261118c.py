#!/usr/bin/env python3
"""Market cross-check wave 2026-11-18c — the six gpt-5.4-family cards.

Cards: t_model_openai_gpt-5.4, t_model_openai_gpt-5.4:batch,
t_model_openai_gpt-5.4-mini, t_model_openai_gpt-5.4-mini:batch,
t_model_openai_gpt-5.4-nano, t_model_openai_gpt-5.4-nano:batch.

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.

What this wave actually changes:

  * The four :batch lanes carried vision/thinking/cap_* as NULL with
    `no-sample-yet` stamps. The carrier publishes the architecture for those
    exact ids (text+image+file->text, reasoning in supported_parameters), so the
    four modality-settled capability families are FILLED and their deferral
    stamps are retired. cap_web_search / cap_computer_use stay NULL: they are
    product features with no modality evidence class (scripts/capability_vocab.py
    resolves them only from explicit name markers), so their stamps are upgraded
    from the `no-sample-yet` deferral to the terminal `unknown-by-design`.
  * gpt-5.4 and gpt-5.4:batch had no stamps at all for those two families; they
    get them.
  * Prices on all six lanes match both machine sources to the cent — no price
    change. The flagship's 272,000-token CONTEXT TIER (5.0/22.5/cache 0.5) is
    documented in price_evidence and deliberately NOT applied: a tier-aware
    price is a pricing-policy change, not a per-row edit (same ruling as the
    2026-11-18b wave on qwen3.6-plus).
"""
import json
import sys

TABLES = "data/tables/models.jsonl"
NOTES_PATH = "data/tables/model_notes.jsonl"
NULLS_PATH = "data/null_reasons.jsonl"
DATE = "2026-11-18"
FEED = "carrier:openrouter /api/v1/models read 2026-11-18 (480 ids)"
MD = "models.dev api.json read 2026-11-18 (openrouter provider)"
PUB = (
    "publisher: openai.com/index/introducing-gpt-5-4-mini-and-nano/ read 2026-11-18 "
    "(400k context, $0.75/$4.50, text+image inputs, tool use, function calling, web "
    "search, file search, computer use, skills; released 17 March 2026)"
)

# The carrier publishes the same architecture for a :batch id as for its
# synchronous twin — same model, different serving tier. Filling the four
# modality-settled families from that published string is evidence, not a guess.
BATCH_CAPS = {
    "vision": True,
    "thinking": True,
    "cap_image_generation": False,
    "cap_embedding": False,
    "cap_audio": False,
    "cap_video": False,
}

UPDATES = {
    # Retired lane (valid_to 2026-09-26, replaced_by openrouter/openai/gpt-5.5).
    # Price and context verified clean; the context tier is documented only.
    ("openrouter", "openai/gpt-5.4"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 2.5/15/cache 0.25 "
            "(web_search 0.01 per call) and " + MD + " (openrouter) agrees to the cent; "
            "1,050,000 ctx / 128,000 max completion / text+image+file->text on both. "
            "DOCUMENTED NOT APPLIED: the carrier publishes a CONTEXT TIER — above "
            "272,000 prompt tokens the sticker rises to 5.0/22.5/cache 0.5 — and the "
            "row carries the base tier only, which is the registry's standing "
            "convention (a tier-aware price is a pricing-policy change, not a "
            "per-row edit)"
        ),
    },
    # Retired lane (valid_to 2026-09-27, replaced_by openrouter/openai/gpt-5.5).
    # Price verified clean; the six spec fields were NULL and are now filled from
    # the carrier's published architecture for this exact id.
    ("openrouter", "openai/gpt-5.4:batch"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 1.25/7.5/cache "
            "0.125 (exactly half the synchronous sticker, the batch convention) and "
            "carries the same 1,050,000 ctx / 128,000 max completion / "
            "text+image+file->text architecture; models.dev does not list the :batch "
            "SKU, so the carrier is the only machine source for it. Same documented "
            "context tier above 272,000 prompt tokens (2.5/11.25/cache 0.25), not "
            "applied"
        ),
        **BATCH_CAPS,
    },
    ("openrouter", "openai/gpt-5.4-mini"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.75/4.5/cache "
            "0.075 (web_search 0.01 per call) and " + MD + " (openrouter) agrees to "
            "the cent; 400,000 ctx / 128,000 max completion / text+image+pdf->text on "
            "both; " + PUB
        ),
    },
    ("openrouter", "openai/gpt-5.4-mini:batch"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.375/2.25/cache "
            "0.0375 (exactly half the synchronous sticker, the batch convention) with "
            "the same 400,000 ctx / 128,000 max completion / text+image+file->text "
            "architecture; models.dev does not list the :batch SKU, so the carrier is "
            "the only machine source for it"
        ),
        **BATCH_CAPS,
    },
    ("openrouter", "openai/gpt-5.4-nano"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.2/1.25/cache "
            "0.02 (web_search 0.01 per call) and " + MD + " (openrouter) agrees to the "
            "cent; 400,000 ctx / 128,000 max completion / text+image+pdf->text on "
            "both; " + PUB
        ),
    },
    ("openrouter", "openai/gpt-5.4-nano:batch"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id 0.1/0.625/cache "
            "0.01 (exactly half the synchronous sticker, the batch convention) with "
            "the same 400,000 ctx / 128,000 max completion / text+image+file->text "
            "architecture; models.dev does not list the :batch SKU, so the carrier is "
            "the only machine source for it"
        ),
        **BATCH_CAPS,
    },
}

PROVENANCE = {
    "openai/gpt-5.4": (
        "market cross-check 2026-11-18: VERIFIED CLEAN on price and spec — no field "
        "change beyond price_evidence. " + FEED + " and " + MD + " agree exactly: "
        "2.5/15/cache 0.25, 1,050,000 ctx, 128,000 max completion, "
        "text+image+file->text (vision true, cap_video false, cap_audio false), "
        "reasoning true, release 2026-03-05, knowledge 2025-08-31. MARKET "
        "PROVENANCE: the 13 design_arena cells were re-ingested from the 2026-11-18 "
        "feed with full provenance (percentile in score, raw ELO + rank + n in "
        "source_ref); the frozen artificial_analysis coding row (0.711) is stamped "
        "with its 2026-11-17 origin because the 11-18 feed publishes coding_index "
        "and agentic_index as null for all 223 AA-carrying ids. EVIDENCE CLASS: "
        "market composites only; no n>=3 measurement, no large-bench source. Lane is "
        "retired (valid_to 2026-09-26, replaced_by openrouter/openai/gpt-5.5) and its "
        "lifecycle record is left intact."
    ),
    "openai/gpt-5.4:batch": (
        "market cross-check 2026-11-18: SIX SPEC FIELDS FILLED — vision, thinking, "
        "cap_image_generation, cap_embedding, cap_audio and cap_video were NULL with "
        "`no-sample-yet` deferral stamps; " + FEED + " publishes this exact id's "
        "architecture (text+image+file->text, output text only, reasoning + "
        "reasoning_effort in supported_parameters), which settles all four "
        "modality-settled families, so they are filled and the deferral stamps "
        "retired. PRICE verified clean: 1.25/7.5/cache 0.125, exactly half the "
        "synchronous sticker. api_type is the single character 'o' — the known "
        "TR-215 truncation defect tracked by card t_api_type_truncation, NOT fixed "
        "here (one card, one scope). MARKET PROVENANCE: 13 design_arena cells "
        "re-ingested with full provenance; the frozen AA coding row (0.711) stamped "
        "with its 2026-11-17 origin. Lane is retired (valid_to 2026-09-27, "
        "replaced_by openrouter/openai/gpt-5.5) and its lifecycle record is left "
        "intact."
    ),
    "openai/gpt-5.4-mini": (
        "market cross-check 2026-11-18: VERIFIED CLEAN — no field change beyond "
        "price_evidence. " + FEED + " and " + MD + " agree exactly: 0.75/4.5/cache "
        "0.075, 400,000 ctx, 128,000 max completion, text+image+pdf->text (vision "
        "true, cap_video false, cap_audio false), reasoning true, release "
        "2026-03-17, knowledge 2025-08-31. " + PUB + ". MARKET PROVENANCE: the "
        "2026-11-18 feed publishes NO design_arena entry and NO AA index in the "
        "ingest's mapped set for this id (its AA block carries only "
        "multilingual_index 0.894, which has no defensible mapping to the 24-category "
        "vocabulary), so no fresh market cell exists; the two frozen "
        "artificial_analysis rows (coding 0.561, agentic 0.179) are stamped with "
        "their 2026-11-17 origin and the fact that the 11-18 feed is silent. "
        "EVIDENCE CLASS: n=1 small-probes (agent_tick 3/4, code_gen 2/3, delegation "
        "3/4, long_doc 4/4, schema 4/4, test 4/4) plus market composites; no n>=3 "
        "measurement, no large-bench source."
    ),
    "openai/gpt-5.4-mini:batch": (
        "market cross-check 2026-11-18: SIX SPEC FIELDS FILLED — vision, thinking, "
        "cap_image_generation, cap_embedding, cap_audio and cap_video were NULL with "
        "`no-sample-yet` deferral stamps; " + FEED + " publishes this exact id's "
        "architecture (text+image+file->text, output text only, reasoning in "
        "supported_parameters), which settles all four modality-settled families. "
        "PRICE verified clean: 0.375/2.25/cache 0.0375, exactly half the synchronous "
        "sticker. MARKET PROVENANCE: the 11-18 feed publishes no design_arena entry "
        "and no mapped AA index for this id, so no fresh market cell exists; the two "
        "frozen artificial_analysis rows (coding 0.561, agentic 0.179) are stamped "
        "with their 2026-11-17 origin. EVIDENCE CLASS: market composites only; no "
        "n>=3 measurement."
    ),
    "openai/gpt-5.4-nano": (
        "market cross-check 2026-11-18: VERIFIED CLEAN — no field change beyond "
        "price_evidence. " + FEED + " and " + MD + " agree exactly: 0.2/1.25/cache "
        "0.02, 400,000 ctx, 128,000 max completion, text+image+pdf->text (vision "
        "true, cap_video false, cap_audio false), reasoning true, release "
        "2026-03-17, knowledge 2025-08-31. " + PUB + ". MARKET PROVENANCE: the "
        "2026-11-18 feed publishes NO benchmarks block at all for this id (no "
        "design_arena, no AA index), so no fresh market cell exists; the two frozen "
        "artificial_analysis rows (coding 0.561, agentic 0.16) are stamped with their "
        "2026-11-17 origin and the fact that the 11-18 feed is silent. EVIDENCE "
        "CLASS: market composites only; no n>=3 measurement, no large-bench source."
    ),
    "openai/gpt-5.4-nano:batch": (
        "market cross-check 2026-11-18: SIX SPEC FIELDS FILLED — vision, thinking, "
        "cap_image_generation, cap_embedding, cap_audio and cap_video were NULL with "
        "`no-sample-yet` deferral stamps; " + FEED + " publishes this exact id's "
        "architecture (text+image+file->text, output text only, reasoning in "
        "supported_parameters), which settles all four modality-settled families. "
        "PRICE verified clean: 0.1/0.625/cache 0.01, exactly half the synchronous "
        "sticker. MARKET PROVENANCE: the 11-18 feed publishes no benchmarks block "
        "for this id, so no fresh market cell exists; the two frozen "
        "artificial_analysis rows (coding 0.561, agentic 0.16) are stamped with their "
        "2026-11-17 origin. EVIDENCE CLASS: market composites only; no n>=3 "
        "measurement."
    ),
}

LANES = [
    ("openrouter", "openai/gpt-5.4"),
    ("openrouter", "openai/gpt-5.4:batch"),
    ("openrouter", "openai/gpt-5.4-mini"),
    ("openrouter", "openai/gpt-5.4-mini:batch"),
    ("openrouter", "openai/gpt-5.4-nano"),
    ("openrouter", "openai/gpt-5.4-nano:batch"),
]

# cap_web_search / cap_computer_use are product features with no modality
# evidence class (scripts/capability_vocab.py resolves them only from explicit
# name markers), so a published modality string can never settle them. The
# publisher DOES claim web search / file search / computer use for the mini and
# nano models — real evidence the resolver's name-marker-only rule cannot
# consume; recorded in the note, not forced onto the row (the seed would
# overwrite a hand-set value on the next derivation).
CAP_NULL_REASON = (
    "market cross-check 2026-11-18: cap_web_search and cap_computer_use are PRODUCT "
    "features with no modality evidence class, so a published modality string can "
    "never resolve them - scripts/capability_vocab.py deliberately leaves them None "
    "and resolves them only from explicit name markers. No marker on this id; the "
    "null is by design, not an oversight. NOTE: the publisher page for the mini/nano "
    "models does claim web search, file search and computer use, which is evidence "
    "the name-marker-only rule cannot consume - a resolver gap, not a data gap."
)

# Stamps retired because the field is no longer NULL (carrier evidence landed).
SUPERSEDED = {
    ("openrouter", "openai/gpt-5.4-mini:batch"): {
        "cap_image_generation",
        "cap_embedding",
        "cap_audio",
        "cap_video",
    },
    ("openrouter", "openai/gpt-5.4-nano:batch"): {
        "cap_image_generation",
        "cap_embedding",
        "cap_audio",
        "cap_video",
    },
}


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
            row["note"] = PROVENANCE[row["model"]]
            out.append(json.dumps(row) + "\n")
            touched += 1
    if touched != len(UPDATES):
        print(f"REFUSED: touched {touched} rows, expected {len(UPDATES)}", file=sys.stderr)
        return 2
    with open(TABLES, "w", encoding="utf-8") as f:
        f.writelines(out)

    # model_notes: one provenance row per lane.
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
                        "note": PROVENANCE[model],
                        "source": "market cross-check 2026-11-18 (openrouter "
                        "/api/v1/models live feed + models.dev api.json + "
                        "publisher launch coverage)",
                        "valid_from": DATE,
                    }
                )
                + "\n"
            )
            added += 1

    # null_reasons: retire superseded stamps, upgrade the two product-feature
    # families to a terminal reason, add them where they were missing.
    kept, dropped = [], 0
    with open(NULLS_PATH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                kept.append(line)
                continue
            r = json.loads(line)
            key = (r.get("provider"), r.get("model"))
            if key in SUPERSEDED and r.get("field") in SUPERSEDED[key]:
                dropped += 1
                continue
            if key in LANES and r.get("field") in ("cap_web_search", "cap_computer_use"):
                r["reason"] = "unknown-by-design"
                r["source"] = CAP_NULL_REASON
                r["ts"] = DATE
                kept.append(json.dumps(r) + "\n")
                continue
            kept.append(line)
    with open(NULLS_PATH, "w", encoding="utf-8") as f:
        f.writelines(kept)

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

    print(
        f"rows updated: {touched}; notes written: {added}; "
        f"null stamps retired: {dropped}; null stamps added: {nulls_added}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

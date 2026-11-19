#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19b — six mixed-family cards.

Cards: t_model_deepseek_deepseek-v4-pro-0813, t_model_google_gemini-3.7-flash,
t_model_moonshotai_kimi-k2.5, t_model_nvidia_nemotron-3-nano-30b-a3b,
t_model_nvidia_nemotron-3-super-120b-a12b, t_model_openai_gpt-4.

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.

What this wave changes:

  * FOUR PRICE DRIFTS on openrouter lanes, each where the live carrier feed AND
    models.dev's openrouter provider agree to the digit and the stored sticker
    disagrees:
      - deepseek/deepseek-v4-pro-0813  1.32/3.96  -> 0.111/4.44 (cache 0.097)
      - moonshotai/kimi-k2.5           0.45/2.25  -> 0.5/2.5   (cache 0.15)
      - nvidia/nemotron-3-nano-30b-a3b 0.05/0.2   -> 0.06/0.24 (cache 0.012)
      - nvidia/nemotron-3-super-120b-a12b 0.08/0.45 -> 0.075/0.4 (no cache rate)
    public_price re-blended 0.96*in + 0.04*out, the convention every openrouter
    row follows.

  * TWO CAPABILITY-BLIND MIRROR ROWS corrected: the commandcode and
    commandcode-2 lanes of google/gemini-3.7-flash carried vision=false AND
    thinking=false against a carrier architecture of text+image+file+audio+video
    -> text with a MANDATORY reasoning ladder (high/medium/low). Same defect
    class the 2026-11-17 wave fixed on gemini-3.5-flash and gemini-3.6-flash.

  * TWO release_date FILLS (a MODEL property, not a lane property):
    moonshotai/kimi-k2.5 on both xkiro lanes (2026-01-27) and openai/gpt-4 on
    openrouter (2023-11-06).

  * THREE modality-settled cap_* fills on the kimi-k2.5 xkiro twins
    (cap_image_generation / cap_embedding / cap_audio were NULL; the carrier
    publishes text+image->text, output text only). cap_video is LEFT ALONE: it
    already carried true and models.dev shows several providers publishing video
    input for this id, so the openrouter architecture cannot settle it.

  * MARKET CELLS re-ingested with provenance for every id the feed still
    publishes a design_arena block for; AA cells the feed no longer republishes
    are FROZEN with origin index + raw score + not-republished.

  * cap_web_search / cap_computer_use stay NULL on the openrouter lanes and are
    stamped `unknown-by-design` (product features, no modality evidence class).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-19"
PRIOR = "2026-11-17"
PAYLOAD = "/home/kara/.hermes/cache/scratch/kb-mr-20261119b/or_models.json"
TABLES = Path("data/tables/models.jsonl")
BENCH = Path("data/tables/benchmarks.jsonl")
NOTES = Path("data/tables/model_notes.jsonl")
NULLS = Path("data/null_reasons.jsonl")

FEED = "carrier:openrouter /api/v1/models read 2026-11-19 (480 ids)"
MD = "models.dev api.json read 2026-11-19 (openrouter provider, 383 ids)"

# (provider, model) -> {column: new value}
UPDATES = {
    # ---- deepseek-v4-pro-0813: the openrouter lane carried the arcee/novita
    # sticker (1.32/3.96), not openrouter's own 0.111/4.44.
    ("openrouter", "deepseek/deepseek-v4-pro-0813"): {
        "normalized_price": 0.111, "public_in_per_m": 0.111,
        "public_out_per_m": 4.44, "public_cache_read_per_m": 0.097,
        "public_price": 0.28416,
    },
    # ---- gemini-3.7-flash: capability-blind commandcode mirror rows.
    ("commandcode", "google/gemini-3.7-flash"): {"vision": True, "thinking": True},
    ("commandcode-2", "google/gemini-3.7-flash"): {"vision": True, "thinking": True},
    # ---- kimi-k2.5: openrouter sticker was 10% under; xkiro twins lacked the
    # model's release date and all four modality-settled cap_* flags.
    ("openrouter", "moonshotai/kimi-k2.5"): {
        "normalized_price": 0.5, "public_in_per_m": 0.5,
        "public_out_per_m": 2.5, "public_cache_read_per_m": 0.15,
        "public_price": 0.58,
    },
    ("xkiro", "moonshotai/kimi-k2.5"): {
        "release_date": "2026-01-27", "cap_image_generation": False,
        "cap_embedding": False, "cap_audio": False,
    },
    ("xkiro-2", "moonshotai/kimi-k2.5"): {
        "release_date": "2026-01-27", "cap_image_generation": False,
        "cap_embedding": False, "cap_audio": False,
    },
    # ---- nemotron nano: stored 0.05/0.2 is the novita sticker.
    ("openrouter", "nvidia/nemotron-3-nano-30b-a3b"): {
        "normalized_price": 0.06, "public_in_per_m": 0.06,
        "public_out_per_m": 0.24, "public_cache_read_per_m": 0.012,
        "public_price": 0.0672,
    },
    # ---- nemotron super: stored 0.08/0.45 vs 0.075/0.4 on both sources.
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b"): {
        "normalized_price": 0.075, "public_in_per_m": 0.075,
        "public_out_per_m": 0.4, "public_price": 0.088,
    },
    # ---- gpt-4: price verified clean (30/60); release_date was NULL.
    ("openrouter", "openai/gpt-4"): {"release_date": "2023-11-06"},
}

PRICE_EVIDENCE = {
    ("openrouter", "deepseek/deepseek-v4-pro-0813"): (
        "normalized:payg-sticker | " + FEED + " prices this id 0.111/4.44/cache "
        "0.097 and " + MD + " agrees to the digit; 1,048,576 ctx / 943,718 max "
        "completion / text->text / reasoning supported (efforts max, high, low, "
        "none; default high). The stored 1.32/3.96 was the arcee/novita/"
        "digitalocean sticker, not openrouter's."
    ),
    ("openrouter", "moonshotai/kimi-k2.5"): (
        "normalized:payg-sticker | " + FEED + " prices this id 0.5/2.5/cache 0.15 "
        "and " + MD + " agrees to the digit; 262,144 ctx / 235,929 max completion "
        "/ text+image->text / reasoning supported (not mandatory). The stored "
        "0.45/2.25 was 10% under both sources."
    ),
    ("openrouter", "nvidia/nemotron-3-nano-30b-a3b"): (
        "normalized:payg-sticker | " + FEED + " prices this id 0.06/0.24/cache "
        "0.012 and " + MD + " agrees to the digit; 262,144 ctx / 235,929 max "
        "completion / text->text / reasoning supported (not mandatory). The "
        "stored 0.05/0.2 was the novita sticker."
    ),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b"): (
        "normalized:payg-sticker | " + FEED + " prices this id 0.075/0.4 with NO "
        "cache rate published, and " + MD + " agrees to the digit; 262,144 ctx / "
        "16,384 max completion / text->text / reasoning supported (efforts "
        "medium, low, none; default medium). The stored 0.08/0.45 matched no "
        "source; cache-read stays NULL = not published, never 0."
    ),
    ("openrouter", "openai/gpt-4"): (
        "normalized:payg-sticker | " + FEED + " prices this id 30.0/60.0 with no "
        "cache rate and " + MD + " agrees to the digit; 8,191 ctx / 4,096 max "
        "completion / text->text / no reasoning block. Price verified clean — "
        "no change. release_date filled 2023-11-06 from " + MD + " (the feed's "
        "created stamp 2023-05-28 is the API listing date, not the release)."
    ),
}

NOTES_TEXT = {
    ("openrouter", "deepseek/deepseek-v4-pro-0813"): (
        "market cross-check 2026-11-19: ONE PRICE CORRECTED — 1.32/3.96/cache "
        "0.044 -> 0.111/4.44/cache 0.097. Both machine sources publish the new "
        "sticker (" + FEED + " input_cache_read 0.000000097; " + MD + " cost "
        "0.111/4.44/0.097); 1.32/3.96 is the arcee/novita/digitalocean/requesty "
        "sticker for the same weights, so the openrouter lane had been ranked on "
        "another carrier's price. public_price re-blended 0.96*in+0.04*out = "
        "0.28416. SPEC verified clean: 1,048,576 ctx, 943,718 max completion, "
        "text->text (vision false), reasoning supported with a max/high/low/none "
        "effort ladder and default high (thinking true). AA: the feed publishes "
        "intelligence 23.1 only (no coding/agentic block), so the stored coding "
        "0.688 and agentic 0.413 cells are FROZEN 2026-11-17 evidence stamped "
        "not-republished; the feed publishes NO design_arena block for this id, "
        "so there is no arena cell to re-ingest. EVIDENCE CLASS: market "
        "composites only; no n>=3 measurement. Lane is LIVE (valid_to NULL). "
        "The xkiro/xkiro-2 twins are retired (valid_to 2026-10-23) and carry "
        "xkiro's own plan sticker — untouched by this card."
    ),
    ("commandcode", "google/gemini-3.7-flash"): (
        "market cross-check 2026-11-19: TWO CAPABILITY FLAGS CORRECTED — vision "
        "false -> true and thinking false -> true. " + FEED + " publishes this "
        "id's architecture as text+image+file+audio+video -> text with a "
        "MANDATORY reasoning ladder (high/medium/low, default medium), and "
        "models.dev publishes the same modalities for the google, openrouter and "
        "commandcode entries. The commandcode mirror row was capability-blind — "
        "the same defect class the 2026-11-17 wave fixed on gemini-3.5-flash and "
        "gemini-3.6-flash. cap_audio/cap_video were already true (both are input "
        "modalities) and cap_image_generation/cap_embedding already false. "
        "PRICE FLAGGED, NOT CHANGED: the row carries 0.75/3.75 with evidence "
        "class 'commandcode-no-markup', which equals Google's own sticker, while "
        "models.dev's commandcode entry publishes 1.5/7.5/cache 0.15/cache-write "
        "0.08334 — exactly 2x. The keyless carrier catalog "
        "(api.commandcode.ai/provider/v1/models, read 2026-11-19, 105 ids) "
        "publishes context_length 1,048,576 and NO price, so it cannot settle "
        "the conflict; the lane is retired (valid_to 2026-09-27) and the "
        "no-markup evidence class is the row's own record, so the 2x gap is "
        "recorded here for the pricing lane rather than silently overwritten. "
        "EVIDENCE CLASS: market composites only; no n>=3 measurement."
    ),
    ("commandcode-2", "google/gemini-3.7-flash"): (
        "market cross-check 2026-11-19: TWO CAPABILITY FLAGS CORRECTED — vision "
        "false -> true and thinking false -> true, on the commandcode-2 mirror of "
        "the same id. " + FEED + " publishes text+image+file+audio+video -> text "
        "with a MANDATORY reasoning ladder (high/medium/low, default medium). "
        "Same capability-blind mirror defect the 2026-11-17 wave fixed on the "
        "gemini-3.5-flash and gemini-3.6-flash mirrors. PRICE FLAGGED, NOT "
        "CHANGED: 0.75/3.75 (Google's sticker, 'no-markup' evidence class) vs "
        "models.dev commandcode 1.5/7.5 — a 2x gap the keyless carrier catalog "
        "cannot settle (it publishes no price). Lane is retired (valid_to "
        "2026-09-27). EVIDENCE CLASS: market composites only."
    ),
    ("openrouter", "moonshotai/kimi-k2.5"): (
        "market cross-check 2026-11-19: ONE PRICE CORRECTED — 0.45/2.25 -> "
        "0.5/2.5/cache 0.15. Both machine sources publish the new sticker ("
        + FEED + " input_cache_read 0.00000015; " + MD + " cost 0.5/2.5/0.15); "
        "0.45/2.25 was 10% under both. public_price re-blended 0.96*in+0.04*out "
        "= 0.58. SPEC verified clean: 262,144 ctx, 235,929 max completion, "
        "text+image->text (vision true), reasoning supported but NOT mandatory "
        "(thinking true). MARKET PROVENANCE: the feed publishes 13 design_arena "
        "entries for this id, re-ingested with raw ELO + rank + arena population "
        "n=186; the AA cells (coding 0.468 and the openness/multilingual block) "
        "are FROZEN 2026-11-17 evidence stamped not-republished because the feed "
        "publishes no AA coding/agentic block for this id. EVIDENCE CLASS: market "
        "composites only; no n>=3 measurement. Lane is retired (valid_to "
        "2026-09-26)."
    ),
    ("xkiro", "moonshotai/kimi-k2.5"): (
        "market cross-check 2026-11-19: FOUR SPEC FIELDS FILLED — release_date "
        "2026-01-27 and cap_image_generation/cap_embedding/cap_audio all false. "
        "cap_video is LEFT AT true: models.dev shows several providers publishing "
        "video input for this id, so the openrouter architecture (text+image) "
        "cannot settle it and an existing non-null value is not flipped on a "
        "conflicting source. release_date is a MODEL property, not a lane property: "
        + MD + " publishes 2026-01-27 for every provider carrying this id and "
        "the openrouter twin already carries it; xkiro publishes no release date "
        "of its own. The four cap_* flags are modality-settled by the carrier "
        "architecture (text+image -> text, output text only), which is the same "
        "evidence class the 2026-11-17 wave used for the Mistral and Gemini "
        "mirrors. PRICE untouched: 0.3705/1.8525 is xkiro's own plan sticker "
        "(internal /30 multiplier), not a market price. Lane is retired "
        "(valid_to 2026-10-15)."
    ),
    ("xkiro-2", "moonshotai/kimi-k2.5"): (
        "market cross-check 2026-11-19: FOUR SPEC FIELDS FILLED — release_date "
        "2026-01-27 and cap_image_generation/cap_embedding/cap_audio all false, "
        "on the xkiro-2 mirror. cap_video is LEFT AT true (models.dev shows "
        "providers publishing video input for this id; the openrouter "
        "architecture cannot settle it). release_date is a MODEL property: "
        + MD + " publishes 2026-01-27 for every provider carrying this id. The "
        "cap_* flags are modality-settled by the carrier architecture "
        "(text+image -> text). PRICE untouched (xkiro plan sticker). Lane is "
        "retired (valid_to 2026-10-23)."
    ),
    ("openrouter", "nvidia/nemotron-3-nano-30b-a3b"): (
        "market cross-check 2026-11-19: ONE PRICE CORRECTED — 0.05/0.2 -> "
        "0.06/0.24/cache 0.012. Both machine sources publish the new sticker ("
        + FEED + " input_cache_read 0.000000012; " + MD + " cost 0.06/0.24/"
        "0.012); 0.05/0.2 is the novita sticker for the same weights. "
        "public_price re-blended 0.96*in+0.04*out = 0.0672. SPEC verified clean: "
        "262,144 ctx, 235,929 max completion, text->text (vision false), "
        "reasoning supported but not mandatory (thinking true). AA: the feed "
        "publishes openness 83.3 and multilingual 0.612 only — no coding/agentic "
        "block — so the stored coding 0.144 / agentic 0.01 cells are FROZEN "
        "2026-11-17 evidence stamped not-republished; the feed publishes NO "
        "design_arena block for this id. The 8 live-probe-2026-11-07 rows are "
        "n=1 small-probe evidence and are untouched. EVIDENCE CLASS: market "
        "composites + n=1 probe; no n>=3 measurement. Lane is LIVE."
    ),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b"): (
        "market cross-check 2026-11-19: ONE PRICE CORRECTED — 0.08/0.45 -> "
        "0.075/0.4. Both machine sources publish the new sticker (" + FEED
        + " and " + MD + " agree to the digit); 0.08/0.45 matched no source. "
        "public_price re-blended 0.96*in+0.04*out = 0.088. cache-read stays NULL "
        "= not published by either source, never 0. SPEC verified clean: "
        "262,144 ctx, 16,384 max completion, text->text (vision false), "
        "reasoning supported with a medium/low/none effort ladder and default "
        "medium (thinking true). AA: the feed publishes openness 83.3 only, so "
        "the stored coding 0.377 / agentic 0.017 cells are FROZEN 2026-11-17 "
        "evidence stamped not-republished; no design_arena block exists for this "
        "id. The 8 live-probe-2026-11-07 rows are n=1 small-probe evidence and "
        "are untouched. EVIDENCE CLASS: market composites + n=1 probe. Lane is "
        "LIVE."
    ),
    ("openrouter", "openai/gpt-4"): (
        "market cross-check 2026-11-19: ONE SPEC FIELD FILLED — release_date "
        "2023-11-06 from " + MD + ". The feed's created stamp (2023-05-28) is "
        "the API listing date, not the release; models.dev's release_date is the "
        "registry's own field and is the value every other lane in this table "
        "uses. PRICE verified clean, no change: 30.0/60.0 on both sources, no "
        "cache rate published (cache-read/-write stay NULL). SPEC verified "
        "clean: 8,191 ctx, 4,096 max completion, text->text (vision false), no "
        "reasoning block (thinking false). AA: the feed publishes no AA block, "
        "so the stored coding 0.131 cell is FROZEN 2026-11-17 evidence stamped "
        "not-republished; no design_arena block exists. EVIDENCE CLASS: market "
        "composites only. Lane is retired (valid_to 2026-09-26)."
    ),
}

CAP_NULL_REASON = (
    "market cross-check 2026-11-19: cap_web_search and cap_computer_use are "
    "PRODUCT features with no modality evidence class - scripts/capability_vocab.py "
    "resolves them only from explicit name markers, so a published architecture "
    "string can never settle them. The 2026-11-18c wave ruled that a carrier "
    "price line is not a capability declaration; this wave follows that ruling "
    "rather than splitting the table's convention."
)

# Fields whose NULL stamp is retired because carrier evidence landed.
SUPERSEDED_FIELDS = {
    "cap_image_generation",
    "cap_embedding",
    "cap_audio",
    "cap_video",
    "release_date",
}

AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}

LANES = sorted({m for (_p, m) in UPDATES})
OPENROUTER_LANES = [m for (p, m) in UPDATES if p == "openrouter"]


def fill_spec():
    lines = TABLES.read_text(encoding="utf-8").splitlines(keepends=True)
    out, touched = [], 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        key = (r.get("provider"), r.get("model"))
        if key not in UPDATES:
            out.append(line)
            continue
        for col, val in UPDATES[key].items():
            r[col] = val
        if key in PRICE_EVIDENCE:
            r["price_evidence"] = PRICE_EVIDENCE[key]
        r["note"] = NOTES_TEXT[key]
        touched += 1
        out.append(json.dumps(r, ensure_ascii=False) + "\n")
    if touched != len(UPDATES):
        print(f"REFUSED: touched {touched} rows, expected {len(UPDATES)}", file=sys.stderr)
        return 2
    TABLES.write_text("".join(out), encoding="utf-8")
    print(f"spec rows updated: {touched}")
    return 0


def reingest_cells():
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    rows, report = build_rows(payload, load_registry_ids(str(TABLES)), DATE)
    mine = [r for r in rows if r["model"] in LANES]
    fresh_cells = {(r["model"], r["category"]) for r in mine}
    print(f"feed emitted={report['rows_emitted']} mine={len(mine)} cells={len(fresh_cells)}")

    out, dropped, stamped = [], 0, 0
    for line in BENCH.read_text(encoding="utf-8").splitlines(keepends=True):
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if r.get("model") not in LANES or r.get("source") != "market":
            out.append(line)
            continue
        if (r["model"], r["category"]) in fresh_cells:
            dropped += 1
            continue
        if r.get("source_ref"):
            out.append(line)
            continue
        index = next((k for k, v in AA_CATS.items() if v == r["category"]), None)
        if index is None:
            out.append(line)
            continue
        raw = round(float(r["score"]) * 100.0, 6)
        r["source_ref"] = (
            f"openrouter-models-api:{PRIOR}:index={index}:raw_score={raw}"
            f":not-republished-{DATE}"
        )
        stamped += 1
        out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    pending = sorted(mine, key=lambda r: (r["model"], r["category"]))
    final = []
    for line in out:
        if pending and line.strip():
            while pending and json.loads(line)["model"] > pending[0]["model"]:
                final.append(json.dumps(pending.pop(0), ensure_ascii=False, sort_keys=True) + "\n")
        final.append(line)
    for r in pending:
        final.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    BENCH.write_text("".join(final), encoding="utf-8")
    print(f"dropped {dropped} superseded market rows; wrote {len(mine)} fresh; stamped {stamped} frozen")


def add_notes():
    existing = set()
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    added = 0
    with NOTES.open("a", encoding="utf-8") as f:
        for (provider, model), note in NOTES_TEXT.items():
            if (provider, model, DATE) in existing:
                print(f"note already present: {provider}/{model}")
                continue
            f.write(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model,
                        "note": note,
                        "source": "openrouter /api/v1/models (480 ids) + "
                        "models.dev api.json (openrouter 383 ids, commandcode 93 ids), "
                        "both read 2026-11-19",
                        "valid_from": DATE,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            added += 1
    print(f"notes written: {added}")


def fix_null_reasons():
    kept, dropped, upgraded = [], 0, 0
    for line in NULLS.read_text(encoding="utf-8").splitlines(keepends=True):
        if not line.strip():
            kept.append(line)
            continue
        r = json.loads(line)
        key = (r.get("provider"), r.get("model"))
        field = r.get("field")
        if key in UPDATES and field in SUPERSEDED_FIELDS and field in UPDATES[key]:
            dropped += 1
            continue
        if key in UPDATES and field in ("cap_web_search", "cap_computer_use"):
            r["reason"] = "unknown-by-design"
            r["source"] = CAP_NULL_REASON
            r["ts"] = DATE
            upgraded += 1
            kept.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            continue
        kept.append(line)
    NULLS.write_text("".join(kept), encoding="utf-8")

    have = set()
    for line in NULLS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            have.add((r.get("provider"), r.get("model"), r.get("field")))
    added = 0
    with NULLS.open("a", encoding="utf-8") as f:
        for model in OPENROUTER_LANES:
            for field in ("cap_web_search", "cap_computer_use"):
                if ("openrouter", model, field) in have:
                    continue
                f.write(
                    json.dumps(
                        {
                            "provider": "openrouter",
                            "model": model,
                            "field": field,
                            "reason": "unknown-by-design",
                            "source": CAP_NULL_REASON,
                            "ts": DATE,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                added += 1
    print(f"null stamps retired: {dropped}; upgraded: {upgraded}; added: {added}")


def main():
    rc = fill_spec()
    if rc:
        return rc
    reingest_cells()
    add_notes()
    fix_null_reasons()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

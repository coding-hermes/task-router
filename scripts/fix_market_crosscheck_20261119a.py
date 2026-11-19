#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19a — the six gpt-4.1-family cards.

Cards: t_model_openai_gpt-4.1, t_model_openai_gpt-4.1:batch,
t_model_openai_gpt-4.1-mini, t_model_openai_gpt-4.1-mini:batch,
t_model_openai_gpt-4.1-nano, t_model_openai_gpt-4.1-nano:batch.

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.

What this wave actually changes:

  * MARKET CELLS. All 40 source=market rows for these six ids carried
    source_ref=NULL. The 2026-11-19 feed publishes six design_arena entries per
    id, so those 36 cells are re-ingested through
    scripts/router_market_ingest.py's own build_rows (raw ELO, rank and the
    arena population land in source_ref). The percentile MOVES on every one of
    them: the 2026-11-17 rows were normalized against the category's entry
    count, the ingest now normalizes against the arena's ranked-model count
    (n=186), which is the fix landed by card t_market_pct_normalization. The
    feed publishes no artificial_analysis block for any of the six ids, so the
    four AA cells (mini coding 0.202, mini:batch 0.202, nano coding 0.111,
    nano:batch 0.111) are FROZEN 2026-11-17 evidence and are stamped with
    origin index + raw score + not-republished.

  * SPEC NULLS FILLED on the three :batch lanes. vision, thinking,
    cap_image_generation, cap_embedding, cap_audio and cap_video were NULL with
    `no-sample-yet` deferral stamps. The carrier publishes the architecture for
    those exact ids (text+image+file->text, output text only, no reasoning in
    supported_parameters), which settles all four modality-settled families.
    thinking is filled FALSE, not true: gpt-4.1 is not a reasoning model
    (models.dev reasoning=false, no reasoning/reasoning_effort in the carrier's
    supported_parameters) — the gpt-5.4 wave filled TRUE because that family is
    a reasoning family, and copying that value here would be a fabrication.

  * ONE PRICE CORRECTED. gpt-4.1-nano carried public_cache_read_per_m 0.025;
    both machine sources publish 0.03 (carrier input_cache_read 0.00000003,
    models.dev cost.cache_read 0.03). 0.025 is 25% of the input sticker, the
    family's old cache ratio, not this id's published rate. The other five
    lanes match both sources to the digit — no price change.

  * cap_web_search / cap_computer_use stay NULL. The carrier DOES publish a
    web_search price line (0.01 per call) on all six ids, which is real
    evidence the resolver cannot consume: scripts/capability_vocab.py resolves
    these two families only from explicit name markers, and the 2026-11-18c
    wave ruled that a carrier price line is not a capability declaration. The
    stamps are upgraded from the `no-sample-yet` deferral to the terminal
    `unknown-by-design` and the evidence is recorded in the note, so the gap is
    a resolver gap on the record rather than a silent blank.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-19"
PRIOR = "2026-11-17"
PAYLOAD = "/home/kara/.hermes/cache/scratch/kb-mr-20261119/or_models.json"
TABLES = Path("data/tables/models.jsonl")
BENCH = Path("data/tables/benchmarks.jsonl")
NOTES = Path("data/tables/model_notes.jsonl")
NULLS = Path("data/null_reasons.jsonl")
PROVIDER = "openrouter"

LANES = [
    "openai/gpt-4.1",
    "openai/gpt-4.1:batch",
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1-mini:batch",
    "openai/gpt-4.1-nano",
    "openai/gpt-4.1-nano:batch",
]
BATCH_LANES = [m for m in LANES if m.endswith(":batch")]

AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}

FEED = "carrier:openrouter /api/v1/models read 2026-11-19 (480 ids)"
MD = "models.dev api.json read 2026-11-19 (openrouter provider, 383 ids)"

# The carrier publishes the same architecture for a :batch id as for its
# synchronous twin — same model, different serving tier. thinking is FALSE:
# gpt-4.1 is not a reasoning model on either machine source.
BATCH_CAPS = {
    "vision": True,
    "thinking": False,
    "cap_image_generation": False,
    "cap_embedding": False,
    "cap_audio": False,
    "cap_video": False,
}

# gpt-4.1-nano cache-read: row 0.025 vs both sources 0.03.
PRICE_FIX = {
    "openai/gpt-4.1-nano": {"public_cache_read_per_m": 0.03},
}

PRICE_EVIDENCE = {
    "openai/gpt-4.1": (
        "normalized:payg-sticker | " + FEED + " prices this id 2.0/8.0/cache 0.5 "
        "(web_search 0.01 per call) and " + MD + " agrees to the cent; 1,047,576 "
        "ctx / 32,768 max completion / text+image+file->text on both; release "
        "2025-04-14, knowledge 2024-04"
    ),
    "openai/gpt-4.1:batch": (
        "normalized:payg-sticker | " + FEED + " prices this id 1.0/4.0/cache 0.25 "
        "(exactly half the synchronous sticker, the batch convention) with the "
        "same 1,047,576 ctx / 32,768 max completion / text+image+file->text "
        "architecture; models.dev publishes no :batch SKU, so the carrier is the "
        "only machine source for it"
    ),
    "openai/gpt-4.1-mini": (
        "normalized:payg-sticker | " + FEED + " prices this id 0.4/1.6/cache 0.1 "
        "(web_search 0.01 per call) and " + MD + " agrees to the cent; 1,047,576 "
        "ctx / 32,768 max completion / text+image+file->text on both; release "
        "2025-04-14, knowledge 2024-04"
    ),
    "openai/gpt-4.1-mini:batch": (
        "normalized:payg-sticker | " + FEED + " prices this id 0.2/0.8/cache 0.05 "
        "(exactly half the synchronous sticker, the batch convention) with the "
        "same 1,047,576 ctx / 32,768 max completion / text+image+file->text "
        "architecture; models.dev publishes no :batch SKU, so the carrier is the "
        "only machine source for it"
    ),
    "openai/gpt-4.1-nano": (
        "normalized:payg-sticker | " + FEED + " prices this id 0.1/0.4/cache 0.03 "
        "(web_search 0.01 per call) and " + MD + " agrees to the cent; 1,047,576 "
        "ctx / 942,818 max completion / text+image+file->text on both; release "
        "2025-04-14, knowledge 2024-04. CACHE-READ CORRECTED 0.025 -> 0.03: the "
        "row carried 25% of the input sticker (the family's old ratio) while both "
        "machine sources publish 30%"
    ),
    "openai/gpt-4.1-nano:batch": (
        "normalized:payg-sticker | " + FEED + " prices this id 0.05/0.2/cache "
        "0.0125 (exactly half the synchronous input/output sticker) with the same "
        "1,047,576 ctx / 32,768 max completion / text+image+file->text "
        "architecture; models.dev publishes no :batch SKU, so the carrier is the "
        "only machine source for it. FEED INCONSISTENCY RECORDED, NOT SMOOTHED: "
        "the carrier's batch cache-read (0.0125) is 41.7% of the synchronous "
        "cache-read (0.03), not the 50% the input/output halving implies — the "
        "batch cache line is derived from the retired 0.025 sync rate"
    ),
}

PROVENANCE = {
    "openai/gpt-4.1": (
        "market cross-check 2026-11-19: VERIFIED CLEAN on price and spec — no "
        "field change beyond price_evidence. " + FEED + " and " + MD + " agree "
        "exactly: 2.0/8.0/cache 0.5, 1,047,576 ctx, 32,768 max completion, "
        "text+image+file->text (vision true, cap_video false, cap_audio false), "
        "reasoning false (thinking false), release 2025-04-14, knowledge "
        "2024-04. MARKET PROVENANCE: the 6 design_arena cells were re-ingested "
        "from the 2026-11-19 feed with full provenance (percentile in score, raw "
        "ELO + rank + arena population n=186 in source_ref) and every percentile "
        "MOVED because the 2026-11-17 rows were normalized against the category "
        "entry count instead of the arena population (card "
        "t_market_pct_normalization). The feed publishes no artificial_analysis "
        "block for this id, so no AA cell exists to re-ingest. EVIDENCE CLASS: "
        "market composites only; no n>=3 measurement, no large-bench source. "
        "Lane is retired (valid_to 2026-09-26, replaced_by "
        "openrouter/openai/gpt-5.5) and its lifecycle record is left intact."
    ),
    "openai/gpt-4.1:batch": (
        "market cross-check 2026-11-19: SIX SPEC FIELDS FILLED — vision, "
        "thinking, cap_image_generation, cap_embedding, cap_audio and cap_video "
        "were NULL with `no-sample-yet` deferral stamps; " + FEED + " publishes "
        "this exact id's architecture (text+image+file->text, output text only, "
        "no reasoning in supported_parameters), which settles all four "
        "modality-settled families, so they are filled and the deferral stamps "
        "retired. thinking is FALSE, not true: gpt-4.1 is not a reasoning model "
        "on either machine source. PRICE verified clean: 1.0/4.0/cache 0.25, "
        "exactly half the synchronous sticker. MARKET PROVENANCE: 6 "
        "design_arena cells re-ingested with full provenance (raw ELO + rank + "
        "arena population n=186), every percentile moved by the population "
        "normalization fix; the feed publishes no AA block, so no AA cell "
        "exists. EVIDENCE CLASS: market composites only; no n>=3 measurement. "
        "This lane is LIVE (valid_to NULL) while its synchronous twin is "
        "retired, so it is the only routable member of the pair."
    ),
    "openai/gpt-4.1-mini": (
        "market cross-check 2026-11-19: VERIFIED CLEAN on price and spec — no "
        "field change beyond price_evidence. " + FEED + " and " + MD + " agree "
        "exactly: 0.4/1.6/cache 0.1, 1,047,576 ctx, 32,768 max completion, "
        "text+image+file->text (vision true, cap_video false, cap_audio false), "
        "reasoning false (thinking false), release 2025-04-14, knowledge "
        "2024-04. MARKET PROVENANCE: the 6 design_arena cells were re-ingested "
        "from the 2026-11-19 feed with full provenance (raw ELO + rank + arena "
        "population n=186) and every percentile moved by the population "
        "normalization fix. The feed publishes no artificial_analysis block, so "
        "the artificial_analysis_coding cell (0.202) is FROZEN 2026-11-17 "
        "evidence stamped with origin index + raw score + not-republished. "
        "EVIDENCE CLASS: market composites only; no n>=3 measurement. Lane is "
        "retired (valid_to 2026-09-26, replaced_by "
        "openrouter/openai/gpt-5.4-mini) and its lifecycle record is left intact."
    ),
    "openai/gpt-4.1-mini:batch": (
        "market cross-check 2026-11-19: SIX SPEC FIELDS FILLED — vision, "
        "thinking, cap_image_generation, cap_embedding, cap_audio and cap_video "
        "were NULL with `no-sample-yet` deferral stamps; " + FEED + " publishes "
        "this exact id's architecture (text+image+file->text, output text only, "
        "no reasoning in supported_parameters), which settles all four "
        "modality-settled families. thinking is FALSE, not true: gpt-4.1 is not "
        "a reasoning model. PRICE verified clean: 0.2/0.8/cache 0.05, exactly "
        "half the synchronous sticker. MARKET PROVENANCE: 6 design_arena cells "
        "re-ingested with full provenance (raw ELO + rank + arena population "
        "n=186); the artificial_analysis_coding cell (0.202) is FROZEN "
        "2026-11-17 evidence stamped not-republished because the feed publishes "
        "no AA block. EVIDENCE CLASS: market composites only; no n>=3 "
        "measurement. This lane is LIVE (valid_to NULL) while its synchronous "
        "twin is retired."
    ),
    "openai/gpt-4.1-nano": (
        "market cross-check 2026-11-19: ONE PRICE CORRECTED — "
        "public_cache_read_per_m 0.025 -> 0.03. Both machine sources publish "
        "0.03 (carrier input_cache_read 0.00000003, models.dev cost.cache_read "
        "0.03); 0.025 was 25% of the input sticker, the family's old cache "
        "ratio, not this id's published rate. Input/output 0.1/0.4, 1,047,576 "
        "ctx, 942,818 max completion, text+image+file->text (vision true, "
        "cap_video false, cap_audio false), reasoning false (thinking false), "
        "release 2025-04-14, knowledge 2024-04 all verified clean. MARKET "
        "PROVENANCE: the 6 design_arena cells were re-ingested from the "
        "2026-11-19 feed with full provenance (raw ELO + rank + arena population "
        "n=186) and every percentile moved by the population normalization fix; "
        "the artificial_analysis_coding cell (0.111) is FROZEN 2026-11-17 "
        "evidence stamped not-republished. EVIDENCE CLASS: market composites "
        "only; no n>=3 measurement. Lane is retired (valid_to 2026-09-26, "
        "replaced_by openrouter/openai/gpt-5.4-nano)."
    ),
    "openai/gpt-4.1-nano:batch": (
        "market cross-check 2026-11-19: SIX SPEC FIELDS FILLED — vision, "
        "thinking, cap_image_generation, cap_embedding, cap_audio and cap_video "
        "were NULL with `no-sample-yet` deferral stamps; " + FEED + " publishes "
        "this exact id's architecture (text+image+file->text, output text only, "
        "no reasoning in supported_parameters). thinking is FALSE, not true: "
        "gpt-4.1 is not a reasoning model. PRICE verified clean: 0.05/0.2/cache "
        "0.0125, exactly half the synchronous input/output sticker. FEED "
        "INCONSISTENCY RECORDED, NOT SMOOTHED: the carrier's batch cache-read "
        "(0.0125) is 41.7% of the synchronous cache-read (0.03), not the 50% "
        "the input/output halving implies — the batch cache line is derived from "
        "the retired 0.025 sync rate, so a future pass must not 'fix' it to "
        "0.015 without a fresh carrier read. MARKET PROVENANCE: 6 design_arena "
        "cells re-ingested with full provenance (raw ELO + rank + arena "
        "population n=186); the artificial_analysis_coding cell (0.111) is "
        "FROZEN 2026-11-17 evidence stamped not-republished. EVIDENCE CLASS: "
        "market composites only; no n>=3 measurement. This lane is LIVE "
        "(valid_to NULL) while its synchronous twin is retired."
    ),
}

CAP_NULL_REASON = (
    "market cross-check 2026-11-19: cap_web_search and cap_computer_use are "
    "PRODUCT features with no modality evidence class - scripts/capability_vocab.py "
    "resolves them only from explicit name markers, so a published architecture "
    "string can never settle them. NOTE: the carrier DOES publish a web_search "
    "price line (0.01 per call) on all six gpt-4.1 ids, which is real evidence the "
    "resolver cannot consume - a resolver gap on the record, not a data gap. The "
    "2026-11-18c wave ruled that a carrier price line is not a capability "
    "declaration; this wave follows that ruling rather than splitting the table's "
    "convention."
)

# Stamps retired because the field is no longer NULL (carrier evidence landed).
SUPERSEDED_FIELDS = {
    "cap_image_generation",
    "cap_embedding",
    "cap_audio",
    "cap_video",
}


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

    # Insert the fresh rows in model order, keeping the file's model grouping.
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


def fill_spec():
    lines = TABLES.read_text(encoding="utf-8").splitlines(keepends=True)
    out, touched = [], 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if r.get("provider") != PROVIDER or r.get("model") not in LANES:
            out.append(line)
            continue
        model = r["model"]
        changed = False
        if model in BATCH_LANES:
            for field, value in BATCH_CAPS.items():
                if r.get(field) is None:
                    r[field] = value
                    changed = True
        for field, value in PRICE_FIX.get(model, {}).items():
            if r.get(field) != value:
                r[field] = value
                changed = True
        r["price_evidence"] = PRICE_EVIDENCE[model]
        r["note"] = PROVENANCE[model]
        changed = True
        if changed:
            touched += 1
            out.append(json.dumps(r, ensure_ascii=False) + "\n")
            continue
        out.append(line)
    if touched != len(LANES):
        print(f"REFUSED: touched {touched} rows, expected {len(LANES)}", file=sys.stderr)
        return 2
    TABLES.write_text("".join(out), encoding="utf-8")
    print(f"spec rows updated: {touched}")
    return 0


def add_notes():
    existing = set()
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    added = 0
    with NOTES.open("a", encoding="utf-8") as f:
        for model in LANES:
            if (PROVIDER, model, DATE) in existing:
                print(f"note already present: {model}")
                continue
            f.write(
                json.dumps(
                    {
                        "provider": PROVIDER,
                        "model": model,
                        "note": PROVENANCE[model],
                        "source": "openrouter /api/v1/models (480 ids) + "
                        "models.dev api.json (openrouter provider, 383 ids), "
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
        model = r.get("model")
        field = r.get("field")
        if model in BATCH_LANES and field in SUPERSEDED_FIELDS:
            dropped += 1
            continue
        if model in LANES and field in ("cap_web_search", "cap_computer_use"):
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
        for model in LANES:
            for field in ("cap_web_search", "cap_computer_use"):
                if (PROVIDER, model, field) in have:
                    continue
                f.write(
                    json.dumps(
                        {
                            "provider": PROVIDER,
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

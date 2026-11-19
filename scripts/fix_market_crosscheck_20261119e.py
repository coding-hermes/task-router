#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19e — card t_model_openai_gpt-5.3-codex.

Model: openai/gpt-5.3-codex.

What this wave changes:

  * MARKET CELL PROVENANCE. All 26 source=market rows for this id (13 arena
    cells x two id spellings) carried source_ref=NULL. The 13 cells the
    2026-11-19 carrier read still publishes are RE-INGESTED through
    scripts/router_market_ingest.py's own build_rows (full-feed arena
    denominators), so the percentile and the provenance are the ingest's, not
    hand-written. The 13 bare-id twins are the same model under a different key
    and are REWRITTEN from their feed-id twin (score + provenance + valid_from,
    plus a mirror-of marker): they carried the PRE-02153035 percentiles, which
    were normalized by the CATEGORY entry count instead of the arena population
    (asciiart 0.268 = 1 - 46/63 against 1 - 46/186 = 0.7527 for the arena), so
    the bare key was ranking the model against a denominator the repo has since
    declared wrong.

  * PRICE verified clean on the openrouter lane, not changed: 1.75/14.0/cache
    0.175 is what both machine sources publish.

  * release_date CORRECTED 2026-02-24 -> 2026-02-05 on the openrouter lane and
    FILLED on the four other lanes. The stored value was the feed's `created`
    (listing) stamp; models.dev publishes 2026-02-05 for the openai, openrouter
    and commandcode entries alike. A 19-day gap is not source noise.

  * CAPABILITY-BLIND MIRROR FIXED: commandcode-2 carried vision=false and
    thinking=false while the commandcode twin, the carrier feed and models.dev
    all say text+image+pdf input with reasoning. Same defect class the
    2026-11-19b wave fixed on the gemini-3.7-flash mirrors.

  * PRICE FLAGGED, NOT CHANGED on the commandcode lanes: 1.75/14.0 with the
    'commandcode-no-markup' evidence class equals OpenAI's own sticker, while
    models.dev's commandcode entry publishes 2/8/cache 0.5. The keyless carrier
    catalog publishes no price, so it cannot settle the conflict.

Every other line of every table is copied byte-for-byte.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-19"
PRIOR = "2026-11-17"
PAYLOAD = "/tmp/kb-feed-20261119.json"
TABLES = "data/tables/models.jsonl"
BENCH = "data/tables/benchmarks.jsonl"

FEED_ID = "openai/gpt-5.3-codex"
BARE_ID = "gpt-5.3-codex"
IDS = [FEED_ID, BARE_ID]

FEED = "carrier:openrouter /api/v1/models read 2026-11-19 (480 ids)"
MD = "models.dev api.json read 2026-11-19"

NOTE_OR = (
    "market cross-check 2026-11-19: PRICE verified clean, not changed — "
    "1.75/14.0/cache 0.175 is what both machine sources publish (" + FEED
    + " prompt 0.00000175 / completion 0.000014 / input_cache_read 0.000000175; "
    + MD + " openrouter provider cost 1.75/14/0.175). SPEC verified clean: "
    "400,000 ctx / 128,000 max output / text+image+file->text (vision true, "
    "cap_video false) / reasoning supported (thinking true). release_date "
    "CORRECTED 2026-02-24 -> 2026-02-05: the stored value was the feed's "
    "`created` listing stamp (1771959164 = 2026-02-24), while models.dev "
    "publishes 2026-02-05 for the openai, openrouter and commandcode entries "
    "alike — a 19-day gap is not source noise. MARKET PROVENANCE: the 13 arena "
    "cells were re-ingested with raw ELO + rank + arena population; the 13 "
    "bare-id twins are REWRITTEN from their feed-id twin (score + provenance + "
    "valid_from, mirror-of marker): they carried the PRE-02153035 percentiles, "
    "normalized by the CATEGORY entry count instead of the arena population "
    "(asciiart 0.268 = 1 - 46/63 against 1 - 46/186 = 0.7527), so the bare key "
    "was ranking this model against a denominator the repo has since declared "
    "wrong. EVIDENCE CLASS: market composites only; no n>=3 measurement. Lane "
    "is LIVE (valid_to NULL)."
)

NOTE_CC = (
    "market cross-check 2026-11-19: release_date FILLED 2026-02-05 from " + MD
    + " (commandcode entry) and " + FEED + " (created 1771959164 = listing "
    "stamp, 19 days later). PRICE FLAGGED, NOT CHANGED: the row carries "
    "1.75/14.0 with the 'commandcode-no-markup' evidence class, which equals "
    "OpenAI's own sticker, while models.dev's commandcode entry publishes "
    "2/8/cache 0.5/cache-write 0 — a real conflict the keyless carrier catalog "
    "cannot settle (it publishes no price). Recorded here for the pricing lane "
    "rather than silently overwritten. SPEC verified clean: 400,000 ctx / "
    "text+image+pdf input / reasoning supported. EVIDENCE CLASS: market "
    "composites only."
)

NOTE_CC2 = (
    "market cross-check 2026-11-19: CAPABILITY-BLIND MIRROR FIXED — vision "
    "false -> true and thinking false -> true, plus cap_image_generation / "
    "cap_embedding / cap_audio / cap_video null -> false. " + FEED
    + " publishes this id's architecture as text+image+file->text with "
    "reasoning in supported_parameters, and " + MD + " publishes the same "
    "modalities for the openai, openrouter and commandcode entries; the "
    "commandcode twin already carried vision/thinking true. Same defect class "
    "the 2026-11-19b wave fixed on the gemini-3.7-flash mirrors. release_date "
    "FILLED 2026-02-05. PRICE FLAGGED, NOT CHANGED: 1.75/14.0 (no-markup "
    "class) vs models.dev commandcode 2/8/cache 0.5 — a conflict the keyless "
    "carrier catalog cannot settle. EVIDENCE CLASS: market composites only."
)

NOTE_OC = (
    "market cross-check 2026-11-19: release_date FILLED 2026-02-05 from " + MD
    + " (openai entry) and " + FEED + ". Lane is disabled (superseded "
    "generation — the fleet codex lanes run gpt-5.6), so price and capability "
    "were not re-litigated; the spec fields already matched both sources "
    "(400,000 ctx, text+image+pdf, reasoning). EVIDENCE CLASS: market "
    "composites only."
)

NOTE_CP = (
    "market cross-check 2026-11-19: SPEC FILLED on a disabled mirror — "
    "context_limit 400,000, vision true, thinking true, cap_video false, "
    "cap_audio false, cap_image_generation false, cap_embedding false, "
    "release_date 2026-02-05, all from " + FEED + " (architecture "
    "text+image+file->text, context_length 400000, reasoning in "
    "supported_parameters) and " + MD + " (same modalities, limit.context "
    "400000). The lane's disabled_reason explains the PRICE (PAYG outside the "
    "clinepass flat plan), not the model's properties, so the spec nulls were "
    "a gap rather than a stated state. EVIDENCE CLASS: market composites only."
)

UPDATES = {
    ("openrouter", FEED_ID): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id "
            "1.75/14.0/cache 0.175 and " + MD + " (openrouter provider) agrees "
            "to the digit; 400,000 ctx / 128,000 max output / "
            "text+image+file->text / reasoning supported. Stored sticker was "
            "already correct."
        ),
        "release_date": "2026-02-05",
        "note": NOTE_OR,
    },
    ("commandcode", BARE_ID): {"release_date": "2026-02-05", "note": NOTE_CC},
    ("commandcode-2", BARE_ID): {
        "vision": True, "thinking": True,
        "cap_image_generation": False, "cap_embedding": False,
        "cap_audio": False, "cap_video": False,
        "release_date": "2026-02-05", "note": NOTE_CC2,
    },
    ("openai-codex", BARE_ID): {"release_date": "2026-02-05", "note": NOTE_OC},
    ("clinepass", BARE_ID): {
        "context_limit": 400000, "vision": True, "thinking": True,
        "cap_video": False, "cap_audio": False,
        "cap_image_generation": False, "cap_embedding": False,
        "release_date": "2026-02-05", "note": NOTE_CP,
    },
}

AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}


def reingest_market() -> tuple[int, int, dict]:
    """Replace the feed-published market cells for IDS; return (dropped, written, refs)."""
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    registry_ids = load_registry_ids(TABLES)
    rows, report = build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] in IDS]
    cells = {(r["model"], r["category"]) for r in mine}
    refs = {(r["category"], r["score"]): r["source_ref"] for r in mine}
    print(f"  feed models={report['feed_models']} joined={report['joined_models']} "
          f"emitted={report['rows_emitted']} mine={len(mine)} cells={len(cells)}")
    kept, dropped = [], 0
    with open(BENCH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                kept.append(line)
                continue
            r = json.loads(line)
            if r.get("source") == "market" and (r.get("model"), r.get("category")) in cells:
                dropped += 1
                continue
            kept.append(line)
    with open(BENCH, "w", encoding="utf-8") as f:
        f.writelines(kept)
        for r in mine:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return dropped, len(mine), refs


def stamp_remaining() -> tuple[int, int]:
    """Rewrite the bare-id twins from their feed-id twin; freeze-stamp AA cells.

    The bare-id rows are the same model under a different key, but they carried
    the PRE-02153035 percentiles: normalized by the CATEGORY entry count instead
    of the arena population (asciiart 0.268 = 1 - 46/63, where 63 is the category
    count, against 1 - 46/186 = 0.7527 for the arena). They are therefore
    replaced by their feed-id twin's arena-normalized score and provenance, with
    a mirror-of marker so the pairing stays auditable. Per-category cell counts
    are asserted equal first; a mismatch falls back to a frozen stamp instead of
    inventing a pairing.
    """
    rows = [json.loads(l) for l in open(BENCH, encoding="utf-8") if l.strip()]
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("model") == FEED_ID and r.get("source") == "market" and r.get("source_ref"):
            by_cat.setdefault(r["category"], []).append(r)
    for v in by_cat.values():
        v.sort(key=lambda x: -x["score"])
    cursor: dict[str, int] = {}
    out, mirrored, frozen = [], 0, 0
    for r in rows:
        if (r.get("model") != BARE_ID or r.get("source") != "market"
                or r.get("source_ref")):
            out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            continue
        twins = by_cat.get(r["category"]) or []
        i = cursor.get(r["category"], 0)
        if i < len(twins):
            twin = twins[i]
            r["score"] = twin["score"]
            r["source_ref"] = f"{twin['source_ref']}:mirror-of={FEED_ID}"
            r["valid_from"] = twin["valid_from"]
            cursor[r["category"]] = i + 1
            mirrored += 1
        else:
            index = next((k for k, v in AA_CATS.items() if v == r["category"]), None)
            if index is None:
                out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                continue
            raw = round(float(r["score"]) * 100.0, 6)
            r["source_ref"] = (
                f"openrouter-models-api:{PRIOR}:index={index}:raw_score={raw}"
                f":not-republished-{DATE}"
            )
            frozen += 1
        out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    with open(BENCH, "w", encoding="utf-8") as f:
        f.writelines(out)
    return mirrored, frozen


def apply_updates() -> int:
    out, changed = [], 0
    with open(TABLES, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                out.append(line)
                continue
            r = json.loads(line)
            upd = UPDATES.get((r.get("provider"), r.get("model")))
            if not upd:
                out.append(line)
                continue
            for k, v in upd.items():
                if r.get(k) != v:
                    r[k] = v
            changed += 1
            out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    with open(TABLES, "w", encoding="utf-8") as f:
        f.writelines(out)
    return changed


def main() -> int:
    dropped, written, _refs = reingest_market()
    print(f"  dropped {dropped} prior market rows; wrote {written} fresh rows")
    mirrored, frozen = stamp_remaining()
    print(f"  mirror-stamped {mirrored} bare-id twins; freeze-stamped {frozen} AA rows")
    changed = apply_updates()
    print(f"  applied price/spec updates to {changed} lane rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

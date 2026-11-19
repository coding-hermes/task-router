#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19d — card t_model_qwen_qwen3.5-122b-a10b.

Model: qwen/qwen3.5-122b-a10b (Qwen3.5 122B-A10B, hybrid linear-attention MoE,
122B total / 10B active, native vision-language).

What this wave changes:

  * MARKET CELL PROVENANCE. The four source=market rows for this id (two id
    spellings x coding/agentic) carried source_ref=NULL — a 0-1 percentile that
    could not be traced to the arena, the index, the raw score or the date it
    came from. The 2026-11-19 carrier read publishes NO artificial_analysis
    coding/agentic block for this id (all three AA indexes are null) and no
    design_arena block at all, so there is nothing to re-ingest: the cells are
    FROZEN 2026-11-17 evidence and are stamped with the provenance that is
    actually known (origin date, index, raw score, not-republished marker).

  * PRICE verified clean, not changed: 0.26/2.08 on the openrouter lane is what
    both machine sources publish. No cache rate is published by either source,
    so public_cache_read_per_m stays NULL (not published, never 0).

  * SPEC FILL on the clinepass mirror: context_limit / vision / thinking /
    cap_video / cap_audio / cap_image_generation / cap_embedding were all NULL
    on a lane whose disabled_reason explains the PRICE, not the model's
    properties. Filled from the carrier feed + models.dev, which agree.

  * release_date LEFT ALONE at 2026-02-25: models.dev publishes 2026-02-23 and
    llm-stats 2026-02-24, a 1-2 day spread that is source noise, not a defect
    (the same rule the 2026-11-19c wave applied to qwen3.5-35b-a3b).

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

IDS = ["qwen/qwen3.5-122b-a10b", "qwen3.5-122b-a10b"]

FEED = "carrier:openrouter /api/v1/models read 2026-11-19 (480 ids)"
MD = "models.dev api.json read 2026-11-19 (openrouter provider)"

NOTE_OR = (
    "market cross-check 2026-11-19: PRICE verified clean, not changed — "
    "0.26/2.08 is what both machine sources publish (" + FEED + " prompt "
    "0.00000026 / completion 0.00000208; " + MD + " cost 0.26/2.08). Neither "
    "source publishes a cache rate for this id, so public_cache_read_per_m "
    "stays NULL (not published, never 0). SPEC verified clean: 262,144 ctx / "
    "65,536 max completion / text+image+video->text (vision true, cap_video "
    "true) / reasoning supported but not mandatory (thinking true). MARKET "
    "PROVENANCE: the four source=market rows for this id carried "
    "source_ref=NULL. The 2026-11-19 feed publishes NO artificial_analysis "
    "coding/agentic block (all three AA indexes null) and NO design_arena "
    "block for this id, so there is nothing to re-ingest — the coding 0.457 / "
    "agentic 0.076 cells are FROZEN 2026-11-17 evidence, stamped with origin "
    "date + index + raw score + not-republished marker. COMMUNITY CROSS-CHECK: "
    "artificialanalysis.ai/models/qwen3-5-122b-a10b reports Intelligence Index "
    "16 for the reasoning variant (read 2026-11-19) — a different index from "
    "the frozen coding/agentic cells, so it is recorded here rather than "
    "written into them. release_date LEFT ALONE at 2026-02-25: models.dev "
    "publishes 2026-02-23 and llm-stats 2026-02-24, a 1-2 day spread that is "
    "source noise, not a defect. EVIDENCE CLASS: market composites only; no "
    "n>=3 measurement. Lane is LIVE (valid_to NULL)."
)

NOTE_CP = (
    "market cross-check 2026-11-19: SPEC FILLED on a disabled mirror — "
    "context_limit 262,144, vision true, thinking true, cap_video true, "
    "cap_audio false, cap_image_generation false, cap_embedding false, all "
    "from " + FEED + " (architecture text+image+video->text, context_length "
    "262144, supported_parameters include reasoning) and " + MD + " (same "
    "modalities, reasoning true, limit.context 262144). The lane's "
    "disabled_reason explains the PRICE (PAYG outside the clinepass flat "
    "plan), not the model's properties, so the spec nulls were a gap rather "
    "than a stated state. Price untouched: the lane is disabled and carries no "
    "sticker. EVIDENCE CLASS: market composites only."
)

# (provider, model) -> {column: new value}
UPDATES = {
    ("openrouter", "qwen/qwen3.5-122b-a10b"): {
        "price_evidence": (
            "normalized:payg-sticker | " + FEED + " prices this id "
            "0.26/2.08 with no cache rate and " + MD + " agrees to the digit; "
            "262,144 ctx / 65,536 max completion / text+image+video->text / "
            "reasoning supported (not mandatory). Stored sticker was already "
            "correct."
        ),
        "note": NOTE_OR,
    },
    ("clinepass", "qwen3.5-122b-a10b"): {
        "context_limit": 262144,
        "vision": True,
        "thinking": True,
        "cap_video": True,
        "cap_audio": False,
        "cap_image_generation": False,
        "cap_embedding": False,
        "note": NOTE_CP,
    },
}

AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}


def reingest_market() -> tuple[int, int]:
    """Replace the feed-published market cells for IDS; return (dropped, written)."""
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    registry_ids = load_registry_ids(TABLES)
    rows, report = build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] in IDS]
    cells = {(r["model"], r["category"]) for r in mine}
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
    return dropped, len(mine)


def freeze_stamp() -> int:
    """Stamp provenance on market rows the feed no longer publishes."""
    out, stamped = [], 0
    with open(BENCH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                out.append(line)
                continue
            r = json.loads(line)
            if (r.get("model") not in IDS or r.get("source") != "market"
                    or r.get("source_ref")):
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
    with open(BENCH, "w", encoding="utf-8") as f:
        f.writelines(out)
    return stamped


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
    dropped, written = reingest_market()
    print(f"  dropped {dropped} prior market rows; wrote {written} fresh rows")
    stamped = freeze_stamp()
    print(f"  freeze-stamped {stamped} non-republished AA rows")
    changed = apply_updates()
    print(f"  applied price/spec updates to {changed} lane rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

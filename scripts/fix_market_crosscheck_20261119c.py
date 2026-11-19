#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19c — six cards, provenance + two price drifts.

Cards: t_model_qwen_qwen3.8-2.4t-a95b, t_model_x-ai_grok-4.6,
t_model_x-ai_grok-4.20, t_model_z-ai_glm-5-turbo, t_model_qwen_qwen3.5-9b,
t_model_qwen_qwen3.5-35b-a3b.

What this wave changes:

  * MARKET CELL PROVENANCE. Every one of the six ids carried source=market rows
    with source_ref=NULL — a 0-1 percentile that cannot be traced back to the
    arena, the raw ELO, the rank or the population it was normalized from. The
    design_arena and artificial_analysis cells the 2026-11-19 feed still
    publishes are RE-INGESTED through scripts/router_market_ingest.py's own
    build_rows (full-feed arena denominators, 480 ids), so the percentiles and
    the provenance are the ingest's, not hand-written. Cells the feed no longer
    emits are FROZEN and stamped with the provenance that is actually known
    (origin date, index, raw score, not-republished marker) rather than dropped
    or left blank.

  * TWO PRICE DRIFTS on openrouter lanes:
      - qwen/qwen3.5-35b-a3b  0.3125/1.25/cache 0.15625 -> 0.14/1.0/cache 0.05
        (live carrier feed AND models.dev's openrouter provider agree to the
        digit; the stored sticker was 2.2x over on input)
      - qwen/qwen3.5-9b       0.1/0.15/cache NULL -> 0.17/0.25/cache 0.153
        (live carrier feed; models.dev's openrouter mirror still shows the
        older 0.142/0.21/cache 0.128 — the carrier's own price list is the
        lane's price, so the feed wins and the divergence is recorded in
        price_evidence rather than silently averaged)
    public_price re-blended 0.96*in + 0.04*out, the convention every openrouter
    row follows.

  * ONE release_date correction: qwen/qwen3.5-9b 2026-03-10 -> 2026-02-23.
    The stored value was the feed's `created` (listing) date; models.dev
    publishes the model's release date, which is the property this column
    means. The two-day gaps on glm-5-turbo (03-15 vs 03-16) and
    qwen3.5-35b-a3b (02-25 vs 02-23) are left alone: a 1-2 day disagreement
    between two sources is source noise, not a defect.

  * NOT CHANGED, deliberately: the valid_to stamps on grok-4.6, grok-4.20,
    glm-5-turbo and qwen3.5-35b-a3b (2026-09-26/27) while the carrier feed
    still publishes all four. That is a lifecycle question, not a market
    evidence question, and it needs its own decision — recorded here so the
    contradiction is not silent.

Every other line of every table is copied byte-for-byte.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-19"
PRIOR = "2026-11-17"
PAYLOAD = "/home/kara/.hermes/cache/scratch/kb-mr-20261119c/or_models.json"
TABLES = "data/tables/models.jsonl"
BENCH = "data/tables/benchmarks.jsonl"

IDS = [
    "qwen/qwen3.8-2.4t-a95b",
    "x-ai/grok-4.6",
    "x-ai/grok-4.20",
    "z-ai/glm-5-turbo",
    "qwen/qwen3.5-9b",
    "qwen/qwen3.5-35b-a3b",
]

FEED = "carrier:openrouter /api/v1/models read 2026-11-19 (480 ids)"
MD = "models.dev api.json read 2026-11-19 (openrouter provider)"

# (provider, model) -> {column: new value}
UPDATES = {
    # ---- qwen3.5-35b-a3b: both machine sources agree, stored was 2.2x over.
    ("openrouter", "qwen/qwen3.5-35b-a3b"): {
        "normalized_price": 0.14, "public_in_per_m": 0.14,
        "public_out_per_m": 1.0, "public_cache_read_per_m": 0.05,
        "public_price": 0.1744,
        "price_evidence": (
            f"{FEED} | {MD} — both publish 0.14/1.0/cache 0.05; the stored "
            "0.3125/1.25/cache 0.15625 matched neither (2.2x over on input)"
        ),
    },
    # ---- qwen3.5-9b: carrier feed is the lane's own price list; models.dev
    # mirror lags. Divergence recorded, not averaged.
    ("openrouter", "qwen/qwen3.5-9b"): {
        "normalized_price": 0.17, "public_in_per_m": 0.17,
        "public_out_per_m": 0.25, "public_cache_read_per_m": 0.153,
        "public_price": 0.1732,
        "release_date": "2026-02-23",
        "price_evidence": (
            f"{FEED} — 0.17/0.25/cache 0.153; the stored 0.1/0.15 with no "
            f"cache rate matched no source. {MD} still shows the older "
            "0.142/0.21/cache 0.128: the carrier's own price list is what this "
            "lane charges, so the feed wins and the mirror divergence is "
            "recorded here rather than averaged"
        ),
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

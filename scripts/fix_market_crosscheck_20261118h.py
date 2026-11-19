#!/usr/bin/env python3
"""Market cross-check for openrouter/qwen/qwen3.8-27b (card t_model_qwen_qwen3.8-27b).

Two jobs, both idempotent:

1. PRICE. The row carried 0.42 / 3.0 / cache 0.085 — the Novita endpoint's
   sticker, not the lane's advertised price. OpenRouter advertises this id at
   0.06 / 2.2 / cache 0.059 (feed top-level pricing, read 2026-11-18) and
   models.dev's openrouter entry publishes the same triple, so the row was 7x
   too expensive on input and the lane ranked as if it were a premium model.
   The registry convention is the feed's top-level sticker: 404 of 462 priced
   openrouter lanes match it exactly.

   The id is served by 25 endpoints spanning 0.035-0.99 $/M input, so the
   advertised minimum moves as endpoints churn (the 2026-11-17 read advertised
   0.15/1.875, DeepInfra's endpoint). That volatility is recorded in the note
   rather than smoothed away.

2. MARKET CELLS. 3 source=market AA rows carried source_ref=NULL. The
   2026-11-18 feed republishes intelligence_index 20.1 but publishes
   coding_index and agentic_index as null, so intelligence is re-ingested
   through scripts/router_market_ingest.py's own build_rows and the other two
   are kept as FROZEN 2026-11-17 evidence with origin index + raw score +
   not-republished stamped on them.

Spec fields are CONFIRMED, not changed: context 1,024,000 (feed + models.dev;
24 of 25 endpoints serve 262,144 and only the 1M endpoints justify the ceiling,
which is recorded in the note), text+image+video->text keeps vision=true and
cap_video=true, reasoning in supported_parameters + models.dev reasoning=true
keeps thinking=true, release_date 2026-08-14 agrees.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-18"
PRIOR = "2026-11-17"
PAYLOAD = "/home/kara/.hermes/cache/scratch/kb-mr/or_models.json"
TABLES = Path("data/tables/models.jsonl")
BENCH = Path("data/tables/benchmarks.jsonl")
NOTES = Path("data/tables/model_notes.jsonl")
PROVIDER = "openrouter"
MODEL = "qwen/qwen3.8-27b"
STALE = {"public_in_per_m": 0.42, "public_out_per_m": 3.0,
         "public_cache_read_per_m": 0.085, "public_price": 0.5232,
         "normalized_price": 0.42}
FRESH = {"public_in_per_m": 0.06, "public_out_per_m": 2.2,
         "public_cache_read_per_m": 0.059, "public_price": 0.1456,
         "normalized_price": 0.06}
EVIDENCE = (
    "normalized:payg-sticker | market cross-check 2026-11-18: openrouter "
    "/api/v1/models top-level pricing and models.dev api.json (openrouter "
    "provider) both publish 0.06/2.2/cache 0.059; the prior 0.42/3.0/0.085 was "
    "the Novita endpoint's sticker, 7x the advertised input rate, so the lane "
    "ranked as a premium model. 25 endpoints span 0.035-0.99 $/M input and the "
    "advertised minimum moves with endpoint churn (2026-11-17 read: 0.15/1.875)"
)
NOTE = (
    "2026-11-18 market cross-check: PRICE CORRECTED 0.42/3.0/cache 0.085 -> "
    "0.06/2.2/cache 0.059. The old triple was the Novita endpoint's sticker; the "
    "lane's advertised price on both machine sources is the feed top-level "
    "minimum, which is the registry convention (404 of 462 priced openrouter "
    "lanes match it exactly). CAVEAT recorded rather than smoothed: this id is "
    "served by 25 endpoints spanning 0.035-0.99 $/M input, so the advertised "
    "minimum moves as endpoints churn — the 2026-11-17 read advertised "
    "0.15/1.875 (DeepInfra). Context 1,024,000 confirmed by feed and models.dev, "
    "but 24 of 25 endpoints serve 262,144 and only StreamLake/Novita/Alibaba/"
    "Nebius/GMICloud serve the 1M window, so the ceiling is the extended limit "
    "rather than the typical served window. text+image+video->text keeps "
    "vision=true and cap_video=true; reasoning in supported_parameters plus "
    "models.dev reasoning=true keeps thinking=true; release_date 2026-08-14 "
    "agrees. AA: intelligence_index 20.1 re-ingested with provenance; "
    "coding_index and agentic_index are null in the 2026-11-18 feed, so the "
    "0.681/0.458 cells are FROZEN 2026-11-17 evidence stamped not-republished. "
    "No design_arena entries exist for this id."
)
AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}


def fix_price():
    lines = TABLES.read_text(encoding="utf-8").splitlines(keepends=True)
    out, fixed = [], 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if r.get("provider") == PROVIDER and r.get("model") == MODEL:
            if all(r.get(k) == v for k, v in STALE.items()):
                r.update(FRESH)
                r["price_evidence"] = EVIDENCE
                fixed += 1
                out.append(json.dumps(r, ensure_ascii=False) + "\n")
                continue
            print(f"row not in the stale state, left alone: {r.get('public_in_per_m')}")
        out.append(line)
    if fixed:
        TABLES.write_text("".join(out), encoding="utf-8")
    print(f"price rows corrected: {fixed}")


def reingest_cells():
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    rows, report = build_rows(payload, load_registry_ids(str(TABLES)), DATE)
    mine = [r for r in rows if r["model"] == MODEL]
    fresh_cells = {r["category"] for r in mine}
    print(f"feed emitted={report['rows_emitted']} mine={len(mine)} cells={sorted(fresh_cells)}")

    out, dropped, stamped = [], 0, 0
    for line in BENCH.read_text(encoding="utf-8").splitlines(keepends=True):
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if r.get("model") != MODEL or r.get("source") != "market":
            out.append(line)
            continue
        if r["category"] in fresh_cells:
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

    # Insert fresh rows in model-sorted position, verbatim lines preserved.
    final, pending = [], sorted(mine, key=lambda r: r["model"])
    for line in out:
        if pending and line.strip():
            while pending and json.loads(line)["model"] > pending[0]["model"]:
                final.append(json.dumps(pending.pop(0), ensure_ascii=False, sort_keys=True) + "\n")
        final.append(line)
    for r in pending:
        final.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    BENCH.write_text("".join(final), encoding="utf-8")
    print(f"dropped {dropped} superseded market rows; wrote {len(mine)} fresh; stamped {stamped} frozen")


def add_note():
    existing = set()
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    if (PROVIDER, MODEL, DATE) in existing:
        print("note already present")
        return
    with NOTES.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"provider": PROVIDER, "model": MODEL, "note": NOTE,
                            "source": "openrouter /api/v1/models (480 ids) + "
                                      "models.dev api.json (openrouter provider), "
                                      "both read 2026-11-18",
                            "valid_from": DATE},
                           ensure_ascii=False, sort_keys=True) + "\n")
    print("note added")


def main():
    fix_price()
    reingest_cells()
    add_note()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

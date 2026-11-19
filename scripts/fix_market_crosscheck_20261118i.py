#!/usr/bin/env python3
"""Market cross-check for openrouter/google/gemini-3.7-flash:batch.

Card t_model_google_gemini-3.7-flash:batch. Three jobs, all idempotent:

1. MARKET CELLS. 21 source=market rows carried source_ref=NULL. The 2026-11-18
   feed publishes 19 design_arena entries for this id, so those cells are
   re-ingested through scripts/router_market_ingest.py's own build_rows (ELO,
   rank and population land in source_ref). The feed publishes no AA block at
   all, so the 2 AA cells (coding 0.761, agentic 0.352) are FROZEN 2026-11-17
   evidence and are stamped with origin index + raw score + not-republished.

2. SPEC NULLS FILLED. vision and thinking were NULL on a lane whose feed row
   declares text+image+file+audio+video->text and lists reasoning /
   reasoning_effort in supported_parameters. Both are set with provenance
   instead of staying blank.

3. PRICE CONFIRMED, NOT CHANGED: 0.375 / 1.875 / cache 0.0375 / cache-write
   0.041667 match the feed to the digit.

The note records the two findings the data rows cannot express: the id is
reachable through /api/v1/batches only (no chat endpoint), so its arena cells
are inherited from the canonical model rather than measured on the batch
surface; and the lane is already retired (valid_to 2026-09-27, replaced_by
google/gemini-3.8-flash), which is what keeps it out of chains. The truncated
api_type='o' on this row is NOT repaired here — the fleet-wide truncation
repair is owned by card t_api_type_truncation.
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
MODEL = "google/gemini-3.7-flash:batch"
AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}
NOTE = (
    "2026-11-18 market cross-check: price 0.375/1.875/cache 0.0375/cache-write "
    "0.041667 CONFIRMED to the digit by openrouter /api/v1/models (480 ids) and "
    "models.dev api.json (openrouter provider); context 1,048,576 confirmed. "
    "vision and thinking were NULL and are now filled with provenance: the feed "
    "declares text+image+file+audio+video->text and lists reasoning + "
    "reasoning_effort in supported_parameters. 19 design_arena cells re-ingested "
    "with raw ELO + rank + population in source_ref; the feed publishes no AA "
    "block for this id, so the 2 AA cells (coding 0.761, agentic 0.352) are "
    "FROZEN 2026-11-17 evidence stamped not-republished. TWO FINDINGS THE ROWS "
    "CANNOT EXPRESS: (a) this id is reachable through /api/v1/batches only — it "
    "has no chat/completions endpoint — so its arena cells are inherited from "
    "the canonical google/gemini-3.7-flash rather than measured on the batch "
    "surface, and a chain must never rank it for a chat request; (b) the lane is "
    "already retired (valid_to 2026-09-27, replaced_by "
    "openrouter/google/gemini-3.8-flash), which is what keeps it out of chains. "
    "The truncated api_type='o' on this row is NOT repaired here — the "
    "fleet-wide truncation repair is owned by card t_api_type_truncation."
)


def fill_spec():
    lines = TABLES.read_text(encoding="utf-8").splitlines(keepends=True)
    out, filled = [], 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if r.get("provider") == PROVIDER and r.get("model") == MODEL:
            changed = False
            if r.get("vision") is None:
                r["vision"] = True
                changed = True
            if r.get("thinking") is None:
                r["thinking"] = True
                changed = True
            if changed:
                filled += 1
                out.append(json.dumps(r, ensure_ascii=False) + "\n")
                continue
        out.append(line)
    if filled:
        TABLES.write_text("".join(out), encoding="utf-8")
    print(f"spec rows filled: {filled}")


def reingest_cells():
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    rows, report = build_rows(payload, load_registry_ids(str(TABLES)), DATE)
    mine = [r for r in rows if r["model"] == MODEL]
    fresh_cells = {r["category"] for r in mine}
    print(f"feed emitted={report['rows_emitted']} mine={len(mine)} cells={len(fresh_cells)}")

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
    fill_spec()
    reingest_cells()
    add_note()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

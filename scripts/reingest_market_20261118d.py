#!/usr/bin/env python3
"""Re-ingest the 2026-11-18 market cells for the three remaining Mistral cards.

Cards t_model_mistralai_mistral-medium-3.1, t_model_mistralai_mistral-small-2603
and t_model_mistralai_mistral-small-3.2-24b-instruct were left open by the
2026-11-17 Mistral wave (commit 034ef4b1), which fixed their prices and spec
fields but wrote no market provenance: every source=market row for these ids
carries source_ref=NULL, so the stored 0-1 score cannot be traced back to the
ELO/rank/population it was normalized from.

Uses scripts/router_market_ingest.py's own build_rows (full-feed denominators,
480 ids) so the percentiles and the source_ref provenance are the ingest's, not
hand-written. Only the targeted model ids are rewritten; every other line of
benchmarks.jsonl is copied byte-for-byte.

Two dispositions beyond the plain re-ingest:

  * Cells the 2026-11-18 feed still emits are replaced with fresh rows carrying
    percentile-in-score + raw ELO/rank/n in source_ref.
  * The frozen artificial_analysis coding/agentic rows are NOT re-emitted by the
    2026-11-18 feed (it publishes coding_index and agentic_index as null for all
    223 AA-carrying ids). They are real evidence from the 2026-11-17 read, so
    they are kept and stamped with the provenance that is actually known: origin
    date, the index name, the raw score the stored 0-1 value was normalized
    from, and the fact that the current feed is silent. Nothing is invented.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-18"
PRIOR = "2026-11-17"
PAYLOAD = "/tmp/kb-cache/or_models.json"
TABLES = "data/tables/models.jsonl"
BENCH = "data/tables/benchmarks.jsonl"
IDS = {
    "mistralai/mistral-medium-3.1",
    "mistralai/mistral-small-2603",
    "mistralai/mistral-small-3.2-24b-instruct",
}
# AA index -> the category the ingest maps it to (market_category_vocab).
AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}


def main():
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    registry_ids = load_registry_ids(TABLES)
    rows, report = build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] in IDS]
    cells = {(r["model"], r["category"]) for r in mine}
    print(
        f"feed models={report['feed_models']} joined={report['joined_models']} "
        f"emitted={report['rows_emitted']} mine={len(mine)} cells={len(cells)}"
    )
    by_model = {}
    for r in mine:
        by_model.setdefault(r["model"], []).append(r["category"])
    for model in sorted(by_model):
        print(f"  {model}: {len(by_model[model])} cells")

    kept, dropped, stamped = [], 0, 0
    with open(BENCH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                kept.append(line)
                continue
            r = json.loads(line)
            if r.get("model") not in IDS or r.get("source") != "market":
                kept.append(line)
                continue
            key = (r.get("model"), r.get("category"))
            if key in cells:
                dropped += 1
                continue
            # Frozen AA cell the current feed no longer publishes: keep the
            # evidence, stamp what is actually known about its origin.
            if r.get("category") in AA_CATS.values() and not r.get("source_ref"):
                index = next(
                    (k for k, v in AA_CATS.items() if v == r["category"]), None
                )
                raw = round(float(r["score"]) * 100.0, 6)
                r["source_ref"] = (
                    f"openrouter-models-api:{PRIOR}:index={index}:raw_score={raw}"
                    f":not-republished-{DATE}"
                )
                stamped += 1
                kept.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                continue
            kept.append(line)
    with open(BENCH, "w", encoding="utf-8") as f:
        f.writelines(kept)
        for r in mine:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    print(
        f"dropped {dropped} prior market rows; wrote {len(mine)} fresh rows; "
        f"stamped {stamped} frozen AA rows with origin provenance"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

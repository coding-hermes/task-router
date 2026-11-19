#!/usr/bin/env python3
"""Re-ingest the 2026-11-18 market cells for the three Kimi K2 cards.

Cards t_model_moonshotai_kimi-k2, t_model_moonshotai_kimi-k2-0905 and
t_model_moonshotai_kimi-k2-thinking carry 9 source=market rows with
source_ref=NULL: the stored 0-1 score cannot be traced back to the ELO, rank and
population it was normalized from.

Uses scripts/router_market_ingest.py's own build_rows (full-feed denominators,
480 ids) so the percentiles and the source_ref provenance are the ingest's, not
hand-written. Only the targeted model ids are rewritten; every other line of
benchmarks.jsonl is copied byte-for-byte.

The frozen artificial_analysis coding row for kimi-k2-thinking is NOT re-emitted
by the 2026-11-18 feed (coding_index and agentic_index are null for every
AA-carrying id), so it is kept and stamped with the provenance that is actually
known rather than dropped.
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
    "moonshotai/kimi-k2",
    "moonshotai/kimi-k2-0905",
    "moonshotai/kimi-k2-thinking",
}
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

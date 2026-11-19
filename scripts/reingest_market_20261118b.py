#!/usr/bin/env python3
"""Re-ingest the 2026-11-18 market cells for the six cards in wave 2026-11-18b.

Uses scripts/router_market_ingest.py's own build_rows (full-feed denominators,
480 ids) so the percentiles and the source_ref provenance are the ingest's, not
hand-written. Only the targeted model ids are rewritten; every other line of
benchmarks.jsonl is copied byte-for-byte. Cells the 2026-11-18 feed no longer
emits (the frozen artificial_analysis rows) are left in place.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-18"
PAYLOAD = "/tmp/kb-cache/or_models.json"
TABLES = "data/tables/models.jsonl"
BENCH = "data/tables/benchmarks.jsonl"
IDS = {
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "qwen/qwen3.6-35b-a3b",
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.1",
    "qwen/qwen3.6-plus",
    "z-ai/glm-5v-turbo",
}


def main():
    payload = json.loads(Path(PAYLOAD).read_text(encoding="utf-8"))
    registry_ids = load_registry_ids(TABLES)
    rows, report = build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] in IDS]
    cells = {(r["model"], r["category"]) for r in mine}
    print(f"feed models={report['feed_models']} joined={report['joined_models']} "
          f"emitted={report['rows_emitted']} mine={len(mine)} cells={len(cells)}")
    by_model = {}
    for r in mine:
        by_model.setdefault(r["model"], []).append(r["category"])
    for model in sorted(by_model):
        print(f"  {model}: {len(by_model[model])} cells")

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
    print(f"dropped {dropped} prior market rows; wrote {len(mine)} fresh rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Re-derive the image-arena market cells that the category-scoped denominator pinned to 0.0.

Card t_market_pct_normalization (2026-11-17): the ingest normalized the feed's
ARENA rank by the CATEGORY's entry count, so every image-arena row went negative
and the clamp stored 0.0 — "top of class" recorded as "worst in class". The four
affected models carry 13 such rows, all category=creative, all score 0.0, all
with source_ref=NULL:

  google/gemini-2.5-flash-image      3 rows (graphicdesign/image/logo)
  google/gemini-3-pro-image-preview  4 rows (+ imageediting)
  openai/gpt-5-image                 3 rows
  openai/gpt-5-image-mini            3 rows

Uses scripts/router_market_ingest.py's own build_rows (now arena-scoped: rank and
n from the same population, 186 ranked models in the `models` arena) so the
percentiles and the source_ref provenance are the ingest's, not hand-written.
Only the targeted model ids are rewritten; every other line of benchmarks.jsonl
is copied byte-for-byte.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_market_ingest import build_rows, load_registry_ids  # noqa: E402

DATE = "2026-11-18"
PAYLOAD = "/tmp/kb-cache/or_models_20261118.json"
TABLES = "data/tables/models.jsonl"
BENCH = "data/tables/benchmarks.jsonl"
IDS = {
    "google/gemini-2.5-flash-image",
    "google/gemini-3-pro-image-preview",
    "openai/gpt-5-image",
    "openai/gpt-5-image-mini",
}


def load_payload() -> dict:
    path = Path(PAYLOAD)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def main() -> int:
    payload = load_payload()
    registry_ids = load_registry_ids(TABLES)
    rows, report = build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] in IDS]
    cells = {(r["model"], r["category"]) for r in mine}
    print(
        f"feed models={report['feed_models']} joined={report['joined_models']} "
        f"emitted={report['rows_emitted']} mine={len(mine)} cells={len(cells)} "
        f"violations={report['population_violations']}"
    )
    for model in sorted({r["model"] for r in mine}):
        got = [r for r in mine if r["model"] == model]
        print(f"  {model}: {len(got)} rows")
        for r in got:
            print(f"    {r['category']} score={r['score']} {r['source_ref']}")

    kept, dropped = [], 0
    with open(BENCH, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                kept.append(line)
                continue
            row = json.loads(line)
            if row.get("model") in IDS and row.get("source") == "market":
                dropped += 1
                continue
            kept.append(line)
    with open(BENCH, "w", encoding="utf-8") as handle:
        handle.writelines(kept)
        for row in mine:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"dropped {dropped} prior market rows; wrote {len(mine)} fresh rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

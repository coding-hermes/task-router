#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20e — card t_model_openai_gpt-5.2-codex.

Model: openai/gpt-5.2-codex (text+image->text, 400,000 ctx, thinking-capable).

What this wave changes:

1. PROVENANCE on all five market cells. The rows on disk came from the
   2026-11-17 pre-provenance ingest and carry no source_ref, so the tier bridge
   could not audit a single percentile. They are recomputed from the live feed
   with the current shape (arena, source_category, raw_elo, rank, n,
   population=arena).

2. The feed publishes NO artificial_analysis values for this id at all (the
   whole block is empty), so no AA cell is emitted. Recorded, not left blank.

3. PRICE + SPEC verified clean, no change: carrier sticker 1.75/14/cache-read
   0.175 matches the stored public_in/out/cache exactly; models.dev agrees. ctx
   400,000 (top_provider 400,000), text+image->text (stored vision=true is
   correct), thinking via supported_parameters.reasoning, release_date
   2026-01-14 = feed created 1768409315 / canonical_slug
   openai/gpt-5.2-codex-20260114. The feed also carries a separate web_search
   price (0.01/call) which the registry does not model; recorded here so the
   omission is visible. Lane retired 2026-09-26 (replaced_by
   openrouter/openai/gpt-5.3-codex).

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "openai/gpt-5.2-codex"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 1.75/14/cache-read 0.175 - "
    "matches the stored public_in/out/cache exactly, so no drift; models.dev "
    "api.json agrees. The feed also publishes a separate web_search price "
    "(0.01 per call) that the registry does not model - recorded here so the "
    "omission is visible rather than silent. Spec confirmed against the feed: "
    "400,000 ctx (top_provider 400,000), text+image->text (stored vision=true is "
    "correct), thinking via supported_parameters.reasoning, release_date "
    "2026-01-14 = feed created 1768409315 / canonical_slug "
    "openai/gpt-5.2-codex-20260114. Lane retired 2026-09-26 (replaced_by "
    "openrouter/openai/gpt-5.3-codex). The feed publishes NO "
    "artificial_analysis values for this id (empty block), so no AA cell exists."
)


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)
    fresh = [r for r in rows if r["model"] == MODEL]
    if not fresh:
        print(f"REFUSING: feed emitted no market rows for {MODEL}", file=sys.stderr)
        return 1
    print(f"recomputed {len(fresh)} market cells for {MODEL}")

    lines = BENCH.read_text().splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") == MODEL:
            dropped += 1
            continue
        kept.append(line)

    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in fresh]
    BENCH.write_text("\n".join(kept + new_lines) + "\n", encoding="utf-8")
    print(f"benchmarks.jsonl: dropped {dropped} provenance-less rows, wrote {len(new_lines)}")

    mlines = MODELS.read_text().splitlines()
    mout: list[str] = []
    touched = 0
    for line in mlines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("provider") == "openrouter" and row.get("model") == MODEL:
            row["price_evidence"] = PRICE_EVIDENCE
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-serialised {touched} row(s) with price provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

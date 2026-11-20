#!/usr/bin/env python3
"""Market cross-check wave 2026-11-20c — card t_model_qwen_qwen3-coder-next.

Model: qwen/qwen3-coder-next (Qwen3 Coder Next, text->text, 262,144 ctx, no
reasoning parameter in the carrier catalog).

What this wave changes:

1. PROVENANCE on the two artificial_analysis cells. The rows on disk came from
   the 2026-11-17 pre-provenance ingest and carry no source_ref, so the tier
   bridge could not audit either number. The feed's AA block for this id is
   ALL-NULL on the 2026-11-20 read (intelligence/coding/agentic/finance/... all
   null; only openness_index=41.7 and multilingual_index=0.794 are published,
   neither of which is a capability index in the registry vocabulary), so the
   2026-11-17 published values are KEPT and dated `not-republished-2026-11-20`
   rather than dropped: they are real published evidence whose age must stay
   visible.

2. The feed publishes NO design_arena entries for this id (empty list), so there
   is no arena percentile to ingest. That absence is recorded here rather than
   left as a silent blank.

3. PRICE + SPEC verified clean, no change: carrier sticker 0.12/0.8/cache-read
   0.07, ctx 262,144 (top_provider 262,144), text->text, no reasoning parameter
   (stored thinking=false is correct), release_date 2026-02-04 = feed created
   1770164101. NOTE the feed's own canonical_slug says `2025-02-03` while its
   created stamp says 2026-02-04; the created stamp is the machine timestamp and
   the stored release_date follows it, so the slug is recorded here as a feed
   inconsistency rather than silently adopted.

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "qwen/qwen3-coder-next"
DATE = "2026-11-20"
FEED = Path("/tmp/or_models_20261120.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

# The feed's AA block is all-null on the 2026-11-20 read; these are the
# 2026-11-17 published values, kept and dated rather than dropped.
AA_KEEP = {
    "artificial_analysis_agentic": ("agentic_index", 0.9),
    "artificial_analysis_coding": ("coding_index", 36.2),
}

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-20 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-20 prices this id 0.12/0.8/cache-read 0.07 - "
    "matches the stored public_in/out/cache exactly, so no drift. models.dev "
    "api.json agrees on the same sticker. Spec confirmed against the feed: "
    "262,144 ctx (top_provider 262,144), text->text, NO reasoning parameter in "
    "supported_parameters (stored thinking=false is correct), release_date "
    "2026-02-04 = feed created 1770164101. FEED INCONSISTENCY recorded, not "
    "adopted: the same entry's canonical_slug reads "
    "qwen/qwen3-coder-next-2025-02-03 while its created stamp is 2026-02-04; the "
    "machine timestamp wins and the slug is left as the feed published it. AA "
    "block all-null on the 2026-11-20 read except openness_index=41.7 and "
    "multilingual_index=0.794 (neither is a capability index in the registry "
    "vocabulary), so the two 2026-11-17 AA cells are kept with "
    "not-republished provenance instead of being dropped."
)


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)
    fresh = [r for r in rows if r["model"] == MODEL]
    print(f"feed emitted {len(fresh)} fresh market cells for {MODEL}")

    lines = BENCH.read_text().splitlines()
    kept: list[str] = []
    aa_rows: list[dict] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") == MODEL:
            if row.get("category") in AA_KEEP:
                aa_rows.append(row)
            dropped += 1
            continue
        kept.append(line)

    aa_out: list[dict] = []
    for row in aa_rows:
        index_name, raw = AA_KEEP[row["category"]]
        row = dict(row)
        row["source_ref"] = (
            f"openrouter-models-api:2026-11-17:index={index_name}:raw_score={raw}"
            f":not-republished-{DATE}"
        )
        aa_out.append(row)
    print(f"kept {len(aa_out)} AA cell(s) with not-republished provenance")

    new_rows = list(fresh) + aa_out
    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in new_rows]
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

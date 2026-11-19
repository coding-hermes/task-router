#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19f — card t_model_qwen_qwen3.5-plus-02-15.

Two jobs, both line-scoped so untouched lines stay byte-identical:

1. Re-ingest the model's design_arena market cells from the live OpenRouter
   feed with the CURRENT provenance shape (source_ref carrying arena,
   source_category, raw_elo, rank, n, population). The eight rows on disk were
   written by the pre-provenance ingest (2026-11-16 pass) and carry no
   source_ref at all, so the tier bridge could not audit where the percentile
   came from.

2. Drop the stale null_reasons stamp for commandcode-2/gpt-5.3-codex
   cap_image_generation: wave 2026-11-19e filled that field to False, so the
   stamp now asserts a null that no longer exists and reds
   tests/test_null_census.py::test_stamp_registry_vocabulary_is_the_finite_ac2_set
   on main.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "qwen/qwen3.5-plus-02-15"
DATE = "2026-11-19"
FEED = Path("/tmp/or_models.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")
NULLS = Path("data/null_reasons.jsonl")
STALE_STAMP = ("commandcode-2", "gpt-5.3-codex", "cap_image_generation")

# Prices are CORRECT on this row (both machine sources agree to the cent), so
# nothing is re-priced. What the row lacked was provenance: "normalized:
# payg-sticker" names no source and no date, so a later reader cannot tell
# whether the sticker was ever checked. The 256k long-context override is a
# real cost fact for a 1M-context lane and the registry has no field for it.
PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-19 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-19 prices this id 0.26/1.56 (cache-read not "
    "published = NULL, never 0) and models.dev api.json read 2026-11-19 "
    "(openrouter/qwen/qwen3.5-plus-02-15) agrees to the cent; public_price "
    "re-blended 0.96*in+0.04*out = 0.312. The feed carries a long-context "
    "override at >=256,000 prompt tokens (0.325/1.95, +25%) which the registry "
    "does not model as a separate tier - noted here, not billed. Spec confirmed "
    "against the publisher (Alibaba Cloud Model Studio qwen3.5-plus page, read "
    "2026-11-19): 1,000,000 ctx, text+image+video input, toggleable thinking - "
    "matches the stored context_limit/vision/cap_video/thinking. Lane is retired "
    "in the registry (valid_to 2026-09-26, replaced_by qwen3.7-plus) but the "
    "carrier still lists it; the sticker stays as the record of what the lane cost."
)


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)
    mine = [r for r in rows if r["model"] == MODEL]
    if not mine:
        print(f"REFUSING: feed emitted no market rows for {MODEL}", file=sys.stderr)
        return 1
    print(f"recomputed {len(mine)} cells for {MODEL}")
    for r in sorted(mine, key=lambda x: (x["category"], x["source_ref"])):
        print("  " + json.dumps(r, sort_keys=True))

    # --- benchmarks.jsonl: replace only this model's market cells -------------
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
    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in mine]
    BENCH.write_text("\n".join(kept + new_lines) + "\n", encoding="utf-8")
    print(f"benchmarks.jsonl: dropped {dropped} provenance-less rows, wrote {len(new_lines)}")

    # --- models.jsonl: provenance on the one targeted row --------------------
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
    print(f"models.jsonl: re-serialised {touched} row(s) with dated provenance")

    # --- null_reasons.jsonl: sweep stamps that assert a null that is gone ----
    # The invariant tests/test_null_census.py pins: a stamp must target a field
    # the row actually leaves NULL. Wave 2026-11-19e filled all four capability
    # booleans on commandcode-2/gpt-5.3-codex but left their stamps behind, so
    # the registry asserted four nulls that no longer exist and main went red.
    # Sweep by the invariant rather than by a hardcoded list, so the next fill
    # cannot leave the same trap.
    model_rows = {}
    for line in MODELS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        model_rows[(row.get("provider"), row.get("model"))] = row
    nlines = NULLS.read_text().splitlines()
    out: list[str] = []
    removed: list[tuple] = []
    for line in nlines:
        if not line.strip():
            continue
        row = json.loads(line)
        key = (row.get("provider"), row.get("model"))
        target = model_rows.get(key)
        if target is not None and target.get(row.get("field")) is not None:
            removed.append((row.get("provider"), row.get("model"), row.get("field")))
            continue
        out.append(line)
    NULLS.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"null_reasons.jsonl: removed {len(removed)} stale stamp(s)")
    for r in removed:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

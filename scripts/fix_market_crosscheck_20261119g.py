#!/usr/bin/env python3
"""Market cross-check wave 2026-11-19g — card t_model_qwen_qwen3.5-397b-a17b.

Model: qwen/qwen3.5-397b-a17b (Qwen3.5 397B-A17B, hybrid linear-attention MoE,
397B total / 17B active, native vision-language, Apache-2.0 open weights).

What this wave changes:

1. PROVENANCE on all nine market cells. The rows on disk came from the
   pre-provenance ingest (2026-11-16 pass) and carry no source_ref, so the tier
   bridge could not audit a single percentile. The seven design_arena cells are
   recomputed from the live feed with the current shape (arena, source_category,
   raw_elo, rank, n, population=arena).

2. The two artificial_analysis cells are KEPT, not dropped: the feed stopped
   republishing coding_index/agentic_index for this id (both null on the
   2026-11-19 read) but the 2026-11-17 values are real published evidence. They
   are annotated `not-republished-2026-11-19` so a reader can see the age
   without the row silently disappearing.

3. PRICE DRIFT on the openrouter lane: stored 0.55/3.5/cache 0.225 against a
   carrier sticker of 0.45/3.0/cache 0.22 — a 1.22x/1.17x over-price on a LIVE
   lane, so chains ranked it against lanes it does not actually beat.

4. The bare-id twin (clinepass/crof serve `qwen3.5-397b-a17b`) gets the same
   nine cells: market evidence describes the MODEL, not one lane's spelling
   (TR-350).

Line-level rewrite: only the targeted rows are re-serialised; every other line
is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

MODEL = "qwen/qwen3.5-397b-a17b"
BARE = "qwen3.5-397b-a17b"
DATE = "2026-11-19"
FEED = Path("/tmp/or_models.json")
BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")

# The feed's AA block for this id is all-null on the 2026-11-19 read; these are
# the 2026-11-17 published values, kept and dated rather than dropped.
AA_KEEP = {
    "artificial_analysis_agentic": ("agentic_index", 8.3),
    "artificial_analysis_coding": ("coding_index", 48.2),
}

PRICE_EVIDENCE = (
    "normalized:payg-sticker | 2026-11-19 cross-check: carrier:openrouter "
    "/api/v1/models read 2026-11-19 prices this id 0.45/3.0/cache-read 0.22; the "
    "stored 0.55/3.5/cache 0.225 was 1.22x/1.17x/1.02x that sticker with no "
    "discount record anywhere in the registry, so a LIVE lane was ranked against "
    "lanes it does not beat. models.dev api.json read 2026-11-19 lists the same "
    "model under the kilo reseller at 0.39/2.34 - a reseller sticker, not this "
    "lane's, so it is recorded and not billed. public_price re-blended "
    "0.96*in+0.04*out = 0.552. Spec confirmed against the feed and the publisher "
    "(open weights, HF Qwen/Qwen3.5-397B-A17B): 262,144 ctx, text+image+video "
    "input, toggleable thinking - matches the stored context_limit/vision/"
    "cap_video/thinking. release_date kept at 2026-02-16: the feed's created "
    "stamp (2026-02-16T06:23:38Z), its canonical_slug (20260216) and the "
    "publisher's LICENSE/announcement all say 02-16; models.dev's 2026-02-15 is "
    "the only dissenting source and is recorded here rather than silently "
    "overriding the majority."
)


def main() -> int:
    payload = json.loads(FEED.read_text())
    registry_ids = rmi.load_registry_ids("data/tables/models.jsonl")
    rows, _report = rmi.build_rows(payload, registry_ids, DATE)
    arena = [r for r in rows if r["model"] == MODEL]
    if not arena:
        print(f"REFUSING: feed emitted no market rows for {MODEL}", file=sys.stderr)
        return 1
    print(f"recomputed {len(arena)} design_arena cells for {MODEL}")

    # --- benchmarks.jsonl ----------------------------------------------------
    lines = BENCH.read_text().splitlines()
    kept: list[str] = []
    aa_rows: list[dict] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source") == "market" and row.get("model") in (MODEL, BARE):
            if row.get("category") in AA_KEEP:
                aa_rows.append(row)
            dropped += 1
            continue
        kept.append(line)

    # Re-stamp the kept AA cells with provenance (one copy per id).
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

    new_rows: list[dict] = []
    for target in (MODEL, BARE):
        for r in arena:
            alt = dict(r)
            alt["model"] = target
            new_rows.append(alt)
        for r in aa_out:
            if r["model"] == target:
                new_rows.append(r)
    new_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in new_rows]
    BENCH.write_text("\n".join(kept + new_lines) + "\n", encoding="utf-8")
    print(f"benchmarks.jsonl: dropped {dropped} provenance-less rows, wrote {len(new_lines)}")

    # --- models.jsonl: price + provenance on the openrouter row --------------
    mlines = MODELS.read_text().splitlines()
    mout: list[str] = []
    touched = 0
    for line in mlines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("provider") == "openrouter" and row.get("model") == MODEL:
            row["normalized_price"] = 0.45
            row["public_in_per_m"] = 0.45
            row["public_out_per_m"] = 3.0
            row["public_cache_read_per_m"] = 0.22
            row["public_price"] = 0.552
            row["price_evidence"] = PRICE_EVIDENCE
            mout.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            touched += 1
            continue
        mout.append(line)
    MODELS.write_text("\n".join(mout) + "\n", encoding="utf-8")
    print(f"models.jsonl: re-priced + re-serialised {touched} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

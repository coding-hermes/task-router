#!/usr/bin/env python3
"""Correct the stale sticker on openrouter/deepseek/deepseek-v4-flash-vision-exp.

Card t_model_deepseek_deepseek-v4-flash-vision-exp. The row carried
0.2156 / 0.6468 / cache 0.00686 — exactly 0.49x the live sticker, i.e. a stale
half-price read that made the lane rank ~2x cheaper than it bills. Both machine
sources agree on 0.44 / 1.32 / cache 0.014:

  * openrouter /api/v1/models (480 ids, read 2026-11-18): prompt 0.00000044,
    completion 0.00000132, input_cache_read 0.000000014 per token
  * models.dev api.json, provider=openrouter (read 2026-11-18): cost
    input 0.44, output 1.32, cache_read 0.014

public_price is re-blended with the documented 0.96*in + 0.04*out mix
(scripts/pricing/helpers.py) = 0.4752. normalized_price tracks the input
sticker for a PAYG lane (527 of 541 priced openrouter rows), so it moves to
0.44 with it.

Spec fields are CONFIRMED, not changed: context 1,048,576 (feed + models.dev;
top_provider 1,048,575 is the same window minus one token), text+image->text
(vision=true), reasoning in supported_parameters + models.dev reasoning=true
(thinking=true), release_date 2026-08-21 agrees with models.dev.

Idempotent: only rewrites the row while it still carries the stale values.
"""
import json
from pathlib import Path

TABLES = Path("data/tables/models.jsonl")
NOTES = Path("data/tables/model_notes.jsonl")
VALID_FROM = "2026-11-18"
MODEL = "deepseek/deepseek-v4-flash-vision-exp"
PROVIDER = "openrouter"
STALE = {"public_in_per_m": 0.2156, "public_out_per_m": 0.6468,
         "public_cache_read_per_m": 0.00686, "public_price": 0.232848,
         "normalized_price": 0.2156}
FRESH = {"public_in_per_m": 0.44, "public_out_per_m": 1.32,
         "public_cache_read_per_m": 0.014, "public_price": 0.4752,
         "normalized_price": 0.44}
EVIDENCE = (
    "normalized:payg-sticker | market cross-check 2026-11-18: openrouter "
    "/api/v1/models (480 ids) and models.dev api.json (openrouter provider) both "
    "publish 0.44/1.32/cache 0.014; the prior 0.2156/0.6468/0.00686 was exactly "
    "0.49x that sticker (stale half-price read) and made the lane rank ~2x "
    "cheaper than it bills. public_price re-blended 0.96*in+0.04*out = 0.4752"
)
NOTE = (
    "2026-11-18 market cross-check: PRICE CORRECTED 0.2156/0.6468/cache 0.00686 "
    "-> 0.44/1.32/cache 0.014. The old triple was exactly 0.49x the live sticker "
    "on both machine sources, so the lane was ranking ~2x cheaper than it bills; "
    "public_price re-blended to 0.4752 and normalized_price follows the input "
    "sticker (PAYG convention). Context 1,048,576 confirmed by feed and "
    "models.dev (top_provider 1,048,575 is the same window minus one token), "
    "text+image->text keeps vision=true, reasoning in supported_parameters plus "
    "models.dev reasoning=true keeps thinking=true, release_date 2026-08-21 "
    "agrees. The 2026-11-18 feed publishes benchmarks:null for this id, so the 2 "
    "AA cells (coding 0.65, agentic 0.475) are FROZEN 2026-11-17 evidence and are "
    "stamped with origin index + raw score + not-republished rather than "
    "dropped. No design_arena entries exist for this id, so no ELO cell is "
    "ingestible."
)


def main():
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

    existing = set()
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    if (PROVIDER, MODEL, VALID_FROM) in existing:
        print("note already present")
        return 0
    with NOTES.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"provider": PROVIDER, "model": MODEL, "note": NOTE,
                            "source": "openrouter /api/v1/models (480 ids) + "
                                      "models.dev api.json (openrouter provider), "
                                      "both read 2026-11-18",
                            "valid_from": VALID_FROM},
                           ensure_ascii=False, sort_keys=True) + "\n")
    print("note added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

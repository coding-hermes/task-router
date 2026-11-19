#!/usr/bin/env python3
"""Record the 2026-11-18 market cross-check findings for the three Kimi K2 cards.

Two jobs, both idempotent:

  1. release_date on the openrouter lane of moonshotai/kimi-k2-0905: stored
     2025-09-04, models.dev openrouter says 2025-09-05. The documented rule
     (docs/lifecycle-prune-20260926.md) makes models.dev release_date the
     authority for the lane's own provider, and the model id itself encodes
     0905. The feed's `created` stamp (1757021147) is the UTC instant of the
     same launch — 2025-09-05 04:05 in the publisher's timezone — so it
     corroborates rather than contradicts. Corrected to 2025-09-05.

  2. one model_notes.jsonl row per lane carrying the dispositions the data rows
     cannot express: which cells were re-ingested, which AA cell is frozen
     2026-11-17 evidence, and which fields stay NULL because neither machine
     source publishes them.
"""
import json
from pathlib import Path

TABLES = Path("data/tables/models.jsonl")
NOTES = Path("data/tables/model_notes.jsonl")
VALID_FROM = "2026-11-18"
SOURCE = (
    "openrouter /api/v1/models (480 ids) + models.dev api.json (openrouter "
    "provider), both read 2026-11-18"
)

RELEASE_FIX = {
    "provider": "openrouter",
    "model": "moonshotai/kimi-k2-0905",
    "old": "2025-09-04",
    "new": "2025-09-05",
    "lifecycle_source": (
        "market cross-check 2026-11-18: models.dev openrouter release_date "
        "2025-09-05 (documented authority for the lane's own provider); the id "
        "marker 0905 agrees and the feed created stamp 1757021147 is the same "
        "launch instant in UTC"
    ),
}

ROWS = [
    {
        "provider": "openrouter",
        "model": "moonshotai/kimi-k2",
        "note": (
            "2026-11-18 market cross-check: price 0.57/2.3 confirmed by both "
            "machine sources; NEITHER publishes an input_cache_read rate, so "
            "public_cache_read_per_m stays NULL (not published, never 0). "
            "Context 131,072 and text->text confirmed, so vision=false / "
            "thinking=false stand (no reasoning parameter in "
            "supported_parameters). release_date 2025-07-11 agrees with "
            "models.dev. 5 design_arena cells re-ingested with raw ELO + rank + "
            "population in source_ref (codecategories 1018/128/175, dataviz "
            "1018/118/162, gamedev 957/132/174, uicomponent 1017/121/170, "
            "website 1039/129/181). The feed's AA block carries only "
            "openness_index 88.9 and multilingual_index 0.786 — neither has a "
            "defensible mapping to the 24-category vocabulary, so no AA cell is "
            "ingested for this id."
        ),
    },
    {
        "provider": "openrouter",
        "model": "moonshotai/kimi-k2-0905",
        "note": (
            "2026-11-18 market cross-check: price 0.6/2.5 confirmed by both "
            "machine sources; no cache rate published by either, so "
            "public_cache_read_per_m stays NULL. Context 262,144 and text->text "
            "confirmed (vision=false, thinking=false). release_date CORRECTED "
            "2025-09-04 -> 2025-09-05: models.dev is the documented authority "
            "for the lane's own provider, the id marker 0905 agrees, and the "
            "feed's created stamp is the same launch instant expressed in UTC. "
            "2 design_arena cells re-ingested with provenance (codecategories "
            "1082/115/175, website 1096/117/181). AA block carries only "
            "openness_index 27.8 and multilingual_index 0.808 — unmapped, no AA "
            "cell ingested."
        ),
    },
    {
        "provider": "openrouter",
        "model": "moonshotai/kimi-k2-thinking",
        "note": (
            "2026-11-18 market cross-check: price 0.6/2.5/cache 0.15 confirmed "
            "by both machine sources; context 262,144; reasoning is in "
            "supported_parameters and models.dev publishes reasoning=true, so "
            "thinking=true stands; text->text keeps vision=false. release_date "
            "2025-11-06 agrees with models.dev. 1 design_arena cell re-ingested "
            "(website 1102/114/181). AA coding_index 0.21 is FROZEN 2026-11-17 "
            "evidence — the 2026-11-18 feed publishes coding_index and "
            "agentic_index as null for every AA-carrying id — so the row is "
            "kept and stamped not-republished rather than dropped. AA "
            "openness_index 88.9 and multilingual_index 0.735 are unmapped."
        ),
    },
]


def main():
    # 1. release_date correction, idempotent.
    lines = TABLES.read_text(encoding="utf-8").splitlines(keepends=True)
    out, fixed = [], 0
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        if (
            r.get("provider") == RELEASE_FIX["provider"]
            and r.get("model") == RELEASE_FIX["model"]
            and r.get("release_date") == RELEASE_FIX["old"]
        ):
            r["release_date"] = RELEASE_FIX["new"]
            r["lifecycle_source"] = RELEASE_FIX["lifecycle_source"]
            r["lifecycle_checked_at"] = VALID_FROM
            fixed += 1
            out.append(json.dumps(r, ensure_ascii=False) + "\n")
            continue
        out.append(line)
    if fixed:
        TABLES.write_text("".join(out), encoding="utf-8")
    print(f"release_date rows corrected: {fixed}")

    # 2. notes, idempotent.
    existing = set()
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    added = 0
    with NOTES.open("a", encoding="utf-8") as f:
        for row in ROWS:
            if (row["provider"], row["model"], VALID_FROM) in existing:
                print(f"skip (already noted): {row['model']}")
                continue
            out = dict(row, source=SOURCE, valid_from=VALID_FROM)
            f.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
            added += 1
    print(f"notes added: {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

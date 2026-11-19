#!/usr/bin/env python3
"""Record the 2026-11-18 market cross-check findings for the three Mistral cards.

Appends one model_notes.jsonl row per lane, idempotently (a second run is a
no-op). The notes carry the dispositions that the data rows cannot express:

  * which cells were re-ingested and which are frozen 2026-11-17 evidence,
  * the AA indexes the current feed no longer publishes,
  * the publisher-vs-aggregator disagreements that were deliberately NOT
    written into the registry (a third-party review site's 128K context claim
    and its two mutually contradictory Intelligence Index figures), and
  * the fields that stay NULL because neither machine source publishes them.
"""
import json
from pathlib import Path

NOTES = "data/tables/model_notes.jsonl"
VALID_FROM = "2026-11-18"
SOURCE = (
    "openrouter /api/v1/models (480 ids) + models.dev api.json (openrouter "
    "provider), both read 2026-11-18; publisher launch post / HF model card "
    "where the machine sources are silent"
)

ROWS = [
    {
        "provider": "openrouter",
        "model": "mistralai/mistral-medium-3.1",
        "note": (
            "2026-11-18 market cross-check: price 0.44/2.2/cache 0.044 and "
            "context 131,072 confirmed to the cent by BOTH machine sources "
            "(no change; the 2026-11-17 wave already corrected the 10% "
            "under-price). Architecture text+image+file->text, no reasoning "
            "parameter in supported_parameters, so vision=true / thinking=false "
            "stand. 8 design_arena cells re-ingested with raw ELO + rank + "
            "population in source_ref (codecategories 1105/106/175, 3d "
            "1054/107/166, asciiart 981/75/104, dataviz 1139/87/162, gamedev "
            "1057/115/174, svg 978/96/123, uicomponent 1086/107/170, website "
            "1121/107/181). AA coding_index 0.205 and agentic_index 0.019 are "
            "FROZEN 2026-11-17 evidence: the 2026-11-18 feed publishes both as "
            "null for every AA-carrying id, so the rows are kept and stamped "
            "not-republished rather than dropped. AA openness_index 11.1 has no "
            "defensible mapping to the 24-category vocabulary and is not "
            "ingested. The feed carries no hugging_face_id for this id, so no "
            "publisher card exists to override the aggregators."
        ),
    },
    {
        "provider": "openrouter",
        "model": "mistralai/mistral-small-2603",
        "note": (
            "2026-11-18 market cross-check: price 0.15/0.6/cache 0.015 and "
            "context 262,144 confirmed by both machine sources (no change). "
            "Mistral's own launch post says '256k context window' — that is "
            "256Ki rounded, consistent with 262,144, not a conflict. reasoning "
            "is in supported_parameters and models.dev publishes reasoning=true, "
            "so thinking=true stands. AA intelligence_index 4.3 re-ingested "
            "with raw_score provenance; AA coding_index 0.266 and "
            "agentic_index 0.008 are FROZEN 2026-11-17 evidence (the current "
            "feed publishes both null for all AA ids) and are stamped "
            "not-republished. The feed publishes NO design_arena entry for this "
            "id, so it gets no arena cell at all — an absence, not a gap. The "
            "xkiro twins of this model are a separate lane family and were not "
            "touched by this pass."
        ),
    },
    {
        "provider": "openrouter",
        "model": "mistralai/mistral-small-3.2-24b-instruct",
        "note": (
            "2026-11-18 market cross-check: price 0.075/0.2 confirmed by both "
            "machine sources (no change; the 2026-11-17 wave removed the 1.25x "
            "conversion residue). NEITHER source publishes an input_cache_read "
            "rate for this id, so public_cache_read_per_m stays NULL — not "
            "published, never 0. Context 256,000 on both machine sources; the "
            "publisher HF card (mistralai/Mistral-Small-3.2-24B-Instruct-2506) "
            "states no context figure in its README, so there is no publisher "
            "value to override it with. A third-party review site's '128K "
            "context' claim and its Intelligence Index figures (9.9 on one "
            "aggregator, 14.7 on another) were REJECTED as non-authoritative: "
            "they contradict each other and neither is a publisher or a "
            "benchmark-owner publication. 5 design_arena cells re-ingested with "
            "raw ELO + rank + population (codecategories 891/144/175, dataviz "
            "929/130/162, gamedev 871/144/174, uicomponent 894/138/170, website "
            "884/147/181). The feed publishes no artificial_analysis block for "
            "this id at all."
        ),
    },
]


def main():
    path = Path(NOTES)
    existing = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        existing.add((r.get("provider"), r.get("model"), r.get("valid_from")))
    added = 0
    with path.open("a", encoding="utf-8") as f:
        for row in ROWS:
            key = (row["provider"], row["model"], VALID_FROM)
            if key in existing:
                print(f"skip (already noted): {row['model']}")
                continue
            out = dict(row, source=SOURCE, valid_from=VALID_FROM)
            f.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
            added += 1
    print(f"notes added: {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Retire null_reasons stamps whose field is no longer NULL.

The stamp registry (data/null_reasons.jsonl) is a set of CLAIMS: "this field is
NULL on this row, and here is why". A thin-row completion that fills the field
makes the claim false, and tests/test_null_census.py::test_stamp_registry_
vocabulary_is_the_finite_ac2_set asserts the claim against the row — so a fill
that leaves its stamp behind turns main red.

dd401d31 (2026-11-18) completed the qwen/qwen3.7-plus:batch thin row
(cap_audio / cap_embedding / cap_image_generation / cap_video NULL -> false) and
left the four 'no-sample-yet' stamps in place. This retires exactly those four
rows and nothing else: every other line is copied byte-for-byte.

Deleting a stamp for a field that is no longer NULL is a no-op for the census
itself (a non-null field is never counted as an unexplained null), so this
changes no classification — it only makes the registry true again.
"""
import json

STAMPS = "data/null_reasons.jsonl"
MODELS = "data/tables/models.jsonl"

# (provider, model, field) whose stamp is now false.
RETIRE = {
    ("openrouter", "qwen/qwen3.7-plus:batch", "cap_image_generation"),
    ("openrouter", "qwen/qwen3.7-plus:batch", "cap_embedding"),
    ("openrouter", "qwen/qwen3.7-plus:batch", "cap_audio"),
    ("openrouter", "qwen/qwen3.7-plus:batch", "cap_video"),
}


def main() -> int:
    models = {}
    with open(MODELS, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                models[(row.get("provider"), row.get("model"))] = row

    kept, retired = [], []
    with open(STAMPS, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                kept.append(line)
                continue
            row = json.loads(line)
            key = (row.get("provider"), row.get("model"), row.get("field"))
            if key in RETIRE:
                model = models.get((row.get("provider"), row.get("model")))
                if model is None:
                    raise SystemExit(f"stamp targets a missing row: {key}")
                if model.get(row.get("field")) is None:
                    raise SystemExit(
                        f"refusing to retire a TRUE stamp (field is still NULL): {key}"
                    )
                retired.append(key)
                continue
            kept.append(line)

    missing = RETIRE - set(retired)
    if missing:
        raise SystemExit(f"stamps not found: {sorted(missing)}")

    with open(STAMPS, "w", encoding="utf-8") as handle:
        handle.writelines(kept)
    print(f"retired {len(retired)} stale stamps: {sorted(retired)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

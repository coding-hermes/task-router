#!/usr/bin/env python3
"""Repair the single-character api_type / data_class values (card t_api_type_truncation).

FINDING (2026-11-16, re-derived 2026-11-18): api_type is one character on 512 of
2,179 models.jsonl rows — o=455, r=29, c=26, plus 'chat'=2 — and data_class is
one character on 40 rows (u=22, z=18). The card hypothesised a per-provider
import or a CSV/parquet round-trip. The history says otherwise: `git log -S`
puts the FIRST appearance of every one of these values in a single commit,
b62eb204 (2026-10-14, "file 13 HELD-lane stickers + 2026-10-14 research slice"),
which introduced 457 'o', 26 'c' and 26 'r' at once across 17 providers and added
no importer. The importers write full strings (router_clinepass.py:144
api_type='openai-chat', router_muse_code.py:144 api_type='responses'), so this is
a one-off data-entry truncation in that filing wave, not a live writer.

The mapping is unambiguous because the vocabulary is closed and each truncated
value is the first token of exactly one member:
    o    -> openai-chat        (455)
    r    -> responses          (29)
    c    -> chat_completions   (26)
    chat -> chat_completions   (2; the only vocabulary member it can abbreviate)
    u    -> unknown            (22, data_class)
    z    -> zdr                (18, data_class)

Every repaired row gets a note sentence naming the repair and its evidence, so
the value is never a silent rewrite. tests/test_api_type_vocabulary.py is the
ratchet: it fails if any row leaves the closed vocabulary again.
"""
import json
from collections import Counter

TABLES = "data/tables/models.jsonl"

API_TYPE_MAP = {
    "o": "openai-chat",
    "r": "responses",
    "c": "chat_completions",
    "chat": "chat_completions",
}
DATA_CLASS_MAP = {"u": "unknown", "z": "zdr"}

NOTE = (
    " api_type/data_class truncation repaired 2026-11-18 (card t_api_type_truncation): "
    "the single-character value was the first token of the closed-vocabulary member, "
    "introduced by the 2026-10-14 filing wave b62eb204 (no importer writes it; "
    "router_clinepass.py writes openai-chat, router_muse_code.py writes responses)."
)


def main() -> int:
    out = []
    api_fixed: Counter[str] = Counter()
    dc_fixed: Counter[str] = Counter()
    touched = 0
    with open(TABLES, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                out.append(line)
                continue
            row = json.loads(line)
            changed = False
            api = row.get("api_type")
            if api in API_TYPE_MAP:
                row["api_type"] = API_TYPE_MAP[api]
                api_fixed[f"{api}->{API_TYPE_MAP[api]}"] += 1
                changed = True
            dc = row.get("data_class")
            if dc in DATA_CLASS_MAP:
                row["data_class"] = DATA_CLASS_MAP[dc]
                dc_fixed[f"{dc}->{DATA_CLASS_MAP[dc]}"] += 1
                changed = True
            if changed:
                touched += 1
                row["note"] = (row.get("note") or "").rstrip() + NOTE
                out.append(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            else:
                out.append(line)
    with open(TABLES, "w", encoding="utf-8") as handle:
        handle.writelines(out)
    print(f"rows touched: {touched}")
    print("api_type:", dict(sorted(api_fixed.items())))
    print("data_class:", dict(sorted(dc_fixed.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

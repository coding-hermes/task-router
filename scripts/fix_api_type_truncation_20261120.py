#!/usr/bin/env python3
"""Repair single-character api_type / data_class truncations (2026-11-20).

The 2026-11-18 repair (commit 85c214a1, scripts/fix_api_type_truncation_20261118.py)
left the tree with ZERO single-character values. The 2026-11-20 daily tick
(fe6f81bc) put 157 api_type and 5 data_class truncations back:

    api_type   o x147 (clinepass)  r x3 (commandcode)  o x1 (openrouter)
               c x3 (xkiro)        c x3 (xkiro-2)
    data_class z x1 (openrouter)   u x2 (xkiro)        u x2 (xkiro-2)

tests/test_api_type_vocabulary.py is the ratchet and it is RED on origin/main
because of them. This script re-applies the SAME closed-vocabulary mapping the
11-18 repair established (o->openai-chat, r->responses, c->chat_completions,
z->zdr, u->unknown) and refuses to guess anything outside that table.

This is a data repair, not a root-cause fix: the writer that re-truncates is
still in the daily-tick path (router_clinepass.py itself writes
api_type='openai-chat', so the truncation enters through a different writer).
The recurrence is recorded in the commit message so it can be filed against the
writer rather than repaired a third time.

Line-level rewrite: only rows carrying a single-character value are
re-serialised; every other line is copied byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MODELS = Path("data/tables/models.jsonl")

API_TYPE_MAP = {
    "o": "openai-chat",
    "r": "responses",
    "c": "chat_completions",
}
DATA_CLASS_MAP = {
    "z": "zdr",
    "u": "unknown",
    "t": "training-opt-in",
}


def main() -> int:
    lines = MODELS.read_text().splitlines()
    out: list[str] = []
    fixed_api: dict[str, int] = {}
    fixed_dc: dict[str, int] = {}
    unknown: list[str] = []
    touched = 0

    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        changed = False
        for field, table, counter in (
            ("api_type", API_TYPE_MAP, fixed_api),
            ("data_class", DATA_CLASS_MAP, fixed_dc),
        ):
            value = row.get(field)
            if isinstance(value, str) and len(value) == 1:
                replacement = table.get(value)
                if replacement is None:
                    unknown.append(f"{row.get('provider')}/{row.get('model')} {field}={value!r}")
                    continue
                row[field] = replacement
                key = f"{value}->{replacement}"
                counter[key] = counter.get(key, 0) + 1
                changed = True
        if changed:
            touched += 1
            out.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        else:
            out.append(line)

    if unknown:
        print("REFUSING: unmapped single-character values:", file=sys.stderr)
        for item in unknown:
            print("  " + item, file=sys.stderr)
        return 1

    MODELS.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"models.jsonl: repaired {touched} row(s)")
    print("  api_type:", dict(sorted(fixed_api.items())))
    print("  data_class:", dict(sorted(fixed_dc.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

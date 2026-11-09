#!/usr/bin/env python3
"""migrate_reasoning_effort_tr398.py — backfill the reasoning_effort axis.

TR-398: the unit of capability becomes (model, category, reasoning_effort).
Every pre-effort-era row in the committed tables is stamped
reasoning_effort='default' EXPLICITLY — an unexplained NULL is junk per the
house data rule, and per-effort grouping must never see NULL as a fourth
state. benchmarks.jsonl rows carry the effort a measurement ran at; every
existing row predates the axis, so 'default' is the honest stamp (the seed
rebuilds derived tables; model_perf.jsonl and model_tier.jsonl are seeded
outputs backfilled here so the committed tree satisfies the commit gate).

The provider-name alias table lives in scripts/capability_vocab.py
(PROVIDER_EFFORT_ALIASES) — the ONE place. This migration never maps an
unknown provider name down: rows that already carry a value are left
untouched, verbatim.

Idempotent: rows already carrying reasoning_effort are never rewritten.
Run: python3 scripts/migrate_reasoning_effort_tr398.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
import capability_vocab as _ev  # noqa: E402

REPO = os.path.dirname(_HERE)
TABLES = ("model_perf", "benchmarks", "model_tier")


def migrate(path: str, dry_run: bool) -> tuple:
    n_stamp = n_kept = 0
    rows = []
    if not os.path.exists(path):
        return 0, 0, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    for r in rows:
        if r.get("reasoning_effort"):
            n_kept += 1
            continue
        stored, status = _ev.normalize_reasoning_effort(r.get("reasoning_effort"))
        # pre-effort-era rows have NO effort info: 'default' is the only
        # honest stamp (status 'missing' here by construction).
        r["reasoning_effort"] = "default"
        n_stamp += 1
    if not dry_run and n_stamp:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n_stamp, n_kept, len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="TR-398 reasoning_effort backfill")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    total = 0
    for name in TABLES:
        path = os.path.join(REPO, "data", "tables", f"{name}.jsonl")
        n_stamp, n_kept, n_rows = migrate(path, args.dry_run)
        print(f"{name}.jsonl: {n_rows} rows, stamped {n_stamp}, already stamped {n_kept}")
        total += n_stamp
    print(f"done: {total} row(s) stamped 'default'" + (" (dry run — nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

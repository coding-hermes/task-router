#!/usr/bin/env python3
"""TR-216 one-shot migration (phase 2): make aggregated-display permission and
rollover semantics EXPLICIT on the multi-account provider rows of
data/tables/provider_quota.jsonl.

TR-216 commit 91f7c62 already split the account identities (xkiro -> xkiro-1,
xkiro-2 -> xkiro-2, ...) and declared commandcode's rollover. Two things the
task names were still implicit:

  aggregate_display — quota-layers-spec §2: "Aggregation is a display choice;
      two accounts of the same plan never merge into '2x the window' unless
      the provider's own docs say the limit is per-person." No L0 row said
      whether summing limits across accounts is doc-permitted, so every
      consumer had to guess (and guessing defaults to a wrong 2x). Every
      multi-account row now carries {"sum_limits": "unknown", "reason": ...}:
      no provider doc statement permits summing, so the aggregate display is
      UNKNOWN — never a computed 2x. The L2 view (router_quota_accounting.py)
      surfaces this field verbatim on split blocks and never emits an
      aggregate row of its own.

  rollover on xkiro/-2 and opencode-go/-2 — commandcode's semantics are
      modeled (included no-rollover / top-up never-expire). xkiro and
      opencode-go document window resets but no rollover-or-forfeit statement
      for unused budget, so they are explicitly marked unknown WITH the
      reason (Bane's null-with-reason law) instead of silently absent.

Rules of this migration (same doctrine as migrate_quota_tr206.py):
- research content is NOT edited: per-row decisions only ADD the two fields.
- decisions are keyed by provider_id over the six multi-account providers,
  with the row's window_kind asserted non-empty so a drifted file fails
  loudly instead of being silently mis-annotated.
- idempotent: a row already carrying identical values is left untouched, so
  a second run rewrites nothing (git shows no further diff).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.path.join(REPO, "data", "tables", "provider_quota.jsonl")

# provider_id -> fields to inject on EVERY row of that provider.
# commandcode/-2 keep their existing (modeled) rollover; only aggregate_display
# is added there. xkiro/-2 and opencode-go/-2 get both fields, rollover unknown.
INJECT: dict[str, dict[str, Any]] = {
    "xkiro": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "per-account windows with independent rolling anchors "
                      "(windows[].resets_in_sec is read per account); no doc "
                      "statement permits summing limits across accounts",
        },
        "rollover": {
            "included_credits": "unknown",
            "topup_credits": "unknown",
            "reason": "docs document window resets (windows[].resets_in_sec) "
                      "but state no rollover/forfeit semantics for unused "
                      "budget; wallet balance expiry is undocumented",
        },
    },
    "xkiro-2": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "independent rolling windows observed live on this "
                      "second account; no doc statement permits summing "
                      "limits across accounts",
        },
        "rollover": {
            "included_credits": "unknown",
            "topup_credits": "unknown",
            "reason": "docs document window resets (windows[].resets_in_sec) "
                      "but state no rollover/forfeit semantics for unused "
                      "budget; wallet balance expiry is undocumented",
        },
    },
    "opencode-go": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "5h limits are PER MODEL per account with independent "
                      "anchors (rolling 5h, calendar week, subscription "
                      "anniversary); no doc statement permits summing across "
                      "accounts",
        },
        "rollover": {
            "included_credits": "unknown",
            "topup_credits": "unknown",
            "reason": "windows observed resetting (rolling 5h / calendar "
                      "week / subscription-anniversary month) but docs state "
                      "no rollover or forfeit semantics for unused budget; "
                      "no top-up concept documented",
        },
    },
    "opencode-go-2": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "second account with its own per-model windows; no doc "
                      "statement permits summing limits across accounts",
        },
        "rollover": {
            "included_credits": "unknown",
            "topup_credits": "unknown",
            "reason": "windows observed resetting (rolling 5h / calendar "
                      "week / subscription-anniversary month) but docs state "
                      "no rollover or forfeit semantics for unused budget; "
                      "no top-up concept documented",
        },
    },
    "commandcode": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "seats are separate subscriptions, each with its own "
                      "5h/weekly pacing windows; no doc statement permits "
                      "summing limits across accounts",
        },
    },
    "commandcode-2": {
        "aggregate_display": {
            "sum_limits": "unknown",
            "reason": "seats are separate subscriptions, each with its own "
                      "5h/weekly pacing windows; no doc statement permits "
                      "summing limits across accounts",
        },
    },
}


def main() -> int:
    if not os.path.exists(TABLE):
        print(f"migrate_quota_tr216: table not found: {TABLE}", file=sys.stderr)
        return 1
    with open(TABLE, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]

    changed = 0
    out_lines = []
    touched: dict[str, int] = {pid: 0 for pid in INJECT}
    for row in rows:
        pid = row.get("provider_id")
        extra = INJECT.get(pid)
        if extra:
            if not row.get("window_kind"):
                print(f"migrate_quota_tr216: {pid} row without window_kind — "
                      "file drifted, refusing to annotate", file=sys.stderr)
                return 1
            before = json.dumps(row, sort_keys=True, ensure_ascii=False)
            for key, value in extra.items():
                if row.get(key) != value:
                    row[key] = value
            after = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if before != after:
                changed += 1
                touched[pid] += 1
        out_lines.append(json.dumps(row, ensure_ascii=False))

    tmp = TABLE + ".tr216.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    os.replace(tmp, TABLE)

    print(f"migrate_quota_tr216: {changed} row(s) updated of {len(rows)}")
    for pid, n in sorted(touched.items()):
        print(f"  {pid}: {n} row(s) annotated")
    print("idempotence: re-run annotates only rows whose values differ")
    return 0


if __name__ == "__main__":
    sys.exit(main())

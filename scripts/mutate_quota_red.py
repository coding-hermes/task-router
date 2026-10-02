#!/usr/bin/env python3
"""Apply 3 contract mutations to the quota table in place (RED proof arm).

Usage: python3 mutate_quota_red.py <tables-dir> [arm]
  arm 1: strip reason from row 6 (clinepass NULL limit)  [missing-reason]
  arm 2: strip alias_of from row 28 (gw-deepseek)        [alias-inheritance]
  arm 3: grok-build pool row back to plain account scope [pool-vs-account]
  arm 4: strip source from row 47 (openai-codex window)  [source rejection]
"""

import json
import sys

tables = sys.argv[1]
arm = int(sys.argv[2]) if len(sys.argv) > 2 else 0

path = tables + "/provider_quota.jsonl"
with open(path, encoding="utf-8") as fh:
    lines = fh.read().split("\n")

if arm == 1:
    idx = 5
    r = json.loads(lines[idx])
    assert r["provider_id"] == "clinepass" and r["limit"] is None and "reason" in r
    del r["reason"]
elif arm == 2:
    idx = 27
    r = json.loads(lines[idx])
    assert r["provider_id"] == "gw-deepseek" and "alias_of" in r
    del r["alias_of"]
elif arm == 3:
    idx = 19
    r = json.loads(lines[idx])
    assert r["provider_id"] == "grok-build" and r["scope"].startswith("pool:")
    r["scope"] = "account"
    del r["pool"]
elif arm == 4:
    idx = 46
    r = json.loads(lines[idx])
    assert r["provider_id"] == "openai-codex" and "source" in r
    del r["source"]
else:
    sys.exit("arm must be 1..4")

lines[idx] = json.dumps(r, ensure_ascii=False)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print(f"arm {arm}: mutated line {idx + 1}")

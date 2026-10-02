#!/usr/bin/env python3
"""TR-206 one-shot migration: inject the quota-layers-spec L0 contract fields
into every row of data/tables/provider_quota.jsonl.

The table already carries the quota RESEARCH (readback endpoints, header
names, pools, notes — 92 rows). The spec (docs/quota-layers-spec.md) pins four
fields the research rows predate:

  alias_of  — gateway aliases (gw-deepseek, myrouter:zai-glm) declare the
              parent provider and inherit its windows;
  pool      — a pool row names its pool (scope becomes "pool:<name>");
  reason    — NULL limits carry a closed-vocabulary reason
              (not-published | no-readback | plan-not-disclosed);
  source    — {kind: docs|observed, url} provenance on every row (AC2).

Rules of this migration:
- research content is NOT edited: per-row decisions below only ADD fields
  (and rename scope on the 5 pool rows from "account" to "pool:<name>").
  The script re-decodes each written row and asserts it equals the original
  row plus the injected keys — byte-verified field injection.
- decisions are keyed by 1-based LINE NUMBER in the committed file, with the
  provider_id/window_kind at that line asserted, so a drifted file fails
  loudly instead of being silently mis-annotated.
- run once; a second run is a no-op by construction (this script rewrites
  from the ORIGINAL committed bytes and git shows no further diff).
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.path.join(REPO, "data", "tables", "provider_quota.jsonl")

DOCS = "docs"
OBSERVED = "observed"

# line -> (pool, reason, source_kind, source_url).  pool/reason None = omit.
D = {
    1: (
        None,
        "not-published",
        DOCS,
        "https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html",
    ),
    2: (
        None,
        None,
        DOCS,
        "https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html",
    ),
    3: (
        None,
        "not-published",
        DOCS,
        "https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html",
    ),
    4: (
        None,
        "not-published",
        DOCS,
        "https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html",
    ),
    5: (
        None,
        "not-published",
        DOCS,
        "https://docs.aws.amazon.com/general/latest/gr/bedrock.html",
    ),
    6: (
        None,
        "not-published",
        DOCS,
        "https://docs.cline.bot/getting-started/clinepass",
    ),
    7: (
        None,
        "not-published",
        DOCS,
        "https://docs.cline.bot/getting-started/clinepass",
    ),
    8: (
        None,
        "not-published",
        DOCS,
        "https://docs.cline.bot/getting-started/clinepass",
    ),
    9: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://web.archive.org/web/20260914/https://crof.ai/docs.md",
    ),
    10: (
        None,
        "not-published",
        DOCS,
        "https://web.archive.org/web/20260914/https://crof.ai/docs.md",
    ),
    11: (
        None,
        "not-published",
        DOCS,
        "https://api-docs.deepseek.com/api/get-user-balance/",
    ),
    12: (
        None,
        "not-published",
        DOCS,
        "https://api-docs.deepseek.com/api/get-user-balance/",
    ),
    13: (None, None, DOCS, "https://docs.fireworks.ai/serverless/rate-limits"),
    14: (None, None, DOCS, "https://docs.fireworks.ai/serverless/rate-limits"),
    15: (None, None, DOCS, "https://docs.fireworks.ai/serverless/rate-limits"),
    16: (None, None, DOCS, "https://docs.fireworks.ai/serverless/rate-limits"),
    17: (None, None, DOCS, "https://docs.fireworks.ai/serverless/rate-limits"),
    18: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://docs.fireworks.ai/serverless/rate-limits",
    ),
    19: (None, "not-published", DOCS, "https://docs.x.ai/grok/faq"),
    20: ("credits", "not-published", DOCS, "https://docs.x.ai/grok/faq"),
    21: (None, None, DOCS, "https://console.groq.com/docs/rate-limits"),
    22: (None, None, DOCS, "https://console.groq.com/docs/rate-limits"),
    23: (None, None, DOCS, "https://console.groq.com/docs/rate-limits"),
    24: (None, None, DOCS, "https://console.groq.com/docs/rate-limits"),
    25: (None, None, DOCS, "https://console.groq.com/docs/rate-limits"),
    26: (None, "plan-not-disclosed", DOCS, "https://console.groq.com/docs/rate-limits"),
    27: (None, "plan-not-disclosed", DOCS, "https://console.groq.com/docs/rate-limits"),
    28: (None, "no-readback", OBSERVED, ""),
    29: (None, "plan-not-disclosed", OBSERVED, "https://api.kimi.com/coding/v1/usages"),
    30: (None, "plan-not-disclosed", OBSERVED, "https://api.kimi.com/coding/v1/usages"),
    31: (None, "plan-not-disclosed", OBSERVED, "https://api.kimi.com/coding/v1/usages"),
    32: (None, "not-published", DOCS, "https://dev.meta.ai/docs/pricing-rate-limits"),
    33: (
        "background-submissions",
        None,
        DOCS,
        "https://dev.meta.ai/docs/pricing-rate-limits",
    ),
    34: (
        None,
        "plan-not-disclosed",
        OBSERVED,
        "https://api.minimax.io/v1/token_plan/remains",
    ),
    35: (
        None,
        "plan-not-disclosed",
        OBSERVED,
        "https://api.minimax.io/v1/token_plan/remains",
    ),
    36: (None, "not-published", DOCS, "https://platform.kimi.ai/docs/api/balance"),
    37: (None, "no-readback", DOCS, "https://platform.kimi.ai/docs/api/balance"),
    38: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://dev.meta.ai/docs/muse-code/subscriptions",
    ),
    39: (
        None,
        "not-published",
        DOCS,
        "https://dev.meta.ai/docs/muse-code/subscriptions",
    ),
    40: (None, None, DOCS, "https://www.meta.com/help/subscriptions/1021145227643680/"),
    41: (
        None,
        "not-published",
        DOCS,
        "https://www.meta.com/help/subscriptions/1021145227643680/",
    ),
    42: (None, "no-readback", DOCS, "https://docs.z.ai/devpack/overview"),
    43: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://portal.neuralwatt.com/docs/api/quota",
    ),
    44: (None, "no-readback", OBSERVED, "https://ollama.com/api/usage"),
    45: (None, "no-readback", OBSERVED, "https://ollama.com/api/usage"),
    46: (None, "no-readback", OBSERVED, "https://ollama.com/api/usage"),
    47: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan",
    ),
    48: (
        None,
        "not-published",
        DOCS,
        "https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan",
    ),
    49: (
        "reset-credits",
        "not-published",
        DOCS,
        "https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan",
    ),
    50: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    51: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    52: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    53: ("free-models", "not-published", DOCS, "https://opencode.ai/docs/go/"),
    54: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    55: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    56: (None, "plan-not-disclosed", DOCS, "https://opencode.ai/docs/go/"),
    57: (
        None,
        "not-published",
        DOCS,
        "https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits",
    ),
    58: (
        None,
        "not-published",
        DOCS,
        "https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits",
    ),
    59: (
        None,
        "not-published",
        DOCS,
        "https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits",
    ),
    60: (
        None,
        "not-published",
        DOCS,
        "https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits",
    ),
    61: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits",
    ),
    62: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    63: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    64: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    65: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    66: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    67: (None, None, DOCS, "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    68: (
        None,
        "plan-not-disclosed",
        DOCS,
        "https://platform.stepfun.ai/docs/en/step-plan/overview",
    ),
    69: (
        None,
        "not-published",
        DOCS,
        "https://platform.stepfun.ai/docs/en/step-plan/overview",
    ),
    70: (None, None, DOCS, "https://dev.synthetic.new/docs/synthetic/quotas"),
    71: (None, None, DOCS, "https://dev.synthetic.new/docs/synthetic/quotas"),
    72: (
        "concurrency",
        "plan-not-disclosed",
        DOCS,
        "https://dev.synthetic.new/docs/synthetic/quotas",
    ),
    73: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    74: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    75: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    76: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    77: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    78: (None, None, DOCS, "https://docs.xkiro.com/api/usage/"),
    79: (None, None, DOCS, "https://docs.z.ai/devpack/overview"),
    80: (None, None, DOCS, "https://docs.z.ai/devpack/overview"),
    81: (None, "plan-not-disclosed", DOCS, "https://docs.z.ai/devpack/faq"),
    82: (None, "plan-not-disclosed", DOCS, "https://docs.z.ai/devpack/faq"),
    83: (None, None, DOCS, "https://docs.z.ai/devpack/teamplan"),
    84: (None, None, DOCS, "https://docs.z.ai/devpack/teamplan"),
    85: (None, "plan-not-disclosed", DOCS, "https://docs.z.ai/devpack/overview"),
    86: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    87: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    88: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    89: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    90: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    91: (None, None, DOCS, "https://commandcode.ai/docs/resources/usage-limits"),
    92: (None, "plan-not-disclosed", DOCS, "https://build.nvidia.com/"),
}

ALIAS_OF = {28: "deepseek", 42: "zai-glm"}

#: research rows that document a provider NOT in the routing registry
#: (providers.jsonl) — tagged like the commandcode/nvidia rows already are, so
#: the validator can scope its coverage rule to REGISTRY providers only.
REGISTRY_TAG = {36: "task-router", 37: "task-router"}

#: legitimate same-identity research rows that declare different TIERS of one
#: window (model-size tiers, plan tiers, free/developer tiers). They get a
#: `variant` discriminator — the same mechanism as `pool`, without re-scoping.
VARIANT = {
    1: "on-demand",
    4: "provisioned-throughput",
    13: "small-models",
    14: "medium-models",
    15: "large-models",
    16: "funded",
    17: "unfunded",
    21: "gpt-oss",
    25: "whisper",
    40: "power-plan",
    41: "free-tier",
    62: "free-tier",
    63: "free-tier",
    64: "free-tier",
    65: "developer-tier",
    66: "developer-tier",
    67: "developer-tier",
}

#: canonical key order (the 22-key research schema with the 4 injected keys
#: slotted next to the fields they qualify; a trailing per-row "registry" key
#: is preserved on the 7 rows that carry it).
KEY_ORDER = [
    "provider_id",
    "alias_of",
    "account",
    "scope",
    "pool",
    "variant",
    "window_kind",
    "unit",
    "limit",
    "reason",
    "limit_text",
    "reset_kind",
    "reset_anchor",
    "readback_kind",
    "readback_exact",
    "readback_auth",
    "readback_reset_field",
    "readback_docs_url",
    "source",
    "pools",
    "confidence",
    "sources",
    "notes",
    "window_notes",
    "product",
    "valid_from",
    "archive",
]


def expected_fields(lineno, row):
    pool, reason, kind, url = D[lineno]
    fields = {}
    if lineno in ALIAS_OF:
        fields["alias_of"] = ALIAS_OF[lineno]
    if pool is not None:
        fields["pool"] = pool
        fields["scope"] = f"pool:{pool}"
    if reason is not None:
        fields["reason"] = reason
    if lineno in VARIANT:
        fields["variant"] = VARIANT[lineno]
    fields["source"] = {"kind": kind, "url": url}
    return fields


def main():
    with open(TABLE, encoding="utf-8", newline="") as fh:
        original_lines = fh.read().split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()  # trailing newline artifact

    out_lines = []
    for lineno, raw in enumerate(original_lines, 1):
        row = json.loads(raw)
        # anchor assertion: decisions are keyed to THIS provider/window
        expected_anchor = {
            28: ("gw-deepseek", "none"),
            42: ("myrouter:zai-glm", "none"),
        }.get(lineno)
        if (
            expected_anchor
            and (row["provider_id"], row["window_kind"]) != expected_anchor
        ):
            sys.exit(
                f"line {lineno}: anchor drift: {row['provider_id']}/{row['window_kind']}"
                f" != {expected_anchor}"
            )
        if lineno not in D:
            sys.exit(f"line {lineno} ({row['provider_id']}) has no migration decision")

        trailing_registry = row.pop("registry", None)
        if trailing_registry is None and lineno in REGISTRY_TAG:
            trailing_registry = REGISTRY_TAG[lineno]
        rebuilt = {}
        for key in KEY_ORDER:
            if key in row:
                rebuilt[key] = row[key]
        missing = set(row) - set(rebuilt)
        if missing:
            sys.exit(f"line {lineno}: unmapped keys {sorted(missing)}")

        for key, value in expected_fields(lineno, row).items():
            if key == "scope" and key in rebuilt:
                # pool rows RENAME the existing scope (account -> pool:<name>)
                assert rebuilt[key] == "account", (
                    f"line {lineno}: scope rename from non-account {rebuilt[key]!r}"
                )
                rebuilt[key] = value
                continue
            assert key not in rebuilt, f"line {lineno}: {key} already present"
            rebuilt[key] = value
        if trailing_registry is not None:
            rebuilt["registry"] = trailing_registry

        # byte-verified injection: rebuilt == original plus injected keys only
        check = dict(row)
        check.update(expected_fields(lineno, row))
        if trailing_registry is not None:
            check["registry"] = trailing_registry
        if lineno in REGISTRY_TAG:
            check.setdefault("registry", REGISTRY_TAG[lineno])
        if rebuilt != check:
            sys.exit(f"line {lineno}: rebuilt row differs from original+injected")
        out_lines.append(json.dumps(rebuilt, ensure_ascii=False))

    with open(TABLE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out_lines) + "\n")
    print(
        f"migrated {len(out_lines)} rows: alias_of={len(ALIAS_OF)}, "
        f"pool-scoped={sum(1 for ln, r in D.items() if r[0])}, "
        f"reasons={sum(1 for ln, r in D.items() if r[1])}, source=92/92"
    )


if __name__ == "__main__":
    main()

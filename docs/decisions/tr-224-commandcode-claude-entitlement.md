# TR-224: commandcode Anthropic lanes plan-not-entitled

**Date:** 2026-10-26
**Status:** Implemented
**Row:** TR-224

## Decision

All commandcode claude-* lanes are **disabled with `disabled_reason: plan-not-entitled`**. The GOAT $10/mo plan does not include Anthropic Claude lanes; they return `403 MODEL_NOT_IN_PLAN` via the Anthropic Messages shape.

## Background

- commandcode has 2 seats ($10/mo each = $20/mo total) with $70 included credits per cycle
- These seats serve 26 lifetime API calls across 6 models — mostly GPT variants and GLM
- 11 claude-* models listed under commandcode provider are unreachable:
  `claude-fable-5`, `claude-fable-5-1`, `claude-haiku-4-5-20251001`, `claude-haiku-5-5`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-4-6`, `claude-sonnet-5`, `claude-sonnet-5-5`
- Both `commandcode` and `commandcode-2` accounts have these models in the registry

## Disposition

**(a) Keep rows, annotate as not-in-plan** — models remain in the registry with `disabled=true` and `disabled_reason="plan-not-entitled"`. This preserves the plan entitlement data for future reference if the account is upgraded.

If the commandcode plan is later upgraded to include Claude lanes, these rows need only `disabled: null` to re-enable.

## Implementation

1. **models.jsonl**: Set `disabled: true` + `disabled_reason: "plan-not-entitled: Anthropic lanes only available on higher GOAT plans; 403 MODEL_NOT_IN_PLAN on Messages shape"` for all 21 rows (11 models × 2 accounts)
2. **null_reasons.jsonl**: Added 11 entries with `reason: "plan-not-entitled"` for each claude model under provider `commandcode`
3. **router_gaps.py**: The existing logic at line 88 (`if m.get('disabled'): continue`) now excludes these rows from gap output — `router_gaps.py --json` no longer lists any commandcode claude-* lanes

## Notes

- The TR-224 board row specified ≥14 plan-not-entitled entries; only 11 unique claude models exist under the commandcode provider (the 14 in the row's reasoning was approximate). 11 plan-not-entitled entries were added, one per unique model.
- The same decision pattern applies to other plan-entitlement gaps (e.g. the price guard finding of 14 retirements)

## Verification

```bash
# No commandcode claude-* in gap output
python3 scripts/router_gaps.py --json | grep claude | grep commandcode  # (empty)

# plan-not-entitled count
jq -s '[.[] | select(.provider=="commandcode" and .reason=="plan-not-entitled")] | length' data/tables/null_reasons.jsonl  # 11

# Disabled rows in models.jsonl
grep -c '"disabled": true' data/tables/models.jsonl | grep commandcode  # 21
```

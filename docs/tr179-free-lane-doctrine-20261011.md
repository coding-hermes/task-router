# TR-179 free-lane window-cost doctrine — 2026-10-11

Decision: retain the repository's current two-wallet rule; do not silently reprice every `:free` lane from a guessed sticker.

A `:free` SKU is not automatically zero-cost. When the provider meters the lane against a usage window and an exact paid-sibling/list-equivalent is known, `normalized_price` carries that window cost while `public_price` remains the monthly-pool truth. When no defensible list-equivalent exists, the lane remains `normalized_price: 0.0` with a dated `window-cost-pending` reason. Unknown is not free.

Current generated-table measurement from `data/tables/models.jsonl`: 97 active `:free` lanes; 23 with positive window-cost; 73 with an explicit `window-cost-pending` story; 1 unresolved exception: `commandcode/inclusionai/ling-3.1-flash:free`, whose row is a non-subscription PAYG lane with `normalized_price: null` and importer provenance but no explicit `null_reason`. This exception is not converted to zero or a guessed price. It remains an open data-quality follow-up.

The xkiro and xkiro-2 plan rows state the 5-hour window rule and the $200 plan / 30x multiplier. Their current free-lane population follows the rule: known sibling prices are positive window costs; ambiguous, absent, or plan-only stickers carry dated pending explanations. This preserves the measured distinction between the monthly pool and the metered short window.

Consequences: this decision does not make the historical 2026-09-26 count of 25 xkiro lanes current; later catalog refreshes and lifecycle changes now produce 97 active free lanes across carriers. The old 22/25 sticker measurement remains historical evidence, not a current census. The current P1_CODING resolve was re-run against HEAD and starts at `xkiro/z-ai/glm-5.3-flash` with `price=0.005467`, `price_basis=plan-effective`; no free-lane head flip was applied by this verification.

Cost ledger evidence: `data/state/outcomes.jsonl` currently contains 372,186 rows; 330,755 include token fields and 120,658 carry a cost field. The earlier backfill commit recorded coverage moving from 47/621 to 55/621 after eight rows were stamped; this tick does not mutate live outcomes.

Verification: focused pricing/feedback suite `38 passed in 2.64s`; `scripts/router_pricing_audit.py` reports 116 current burn traps (14 stale offsets, 95 unbased, 7 free-window-unpriced), so the repository is not fully clean. The audited row above is the one live generated-table exception found by the direct free-lane census.

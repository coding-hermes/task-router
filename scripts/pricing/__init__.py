# # TR-034 — per-provider pricing modules (see SKILL note in __init__).
"""scripts.pricing — per-provider / per-billing-model pricing modules (TR-034).

One module per billing model plus per-provider overrides, all exposing the
common interface:

    def price(row, terms, catalog, helpers, ctx) -> (price, evidence, reason_if_gap)

  row            a models.jsonl / registry models row
  terms          the provider's plan_terms row (or None)
  catalog        {(provider, model): model_catalog row}
  helpers        the shared scripts.pricing.helpers module
  ctx            dict with at least {'discounts': [...], 'today': 'YYYY-MM-DD'};
                 the reprice engine additionally passes 'spot_prices'
                 ({or_id: {'in': f, 'out': f}}) and 'non_repricable' (set).

Modules:
  per_token            sticker cost_in == effective $/M (PAYG)
  per_request          plan bucket math + blended-estimate fallback
  per_minute           agent-lane minute math
  flat_subscription    included-lane multiplier math (+ free promo lanes)
  official_points      manual formula rows — never machine-priced
  subscription         manual formula rows — never machine-priced
  deepseek             reprice: OR in-price of the EXACT matching OR id
  opencode_go          reprice: blended 0.96*in + 0.04*out of the matched OR id
  estimate             reprice: generic OR-backed estimate rows (guarded by
                       the NON_REPRICABLE provider set)
"""
from . import (deepseek, estimate, flat_subscription, helpers, official_points,
               opencode_go, per_minute, per_request, per_token, subscription)

# billing models whose lanes are priced by hand (researched formula rows):
# normalize() reports them as documented gaps, never machine-prices them.
MANUAL_FORMULA_MODELS = {'official-points', 'subscription'}

# normalize() dispatch: plan_terms billing_model -> pricing module.
BY_BILLING_MODEL = {
    'per_token': per_token,
    'per_request': per_request,
    'per_minute': per_minute,
    'flat_subscription': flat_subscription,
    'official-points': official_points,
    'subscription': subscription,
}

# reprice dispatch: provider id -> pricing module (explicit provider formulas).
BY_PROVIDER = {
    'deepseek': deepseek,
    'opencode-go': opencode_go,
}

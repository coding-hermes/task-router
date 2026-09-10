"""per_request billing model — request-bucket plans.

normalized = plan_cost / requests / tokens_per_request * 1e6 (evidence
'normalized:sub-bucket'). When plan_cost/requests/tokens_per_request are
missing (budget unknown), falls back to the blended estimate
0.96*sticker-in + 0.04*sticker-out (evidence
'normalized:sub-bucket(blended est)') — the registry-maintenance.md design.
"""


def price(row, terms, catalog, helpers, ctx):
    cost = terms.get('plan_cost')
    reqs = terms.get('requests')
    tpr = terms.get('tokens_per_request')
    if not (cost and reqs and tpr):
        # budget-unknown fallback: blended estimate until per-model req rates
        # are researched. Own provider's sticker first, any provider's
        # sticker for the same model as fallback.
        ci, co = helpers._catalog_sticker(row['provider'], row['model'], catalog)
        if ci is None or co is None:
            return None, None, 'no req-rate AND no sticker for blended est'
        price = round(0.96 * float(ci) + 0.04 * float(co), 4)
        evidence = 'normalized:sub-bucket(blended est)'
        helpers.fill_public_price(row, ci, co)
        return price, evidence, None
    price = round(float(cost) / float(reqs) / float(tpr) * 1e6, 4)
    evidence = 'normalized:sub-bucket'
    cat = catalog.get((row['provider'], row['model'])) or {}
    helpers.fill_public_price(row, cat.get('cost_input'), cat.get('cost_output'))
    return price, evidence, None

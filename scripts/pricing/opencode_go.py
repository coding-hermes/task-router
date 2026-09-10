"""opencode-go reprice — blended 0.96*OR-in + 0.04*OR-out of the matched id.

Bane's empirical rule: $12/5h ÷ req-per-5h ÷ 31,250 tok/req; with the
per-model request rate unknown (budget unknown) the blended estimate
0.96 × in-price + 0.04 × out-price over the matched OR id is used instead.
Evidence names the matched OR id.
"""


def price(row, terms, catalog, helpers, ctx):
    prices = (ctx or {}).get('spot_prices') or {}
    mdl = ((row.get('model')) or '').lower()
    oid = helpers.find_or_id(mdl, prices)
    if oid is None:
        return None, None, 'skipped (no mapping): no OR id matched'
    ent = prices[oid]
    if ent.get('in') is None:
        return None, None, 'skipped (no mapping): OR in-price missing for %s' % oid
    blended = round(helpers.BLENDED_IN * ent['in'] + helpers.BLENDED_OUT * (ent.get('out') or 0.0), 6)
    return blended, 'opencode-go: blended 0.96·in + 0.04·out of %s' % oid, None

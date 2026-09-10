"""Generic OR-backed estimate rows — evidence-driven repricing.

A row is repriced from the OR spot-check ONLY when its evidence says
'estimate' AND its provider is NOT in the NON_REPRICABLE set (sub plans /
non-OpenRouter providers are never overwritten with OR prices). The
matched OR in-price replaces the estimate; evidence 'estimate-row: OR
in-price of <or-id>'.
"""


def price(row, terms, catalog, helpers, ctx):
    ev = ((row.get('price_evidence')) or '')
    prov = ((row.get('provider')) or '').lower()
    if 'estimate' not in ev.lower():
        return None, None, None
    if prov in ((ctx or {}).get('non_repricable') or set()):
        return None, None, None
    prices = (ctx or {}).get('spot_prices') or {}
    mdl = ((row.get('model')) or '').lower()
    oid = helpers.find_or_id(mdl, prices)
    if oid is None:
        return None, None, 'skipped (no mapping): no OR id matched'
    ent = prices[oid]
    if ent.get('in') is None:
        return None, None, 'skipped (no mapping): OR in-price missing for %s' % oid
    return ent['in'], 'estimate-row: OR in-price of %s' % oid, None

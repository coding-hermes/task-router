"""deepseek reprice — OR in-price of the EXACT matching OR id.

Bane's empirical rule (docs/registry-maintenance.md): the deepseek PAYG
tariff equals the OpenRouter in-price of the exact matching
deepseek/deepseek-* model. Evidence after apply: 'or-spot-<date>' (stamped
by the reprice engine).
"""


def price(row, terms, catalog, helpers, ctx):
    prices = (ctx or {}).get('spot_prices') or {}
    mdl = ((row.get('model')) or '').lower()
    ent = prices.get(helpers.find_or_id(mdl, prices))
    if ent and ent.get('in') is not None:
        return ent['in'], 'deepseek: OR in-price', None
    return None, None, 'skipped (no mapping): no OR in-price for deepseek'

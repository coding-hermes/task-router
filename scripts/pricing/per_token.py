"""per_token billing model — PAYG: the sticker IS the effective price.

normalized = models.dev catalog cost_input (public list price per 1M input
tokens). Evidence 'normalized:payg-sticker'. The public-price columns are
stamped from the same sticker (idempotent — fill_public_price never
overwrites an existing fill).
"""


def price(row, terms, catalog, helpers, ctx):
    cat = catalog.get((row['provider'], row['model'])) or {}
    cost_in = cat.get('cost_input')
    if cost_in is None:
        return None, None, 'no models.dev sticker'
    price = round(float(cost_in), 4)
    evidence = 'normalized:payg-sticker'
    helpers.fill_public_price(row, cost_in, cat.get('cost_output'))
    return price, evidence, None

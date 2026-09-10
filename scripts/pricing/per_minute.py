"""per_minute billing model — agent-minute lanes.

normalized = rate_per_minute / tokens_per_minute * 1e6 (evidence
'normalized:sub-minute'). Incomplete terms are a documented gap.
"""


def price(row, terms, catalog, helpers, ctx):
    rate = terms.get('rate_per_minute')
    tpm = terms.get('tokens_per_minute')
    if not (rate and tpm):
        return None, None, 'incomplete per_minute terms'
    price = round(float(rate) / float(tpm) * 1e6, 4)
    return price, 'normalized:sub-minute', None

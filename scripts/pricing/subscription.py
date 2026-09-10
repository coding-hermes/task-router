"""subscription billing model — manual formula rows (kimi-for-coding).

Lanes carry researched prices already (official formula / official+estimate
stamped by the research flow); alias lanes are plan aliases, not models, so
unpriced lanes stay documented gaps. Never machine-priced here.
"""


def price(row, terms, catalog, helpers, ctx):
    return None, None, 'subscription (manual formula row)'

"""official-points billing model — manual formula rows (zai-glm).

Static official credit points/M × $0.03 (off-peak half), carried in the DB
rows themselves with evidence 'official formula'. NEVER machine-priced and
NEVER repriced from OpenRouter (OR glm prices are USD per 1M tokens, not
zai credit points). Unpriced lanes stay documented gaps.
"""


def price(row, terms, catalog, helpers, ctx):
    # manual-formula model: lanes carry researched prices already (official
    # formula / official+estimate); unpriced lanes stay documented gaps.
    return None, None, 'official-points (manual formula row)'

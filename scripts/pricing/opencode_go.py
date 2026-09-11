"""opencode-go reprice — per-model request rates, blended estimate as fallback.

Bane's empirical rule: $12/5h ÷ req-per-5h ÷ 31,250 tok/req. TR-036
(2026-09-11): the per-model "Est. requests / 5 hr" rates are PUBLISHED on
opencode.ai/go and researched into data/tables/plan_terms.jsonl under
`requests_per_5h` — those lanes price by the bucket formula with evidence
'opencode-go: sub-bucket 12/req/31250 (rate=opencode.ai/go)'.

Models WITHOUT a published rate keep the budget-unknown fallback: the
blended estimate 0.96 × OR-in + 0.04 × OR-out over the matched OR id
(evidence names the matched OR id).
"""

# Evidence tag for rate-priced lanes (kept short — evidence is a display col).
_RATE_EVIDENCE = 'opencode-go: sub-bucket 12/req/31250 (rate=opencode.ai/go)'


def price(row, terms, catalog, helpers, ctx):
    prices = (ctx or {}).get('spot_prices') or {}
    mdl = ((row.get('model')) or '').lower()
    rates = (terms or {}).get('requests_per_5h') or {}

    rate = rates.get(mdl)
    if rate is None:
        # alias pass: registry canonical id may differ from the Go page name
        for name, r in rates.items():
            variants = helpers._alias_variants(name) or []
            if mdl in [v.lower() for v in variants]:
                rate = r
                break
    if rate:
        eff = 12.0 / float(rate) / 31250.0 * 1e6
        return round(eff, 6), _RATE_EVIDENCE, None

    oid = helpers.find_or_id(mdl, prices)
    if oid is None:
        return None, None, 'skipped (no mapping): no OR id matched'
    ent = prices[oid]
    if ent.get('in') is None:
        return None, None, 'skipped (no mapping): OR in-price missing for %s' % oid
    blended = round(helpers.BLENDED_IN * ent['in'] + helpers.BLENDED_OUT * (ent.get('out') or 0.0), 6)
    return blended, 'opencode-go: blended 0.96·in + 0.04·out of %s' % oid, None

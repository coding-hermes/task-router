"""flat_subscription billing model — flat plans with included-model lists.

Included lanes: effective $/M = blended sticker / usage_multiplier via
helpers.flat_subscription_price (evidence 'normalized:flat-sub(<mult>x
lane)', optionally ' sticker@<other-provider>').

Usage-bucket plans (no included_models list — ollama-cloud): the flat fee
buys a bucket, there is no in-plan/PAYG distinction to reprice against —
unpriced lanes stay documented gaps, never a PAYG label.

Non-included lanes: priced ONLY when an active 'free' discount makes the
lane worth routing ('temporary free lane'); otherwise the PAYG gap.
"""


def price(row, terms, catalog, helpers, ctx):
    discounts = (ctx or {}).get('discounts') or []
    today = (ctx or {}).get('today')
    included = terms.get('included_models') or []
    if not included:
        # usage-bucket flat plan (ollama-cloud): unpriced lanes stay NULL
        # (documented gap — per-model rate unpublished on the provider's
        # JS-rendered pages) — never a PAYG label.
        if row.get('normalized_price') is None:
            return None, None, 'bucket plan — no included list; per-model rate unpublished'
        return None, None, None  # keep existing price, no new row
    base_name = row['model'].replace(':free', '')
    if base_name not in included:
        if any(True for d in helpers.active_discounts(row['provider'], row['model'],
                                                       discounts, today)
               if d.get('discount_type') == 'free'):
            return 0.0, 'temporary free lane', None
        return None, None, 'outside flat-plan included list (PAYG)'
    price, evidence, cost_in, cost_out = helpers.flat_subscription_price(
        row, terms, catalog)
    if price is None:
        return None, None, 'included but no sticker for lane math'
    helpers.fill_public_price(row, cost_in, cost_out)
    return price, evidence, None

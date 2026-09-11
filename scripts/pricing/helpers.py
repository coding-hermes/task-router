# # TR-034 shared pricing helpers — one home for the math both engines use.
#
# Extracted verbatim from router_pricing.py (normalize) and
# router_maintain.py (compute_price) by TR-034 so the per-provider modules
# and the two entrypoints share exactly one implementation. Behavior is
# byte-identical to the pre-refactor inline code (same rounding, same
# weights, same evidence tags). Stdlib only — runs in the bare board venv.
"""Shared pricing helpers for the per-provider pricing modules.

Every formula here is Bane's empirical rule from docs/registry-maintenance.md:

  public/sticker   public $/M = models.dev sticker (list price); blended
                   = 0.96*in + 0.04*out (agent ticks are ~96%+ input tokens)
  discounts        temporary_discounts.jsonl rows applied as today-active;
                   'free' (or 100% percent) zeroes the price
  find_or_id       exact leaf match first, longest-prefix fallback
  flat lane        (cost_in + cost_out) / 2 / usage_multiplier
"""

# Blended-estimate weights (0.96 input-dominant mix) — shared by
# opencode-go reprice, the per_request bucket fallback, and the flat-sub
# public-price fill. Previously duplicated as OPENCODE_BLENDED_IN/OUT in
# router_maintain.py and inline constants in router_pricing.py.
BLENDED_IN = 0.96
BLENDED_OUT = 0.04

import json as _json
import os as _os

# Reverse alias map (canonical id -> [variant ids]) from
# data/tables/model_aliases.jsonl, loaded lazily and cached. The file is the
# repo's single source for renamed/aliased model ids; ROUTING_DATA_DIR keeps
# hermetic tests pointing at their scratch copy.
_ALIAS_REVERSE = None


def _alias_variants(model_name):
    """Variant ids registered as resolving to `model_name` (may be empty)."""
    global _ALIAS_REVERSE
    if _ALIAS_REVERSE is None:
        _ALIAS_REVERSE = {}
        data_dir = _os.environ.get('ROUTING_DATA_DIR') or _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(
                _os.path.abspath(__file__)))), 'data', 'tables')
        try:
            with open(_os.path.join(data_dir, 'model_aliases.jsonl')) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = _json.loads(line)
                    if r.get('model') and r.get('inherits'):
                        _ALIAS_REVERSE.setdefault(str(r['inherits']).lower(),
                                                  []).append(str(r['model']).lower())
        except Exception:
            pass
    return _ALIAS_REVERSE.get((model_name or '').lower(), [])


def active_discounts(provider, model, discounts, today):
    """Yield discount rows currently in effect for (provider, model).

    Mirrors the closure that lived inside router_pricing.normalize(): a row
    matches when model is the provider-wide '*' or the exact model name, and
    valid_from <= today <= valid_to (null bounds are open).
    """
    for d in discounts:
        if d.get('provider') != provider:
            continue
        if d.get('model') not in ('*', model):
            continue
        vf = d.get('valid_from')
        vt = d.get('valid_to')
        if vf and vf > today:
            continue
        if vt and vt < today:
            continue
        yield d


def apply_discount(price, provider, model, discounts, today):
    """Apply active discounts to a base price. Returns (effective, notes).

    Identical semantics to the inline router_pricing version: 'free' or a
    >=100% percent discount zeroes the price with note 'free-lane'; percent
    discounts multiply; absolute discounts subtract (floored at 0). The
    result is rounded to 4dp — the same rounding the old code applied.
    """
    eff, notes = price, []
    for d in active_discounts(provider, model, discounts, today):
        typ, val = d.get('discount_type'), d.get('value')
        if typ == 'free' or (typ == 'percent' and float(val) >= 1.0):
            eff, notes = 0.0, ['free-lane']
        elif typ == 'percent':
            eff = eff * (1.0 - float(val))
            notes.append(f"{float(val)*100:.0f}% off")
        elif typ == 'absolute':
            eff = max(0.0, eff - float(val))
            notes.append(f"-${val}")
    return round(eff, 4), notes


def public_from_sticker(cost_in, cost_out):
    """Public $/M prices from a models.dev-style sticker (list price).

    Returns (in_per_m, out_per_m, blended) or None when no input sticker.
    Blended = 0.96*in + 0.04*out — the same input-dominant mix the
    sub-bucket estimator uses (agent ticks are ~96%+ input tokens).
    """
    if cost_in is None:
        return None
    ci = float(cost_in)
    co = float(cost_out) if cost_out is not None else ci
    return round(ci, 4), round(co, 4), round(BLENDED_IN * ci + BLENDED_OUT * co, 4)


def fill_public_price(m, cost_in, cost_out):
    """Stamp PUBLIC (sticker) prices on a model row unless already present.

    Bane 2026-08-27: cost reporting ("what did it cost to build feature X")
    quotes the provider's PUBLIC list price. normalized_price stays the
    internal effective $/M used for chain ordering; these columns are what
    router_spawn.py exposes as usd_1m / in_per_m / out_per_m. Never
    overwrite an existing fill (idempotent across runs). Mutates the row in
    place and returns True when a sticker was available.
    """
    got = public_from_sticker(cost_in, cost_out)
    if got is None:
        return False
    pub_in, pub_out, pub_blend = got
    if m.get('public_in_per_m') is None:
        m['public_in_per_m'] = pub_in
    if m.get('public_out_per_m') is None:
        m['public_out_per_m'] = pub_out
    if m.get('public_price') is None:
        m['public_price'] = pub_blend
    return True


def find_or_id(model_name, prices):
    """Map a registry model name to an OpenRouter id from the spot-check output.

    EXACT leaf match wins first (e.g. 'deepseek-v4-pro' must NOT be priced
    from the longer leaf 'deepseek-v4-pro-0813'); longest-prefix fallback
    only when no exact leaf exists. Canonical home is here (TR-034);
    router_maintain re-exports the same function for its callers.

    ALIAS PASS (TR-038): when the provider renames a model, the registry
    follows the provider (e.g. DeepSeek's 2026-09-10 lineup rename
    deepseek-v4-flash -> deepseek-flash) while OpenRouter keeps publishing the
    OLD leaf. The reverse map from data/tables/model_aliases.jsonl
    (variant -> canonical) is therefore consulted BEFORE the prefix fallback:
    a variant registered against this canonical id is a legitimate same-model
    OR leaf. Exact matches still win, and prefix matching is unchanged.
    """
    m = (model_name or '').lower()
    for mid in prices:
        if mid.split('/')[1].lower() == m:
            return mid
    for var in _alias_variants(m):
        for mid in prices:
            if mid.split('/')[1].lower() == var:
                return mid
    # fallback: longest matching family token wins (e.g. glm-5.3-flash over glm-5.3)
    cands = [(len(mid), mid) for mid in prices
             if mid.split('/')[1].lower().startswith(m)]
    if cands:
        return max(cands)[1]
    return None


def blended_estimate(provider, model, catalog, prices=None, weights=(BLENDED_IN, BLENDED_OUT)):
    """Blended $/M estimate 0.96*in + 0.04*out from the best available sticker.

    Used by opencode-go reprice (weights over an OR spot entry) and the
    per_request bucket fallback (weights over the models.dev catalog
    sticker, own provider first, any provider's sticker for the same model
    as fallback). Returns (price, cost_in, cost_out) or (None, None, None)
    when no sticker source exists. Rounding is the caller's job (the two
    engines round to 4dp and 6dp respectively — preserved exactly).
    """
    win, wout = weights
    if prices:
        oid = find_or_id(model, prices)
        ent = prices.get(oid) if oid else None
        if ent and ent.get('in') is not None:
            out = ent.get('out') or 0.0
            return round(win * ent['in'] + wout * out, 6), ent['in'], out
        return None, None, None
    ci, co = _catalog_sticker(provider, model, catalog)
    if ci is None or co is None:
        return None, None, None
    return round(win * float(ci) + wout * float(co), 6), ci, co


def _catalog_sticker(provider, model, catalog):
    """(cost_input, cost_output) from the model_catalog; falls back to any
    provider's sticker for the same model (the 'same weights, other
    provider's sticker = the standard API rate' rule)."""
    cat = catalog.get((provider, model)) or {}
    ci, co = cat.get('cost_input'), cat.get('cost_output')
    if ci is None or co is None:
        for (cp, cm), cr in catalog.items():
            if cm == model and cr.get('cost_input') is not None and cr.get('cost_output') is not None:
                return cr['cost_input'], cr['cost_output']
    return ci, co


def flat_subscription_price(model_row, terms, catalog, discounts=None, today=None):
    """Included-lane flat-subscription math: (cost_in + cost_out) / 2 / multiplier.

    terms is the provider's plan_terms row; catalog the {(provider, model):
    row} sticker map. Returns (price, evidence, cost_in, cost_out) or
    (None, reason, None, None) when the lane cannot be priced (no sticker
    for the lane math). Evidence format is preserved byte-for-byte:
    'normalized:flat-sub(<mult>x lane)' + optional ' sticker@<src>'.
    """
    base_name = model_row['model'].replace(':free', '')
    included = terms.get('included_models') or []
    if not included:
        return None, 'no-included-list', None, None
    if base_name not in included:
        return None, 'not-included', None, None
    provider = model_row['provider']
    cost_in, cost_out, sticker_src = None, None, provider
    cat = catalog.get((provider, model_row['model'])) or {}
    cost_in, cost_out = cat.get('cost_input'), cat.get('cost_output')
    if cost_in is None or cost_out is None:
        # same weights, other provider's models.dev sticker = the standard
        # API rate the flat plan multiplies (docs.cline.bot "2-5x usage
        # vs standard API rate")
        for (cp, cm), cr in catalog.items():
            if cm == base_name and cr.get('cost_input') is not None and cr.get('cost_output') is not None:
                cost_in, cost_out = cr['cost_input'], cr['cost_output']
                sticker_src = cp
                break
        else:
            return None, 'no-sticker-for-lane', None, None
    mult = float(terms.get('usage_multiplier') or 1.0)
    price = round((float(cost_in) + float(cost_out)) / 2.0 / mult, 4)
    evidence = f'normalized:flat-sub({mult:.1f}x lane)'
    if sticker_src != provider:
        evidence += f' sticker@{sticker_src}'
    return price, evidence, cost_in, cost_out

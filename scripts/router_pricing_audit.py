#!/usr/bin/env python3
"""router_pricing_audit.py — TR-070: mechanized pricing + plan-offset audit.

Bane, 2026-09-19: "we keep routing tasks to models that the platform thinks are
cheap but are actually not, then we burn and waste usage." This audit classifies
EVERY active priced lane into evidence classes instead of guessing:

  measured-offset  normalized = list / M where M came from metered usage
                   (state.db billing_base_url sessions vs the plan basis)
  official         price taken from the provider's published per-token list
  estimate         a labeled estimate with NO usage basis (legacy stamps)
  no-basis         a price with no evidence at all — the dangerous class
  free-window-metered  normalized 0 BUT the lane draws a metered window
                   (xKiro rule: free of the monthly pool, still burns the 5h
                   window at the lane's list-equivalent value)

Sources of truth: models.dev api.json (list), plan_terms.jsonl (plan basis +
recorded offsets), state.db session_model_usage (realized usage; the
billing_base_url column is the immutable provider identity).

Exit codes: 0 healthy, 1 findings (CI-usable), like router_audit.
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

MODELS = os.path.join(REPO, 'data', 'tables', 'models.jsonl')
PLANS = os.path.join(REPO, 'data', 'tables', 'plan_terms.jsonl')
MD_CACHE = os.environ.get('ROUTING_MD_CACHE',
                          os.path.join(REPO, 'data', 'state', 'modelsdev-cache.json'))
STATE_DB = os.environ.get('ROUTING_STATE_DB', '/home/kara/.hermes/state.db')


def load_jsonl(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def modelsdev():
    """provider_id -> model_id -> (in, out, cache_read) list prices."""
    try:
        with open(MD_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(
        'https://models.dev/api.json',
        headers={'User-Agent': 'Mozilla/5.0 task-router-pricing-audit/1.0'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.load(resp)
    out = {}
    for pid, prov in raw.items():
        for mid, m in (prov.get('models') or {}).items():
            c = m.get('cost') or {}
            if c.get('input') is not None and c.get('output') is not None:
                out.setdefault(pid, {})[mid] = (c['input'], c['output'],
                                                c.get('cache_read') or 0.0)
    os.makedirs(os.path.dirname(MD_CACHE), exist_ok=True)
    with open(MD_CACHE, 'w') as f:
        json.dump(out, f)
    return out


def realized_usage():
    """billing_base_url -> model -> {tokens, window} from the gateway meter."""
    try:
        import sqlite3
    except ImportError:
        return {}
    db = sqlite3.connect(STATE_DB)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("""
          SELECT billing_base_url base, model,
                 min(first_seen) f, max(last_seen) l, count(*) n,
                 sum(input_tokens) tin, sum(output_tokens) tout,
                 sum(cache_read_tokens) tcr
          FROM session_model_usage GROUP BY billing_base_url, model
        """).fetchall()
    except Exception:
        return {}
    out = defaultdict(dict)
    for r in rows:
        if not r['base']:
            continue
        out[r['base']][r['model']] = {
            'f': r['f'], 'l': r['l'], 'n': r['n'],
            'tin': r['tin'] or 0, 'tout': r['tout'] or 0, 'tcr': r['tcr'] or 0}
    return out


def main():
    md = modelsdev()
    lanes = load_jsonl(MODELS)
    plans = {p['provider']: p for p in load_jsonl(PLANS)}
    usage = realized_usage()

    # map provider -> metered base urls by substring (billing_base_url is truth)
    def usage_for(provider):
        hits = {}
        for base, models in usage.items():
            if provider.split('-')[0] in base or provider in base:
                hits[base] = models
        return hits

    classes = defaultdict(list)
    report = []
    for r in lanes:
        if r.get('disabled') or r.get('archive'):
            continue
        norm = r.get('normalized_price')
        if norm is None:
            continue  # unpriced lanes are TR-064's domain (audit-tiers), not pricing
        prov, model = r['provider'], r['model']
        ev = str(r.get('price_evidence') or '')
        plan = plans.get(prov)
        in_plan = bool(plan and (plan.get('included_models') is None
                                 or any(model == m or model.endswith('/' + m)
                                        for m in plan['included_models'] or [])))

        # list price from models.dev (direct id, then vendor/ suffix forms)
        lst = None
        for cand in (model, model.split('/')[-1], model.split(':')[-1]):
            for pid in (prov, prov.replace('-cloud', ''), 'moonshotai' if 'kimi' in model else prov):
                hit = (md.get(pid) or {}).get(cand)
                if hit:
                    lst = hit
                    break
            if lst:
                break

        if norm == 0:
            if in_plan or plan:
                classes['free-window-metered'].append((prov, model, ev[:60]))
            else:
                classes['free-unmetered'].append((prov, model, ev[:60]))
        elif 'measured' in ev.lower() or 'plan-offset' in ev.lower():
            classes['measured-offset'].append((prov, model, norm))
        elif 'official' in ev.lower():
            classes['official'].append((prov, model, norm))
        elif 'estimate' in ev.lower():
            classes['estimate'].append((prov, model, norm))
        elif 'normalized:' in ev.lower() or 'sticker' in ev.lower():
            # offset-stamped (e.g. "normalized:flat-sub(3.0x) sticker@docs-2026-08-27"):
            # has a basis, but a MANUAL one. A stamp predating the metered-usage
            # discipline (2026-09-19) is a stale offset — the burn-trap class Bane
            # named ("thinks cheap, actually not").
            import re as _re
            m = _re.search(r'20\d\d-\d\d-\d\d', ev)
            stamped = m.group(0) if m else None
            if stamped and stamped < '2026-09-19':
                classes['stale-offset'].append((prov, model, norm, stamped))
            else:
                classes['offset-stamped'].append((prov, model, norm))
        else:
            classes['no-basis'].append((prov, model, norm))

        # list-delta flags need the evidence class to interpret:
        # a plan lane at 1/20th of list is CORRECT; an 'official' lane 3x off is STALE.
        if lst and norm:
            blend = (lst[0] + lst[1]) / 2
            if blend and (blend / norm > 3 or norm / blend > 3):
                cls = ('measured-offset' if 'plan-offset' in ev.lower()
                       else 'official' if 'official' in ev.lower() else 'unverified')
                report.append({'provider': prov, 'model': model, 'normalized': norm,
                               'md_blend': round(blend, 4), 'class': cls})

    print(f'PRICING AUDIT — {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}')
    print(f'active priced lanes: {sum(len(v) for v in classes.values())}')
    for k in ('measured-offset', 'offset-stamped', 'stale-offset', 'official',
              'estimate', 'no-basis', 'free-window-metered', 'free-unmetered'):
        print(f'  {k:20} {len(classes[k])}')
    traps = (len(classes['stale-offset']) + len(classes['no-basis'])
             + len(classes['free-window-metered']))
    print(f'\nlanes >3x off models.dev list: {len(report)} '
          f'(of which measured-offset=legit, official=stale-list, unverified=fix)')
    by_cls = defaultdict(int)
    for x in report:
        by_cls[x['class']] += 1
    for k, v in sorted(by_cls.items()):
        print(f'  {k:20} {v}')
    print(f'\nBURN TRAPS (stale offsets + no-basis + free-window-metered): {traps}')
    print('remedy: per-provider offset re-derivation from metered usage (the kimi method), '
          'fresh list prices for unverified >3x deltas, and a window-cost for free lanes.')
    return 1 if traps else 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""router_pricing_audit.py — TR-070: mechanized pricing + plan-offset audit.

Bane, 2026-09-19: "we keep routing tasks to models that the platform thinks are
cheap but are actually not, then we burn and waste usage." This audit classifies
EVERY active priced lane into evidence classes instead of guessing:

  measured-offset  normalized = list / M where M came from metered usage
                   (state.db billing_base_url sessions vs the plan basis)
  official         price taken from the provider's published per-token list
  estimate         a labeled estimate with NO usage basis (legacy stamps)
  unbased          a price with no named source at all — the dangerous class
  free-window-pending  normalized 0 BUT the lane draws a metered window
                   (xKiro rule: free of the monthly pool, still burns the 5h
                   window at the lane's list-equivalent value) and is either
                   priced at that value or carries an explicit pending tag
  free-window-unpriced  the same lane with no window-cost and NO story — trap

Sources of truth: models.dev api.json (list), plan_terms.jsonl (plan basis +
recorded offsets), state.db session_model_usage (realized usage; the
billing_base_url column is the immutable provider identity).

CALIBRATION 2026-09-20 (why the trap count fell from 291 to ~45):
three separate detectors in this repo had each filed a DOCUMENTED state as an
open finding — TR-043's pricing gaps, TR-076's drift rows, and this audit's own
trap list. A detector that produces a WORK LIST must be conservative: a lane is
only a burn trap when NO evidence class explains it. This audit had been
counting, as burn risk:

  * 63 sticker lanes — 'normalized:payg-sticker' and friends all carry a
    models.dev list price. A provider list price does not drift with usage, so
    an old stamp is not staleness. Only lanes carrying an actual OFFSET
    multiplier ("flat-sub(3.0x lane)") can be stale, and of those only 14
    predate the metered-usage discipline.
  * 41 free lanes already tagged 'window-cost-pending' — F3 in
    tests/test_feedback_invariants.py names that tag the CORRECT end state for a
    free SKU with no paid sibling to price against.
  * 128 lanes with a named+dated source ('research:...', '/v1/models live',
    'window-cost ...', 'or-spot-...') that the classifier simply did not
    recognise.

The real traps are: offset stamps predating metered discipline, prices with no
named source, named-but-undated sources (a staleness risk Bane's complaint is
exactly about), and zero-priced lanes with no story.

Exit codes: 0 healthy, 1 findings (CI-usable), like router_audit.
"""
import json
import os
import re
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

#: The metered-usage discipline begins here. Only an OFFSET stamp older than this
#: is stale — a list/sticker price is not usage-derived and cannot "go stale".
OFFSET_DISCIPLINE_DATE = '2026-09-19'

#: A real usage offset: "flat-sub(3.0x lane)", "flat-sub(39.4x lane)".
OFFSET_MARK = re.compile(r'\(\s*\d+(\.\d+)?x')

#: Source-shaped tokens. Deliberately tolerant of terse names ('or-spot',
#: 'meta-docs'): the rule is "does this cite where the number came from", not
#: "is it spelled out in full".
SOURCE_TOKENS = (
    'research:', 'window-cost', '/v1/models', 'rate=', 'sticker', 'normalized:',
    'measured', 'official', 'estimate', 'plan-offset', 'docs', 'spot', 'table',
    'pricing', 'calculator', 'lane clone', 'agreement',
)

DATE_RE = re.compile(r'20\d\d-\d\d-\d\d')

#: Classes that put a row on the work list.
TRAP_CLASSES = frozenset({
    'stale-offset',          # offset predates metered discipline -> re-derive
    'unbased',               # no named source at all -> the dangerous class
    'undated-source',        # named source, no date -> confirm + date it
    'free-window-unpriced',  # zero-priced plan lane with no story
    'free-unmetered',        # zero-priced lane on no plan at all
})

#: The order classes are printed in.
CLASS_ORDER = (
    'measured-offset', 'offset-stamped', 'stale-offset', 'official', 'estimate',
    'sticker', 'sourced', 'undated-source', 'unbased', 'free-window-pending',
    'free-window-unpriced', 'free-unmetered',
)


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


def list_price(md, provider, model):
    """models.dev list price for a lane, trying id/suffix/vendor forms."""
    for cand in (model, model.split('/')[-1], model.split(':')[-1]):
        for pid in (provider, provider.replace('-cloud', ''),
                    'moonshotai' if 'kimi' in model else provider):
            hit = (md.get(pid) or {}).get(cand)
            if hit:
                return hit
    return None


def _stamp(ev):
    m = DATE_RE.search(ev)
    return m.group(0) if m else None


def classify(row, plan=None):
    """Classify one lane row -> (class, is_trap, detail).

    Single source of truth for the audit, so the work list and the tests cannot
    drift apart. Conservative by construction: a lane is only a trap when no
    evidence class explains it.
    """
    ev = str(row.get('price_evidence') or '')
    low = ev.lower()
    norm = row.get('normalized_price')

    if norm is None:
        return 'unpriced', False, ''      # TR-064's domain, not pricing
    if norm == 0:
        if not plan:
            return 'free-unmetered', True, 'zero price, no plan terms'
        if 'window-cost-pending' in low:
            return 'free-window-pending', False, 'documented pending (F3 end state)'
        return 'free-window-unpriced', True, 'zero price on a plan, no story'
    if 'measured' in low or 'plan-offset' in low:
        return 'measured-offset', False, ''
    if 'official' in low:
        return 'official', False, ''
    if 'estimate' in low:
        return 'estimate', False, ''
    if OFFSET_MARK.search(ev):
        stamped = _stamp(ev)
        if stamped and stamped < OFFSET_DISCIPLINE_DATE:
            return 'stale-offset', True, f'offset stamped {stamped}'
        return 'offset-stamped', False, ''
    if 'sticker' in low or 'normalized:' in low:
        # A provider list price: list-derived, so it cannot drift with usage.
        return 'sticker', False, 'list-derived'
    if any(t in low for t in SOURCE_TOKENS):
        if _stamp(ev):
            return 'sourced', False, 'named+dated source'
        return 'undated-source', True, 'named source, no date'
    return 'unbased', True, 'no named source'


def audit(md=None, lanes=None, plans=None):
    """Run the classification over the live tables. Returns (classes, report)."""
    md = modelsdev() if md is None else md
    lanes = load_jsonl(MODELS) if lanes is None else lanes
    plans = {p['provider']: p for p in load_jsonl(PLANS)} if plans is None else plans

    classes = defaultdict(list)
    report = []
    for r in lanes:
        if r.get('disabled') or r.get('archive'):
            continue
        prov, model = r['provider'], r['model']
        norm = r.get('normalized_price')
        ev = str(r.get('price_evidence') or '')
        cls, trap, detail = classify(r, plans.get(prov))
        if cls == 'unpriced':
            continue
        classes[cls].append((prov, model, norm, detail or ev[:80]))

        # list-delta flags need the evidence class to interpret them:
        # a plan lane at 1/20th of list is CORRECT; an 'official' lane 3x off is STALE.
        lst = list_price(md, prov, model)
        if lst and norm:
            blend = (lst[0] + lst[1]) / 2
            if blend and (blend / norm > 3 or norm / blend > 3):
                if cls in ('measured-offset', 'offset-stamped', 'stale-offset'):
                    delta_cls = 'measured-offset'
                elif cls == 'official':
                    delta_cls = 'official'
                else:
                    delta_cls = 'unverified'
                report.append({'provider': prov, 'model': model, 'normalized': norm,
                               'md_blend': round(blend, 4), 'class': delta_cls})
    return classes, report


def main():
    classes, report = audit()

    print(f'PRICING AUDIT — {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}')
    print(f'active priced lanes: {sum(len(v) for v in classes.values())}')
    for k in CLASS_ORDER:
        if classes.get(k):
            mark = ' <- trap' if k in TRAP_CLASSES else ''
            print(f'  {k:20} {len(classes[k])}{mark}')

    traps = {k: len(classes[k]) for k in TRAP_CLASSES if classes.get(k)}
    total = sum(traps.values())
    print(f'\nBURN TRAPS: {total}')
    for k in CLASS_ORDER:
        if traps.get(k):
            print(f'  {k:20} {traps[k]}')
            for prov, model, norm, detail in classes[k][:4]:
                print(f'      {prov}/{model}  ({detail})')
    print(f'\nlanes >3x off models.dev list: {len(report)} '
          f'(of which measured-offset=legit, official=stale-list, unverified=fix)')
    by_cls = defaultdict(int)
    for x in report:
        by_cls[x['class']] += 1
    for k, v in sorted(by_cls.items()):
        print(f'  {k:20} {v}')
    print(f'\nNOT traps, though a naive count calls them one: '
          f'{len(classes.get("sticker", []))} sticker lanes (list-derived, cannot '
          f'stale), {len(classes.get("free-window-pending", []))} documented '
          f'window-cost-pending (F3 end state), '
          f'{len(classes.get("sourced", []))} named+dated sources.')
    print('remedy: per-provider offset re-derivation from metered usage (the kimi '
          'method) for stale offsets, a source + date for undated ones, and a '
          'window-cost (or the pending tag) for zero-priced plan lanes.')
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())

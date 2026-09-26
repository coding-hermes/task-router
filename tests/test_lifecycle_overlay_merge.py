"""TR-180: a lifecycle overlay touches ONLY the keys it names.

Measured on the real data 2026-09-26: the 4-key retirement notice for
ollama-cloud/deepseek-v4-flash:0731 left that lane with 31 of its 37 fields NULL — price,
context_limit, plan_tier and every perf field — because the seed applied the overlay as
`SET <every column> = _o.get(c)`. A notice about ONE DATE rewrote the whole row.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

COLS = ['provider', 'model', 'valid_to', 'lifecycle_source', 'normalized_price',
        'public_in_per_m', 'context_limit', 'plan_tier']


def _overlay_update():
    """Pull `overlay_update` out of router_seed without executing the seed body."""
    src = open(os.path.join(REPO, 'scripts', 'router_seed.py')).read()
    start = src.index('def overlay_update(')
    end = src.index('\n\n\nfor _o in _lc_rows:')
    ns = {}
    exec(src[start:end], ns)  # noqa: S102 — the function is pure
    return ns['overlay_update']


def test_a_thin_overlay_touches_only_its_own_keys():
    f = _overlay_update()
    thin = {'provider': 'x', 'model': 'y', 'valid_to': '2026-09-25',
            'lifecycle_source': 'provider-announcement: retired'}
    cols, vals = f(thin, [c for c in COLS if c not in ('provider', 'model')])
    assert set(cols) == {'valid_to', 'lifecycle_source'}, cols
    assert 'normalized_price' not in cols, 'a date notice must not touch the price'
    assert 'context_limit' not in cols
    assert vals == ['2026-09-25', 'provider-announcement: retired']


def test_a_named_null_still_clears_explicitly():
    """Key-wise merge must not make a deliberate clear impossible."""
    f = _overlay_update()
    o = {'provider': 'x', 'model': 'y', 'normalized_price': None,
         'lifecycle_source': 'manual clear'}
    cols, vals = f(o, [c for c in COLS if c not in ('provider', 'model')])
    assert set(cols) == {'normalized_price', 'lifecycle_source'}, cols
    # the null the overlay NAMED is carried through (order follows the column list)
    assert vals[cols.index('normalized_price')] is None


def test_a_full_overlay_still_applies_wholesale():
    f = _overlay_update()
    o = {c: 1 for c in COLS}
    cols, _ = f(o, [c for c in COLS if c not in ('provider', 'model')])
    assert len(cols) == len(COLS) - 2


def test_the_real_overlay_file_has_a_thin_row_this_protects():
    """The defect was proven on real data; keep the case that exposed it."""
    path = os.path.join(REPO, 'data', 'lifecycle.jsonl')
    if not os.path.exists(path):
        return
    import json
    rows = [json.loads(l) for l in open(path) if l.strip()]
    thin = [r for r in rows if len(r) <= 6]
    if not thin:
        return  # cleaned up later — the unit cases above still pin the rule
    f = _overlay_update()
    cols, _ = f(thin[0], [c for c in COLS if c not in ('provider', 'model')])
    assert 'normalized_price' not in cols

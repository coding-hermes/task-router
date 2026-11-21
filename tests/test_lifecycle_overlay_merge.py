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
    # INT-CI-8: the slice carries `Any` annotations from TR-270 stage 2 but NOT the
    # module's `from typing import Any`. On eager-annotation Pythons (<=3.11, the CI
    # runner) the def evaluates them and dies NameError; 3.14's PEP 649 deferral hid
    # it locally. Seed the exec namespace with every name the signature references.
    from typing import Any
    ns = {'Any': Any}
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


# ---------- the disable flag is ONE-WAY through the overlay ------------------
# Measured 2026-11-21: 41 overlay rows carried `"disabled": null` (a catalog
# snapshot taken while the lane was live) and the key-wise merge CLEARED the
# base disable, so every reseed resurrected the lane. A clean reseed of main
# re-enabled 12 neuralwatt lanes the owner cancelled on 2026-11-16 and the live
# resolver handed out hops to a provider answering HTTP 402.

DISABLE_COLS = COLS + ['disabled', 'disabled_reason']


def test_a_null_disabled_never_clears_a_base_disable():
    """The owner cancellation must survive a stale catalog snapshot."""
    f = _overlay_update()
    o = {'provider': 'neuralwatt', 'model': 'glm-5.3-flex', 'disabled': None,
         'disabled_reason': None, 'lifecycle_source': 'catalog snapshot 2026-09-21'}
    cols, _ = f(o, [c for c in DISABLE_COLS if c not in ('provider', 'model')],
                base_disabled=True)
    assert 'disabled' not in cols, 'a null in the overlay must not re-enable a lane'
    assert 'disabled_reason' not in cols, 'the reason must not be dropped with the flag'
    assert 'lifecycle_source' in cols, 'the rest of the overlay still applies'


def test_an_overlay_may_still_set_a_disable():
    """A retirement notice is exactly the case the overlay exists for."""
    f = _overlay_update()
    o = {'provider': 'x', 'model': 'y', 'disabled': True,
         'disabled_reason': 'provider-announcement: retired 2026-11-16',
         'lifecycle_source': 'provider-announcement'}
    cols, vals = f(o, [c for c in DISABLE_COLS if c not in ('provider', 'model')],
                   base_disabled=None)
    assert 'disabled' in cols and 'disabled_reason' in cols, cols
    assert vals[cols.index('disabled')] is True


def test_a_null_disabled_still_clears_when_the_base_is_enabled():
    """No behaviour change for the ordinary case: clearing a clear flag is a no-op."""
    f = _overlay_update()
    o = {'provider': 'x', 'model': 'y', 'disabled': None,
         'lifecycle_source': 'manual clear'}
    cols, _ = f(o, [c for c in DISABLE_COLS if c not in ('provider', 'model')],
                base_disabled=None)
    assert 'disabled' in cols, cols


def test_the_real_overlay_file_cannot_resurrect_a_disabled_lane():
    """End-to-end over the real files: every overlay row that names a falsy
    `disabled` for a lane the committed tables disable must be neutralised."""
    import json
    lc_path = os.path.join(REPO, 'data', 'lifecycle.jsonl')
    models_path = os.path.join(REPO, 'data', 'tables', 'models.jsonl')
    if not (os.path.exists(lc_path) and os.path.exists(models_path)):
        return
    base = {}
    for line in open(models_path):
        if line.strip():
            r = json.loads(line)
            base[(r.get('provider'), r.get('model'))] = r
    f = _overlay_update()
    updatable = [c for c in DISABLE_COLS if c not in ('provider', 'model')]
    resurrected = []
    for line in open(lc_path):
        if not line.strip():
            continue
        o = json.loads(line)
        if 'disabled' not in o:
            continue
        if o.get('disabled'):
            continue  # an overlay SETTING a disable is the allowed direction
        b = base.get((o.get('provider'), o.get('model')))
        if b is None or not b.get('disabled'):
            continue
        cols, _ = f(o, updatable, base_disabled=b.get('disabled'))
        if 'disabled' in cols:
            resurrected.append(f"{o['provider']}/{o['model']}")
    assert not resurrected, (
        'these lifecycle overlay rows would re-enable a lane the tables disable: '
        f'{resurrected}'
    )

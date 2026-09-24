#!/usr/bin/env python3
"""router_provider_import.py — batch-onboard a vendor model catalog into the registry.

Replaces one-off import sessions: point a PRESET at a vendor's public /v1/models
catalog and the tool does association + lane facts + probe/quota wiring + reseed,
idempotently. Bane 2026-09-17: "why is it taking so long to add a batch of hosts
on a new provider" — this is the fix.

Usage:
  python3 scripts/router_provider_import.py --preset xkiro --dry-run
  python3 scripts/router_provider_import.py --preset xkiro --apply --reseed

Design invariants (do not violate):
- DATA > CODE: all provider/model facts come from the preset file + live catalog;
  the script contains zero provider facts.
- perf_* stays NULL on net-new lanes (invisible to chains until probed — TR-044,
  no family-fill). Battery evidence goes to benchmarks.jsonl, never perf_* (see
  TR-054 percentile-corruption incident).
- Existing lanes are UPDATED in place (row order preserved); lanes missing from
  the catalog are REPORTED, never silently deleted (use --mark-removed to disable).
- Keys are never touched: probe row carries key_env only.
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = os.path.join(REPO, 'data', 'tables')
PRESETS = os.path.join(REPO, 'data', 'catalogs')


# ---------- pure functions (unit-tested) ----------

def dig(d, dotted):
    cur = d
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


#: Sanity ceiling for a per-1M-token lane price. A value above this is not a
#: price — it is a sentinel or a scaling bug, and importing it would corrupt
#: every chain it ranks in.
MAX_LANE_PRICE_PER_M = 10000.0


def _scaled_price(value, scale):
    """Catalog price -> per-1M lane price, or None when it is not a real price.

    NEVER a fabricated number: OpenRouter's `openrouter/fusion` pseudo-model
    reports pricing -1000000 (a router placeholder), and a negative price sorts
    ahead of every honest lane in the chain. Anything negative, non-finite or
    over MAX_LANE_PRICE_PER_M stays None so the lane reads UNPRICED and the gap
    is visible instead of silently winning every price-ordered chain.
    """
    if value is None:
        return None
    try:
        v = float(value) * scale
    except (TypeError, ValueError):
        return None
    if v != v or v in (float('inf'), float('-inf')):   # NaN / inf
        return None
    if v < 0 or v > MAX_LANE_PRICE_PER_M:
        return None
    # Round to 6dp: a per-token value multiplied back up lands on 0.7999999999999999
    # otherwise, and that noise alone rewrites the whole provider on every refresh.
    return round(v, 6)


def normalize(catalog, preset):
    """catalog dict -> {model_id: lane dict} using the preset field map."""
    fmt = preset.get('catalog_format', 'openai_models_list')
    if fmt == 'openai_models_list':
        entries = catalog.get('data') or []
    else:
        entries = catalog if isinstance(catalog, list) else []
    blend_in, blend_out = preset.get('blend', [0.96, 0.04])
    # price_scale: a catalog that quotes PER TOKEN (OpenRouter: pricing.prompt is
    # USD per token) must declare 1_000_000 so the stored price is per 1M tokens
    # like every other lane. Absent = 1.0 (catalogs that already quote per 1M,
    # e.g. xKiro).
    scale = float(preset.get('price_scale', 1.0) or 1.0)
    fm = preset['field_map']
    out = {}
    for e in entries:
        mid = dig(e, fm['model'])
        if not mid:
            continue
        pin, pout = dig(e, fm['price_in']), dig(e, fm['price_out'])
        pin, pout = _scaled_price(pin, scale), _scaled_price(pout, scale)
        price = None if pin is None or pout is None else round(blend_in * float(pin) + blend_out * float(pout), 6)
        # normalized_price and public_price follow DIFFERENT bases in the
        # existing data (measured 2026-09-24: 347/347 openrouter lanes have
        # normalized == input while public == the blend). `normalized_from`
        # lets a preset reproduce the provider's established convention instead
        # of rewriting every lane to satisfy a formula.
        norm_price = price
        if preset.get('normalized_from') == 'price_in' and pin is not None:
            norm_price = float(pin)
        cap_v = dig(e, fm.get('vision', ''))
        cap_t = dig(e, fm.get('thinking', ''))
        lane = {
            'provider': preset['id'],
            'model': mid,
            'normalized_price': norm_price,
            'public_price': price,
            'public_in_per_m': None if pin is None else float(pin),
            'public_out_per_m': None if pout is None else float(pout),
            'context_limit': dig(e, fm['context']) if fm.get('context') else None,
        }
        # Capability flags are only written when the preset actually maps them:
        # emitting `vision: None` for a provider whose catalog doesn't carry the
        # field would diff every lane in the registry and churn 300+ rows on a
        # refresh that is only about price. A catalog's modality STRING is not a
        # vision flag either ("text+image->text" would read as True for anything).
        if fm.get('vision'):
            lane['vision'] = bool(cap_v) if cap_v is not None else None
        if fm.get('thinking'):
            lane['thinking'] = bool(cap_t) if cap_t is not None else None
        # Cache rates (Bane 2026-09-24: cache is the term that compounds in agent
        # loops). Mapped only when the preset declares the field; a catalog that
        # omits it stays NULL (= unpublished), never 0 (= free cache).
        if fm.get('price_cache_read'):
            lane['public_cache_read_per_m'] = _scaled_price(dig(e, fm['price_cache_read']), scale)
        if fm.get('price_cache_write'):
            lane['public_cache_write_per_m'] = _scaled_price(dig(e, fm['price_cache_write']), scale)
        out[mid] = lane
    return out


def diff(existing, new):
    """existing: {(provider,model): row}; new: {model_id: lane} ->
    {added:[...], changed:[(model, fields)], unchanged:[...], removed:[...]}"""
    added, changed, unchanged, removed = [], [], [], []
    existing_models = {m for (_, m) in existing}
    for mid, lane in sorted(new.items()):
        if mid not in existing_models:
            added.append(mid)
            continue
        row = existing[(lane['provider'], mid)]
        touched = {}
        for k, v in lane.items():
            if k in ('provider',):
                continue
            if row.get(k) != v and not (v in (None, False) and row.get(k) is None):
                touched[k] = (row.get(k), v)
        if touched:
            changed.append((mid, touched))
        else:
            unchanged.append(mid)
    removed = sorted(existing_models - set(new))
    return {'added': added, 'changed': changed, 'unchanged': unchanged, 'removed': removed}


def apply_lanes(path, provider, new_lanes, plan_tier, price_evidence, drift=None, variant_notes=None):
    """Update models.jsonl in place: update matching rows, append net-new.
    Returns (updated, appended). Row order of existing file is preserved.
    Evidence discipline: existing rows KEEP their price_evidence (a no-drift
    reimport must not rewrite provenance); rows with catalog drift get a dated
    drift stamp appended; only net-new rows carry the import evidence."""
    rows = [json.loads(l) for l in open(path) if l.strip()]
    updated = appended = 0
    seen = set()
    out = []
    for r in rows:
        if r.get('provider') == provider and r['model'] in new_lanes:
            # merge: existing row is the base (keeps provenance, evidence cols,
            # plan stamps, disabled state); catalog fields overwrite as facts
            lane = dict(r)
            incoming = dict(new_lanes[r['model']])
            # F3 (Bane's rule, enforced by test_feedback_invariants): a `:free`
            # lane is NOT free — it draws the metered window at list-equivalent
            # value. So a catalog's $0 sticker must never overwrite an
            # established window cost, and a zero free lane must never sit
            # without a story. Measured 2026-09-24: an unguarded import flattened
            # 13 free lanes (e.g. gemma-4-26b-it:free 0.195 -> 0.0,
            # nemotron-3-ultra:free 1.5 -> 0.0) and broke the invariant.
            today = datetime.date.today().isoformat()
            if ':free' in str(r['model']) and (incoming.get('normalized_price') or 0) == 0:
                note = ''
                if (r.get('normalized_price') or 0) > 0:
                    incoming['normalized_price'] = r.get('normalized_price')
                    incoming['public_price'] = r.get('public_price')
                    note = (f' | {today} catalog sticker $0; window_cost KEPT '
                            f'({r.get("normalized_price")})')
                elif 'window-cost-pending' not in str(r.get('price_evidence') or '').lower():
                    note = (f' | {today} window-cost-pending: zero-price SKU, no '
                            f'established sibling cost')
                if note:
                    incoming['price_evidence'] = (r.get('price_evidence') or '') + note
            lane.update(incoming)
            lane['provider'] = provider
            lane['model'] = r['model']
            # plan_tier is what the PAYG/plan bucketing and the plan-offset
            # pricing key off. NEVER stamp one onto a live row that has none:
            # measured 2026-09-24, stamping 0 on 376 openrouter rows re-bucketed
            # them and moved the P0_FORE golden head to an openrouter lane.
            lane['plan_tier'] = r.get('plan_tier')
            if drift and r['model'] in drift:
                stamp = ' | catalog drift ' + drift[r['model']]
                if stamp not in (r.get('price_evidence') or ''):
                    lane['price_evidence'] = (r.get('price_evidence') or '') + stamp
            # Variant notes come from the PRESET (source-controlled data, e.g.
            # "throughput SKU"), not from hand-editing the generated JSONL.
            note = (variant_notes or {}).get(r['model'])
            if note and note not in (lane.get('price_evidence') or ''):
                lane['price_evidence'] = (lane.get('price_evidence') or '') + ' | ' + note
            out.append(lane)
            seen.add(r['model'])
            updated += 1
        else:
            out.append(r)
    for mid, lane in new_lanes.items():
        if mid in seen:
            continue
        row = dict(lane)
        row['plan_tier'] = plan_tier
        row['price_evidence'] = price_evidence
        row['valid_from'] = None
        row['valid_to'] = None
        row['archive'] = False
        row['token_factor'] = 1.0
        row['perf_agent_tick'] = None  # TR-044: invisible to chains until probed
        row['perf_long_doc'] = None
        row['perf_debug'] = None
        row['perf_schema'] = None
        row['perf_e2e_vision'] = None
        row['perf_review'] = None
        row['perf_delegation'] = None
        row['perf_guard'] = None
        row['perf_mock'] = None
        row['perf_reasoning'] = None
        out.append(row)
        appended += 1
    with open(path, 'w') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return updated, appended


def ensure_probe_row(path, preset):
    """Idempotently ensure probe_providers.jsonl carries this provider (key_env only)."""
    rows = [json.loads(l) for l in open(path) if l.strip()]
    if any(r.get('id') == preset['id'] for r in rows):
        return False
    probe = preset.get('probe', {})
    rows.append({'id': preset['id'],
                 'base_url': probe.get('base_url'),
                 'key_env': probe.get('key_env'),
                 'default_model': probe.get('default_model'),
                 'enabled': True,
                 'note': probe.get('note', 'via router_provider_import')})
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return True


# ---------- IO ----------

def fetch_catalog(url, user_agent):
    req = urllib.request.Request(url, headers={'User-Agent': user_agent,
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def load_preset(name):
    path = os.path.join(PRESETS, f'{name}.json')
    return json.load(open(path))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--preset', required=True, help='preset name in data/catalogs/<name>.json')
    ap.add_argument('--dry-run', action='store_true', help='diff only, write nothing')
    ap.add_argument('--apply', action='store_true', help='write updates + appends')
    ap.add_argument('--reseed', action='store_true', help='run router_seed.py after apply')
    ap.add_argument('--mark-removed', action='store_true',
                    help='disable lanes absent from the live catalog (default: report only)')
    args = ap.parse_args()

    preset = load_preset(args.preset)
    catalog = fetch_catalog(preset['catalog_url'], preset.get('user_agent', 'task-router-import'))
    new_lanes = normalize(catalog, preset)
    print(f"catalog: {len(new_lanes)} models from {preset['catalog_url']}")

    mpath = os.path.join(TABLES, 'models.jsonl')
    existing = {}
    for l in open(mpath):
        if not l.strip():
            continue
        r = json.loads(l)
        if r.get('provider') == preset['id']:
            existing[(preset['id'], r['model'])] = r
    print(f"registry: {len(existing)} existing {preset['id']} lanes")

    d = diff(existing, new_lanes)
    print(f"diff: +{len(d['added'])} added, ~{len(d['changed'])} changed, "
          f"{len(d['unchanged'])} unchanged, -{len(d['removed'])} removed")
    for mid, touched in d['changed'][:10]:
        fields = ', '.join(f'{k}: {a} -> {b}' for k, (a, b) in touched.items())
        print(f'  ~ {mid}: {fields[:140]}')
    if d['removed']:
        print(f'  removed from catalog: {d["removed"]}')
        if args.mark_removed and args.apply:
            rows = [json.loads(l) for l in open(mpath) if l.strip()]
            for r in rows:
                if r.get('provider') == preset['id'] and r['model'] in d['removed']:
                    r['disabled'] = True
                    r['disabled_reason'] = 'catalog-removed (provider_import)'
            with open(mpath, 'w') as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
            print('  marked removed lanes disabled')
        else:
            print('  (report only — use --mark-removed to disable them)')

    if args.dry_run or not args.apply:
        print('dry-run: nothing written')
        return 0

    policy = preset.get('plan_tier_policy', {})
    plan_tier = policy.get('default')
    evidence = f"provider_import preset={preset['id']} " + policy.get('reason', '')
    updated, appended = apply_lanes(mpath, preset['id'], new_lanes, plan_tier, evidence,
                                   variant_notes=preset.get('variant_notes'))
    print(f'applied: {updated} updated, {appended} appended (plan_tier={plan_tier})')

    if ensure_probe_row(os.path.join(TABLES, 'probe_providers.jsonl'), preset):
        print('probe_providers: row added')
    else:
        print('probe_providers: row already present')

    if args.reseed:
        import subprocess
        rc = subprocess.run([sys.executable, os.path.join(REPO, 'scripts', 'router_seed.py')]).returncode
        print(f'reseed rc: {rc}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

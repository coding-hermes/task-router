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
        if price is None:
            # A carrier whose catalog has no pricing block (commandcode) declares
            # the vendor sticker as DATA in the preset (`sticker_prices`); the
            # provider's established convention for such pass-through lanes is
            # normalized == public == the IN sticker (measured on all 67 live
            # commandcode rows). No sticker declared = the lane stays UNPRICED
            # (a visible gap), never a guess.
            sticker = (preset.get('sticker_prices') or {}).get(mid)
            if isinstance(sticker, dict) and sticker.get('in') is not None:
                st_in = float(sticker['in'])
                st_out = None if sticker.get('out') is None else float(sticker['out'])
                lane = {
                    'provider': preset['id'],
                    'model': mid,
                    'normalized_price': st_in,
                    'public_price': st_in,
                    'public_in_per_m': st_in,
                    'public_out_per_m': st_out,
                    'context_limit': dig(e, fm['context']) if fm.get('context') else None,
                }
                if sticker.get('cache_read') is not None:
                    lane['public_cache_read_per_m'] = float(sticker['cache_read'])
                out[mid] = lane
                continue
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


def apply_lanes(path, provider, new_lanes, plan_tier, price_evidence, drift=None,
                variant_notes=None, usage_multiplier=None):
    """Update models.jsonl in place: update matching rows, append net-new.
    Returns (updated, appended). Row order of existing file is preserved.
    Evidence discipline: existing rows KEEP their price_evidence (a no-drift
    reimport must not rewrite provenance); rows with catalog drift get a dated
    drift stamp appended; only net-new rows carry the import evidence.

    usage_multiplier (2026-09-25): a preset whose provider is a flat plan
    (xkiro: $200/mo = 30x usage) declares the multiplier as DATA; a catalog
    refresh then writes the plan-EFFECTIVE price for plan-covered lanes
    (normalized = list blend / multiplier) and the raw list for wallet-only
    lanes (plan_tier NULL), which is what the original onboarding did by hand.
    Without it a refresh rewrites normalized_price to the raw list — 30x too
    expensive — and every lane silently loses its chain position.
    """
    rows = [json.loads(l) for l in open(path) if l.strip()]
    updated = appended = 0
    seen = set()
    out = []
    mult = float(usage_multiplier or 1.0)

    def plan_effective(lane, priced_from_catalog=True):
        """Apply the plan multiplier to a lane whose price came from the
        catalog in THIS pass. A lane whose price was preserved (catalog had no
        price) or kept for its window-cost story must NOT be divided again."""
        if not priced_from_catalog or mult == 1.0 or lane.get('plan_tier') is None:
            return lane
        if not (lane.get('normalized_price') or 0) > 0:
            return lane
        lane['normalized_price'] = round(float(lane['normalized_price']) / mult, 6)
        note = f' | internal /{mult:g} per {mult:g}x usage multiplier'
        if note not in (lane.get('price_evidence') or ''):
            lane['price_evidence'] = (lane.get('price_evidence') or '') + note
        return lane

    for r in rows:
        if r.get('provider') == provider and r['model'] in new_lanes:
            # merge: existing row is the base (keeps provenance, evidence cols,
            # plan stamps, disabled state); catalog fields overwrite as facts
            lane = dict(r)
            # A catalog that OMITS a fact is not evidence the fact changed
            # (2026-09-25): several fleet carriers serve a /models catalog with
            # no pricing block (commandcode, opencode-go) or no context field.
            # Merging their Nones verbatim NULLs an established price and the
            # lane drops out of every price-ordered chain on a refresh that was
            # only about model ids. Preserve the established value; a catalog
            # value — including a genuine 0/0.0 — always wins.
            incoming = {k: v for k, v in new_lanes[r['model']].items()
                        if not (v is None and r.get(k) is not None)}
            # F3 (Bane's rule, enforced by test_feedback_invariants): a `:free`
            # lane is NOT free — it draws the metered window at list-equivalent
            # value. So a catalog's $0 sticker must never overwrite an
            # established window cost, and a zero free lane must never sit
            # without a story. Measured 2026-09-24: an unguarded import flattened
            # 13 free lanes (e.g. gemma-4-26b-it:free 0.195 -> 0.0,
            # nemotron-3-ultra:free 1.5 -> 0.0) and broke the invariant.
            today = datetime.date.today().isoformat()
            priced_from_catalog = new_lanes[r['model']].get('normalized_price') is not None
            if ':free' in str(r['model']) and (incoming.get('normalized_price') or 0) == 0:
                note = ''
                if (r.get('normalized_price') or 0) > 0:
                    incoming['normalized_price'] = r.get('normalized_price')
                    incoming['public_price'] = r.get('public_price')
                    priced_from_catalog = False
                    note = (f' | {today} catalog sticker $0; window_cost KEPT '
                            f'({r.get("normalized_price")})')
                elif 'window-cost-pending' not in str(r.get('price_evidence') or '').lower():
                    # Wording follows the trapfix vocabulary the pricing audit
                    # greps for ('no paid sibling' / 'reseller catalog listings'):
                    # a pending tag must name WHY, or the class report and the
                    # audit stop agreeing. See tests/test_pricing_audit_classes.py.
                    note = (f' | {today} window-cost-pending: zero-price SKU, no paid '
                            f'sibling in any carrier catalog to price against')
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
            lane = plan_effective(lane, priced_from_catalog)
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
        row = plan_effective(row)          # net-new: price always from catalog
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

    # Dry-run preview must show what APPLY would write (2026-09-25):
    #  * a plan carrier writes the plan-effective price (list / multiplier) on
    #    plan-covered lanes — previewing the raw list made the xkiro diff look
    #    like a 30x price hike when the apply is a near no-op;
    #  * a catalog that omits a fact (commandcode/opencode-go publish no pricing
    #    block) does NOT null the established value — previewing the raw merge
    #    reported ~500 lanes as 'price -> None' on a refresh that touches none.
    # Preview on a copy — the apply path applies both rules itself.
    policy = preset.get('plan_tier_policy', {}) or {}
    mult = float(policy.get('usage_multiplier') or 1.0)
    preview = {}
    for m, l in new_lanes.items():
        lane = dict(l)
        row = existing.get((preset['id'], m))
        if row:
            lane = {k: v for k, v in lane.items()
                    if not (v is None and row.get(k) is not None)}
        if mult != 1.0:
            tier = row.get('plan_tier') if row else policy.get('default')
            if tier is not None and (lane.get('normalized_price') or 0) > 0:
                lane['normalized_price'] = round(float(lane['normalized_price']) / mult, 6)
        preview[m] = lane

    d = diff(existing, preview)
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
                                   variant_notes=preset.get('variant_notes'),
                                   usage_multiplier=policy.get('usage_multiplier'))
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

#!/usr/bin/env python3
"""router_catalog_refresh.py — run the carrier-catalog import for EVERY preset
in data/catalogs/ (TR-167).

The 2026-09-24/25 model wave reached the models.dev-covered carriers via their
sync or a hand pass but NOT the preset carriers in data/catalogs/: 46 lanes
had to be added by hand because nothing on the daily loop called
scripts/router_provider_import.py. This wrapper closes that gap: one sweep,
one preset file per carrier, no prompt edit when a carrier is added.

Usage:
  python3 scripts/router_catalog_refresh.py            # dry-run diff, all presets
  python3 scripts/router_catalog_refresh.py --apply    # file the lanes
  python3 scripts/router_catalog_refresh.py --preset <id> --json

Design invariants (do not violate):
- DISCOVERY, NOT ENumeration: presets come from a directory glob. Adding a
  carrier = dropping data/catalogs/<id>.json. NO preset name may ever be
  hardcoded here (test_catalog_refresh_wiring pins this).
- ONE BAD CARRIER NEVER KILLS THE SWEEP (fail-open, like every router tool):
  a carrier whose catalog is unreachable/unparseable reports status "error"
  and the sweep continues; the exit code is non-zero so the pipeline knows.
- UNPRICED NET-NEW LANES ARE HELD, NEVER FILED (null-debt doctrine, Bane:
  "a NULL must carry a reason"): an added lane with no price would be invisible
  to every price-ordered chain AND count as unexplained null debt (an appended
  row carrying normalized_price=None in a measured column trips the TR-201
  gate). --apply files the PRICED lanes of the preset and holds ONLY the
  unpriced ones — per lane, per the AC ("files them ... or reports them loudly
  if it cannot price them"); a whole-preset hold would block 9 priced lanes
  behind 2 unpriced ones on real data. Held lanes are re-reported every run
  (the diff is recomputed live) until priced or explained in model_notes.
- PRICES > DATA QUIRK: the per-preset quirks (blend, price_scale,
  sticker_prices, plan multipliers, normalized_from) stay in the PRESET; the
  import rules live only in router_provider_import (DATA > CODE).
- Probe rows: key_env only, never a key value (router_provider_import's rule).

Exit codes: 0 = every preset reported (holds are reported loudly but are honest
diffs — aborting the pipeline for them would kill the gap report and the agent
context that prices them next). 1 = any preset errored (TR-024: a required
step that failed must abort, not silently skip) or the registry is missing.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys

# The LIVE install is a symlink (~/.hermes/scripts/router_catalog_refresh.py —
# the cron realpath guard forbids byte-copies that would need data/ shipped
# next to them): a symlinked __file__ resolves under ~/.hermes and REPO would
# find no presets. realpath follows the link back into the checkout — same
# pattern as router_seed.py's _HERE, and the TR-202 doctrine: a live-installed
# runtime tool never derives paths from the abspath idiom
# (test_symlink_path_resolution enforces it repo-wide).
_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import router_provider_import as rpi  # noqa: E402  (the ONE import implementation)
from typing import Any

REPO = os.path.dirname(_SCRIPTS_DIR)

#: Test seam (hermetic suite): when ROUTER_CATALOG_FETCH_DIR is set, catalogs
#: are read from that DIRECTORY as <preset-id>.json (raw JSON exactly as the
#: URL would return) instead of the network — the same injection shape
#: ROUTER_CATALOG_DIR gives the preset list. Unset on any real run:
#: production always fetches live (read at call time; tests monkeypatch env).


def _fetch(preset: Any):
    if os.environ.get('ROUTER_CATALOG_FETCH_DIR'):
        path = os.path.join(os.environ['ROUTER_CATALOG_FETCH_DIR'],
                            preset['id'] + '.json')
        with open(path) as f:
            return json.load(f)
    return rpi.fetch_catalog(preset['catalog_url'],
                             preset.get('user_agent', 'task-router-import'))


def discover_presets(catalogs_dir: Any=None) -> list[Any]:
    """Every preset file in the catalogs dir, sorted (stable report order).

    Glob-discovery is the AC: adding a carrier means dropping one preset file.
    A file that does not PARSE is not a preset — it is reported by the caller
    when loaded, never silently skipped here (parse errors surface per-preset
    as status "error", they do not shrink the sweep).
    """
    d = (catalogs_dir
         or os.environ.get('ROUTER_CATALOG_DIR')
         or os.path.join(REPO, 'data', 'catalogs'))
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in sorted(os.listdir(d))
            if n.endswith('.json')]


def refresh_preset(preset: Any, existing: Any, *, apply: bool=False, fetch: Any=None):
    """One preset end-to-end against the registry rows of ITS provider only.

    existing: {(provider, model): row} — the caller filters to preset['id'].
    fetch: injectable catalog fetcher (tests pass a stub; default live).

    Mirrors router_provider_import.main() faithfully: same preview merge
    (catalog-absent facts never look like price moves; plan carriers preview
    the plan-effective price so a 30x "hike" is never a false alarm), same
    diff, same apply path, same evidence strings. Any drift between this
    preview and importer.main() is a bug — test_catalog_refresh_wiring pins
    the shapes both directions.

    Returns a result dict:
      preset, status (ok|held|error), models, added, changed, unchanged,
      removed, changed_preview ([[model, {field: [old, new]}]] first 10),
      unpriced_new ([model] added lanes with no price),
      applied (None dry-run / True written / False held),
      probe_row (True when apply added the probe_providers row), error
    """
    name = preset['id']
    base = {'preset': name, 'status': 'ok', 'models': 0, 'added': [],
            'changed': 0, 'unchanged': 0, 'removed': [],
            'changed_preview': [], 'unpriced_new': [], 'applied': None,
            'probe_row': None, 'error': None}
    fetch = fetch or _fetch
    catalog = fetch(preset)
    new_lanes = rpi.normalize(catalog, preset)
    base['models'] = len(new_lanes)
    # The diff is THIS preset vs THIS provider's rows only — rows belonging to
    # other providers must never appear as "removed from catalog".
    existing = {k: v for k, v in existing.items() if k[0] == name}

    # Preview on the merged view — the importer's own dry-run rule (2026-09-25):
    # a catalog that OMITS a fact is not evidence the fact changed, and a plan
    # carrier writes the plan-effective price. Previewing raw made honest
    # refreshes look like mass nulls / 30x hikes.
    policy = preset.get('plan_tier_policy', {}) or {}
    mult = float(policy.get('usage_multiplier') or 1.0)
    preview = {}
    for m, lane in new_lanes.items():
        lane = dict(lane)
        row = existing.get((name, m))
        if row:
            lane = {k: v for k, v in lane.items()
                    if not (v is None and row.get(k) is not None)}
        if mult != 1.0:
            tier = row.get('plan_tier') if row else policy.get('default')
            if tier is not None and (lane.get('normalized_price') or 0) > 0:
                lane['normalized_price'] = round(
                    float(lane['normalized_price']) / mult, 6)
        preview[m] = lane

    d = rpi.diff(existing, preview)
    base['added'] = d['added']
    base['changed'] = len(d['changed'])
    base['unchanged'] = len(d['unchanged'])
    base['removed'] = d['removed']
    base['changed_preview'] = [[m, touched] for m, touched in d['changed'][:10]]
    base['unpriced_new'] = sorted(
        m for m in d['added'] if preview[m].get('normalized_price') is None)
    if base['unpriced_new']:
        # A priced-but-$0 lane is a PRICE (the importer keeps $0 for genuinely
        # free SKUs); only a lane the catalog + preset sticker cannot price at
        # all is held.
        base['status'] = 'held'
    # Per-lane holds (measured 2026-10-14 dry-run: a whole-preset hold would
    # have blocked 27 priced lanes behind 14 unpriced ones): the AC files new
    # lanes OR reports them loudly when they cannot be priced — exactly that.
    # Held lanes are never appended; they stay "new" in every future diff
    # until priced or explained.
    fileable = [m for m in d['added'] if m not in set(base['unpriced_new'])]

    if not apply:
        return base

    evidence = f"provider_import preset={name} " + policy.get('reason', '')
    updated, appended = rpi.apply_lanes(
        _models_path(), name, {m: new_lanes[m] for m in fileable},
        plan_tier=policy.get('default'),
        price_evidence=evidence,
        variant_notes=preset.get('variant_notes'),
        usage_multiplier=policy.get('usage_multiplier'))
    base['applied'] = True
    base['probe_row'] = rpi.ensure_probe_row(_probe_path(), preset)
    return base


def _tables_dir():
    return (os.environ.get('ROUTING_DATA_DIR')
            or os.path.join(REPO, 'data', 'tables'))


def _models_path():
    return os.path.join(_tables_dir(), 'models.jsonl')


def _probe_path():
    return os.path.join(_tables_dir(), 'probe_providers.jsonl')


def _report(results: Any, t0: Any, apply: Any) -> tuple[Any, ...]:
    """(prose_text, json_dict) for a sweep. Prose names EVERY preset, the diff
    shapes the AC asks for (new lanes / price moves / catalog removals) and the
    loud held-block that tells the next phase exactly what to price."""
    lines = [f'== carrier catalog refresh ({len(results)} presets, '
             f'{"APPLY" if apply else "dry-run"}) ==']
    totals = {'added': 0, 'changed': 0, 'removed': 0, 'unpriced_held': 0,
              'presets_ok': 0, 'presets_held': 0, 'presets_error': 0}
    for r in results:
        if r['status'] == 'error':
            totals['presets_error'] += 1
            lines.append(f"[{r['preset']}] ERROR: {r['error']}")
            continue
        totals['added'] += len(r['added'])
        totals['changed'] += r['changed']
        totals['removed'] += len(r['removed'])
        tag = {'ok': 'ok', 'held': 'HELD'}[r['status']]
        lines.append(
            f"[{r['preset']}] {tag}: catalog {r['models']} models — "
            f"+{len(r['added'])} new, ~{r['changed']} priced/changed, "
            f"{r['unchanged']} unchanged, -{len(r['removed'])} removed")
        for model, touched in r['changed_preview']:
            fields = ', '.join(f'{k}: {a} -> {b}'
                               for k, (a, b) in touched.items())
            lines.append(f'    ~ {model}: {fields[:140]}')
        if r['removed']:
            lines.append(f'    removed from catalog: {r["removed"]}')
        if r['status'] == 'held':
            totals['unpriced_held'] += len(r['unpriced_new'])
            totals['presets_held'] += 1
            lines.append(
                f"    HELD — {len(r['unpriced_new'])} net-new lane(s) with NO "
                f"price (catalog + preset sticker): "
                f"{r['unpriced_new']}. NOT filed: an unpriced lane is "
                f"invisible to every chain and counts as null debt (priced "
                f"sibling lanes ARE filed). Price them (plan terms / sticker / "
                f"models.dev) — the next run files them automatically — or "
                f"record why they stay unpriced (model_notes.jsonl).")
        elif apply:
            filed = len(r['added']) - len(r['unpriced_new'])
            probe = ('probe row added' if r['probe_row']
                     else 'probe row present')
            lines.append(f'    applied: {filed} lane(s) filed to '
                         f'models.jsonl ({probe})')
        if r['status'] == 'ok':
            totals['presets_ok'] += 1
    if not apply:
        lines.append('dry-run: nothing written (use --apply to file lanes)')
    held = totals['presets_held']
    errs = totals['presets_error']
    lines.append(
        f'summary: +{totals["added"]} new, ~{totals["changed"]} changed, '
        f'-{totals["removed"]} removed across {len(results)} presets — '
        f'{totals["presets_ok"]} applied, {held} held (unpriced), '
        f'{errs} errored')
    summary = {'ran_at': t0.isoformat(), 'apply': apply, 'presets': results,
               'totals': totals}
    return '\n'.join(lines), summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--data-dir', default=None,
                    help='registry tables dir (default: <repo>/data/tables '
                         'or ROUTING_DATA_DIR)')
    ap.add_argument('--preset', action='append', default=[],
                    help='limit the sweep to this preset name (repeatable)')
    ap.add_argument('--apply', action='store_true',
                    help='import lanes into models.jsonl (default: dry-run)')
    ap.add_argument('--json', action='store_true',
                    help='machine report on stdout (prose moves to stderr)')
    args = ap.parse_args()
    if args.data_dir:
        os.environ['ROUTING_DATA_DIR'] = args.data_dir
    t0 = datetime.datetime.now(datetime.timezone.utc)

    if not os.path.exists(_models_path()):
        print(f'router_catalog_refresh: registry missing at {_models_path()} '
              '(seed once or pass --data-dir)', file=sys.stderr)
        return 1

    presets = discover_presets()
    if args.preset:
        want = set(args.preset)
        presets = [p for p in presets
                   if os.path.basename(p)[:-len('.json')] in want]
    if not presets:
        print('router_catalog_refresh: no presets found in '
              f'{os.environ.get("ROUTER_CATALOG_DIR") or os.path.join(REPO, "data", "catalogs")}',
              file=sys.stderr)
        return 1

    by_key = {}
    for l in open(_models_path()):
        if l.strip():
            r = json.loads(l)
            by_key[(r.get('provider'), r['model'])] = r

    results, fatal = [], False
    for path in presets:
        name = os.path.basename(path)[:-len('.json')]
        try:
            with open(path) as f:
                preset = json.load(f)
            existing = {k: v for k, v in by_key.items()
                        if k[0] == preset['id']}
            results.append(refresh_preset(preset, existing, apply=args.apply))
        except Exception as e:  # noqa: BLE001 — one carrier must never kill the sweep
            results.append({'preset': name, 'status': 'error', 'models': 0,
                            'added': [], 'changed': 0, 'unchanged': 0,
                            'removed': [], 'changed_preview': [],
                            'unpriced_new': [], 'applied': None,
                            'probe_row': None,
                            'error': f'{type(e).__name__}: {e}'})
            fatal = True

    text, summary = _report(results, t0, args.apply)
    if args.json:
        print(text, file=sys.stderr)
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(text)
    return 1 if fatal else 0


if __name__ == '__main__':
    sys.exit(main())

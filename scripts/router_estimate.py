#!/usr/bin/env python3
"""router_estimate.py — cost preview for a project's chain (TR-030).

Answers "what will dispatching this project cost?" BEFORE the spawn: resolve
the project's chain exactly as router_spawn.py does (subprocess, same argv
the scheduler uses — no duplicated resolution logic, no drift), then price
each hop with the PUBLIC list prices from the registry.

Pricing model (Bane 2026-08-27 cost-reporting doctrine — public prices):
  per-hop blended $ = tokens_total / 1e6 * public_price
  per-hop split $   = tin/1e6 * public_in_per_m + tout/1e6 * public_out_per_m
  When the public in/out split is known it is authoritative; the blended
  public_price (usd_1m, same field router_spawn reports as usd_1m) is the
  fallback and always present on a priced hop. `estimate_basis` says which.

PAYG vs subscription: the lane's provider row (providers table, `plan`
field) classifies it — plan == 'PAYG' → metered per-token billing; any other
plan name (flat/bucket/contributor/free) is a subscription whose per-token
cost is pre-paid, so the $ figure is the *list* rate, not necessarily what
bills. `billing` field says which, per hop and in the summary.

Fail-open (same doctrine as spawn): if the resolver errors or returns an
empty chain, output a structured object with `error` and exit 0. A priced
hop always carries numbers; None prices render as null and are excluded
from totals, never fabricated.

CLI:
  router_estimate.py --project X [--tokens-in N] [--tokens-out N] [--json]
    --project     required; resolved via the registry project → profile
    --tokens-in   default 100000
    --tokens-out  default 100000
    --json        pure machine-parseable JSON (default output IS json; the
                  flag exists for contract symmetry with the other tools)
Exit: 0 always (estimate tool, fail-open), 2 on usage error (argparse).
"""
import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_SPAWN = os.path.join(_HERE, 'router_spawn.py')
PY = sys.executable or 'python3'

DEFAULT_TOKENS = 100000


def resolve_chain(project):
    """router_spawn.py <project> --format json → parsed doc (or error doc)."""
    try:
        proc = subprocess.run(
            [PY, _SPAWN, project, '--format', 'json'],
            capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001 — fail-open
        return {'error': f'router_spawn.py failed to run: '
                         f'{type(e).__name__}: {e}'}
    if proc.returncode != 0:
        return {'error': f'router_spawn.py exited {proc.returncode}: '
                         f'{proc.stderr.strip()[:200]}'}
    try:
        doc = json.loads(proc.stdout)
    except Exception as e:
        return {'error': f'router_spawn.py stdout not JSON: '
                         f'{type(e).__name__}: {e}'}
    if not isinstance(doc, dict):
        return {'error': 'router_spawn.py stdout was not a JSON object'}
    return doc


def _plan_of(prov_row):
    """(billing, plan_name) — providers.plan == 'PAYG' → metered per-token."""
    plan = prov_row.get('plan')
    if plan == 'PAYG':
        return 'payg', plan
    return 'subscription', plan


def _hop_cost(m, tin, tout):
    """(cost_usd, basis, in_cost, out_cost) from a registry models row.

    Public in/out split is authoritative when known (matches the actual
    blended mix only if tin==tout — the blended public_price assumes a
    balanced token mix, the split prices the given mix exactly).
    """
    pin, pout = m.get('public_in_per_m'), m.get('public_out_per_m')
    blended = m.get('public_price')
    if blended is None:
        blended = m.get('normalized_price')
    if pin is not None and pout is not None:
        in_cost = tin / 1e6 * pin
        out_cost = tout / 1e6 * pout
        return in_cost + out_cost, 'public_in_out_split', in_cost, out_cost
    if blended is not None:
        cost = (tin + tout) / 1e6 * blended
        return cost, 'public_blended', None, None
    return None, 'unpriced', None, None


def estimate(project, tin, tout):
    doc = resolve_chain(project)
    if doc.get('error'):
        return {'project': project, 'tokens_in': tin, 'tokens_out': tout,
                'error': doc['error'], 'head': None, 'top': [],
                'chain_estimated': 0}
    # plan classification from the registry providers table — via spawn's
    # loader so ROUTING_REGISTRY / data/tables fallback stay identical.
    plans = {}
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import router_spawn
        tables = router_spawn._load_registry()
    except Exception:
        tables = {}
    for row in tables.get('providers') or []:
        pid = row.get('id')
        if pid:
            billing, plan = _plan_of(row)
            plans[pid] = {'billing': billing, 'plan': plan}

    hops = doc.get('chain') or []
    priced = []
    for h in hops:
        if not isinstance(h, dict) or not h.get('provider'):
            continue
        pair_doc = plans.get(h['provider']) or {'billing': 'unknown', 'plan': None}
        cost, basis, in_cost, out_cost = _hop_cost(
            {'public_price': h.get('usd_1m'),
             'public_in_per_m': h.get('in_per_m'),
             'public_out_per_m': h.get('out_per_m')},
            tin, tout)
        priced.append({
            'hop': h.get('hop'),
            'provider': h['provider'],
            'model': h.get('model'),
            'billing': pair_doc['billing'],
            'plan': pair_doc['plan'],
            'usd_1m': h.get('usd_1m'),
            'in_per_m': h.get('in_per_m'),
            'out_per_m': h.get('out_per_m'),
            'estimate_basis': basis,
            'in_cost_usd': round(in_cost, 6) if in_cost is not None else None,
            'out_cost_usd': round(out_cost, 6) if out_cost is not None else None,
            'cost_usd': round(cost, 6) if cost is not None else None,
            'fallback': bool(h.get('fallback')),
        })
    unpriced = [p for p in priced if p['cost_usd'] is None]
    priced_hops = [p for p in priced if p['cost_usd'] is not None]
    total = round(sum(p['cost_usd'] for p in priced_hops), 6)
    head = priced[0] if priced else None
    return {
        'project': project,
        'profile': doc.get('profile'),
        'resolved_at': doc.get('resolved_at'),
        'gate': doc.get('gate'),
        'degraded_fallback': doc.get('degraded_fallback'),
        'source': doc.get('source'),
        'tokens_in': tin,
        'tokens_out': tout,
        'price_basis': 'public list prices (registry public_price / '
                       'public_in_per_m + public_out_per_m)',
        'head': head,
        'top': priced[1:4],
        'chain': priced,
        'chain_estimated': len(priced),
        'unpriced_hops': [f"{p['provider']}/{p['model']}" for p in unpriced],
        'totals': {
            'chain_cost_usd': total,
            'head_cost_usd': head['cost_usd'] if head else None,
            'payg_hops': sum(1 for p in priced if p['billing'] == 'payg'),
            'subscription_hops': sum(1 for p in priced
                                     if p['billing'] == 'subscription'),
            'unknown_billing_hops': sum(1 for p in priced
                                        if p['billing'] == 'unknown'),
        },
    }


def render_text(e):
    lines = []
    if e.get('error'):
        return f"ERROR: {e['error']}"
    t = e['tokens_in']
    o = e['tokens_out']
    lines.append(f"▶ estimate {e['project']}  profile={e.get('profile')}  "
                 f"gate={e.get('gate')}  tokens {t}/{o}")
    if e.get('degraded_fallback'):
        lines.append('  (DEGRADED fallback chain)')

    def line(p, tag):
        if p is None:
            return '  (no hops)'
        cost = f"${p['cost_usd']:.4f}" if p['cost_usd'] is not None else 'UNPRICED'
        bills = p['billing']
        plan = f" ({p['plan']})" if p['plan'] else ''
        return (f"  {tag}{p['hop']}: {p['provider']}/{p['model']:<28} "
                f"{cost:<10} {bills}{plan}")

    lines.append(line(e['head'], 'HEAD '))
    for p in e['top']:
        lines.append(line(p, 'hop  '))
    tot = e['totals']
    lines.append(f"  chain total (all {e['chain_estimated']} priced hops): "
                 f"${tot['chain_cost_usd']:.4f}"
                 f"  · payg {tot['payg_hops']} · subscription "
                 f"{tot['subscription_hops']}"
                 + (f" · unpriced {len(e['unpriced_hops'])}"
                    if e['unpriced_hops'] else ''))
    if e['unpriced_hops']:
        lines.append(f"  unpriced (excluded from totals): "
                     f"{', '.join(e['unpriced_hops'])}")
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Per-hop cost preview for a project chain (public prices)')
    ap.add_argument('--project', required=True)
    ap.add_argument('--tokens-in', type=int, default=DEFAULT_TOKENS)
    ap.add_argument('--tokens-out', type=int, default=DEFAULT_TOKENS)
    ap.add_argument('--json', action='store_true',
                    help='pure JSON on stdout (output is JSON by default; '
                         'flag kept for --json contract symmetry)')
    ap.add_argument('--format', choices=['json', 'text'], default='json',
                    help='json (default) or text')
    args = ap.parse_args(argv)
    if args.tokens_in < 0 or args.tokens_out < 0:
        print(json.dumps({'error': '--tokens-in/--tokens-out must be >= 0'}))
        return 0
    e = estimate(args.project, args.tokens_in, args.tokens_out)
    if args.json or args.format == 'json':
        print(json.dumps(e, indent=1))
    else:
        print(render_text(e))
    return 0


if __name__ == '__main__':
    sys.exit(main())

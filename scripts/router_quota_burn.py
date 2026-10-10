#!/usr/bin/env python3
"""router_quota_burn.py — quota L3: the burn-surplus finder (TR-211).

WHY (docs/quota-layers-spec.md section 5, consumer 3): the quota plane can now
SEE a window (L0 declaration, L1 readback, L2 accounting view — TR-206..209)
and DECIDE per call (L3 pacing gate, TR-210), but nothing answers the
owner's actual question: "which provider quota is about to EXPIRE UNUSED, so
I can pick a project to burn it on?" A subscription window that resets with
money still in it is value thrown away — and spending it costs $0 marginal
cash, which is the whole point. This module is that projection, per window:

    expected_unused = remaining - (current_rate * time_to_reset)

    current_rate  = spent_in_window / seconds_elapsed   (the smooth-spend
                   rate; a window that just started has rate ~ 0 — that is
                   fine and reported, spec section 5.3)
    time_to_reset = resets_at - now

Positive expected_unused => surface it: provider, window, surplus in its
unit, reset time, and the marginal cash cost of spending it. Negative =>
no burn recommendation. It NEVER auto-spends: the output is a report and a
recommendation artifact only — burning is the owner's call per project.

LAYER DISCIPLINE (spec section 1): L3 consumes the L2 VIEW
(router_quota_accounting.account / account_ledger) — window math is NOT
reimplemented, the L2 blocks are taken verbatim (remaining_usd, resets_at,
spent_usd carry the L2 reasons with them). NO SECOND STORE: this module
WRITES NOTHING, reads nothing but the ledger and the limit config, makes no
network call, spawns nothing — find_surplus is a pure function over blocks,
and the only I/O is the read-only find_surplus_ledger (the same shape as the
accounting/pacing modules of this family).

ELAPSED TIME (the one projection-specific choice): the L2 block does not
carry "seconds elapsed", so it is derived from the SAME window arithmetic the
L2 view uses (router_quota_accounting.window_bounds — never a second clock):
  calendar windows (weekly/monthly): elapsed = now - window_start.
  rolling windows: the window opens on the first in-window spend, so
    elapsed = window_seconds - time_to_reset  (== now - oldest row).
  a rolling window with NO in-window rows has no reset instant at all
  (L2 leaves resets_at open) — the projection is UNKNOWN with that reason,
  never a fabricated rate.

HONESTY CONTRACT (spec section 6, enforced):
  - no fake zeros: an unlimited or unpriceable window has remaining_usd NULL
    (the L2 reason travels) => verdict 'unknown' + reason, NEVER surplus 0;
  - an estimate never renders as an observation: every entry carries
    `estimated: true` and `basis: 'derived-from-ledger'` — the surplus is a
    projection from PAST spend, not a provider-reported number;
  - a subscription window is never reported as free money without naming
    that it is subscription-covered: every entry carries `cost_basis`
    ('subscription-covered' | 'metered' | 'unknown') and
    `subscription_covered` (true/false/null). The cost basis comes from the
    L2 limit config (`plan_kind` per provider) or an explicit --plan-kind
    override; undeclared => 'unknown' and the marginal cash is NULL with the
    reason, never guessed to $0.
  - marginal cash: 'subscription-covered' => $0.00 marginal (that is the
    point of burning it); 'metered' => the surplus itself in USD (spending
    metered quota costs the metered dollars); 'unknown' => NULL + reason.

CLI (operators; programmatic callers import find_surplus / project_window):

  burn [--provider P] [--ledger PATH] [--limits PATH] [--now WHEN]
       [--plan-kind P=K[,P=K...]] [--json]

  Exit codes (router_quota.py style): 0 ok, 2 usage/validation error
  (operator error — never masked), 1 unexpected failure (clean stderr
  message, no traceback). A --json output is a REPORT DOCUMENT printed to
  stdout — the module writes no file anywhere.
"""
import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import router_quota_accounting as acc  # noqa: E402  (the L2 view — the only data source)

REPO = os.path.dirname(_HERE)

VERDICT_BURN = 'burn'
VERDICT_NO_BURN = 'no-burn'
VERDICT_UNKNOWN = 'unknown'

BASIS_SUBSCRIPTION = 'subscription-covered'
BASIS_METERED = 'metered'
BASIS_UNKNOWN = 'unknown'

# plan_kind vocabulary: what the limit config (or --plan-kind) may declare,
# mapped to the two cost bases. A declared-but-unrecognized kind stays
# 'unknown' WITH the declaration named — never guessed.
_SUBSCRIPTION_KINDS = frozenset({
    'subscription', 'subscription-covered', 'sub', 'plan', 'included'})
_METERED_KINDS = frozenset({
    'metered', 'payg', 'pay-as-you-go', 'on-demand', 'api', 'credit'})


# ------------------------------------------------------------- plan kind ----

def classify_plan(kind):
    """A declared plan_kind -> cost basis ('subscription-covered' | 'metered'
    | 'unknown'); undeclared (None/'') -> None so the caller can tell
    'not declared' from 'declared but unrecognized'."""
    if kind is None:
        return None
    k = str(kind).strip().lower()
    if not k:
        return None
    if k in _SUBSCRIPTION_KINDS:
        return BASIS_SUBSCRIPTION
    if k in _METERED_KINDS:
        return BASIS_METERED
    return BASIS_UNKNOWN


def parse_plan_kinds(spec):
    """`--plan-kind P=K[,P=K...]` -> {provider: kind}. Malformed entries are
    operator errors (the CLI turns them into exit 2, never masked)."""
    kinds = {}
    if not spec:
        return kinds
    for part in str(spec).split(','):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            raise ValueError(
                f'--plan-kind entry {part!r} is not PROVIDER=KIND')
        p, k = part.split('=', 1)
        p, k = p.strip(), k.strip()
        if not p or not k:
            raise ValueError(
                f'--plan-kind entry {part!r} needs both a provider and a '
                'kind (PROVIDER=KIND)')
        kinds[p] = k
    return kinds


def _finite_num(v):
    return (not isinstance(v, bool) and isinstance(v, (int, float))
            and v == v and v not in (float('inf'), float('-inf')))


# -------------------------------------------------------------- the probe ----

def project_window(block, now_ts, cfg=None, plan_kind=None):
    """The burn projection for ONE L2 block — a pure function.

    block: an L2 block verbatim from router_quota_accounting (its
    remaining_usd/resets_at/spent_usd and their reasons are trusted, never
    recomputed). cfg: that provider's limit-config entry (window_hours for
    custom rolling windows, optional plan_kind). plan_kind: an explicit
    override of the config's plan_kind (wins when not None).

    Returns a JSON-ready report entry. Verdicts:
      'burn'     expected_unused > 0 — worth spending before reset;
      'no-burn'  computable but <= 0 — the window will spend itself;
      'unknown'  any input is NULL — the L2 reason (or this module's) says
                 exactly which, and NOTHING is invented in its place.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    now = float(now_ts)
    e = {
        'provider': block.get('provider'),
        'window': block.get('window'),
        'unit': 'usd',
        'limit_usd': block.get('limit_usd'),
        'spent_usd': block.get('spent_usd'),
        'remaining_usd': block.get('remaining_usd'),
        'remaining_reason': block.get('remaining_reason'),
        'resets_at': block.get('resets_at'),
        'time_to_reset_s': None,
        'seconds_elapsed': None,
        'current_rate_usd_per_s': None,
        'projected_spend_usd': None,
        'expected_unused_usd': None,
        'surplus_usd': None,
        'verdict': VERDICT_UNKNOWN,
        'burnable': False,
        'reason': None,
        'estimated': True,
        'basis': 'derived-from-ledger',
        'plan_kind': None,
        'subscription_covered': None,
        'cost_basis': BASIS_UNKNOWN,
        'marginal_cash_usd': None,
        'marginal_cash_reason': None,
    }

    # -- cost basis FIRST: even an unknown window must name its basis (AC5 —
    # a subscription window is never free money unless it says so).
    declared = plan_kind if plan_kind is not None else cfg.get('plan_kind')
    e['plan_kind'] = declared
    basis = classify_plan(declared)
    if basis is not None:
        e['cost_basis'] = basis
    e['subscription_covered'] = {BASIS_SUBSCRIPTION: True,
                                 BASIS_METERED: False}.get(e['cost_basis'])

    def _finish():
        """Marginal cash — only meaningful when there is a surplus to price."""
        if e['verdict'] != VERDICT_BURN:
            e['marginal_cash_reason'] = 'no surplus to price'
            return
        if e['cost_basis'] == BASIS_SUBSCRIPTION:
            # Subscription-covered: spending the surplus costs no cash —
            # named as such, never silent (AC5).
            e['marginal_cash_usd'] = 0.0
            e['marginal_cash_reason'] = None
        elif e['cost_basis'] == BASIS_METERED:
            e['marginal_cash_usd'] = e['surplus_usd']
            e['marginal_cash_reason'] = None
        else:
            e['marginal_cash_reason'] = (
                f"plan_kind not declared for provider "
                f"{e['provider']!r} (set plan_kind in the limit config or "
                f"pass --plan-kind) — the marginal cash cost is unknown, "
                f"not $0")

    remaining = block.get('remaining_usd')
    if not _finite_num(remaining):
        # No-limit or unpriceable-spend window: the L2 reason travels
        # verbatim. NULL + reason, never 0 (spec section 6).
        e['reason'] = (block.get('remaining_reason')
                       or 'remaining_usd missing from the L2 block')
        _finish()
        return e

    bounds = acc.window_bounds(block.get('window'), now,
                               cfg.get('window_hours'))
    if bounds is None:
        e['reason'] = (
            f"unusable window {block.get('window')!r}: the L2 window kind "
            f"must be one of {', '.join(acc.WINDOW_KINDS)}"
            + (' with a positive "window_hours" number'
               if block.get('window') == 'rolling' else ''))
        _finish()
        return e
    start, resets_calendar, span = bounds

    resets = (acc.parse_ts(block.get('resets_at'))
              if block.get('resets_at') else resets_calendar)
    if resets is None:
        e['reason'] = ('resets_at unknown: rolling window has no in-window '
                       'rows yet (it opens on the first spend), so there is '
                       'no reset instant to project against')
        _finish()
        return e
    if not block.get('resets_at'):
        # The block left resets_at open but the window is calendar: report
        # the anchor the projection actually used (AC2 — the entry names
        # the reset time), not the block's None.
        e['resets_at'] = acc._iso(resets)
    t2r = max(0.0, resets - now)

    # elapsed from the SAME window arithmetic the L2 view uses (docstring):
    elapsed = (now - start) if resets_calendar is not None else (span - t2r)
    if elapsed <= 0:
        e['reason'] = ('window has no elapsed time yet; the smooth-spend '
                       'rate is undefined, so expected_unused cannot be '
                       'projected')
        _finish()
        return e

    rate = float(block['spent_usd']) / elapsed
    projected = rate * t2r
    expected = float(remaining) - projected

    e['time_to_reset_s'] = round(t2r, 6)
    e['seconds_elapsed'] = round(elapsed, 6)
    e['current_rate_usd_per_s'] = round(rate, 12)
    e['projected_spend_usd'] = round(projected, 8)
    e['expected_unused_usd'] = round(expected, 8)
    e['surplus_usd'] = round(max(0.0, expected), 8)

    if expected > 0:
        e['verdict'] = VERDICT_BURN
        e['burnable'] = True
        e['reason'] = (f"projected spend ${projected:.4f} leaves "
                       f"${expected:.4f} of provider {e['provider']} unused "
                       f"when {e['window']} resets — a burn candidate at "
                       f"the current smooth-spend rate")
    else:
        e['verdict'] = VERDICT_NO_BURN
        e['reason'] = (f"no surplus: the current rate projects "
                       f"${projected:.4f} spent by reset against "
                       f"${float(remaining):.4f} remaining "
                       f"(expected_unused {expected:.4f}) — no burn "
                       f"recommendation")
    _finish()
    return e


def find_surplus(blocks, now_ts, plan_kinds=None, provider_cfgs=None):
    """Per-window projections over the L2 view — the pure finder.

    blocks: L2 blocks (account() output). plan_kinds: {provider: kind}
    explicit overrides. provider_cfgs: {provider: limit-config entry} (for
    window_hours and the config-side plan_kind). Sorted burn-first: burn
    entries by surplus descending, then no-burn, then unknown (providers
    alphabetical within class) — the actionable windows surface first.
    """
    now = float(now_ts)
    plan_kinds = plan_kinds or {}
    provider_cfgs = provider_cfgs or {}
    entries = [
        project_window(b, now,
                       cfg=provider_cfgs.get(b.get('provider')),
                       plan_kind=plan_kinds.get(b.get('provider')))
        for b in blocks if isinstance(b, dict)]
    rank = {VERDICT_BURN: 0, VERDICT_NO_BURN: 1, VERDICT_UNKNOWN: 2}
    entries.sort(key=lambda e: (rank.get(e['verdict'], 3),
                                -(e['surplus_usd'] or 0.0),
                                str(e['provider'])))
    return entries


def summarize(entries):
    """The report's headline counts: verdict census + total surplus that is
    actually burnable (burn verdicts only — an unknown window contributes
    nothing, not even to a sum)."""
    burn = [e for e in entries if e['verdict'] == VERDICT_BURN]
    return {
        'burn': len(burn),
        'no_burn': sum(1 for e in entries
                       if e['verdict'] == VERDICT_NO_BURN),
        'unknown': sum(1 for e in entries
                       if e['verdict'] == VERDICT_UNKNOWN),
        'total_surplus_usd': round(sum(e['surplus_usd'] for e in burn), 8),
    }


def find_surplus_ledger(path=None, limits=None, now_ts=None,
                        plan_kinds=None):
    """The view over the ledger FILE — the only I/O path (read-only).

    Same conventions as account_ledger: `limits` may be a loaded config dict
    or a PATH (loaded here, at query time); the ledger is read through the
    L2 view (router_quota_accounting.account_ledger) so window math lives in
    exactly one place. WRITES NOTHING.
    """
    path = path or acc.router_outcomes.outcomes_path()
    if isinstance(limits, str):
        limits = acc.load_limits(limits)
    limits = limits if limits is not None else acc.load_limits(acc.limits_path())
    now = now_ts if now_ts is not None else time.time()
    blocks = acc.account_ledger(path=path, limits=limits, now_ts=now)
    return find_surplus(blocks, now, plan_kinds=plan_kinds,
                        provider_cfgs=limits.get('providers') or {})


# ------------------------------------------------------------------ CLI ----

def _fmt_usd(v):
    return '-' if v is None else f'{v:.4f}'


def _print_human(entries, ledger, limits_file, as_of):
    print(f'L3 quota burn-surplus — projection over {ledger}')
    print(f'  as of {as_of} · limits: {limits_file}')
    if not entries:
        print('  no windows (empty ledger, empty limit config)')
        return 0
    s = summarize(entries)
    print(f"  {s['burn']} burnable · {s['no_burn']} no-burn · "
          f"{s['unknown']} unknown · total burnable surplus "
          f"${s['total_surplus_usd']:.4f}")
    for e in entries:
        print(f"  {str(e['provider']):<22} {str(e['window']):<13} "
              f"surplus=${_fmt_usd(e['surplus_usd'])} "
              f"expected_unused=${_fmt_usd(e['expected_unused_usd'])} "
              f"remaining=${_fmt_usd(e['remaining_usd'])} "
              f"resets={e['resets_at'] or '-'}")
        print(f"    verdict: {e['verdict']} — {e['reason']}")
        mc = ('-' if e['marginal_cash_usd'] is None
              else f"${e['marginal_cash_usd']:.4f}")
        print(f"    cost basis: {e['cost_basis']} · marginal cash {mc}"
              + (f" ({e['marginal_cash_reason']})"
                 if e['marginal_cash_reason'] else ''))
        if e['remaining_reason']:
            print(f"    remaining: {e['remaining_reason']}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='router_quota_burn.py',
        description='Quota L3 burn-surplus finder (TR-211): per provider '
                    'window, project expected_unused = remaining - '
                    '(current_rate * time_to_reset) from the L2 accounting '
                    'view and surface windows that will expire unused. '
                    'Report only — never auto-spends, writes nothing.')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    pb = sub.add_parser('burn', help='the one verb: the burn-surplus report')
    pb.add_argument('--provider', default=None,
                    help='restrict the report to one provider')
    pb.add_argument('--ledger', default=None,
                    help='outcome ledger (default: $ROUTING_OUTCOMES_FILE '
                         'or data/state/outcomes.jsonl)')
    pb.add_argument('--limits', default=None,
                    help='limit config JSON (default: '
                         f'${acc.LIMITS_ENV} or {acc.DEFAULT_LIMITS_FILE})')
    pb.add_argument('--now', default=None,
                    help='freeze the clock: ISO-8601 or epoch seconds '
                         '(default: actual now)')
    pb.add_argument('--plan-kind', dest='plan_kind', default=None,
                    help='explicit cost-basis override, PROVIDER=KIND '
                         'comma-separated (KIND: subscription | metered | '
                         '...; wins over the limit config plan_kind)')
    pb.add_argument('--json', action='store_true')

    a = parser.parse_args(argv)
    try:
        if a.command != 'burn':
            parser.error('burn is the one verb (TR-211)')
        if a.provider is not None and not str(a.provider).strip():
            parser.error('provider is empty')
        now_ts = time.time()
        if a.now is not None:
            now_ts = acc.parse_ts(a.now)
            if now_ts is None:
                raise ValueError(
                    f'--now {a.now!r} is not ISO-8601 or epoch seconds')
        plan_kinds = parse_plan_kinds(a.plan_kind)
        ledger = (os.path.abspath(os.path.expanduser(a.ledger))
                  if a.ledger else acc.router_outcomes.outcomes_path())
        lpath = acc.limits_path(a.limits)
        limits = acc.load_limits(lpath)
        entries = find_surplus_ledger(path=ledger, limits=limits,
                                      now_ts=now_ts, plan_kinds=plan_kinds)
        if a.provider:
            want = str(a.provider).strip()
            entries = [e for e in entries if e['provider'] == want]
        as_of = acc._iso(now_ts)
        if a.json:
            print(json.dumps({'as_of': as_of, 'ledger': ledger,
                              'limits_file': lpath,
                              'limits_present': os.path.exists(lpath),
                              'summary': summarize(entries),
                              'windows': entries}, indent=1))
        else:
            _print_human(entries, ledger, lpath, as_of)
        return 0
    except ValueError as e:
        # Operator input error: the parser's own channel (exit 2), so a typo
        # is DETECTABLE and never reads as success (router_quota.py style).
        parser.error(str(e))
    except Exception as e:  # noqa: BLE001  fail-open: clean message, no traceback
        print(f'router_quota_burn error: {type(e).__name__}: {e}',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

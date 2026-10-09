#!/usr/bin/env python3
"""router_quota_accounting.py — quota L2: accounting view over the outcome ledger (TR-209).

WHY (docs/quota-layers-spec.md §4): the quota plane knows the LIMITS (L0,
data/tables/provider_quota.jsonl) and, for some providers, what the provider
SAYS is left (L1 readback, TR-207/TR-208). What it could not answer is "how
much have WE ALREADY SPENT in the current window" — per provider, per window,
spent / remaining / resets_at / headroom. The ledger already holds every row we
need (tokens + cost + ts per session), so L2 is a QUERY over
data/state/outcomes.jsonl, computed on demand, impossible to drift:

    spent      = sum(unit-equivalent usage) over rows in the window
    remaining  = limit - spent        (limit NULL => remaining NULL, reason kept)
    resets_at  = rolling: oldest in-window row + window
                 calendar: next anchor (week start + 7d / 1st of next month)
    headroom   = remaining / seconds_to_reset   (the pacing budget for L3)

NO SECOND STORE (the single most important design choice, spec §1): this module
WRITES NOTHING — no counter file, no cache, no state. It reads the outcome
ledger via router_outcomes.load_outcome_rows() (the same tolerant scan the
averages refresh uses) and reads the limit CONFIG at query time. Delete the
ledger and the accounting is empty; append a row and the accounting moves.
Mutate the ledger and the numbers move with it — there is nothing else to get
out of sync.

LIMIT CONFIG (AC6) — a simple JSON file read at QUERY time, never stored state:

    data/quota_limits.json      (default; override: $ROUTER_QUOTA_LIMITS_FILE)

    {
      "_comment": "...",
      "default_window": "rolling_5h",         // window for providers without one
      "providers": {
        "<provider_id>": {
          "window": "rolling_5h",             // rolling_5h | rolling_daily | daily |
                                              // rolling (+ window_hours) | weekly | monthly
          "window_hours": 24,                 // only for window == "rolling"
          "limit_usd": 10.0                   // number, or null/absent = no limit
        }
      }
    }

Missing file is normal (nothing is configured — every provider then reports
remaining=null with the reason); a CORRUPT file warns on stderr and fails open
to "no limits" (this module is diagnostic and must never break a caller).
Providers absent from the config still get a block: their spend is visible and
their remaining carries `reason: "no limit configured for provider X"`.

UNIT CONVERSION (AC3): a row's own measured `cost_usd` is ground truth and wins.
A row WITHOUT a cost is priced through router_outcomes.plan_effective_cost() —
the SAME single function the cost layer uses (TR-070 price table,
public/normalized split + plan ratio), so the tokens→usd equivalence lives in
one place, tested once. A lane with no declared price yields NULL — never a
fabricated 0 — while the row still counts in the token sums. Tests inject
prices via router_outcomes._PRICE_MAP (the documented module cache).

PER-PROVIDER KEYING (AC5): rows are grouped by `provider` ONLY — never
provider+model. A provider with 498 lanes and one $10 window is ONE budget;
the lane/model count never multiplies it (spec §4).

WINDOW BOUNDARIES (deliberate, AC2-tested):
  rolling windows:  start < ts <= now      (a row exactly at the start edge has
                                            rolled OFF; at `now` it counts)
  calendar windows: start <= ts < resets   (a row exactly at the month/week
                                            start counts; the reset instant does not)
  future-dated rows and rows with no parseable ts never count (bad clocks must
  not inflate a budget) — they are reported in n_rows_outside_window.
  Sums are order-independent: the rolling reset derives from min(ts), not from
  file order.

CLI (AC7 — reachable as `router quota accounting` via task_router.cli, or
standalone `router_quota_accounting.py`):

  accounting [--provider P] [--ledger PATH] [--limits PATH] [--now WHEN] [--json]

  Exit codes (router_quota.py style): 0 ok, 2 usage/validation error
  (operator error — never masked), 1 unexpected failure (clean stderr
  message, no traceback).

Per-provider block: provider, window, spent_usd (+ unpriced accounting),
token sums, limit_usd, remaining_usd (NULL + reason when unknown), resets_at,
headroom_usd_per_s. `--now` accepts ISO-8601 or epoch so operators and tests
can freeze the clock.
"""
import argparse
import datetime
import json
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import router_outcomes  # noqa: E402  (the ledger engine — the ONLY data source)

REPO = os.path.dirname(_HERE)
DEFAULT_LIMITS_FILE = os.path.join(REPO, 'data', 'quota_limits.json')
LIMITS_ENV = 'ROUTER_QUOTA_LIMITS_FILE'
DEFAULT_WINDOW = 'rolling_5h'
WINDOW_KINDS = ('rolling_5h', 'rolling_daily', 'daily', 'rolling', 'weekly',
                'monthly')


# ------------------------------------------------------------------ config ---

def limits_path(explicit=None):
    """Resolved limit-config path: --limits flag > env > repo default."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(LIMITS_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_LIMITS_FILE


def load_limits(path):
    """The limit config, read at QUERY time (never cached, never stored).

    Missing file -> default document (nothing configured — honest, silent).
    Corrupt/malformed -> the SAME default document plus one stderr warning
    (fail-open: a diagnostic must never break a caller, but a broken config
    must not fail silently either).
    """
    default = {'default_window': DEFAULT_WINDOW, 'providers': {}}
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            doc = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f'router_quota_accounting: unreadable limit config {path}: '
              f'{type(e).__name__}: {e} — continuing with NO limits configured',
              file=sys.stderr)
        return default
    if not isinstance(doc, dict):
        print(f'router_quota_accounting: limit config {path} is not a JSON '
              'object — continuing with NO limits configured', file=sys.stderr)
        return default
    providers = doc.get('providers')
    return {'default_window': doc.get('default_window') or DEFAULT_WINDOW,
            'providers': providers if isinstance(providers, dict) else {}}


# ------------------------------------------------------------------- time ----

def parse_ts(value):
    """Ledger `ts` -> epoch seconds (float) or None (never guess).

    Accepts the epoch numbers normalize_row writes and ISO-8601 strings
    (naive ISO is read as UTC, matching router_quota.parse_reset).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            pass
        else:
            return f if math.isfinite(f) else None
        try:
            dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    return None


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
        .isoformat(timespec='seconds')


def window_bounds(kind, now_ts, window_hours=None):
    """(start_ts, resets_at_ts|None, seconds) for a window kind, or None when
    the kind is unusable. resets_at None = rolling: derived per provider from
    the OLDEST in-window row (spec §4), not from the window arithmetic."""
    now = float(now_ts)
    now_dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    if kind == 'weekly':
        start_dt = (now_dt - datetime.timedelta(days=now_dt.weekday())) \
            .replace(hour=0, minute=0, second=0, microsecond=0)
        resets_dt = start_dt + datetime.timedelta(days=7)
        return start_dt.timestamp(), resets_dt.timestamp(), 7 * 86400.0
    if kind == 'monthly':
        start_dt = now_dt.replace(day=1, hour=0, minute=0, second=0,
                                  microsecond=0)
        if now_dt.month == 12:
            resets_dt = start_dt.replace(year=now_dt.year + 1, month=1)
        else:
            resets_dt = start_dt.replace(month=now_dt.month + 1)
        return (start_dt.timestamp(), resets_dt.timestamp(),
                (resets_dt - start_dt).total_seconds())
    if kind == 'rolling':
        try:
            hours = float(window_hours)
        except (TypeError, ValueError):
            return None
        if not hours > 0:
            return None
    elif kind == 'rolling_5h':
        hours = 5.0
    elif kind in ('rolling_daily', 'daily'):
        hours = 24.0
    else:
        return None
    seconds = hours * 3600.0
    return now - seconds, None, seconds


def _in_window(ts, bounds, now_ts):
    """Window membership — boundary semantics pinned by tests (AC2):
    rolling (resets None): start < ts <= now; calendar: start <= ts < resets."""
    start, resets, _seconds = bounds
    if ts > now_ts:
        return False            # future-dated rows never count
    if resets is None:
        return start < ts <= now_ts
    return start <= ts < resets


# --------------------------------------------------------------- conversion --

def _convert_usd(row):
    """(usd|None, klass, detail) for one ledger row.

    The row's own measured cost_usd wins. Otherwise the router's price-table
    conversion — router_outcomes.plan_effective_cost, the SAME single function
    the cost layer uses (TR-070) — prices the row's tokens. No declared price
    -> (None, 'unknown-price', reason): NULL travels, never a fabricated 0.
    """
    c = row.get('cost_usd')
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        return float(c), 'row-cost', None
    tin = row.get('tokens_in')
    tout = row.get('tokens_out')
    has_tin = isinstance(tin, (int, float)) and not isinstance(tin, bool) and tin
    has_tout = (isinstance(tout, (int, float)) and not isinstance(tout, bool)
                and tout)
    if not has_tin and not has_tout:
        return None, 'no-usage', None
    cost, basis = router_outcomes.plan_effective_cost(
        row.get('provider'), row.get('model'), tin or 0, tout or 0)
    if cost is None:
        return None, 'unknown-price', basis
    return float(cost), 'price-table', basis


def _num_or_0(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v) if math.isfinite(v) else 0


# --------------------------------------------------------------- accounting --

def account_provider(provider, rows, cfg, now_ts, default_window=DEFAULT_WINDOW):
    """The L2 block for ONE provider over ITS rows (already provider-keyed).

    Rows are whatever the caller scoped to this provider — the grouping (by
    `provider` ONLY) lives in account(). Every NULL carries its reason
    (Bane's null-with-reason law); unknown is never 0.
    """
    now = float(now_ts)
    cfg = cfg if isinstance(cfg, dict) else {}
    block = {
        'provider': provider,
        'window': None, 'window_start': None, 'as_of': _iso(now),
        'n_rows': 0, 'n_rows_outside_window': 0,
        'tokens_in': 0, 'tokens_out': 0, 'tokens_reasoning': 0,
        'tokens_total': 0,
        'spent_usd': None, 'unpriced_rows': 0, 'unpriced_reason': None,
        'limit_usd': None, 'remaining_usd': None, 'remaining_reason': None,
        'resets_at': None, 'headroom_usd_per_s': None,
    }

    limit_raw = cfg.get('limit_usd')
    limit = None
    if limit_raw is None:
        block['remaining_reason'] = f'no limit configured for provider {provider}'
    elif isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, float)) \
            or not math.isfinite(limit_raw):
        block['remaining_reason'] = (
            f'unusable limit config: limit_usd must be a finite number or '
            f'null, got {limit_raw!r}')
    else:
        limit = float(limit_raw)
        block['remaining_reason'] = None
    block['limit_usd'] = limit

    kind = cfg.get('window') or default_window
    block['window'] = kind
    if kind not in WINDOW_KINDS:
        block['remaining_reason'] = (
            f'unusable limit config: unknown window kind {kind!r} '
            f'(expected one of {", ".join(WINDOW_KINDS)})')
        return block
    bounds = window_bounds(kind, now, cfg.get('window_hours'))
    if bounds is None:
        block['remaining_reason'] = (
            f'unusable limit config: window {kind!r} needs a positive '
            '"window_hours" number')
        return block
    start, resets, seconds = bounds
    block['window_start'] = _iso(start)

    tin = tout = treason = n_rows = n_out = 0
    usd = 0.0
    priced = unknown_rows = no_usage_rows = 0
    unknown_details = set()
    oldest = None
    for r in rows:
        if not isinstance(r, dict):
            n_out += 1
            continue
        ts = parse_ts(r.get('ts'))
        if ts is None or not _in_window(ts, bounds, now):
            n_out += 1
            continue
        n_rows += 1
        oldest = ts if oldest is None else min(oldest, ts)
        tin += _num_or_0(r.get('tokens_in'))
        tout += _num_or_0(r.get('tokens_out'))
        treason += _num_or_0(r.get('tokens_reasoning'))
        cost, klass, detail = _convert_usd(r)
        if cost is None:
            if klass == 'unknown-price':
                unknown_rows += 1
                unknown_details.add(detail or 'no declared price')
            else:
                no_usage_rows += 1
        else:
            usd += cost
            priced += 1

    block['n_rows'] = n_rows
    block['n_rows_outside_window'] = n_out
    block['tokens_in'] = tin
    block['tokens_out'] = tout
    block['tokens_reasoning'] = treason
    block['tokens_total'] = tin + tout + treason

    parts = []
    if unknown_rows:
        parts.append(f'{unknown_rows} row(s) with no declared price '
                     f'({"; ".join(sorted(unknown_details))})')
    if no_usage_rows:
        parts.append(f'{no_usage_rows} row(s) with no token usage to price')
    if parts:
        # unpriced_rows counts the UNKNOWN-PRICE debt only: a row with no
        # token usage genuinely contributes $0, it is not a missing price.
        block['unpriced_rows'] = unknown_rows
        block['unpriced_reason'] = '; '.join(parts)

    if n_rows == 0:
        block['spent_usd'] = 0.0        # an empty window is a MEASURED zero
    elif unknown_rows:
        # SQL-sum semantics (AC3): a NULL-priced term makes the SUM null —
        # an understated budget would fake headroom. Token sums stay exact
        # and the reason names the unpriceable rows.
        block['spent_usd'] = None
    else:
        block['spent_usd'] = round(usd, 8)   # priced rows + genuine zeros

    if resets is None:                  # rolling: oldest in-window row + window
        if oldest is not None:
            resets = oldest + seconds
    block['resets_at'] = _iso(resets)

    if limit is not None:
        if block['spent_usd'] is None:
            block['remaining_reason'] = (
                'spent unknown: ' + (block['unpriced_reason']
                                     or 'no row in window could be priced'))
        else:
            block['remaining_usd'] = round(limit - block['spent_usd'], 8)
            block['remaining_reason'] = None
    if block['remaining_usd'] is not None and resets is not None \
            and resets > now:
        block['headroom_usd_per_s'] = round(
            block['remaining_usd'] / (resets - now), 12)
    return block


def account(rows, limits, now_ts):
    """Per-provider blocks over ledger rows — the L2 VIEW (pure function).

    Grouping is by `provider` ONLY (spec §4: the lane count never multiplies
    a budget). Providers from the config appear even with zero rows, so a
    configured-but-idle budget is visible instead of missing.
    """
    now = float(now_ts)
    by_provider = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = r.get('provider')
        if p is None:
            continue        # unattributable usage cannot sit in a provider budget
        by_provider.setdefault(str(p), []).append(r)
    providers_cfg = limits.get('providers') or {}
    default_window = limits.get('default_window') or DEFAULT_WINDOW
    return [account_provider(p, by_provider.get(p, []),
                             providers_cfg.get(p) or {}, now,
                             default_window=default_window)
            for p in sorted(set(by_provider) | set(providers_cfg))]


def account_ledger(path=None, limits=None, now_ts=None):
    """The view over the ledger FILE — the only I/O path (read-only).

    `limits` may be a loaded config dict or a PATH to a config file (loaded
    here, still at query time — callers that query repeatedly pass the doc
    and re-load it themselves so an edit between queries is honored).
    """
    path = path or router_outcomes.outcomes_path()
    if isinstance(limits, str):
        limits = load_limits(limits)
    limits = limits if limits is not None else load_limits(limits_path())
    rows = router_outcomes.load_outcome_rows(path)
    return account(rows, limits, now_ts if now_ts is not None else time.time())


# -------------------------------------------------------------------- CLI ----

def _fmt_usd(v):
    return '-' if v is None else f'{v:.4f}'


def _print_human(blocks, ledger, limits_file, as_of):
    print(f'L2 quota accounting — view over {ledger}')
    print(f'  as of {as_of} · limits: {limits_file}')
    if not blocks:
        print('  no providers (empty ledger, empty limit config)')
        return 0
    for b in blocks:
        print(f"  {b['provider']:<22} {str(b['window']):<13} "
              f"spent=${_fmt_usd(b['spent_usd'])} "
              f"tokens={b['tokens_in']}/{b['tokens_out']} "
              f"limit=${_fmt_usd(b['limit_usd'])} "
              f"remaining={_fmt_usd(b['remaining_usd'])} "
              f"resets={b['resets_at'] or '-'}")
        if b['remaining_reason']:
            print(f"    remaining: {b['remaining_reason']}")
        if b['unpriced_reason']:
            print(f"    spent: {b['unpriced_reason']}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='router_quota_accounting.py',
        description='Quota L2 accounting: per-provider spent/remaining/resets_at '
                    'derived ONLY as a query over the outcome ledger '
                    '(TR-209; no second store). Limits come from a JSON config '
                    'read at query time.')
    # A subcommand keeps the dispatch shape of router_quota.py (`router quota
    # accounting`) and leaves room for future verbs; only this one exists.
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    p_acc = sub.add_parser('accounting', help='per-provider L2 view (the one verb)')
    for p in (parser, p_acc):
        p.add_argument('--provider', default=None,
                       help='restrict the view to one provider')
        p.add_argument('--ledger', default=None,
                       help='outcome ledger (default: $ROUTING_OUTCOMES_FILE '
                            f'or {router_outcomes._DEFAULT_OUTCOMES})')
        p.add_argument('--limits', default=None,
                       help='limit config JSON (default: '
                            f'${LIMITS_ENV} or {DEFAULT_LIMITS_FILE})')
        p.add_argument('--now', default=None,
                       help='freeze the clock: ISO-8601 or epoch seconds '
                            '(default: actual now)')
        p.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.provider is not None and not str(args.provider).strip():
            parser.error('provider is empty')
        now_ts = time.time()
        if args.now is not None:
            now_ts = parse_ts(args.now)
            if now_ts is None:
                raise ValueError(
                    f'--now {args.now!r} is not ISO-8601 or epoch seconds')
        ledger = (os.path.abspath(os.path.expanduser(args.ledger))
                  if args.ledger else router_outcomes.outcomes_path())
        lpath = limits_path(args.limits)
        limits = load_limits(lpath)
        blocks = account_ledger(path=ledger, limits=limits, now_ts=now_ts)
        if args.provider:
            want = str(args.provider).strip()
            blocks = [b for b in blocks if b['provider'] == want]
        as_of = _iso(now_ts)
        if args.json:
            print(json.dumps({'as_of': as_of, 'ledger': ledger,
                              'limits_file': lpath,
                              'limits_present': os.path.exists(lpath),
                              'providers': blocks}, indent=1))
        else:
            _print_human(blocks, ledger, lpath, as_of)
        return 0
    except ValueError as e:
        # Operator input error: the parser's own channel (exit 2), so a typo
        # is DETECTABLE and never reads as success (router_quota.py style).
        parser.error(str(e))
    except Exception as e:  # noqa: BLE001  fail-open: clean message, no traceback
        print(f'router_quota_accounting error: {type(e).__name__}: {e}',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

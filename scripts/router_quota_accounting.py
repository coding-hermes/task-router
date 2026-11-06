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

PER-PROVIDER KEYING (AC5): rows are grouped by `provider` — never
provider+model. A provider with 498 lanes and one $10 window is ONE budget;
the lane/model count never multiplies it (spec §4).

MULTI-ACCOUNT KEYING (TR-216, AC3): when the limit config declares an
`account` on a provider entry AND the ledger rows carry an `account` field,
spend is grouped and reported PER (provider, account) — two $200 plans of the
same provider (xkiro/xkiro-2) are two budgets, never merged into one window.
Rows WITHOUT an `account` field fall in the config's `default_account` ("default");
rows whose account is not configured get the per-account defaults resolved
from the provider's (first) config entry — spend is always visible, remaining
carries the no-limit reason. A config entry that declares no `account` (the
normal single-account shape) keeps the byte-identical pre-TR-216 behavior:
ONE block per provider, every row of the provider summed into it.

L0-DERIVED ACCOUNT RESOLUTION (TR-216, the real-ledger shape): the outcome
ledger does not carry an `account` field — the fleet's seat providers are
recorded as DISTINCT provider ids (xkiro, xkiro-2, opencode-go-2,
commandcode-2). The L0 research table (data/tables/provider_quota.jsonl) is
the authority on which provider id is which account: rows declaring a uniform
`account` per provider_id feed the view at QUERY time (never stored, same
doctrine as the limits config). The view then (a) labels every block with the
declared account, (b) folds a seat provider id (xkiro-2) into its FAMILY
provider (xkiro) when the table declares both sides as distinct accounts of
the same family — so `accounting --provider xkiro` reports ONE ENTRY PER
ACCOUNT, never a summed row. The fold fires only when BOTH ids declare
accounts AND the accounts differ; a provider merely NAMED like a sibling
stays its own budget. Each folded block names its `seats` (the ledger
provider ids absorbed), and the CLI's --provider filter matches seat ids too.

ALIAS INHERITANCE (TR-216, spec §2 "an alias does not own quota"): a gateway
alias (gw-deepseek -> deepseek, myrouter:zai-glm -> zai-glm) carries `alias_of`
on its L0 rows, owns no window, and INHERITS the parent's. The view reads those
declarations from the committed table at query time (`load_alias_map`), the
same no-second-store doctrine as the account map, and folds alias rows into the
PARENT's block: one budget, never a second one drawing down the same window.
A limits-config `alias_of` map may override or extend the table (an operator's
hand beats the data, and the closer hand wins). Chains resolve transitively and
ORDER-INDEPENDENTLY (every row is canonicalized on its own, so a->b->c writes
the same answer whatever order the declarations landed in); a cycle stops
folding instead of looping or silently dropping spend. Folded blocks name the
absorbed ids in `aliases`, and the CLI --provider filter resolves an alias to
its parent block.

AGGREGATED DISPLAY (TR-216): the view NEVER emits a summed cross-account row.
Whether a consumer may sum the per-account limits is a DISPLAY declaration
that lives in the L0 table (`aggregate_display.sum_limits`: "unknown" until a
provider doc permits it — quota-layers-spec §2); split blocks surface that
declaration verbatim, so an aggregate stays UNKNOWN instead of becoming a
wrong 2x.

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
from __future__ import annotations
import argparse
import datetime
import json
import math
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import router_outcomes  # noqa: E402  (the ledger engine — the ONLY data source)
from typing import Any

REPO = os.path.dirname(_HERE)
DEFAULT_LIMITS_FILE = os.path.join(REPO, 'data', 'quota_limits.json')
LIMITS_ENV = 'ROUTER_QUOTA_LIMITS_FILE'
DEFAULT_WINDOW = 'rolling_5h'
WINDOW_KINDS = ('rolling_5h', 'rolling_daily', 'daily', 'rolling', 'weekly',
                'monthly')
# The L0 research table — authority for which ledger provider id is WHICH
# ACCOUNT (TR-216). Read at query time, never stored, like the limits config.
DEFAULT_QUOTA_TABLE = os.path.join(REPO, 'data', 'tables',
                                   'provider_quota.jsonl')
QUOTA_TABLE_ENV = 'ROUTER_PROVIDER_QUOTA_FILE'


# ------------------------------------------------------------------ config ---

def limits_path(explicit: Any=None):
    """Resolved limit-config path: --limits flag > env > repo default."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(LIMITS_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_LIMITS_FILE


def load_limits(path: Any):
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


# ------------------------------------------------------ L0 account table ----

def quota_table_path(explicit: Any=None):
    """Resolved L0 table path: --flag/explicit > env > repo default."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(QUOTA_TABLE_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_QUOTA_TABLE


def _load_quota_table_rows(path: Any) -> list:
    """Decoded rows of the L0 table.

    TOLERANT PER LINE, like the ledger scan: the table is append-written, so a
    torn tail is normal. One bad line must never blank the WHOLE table — that
    would silently drop every alias/account label and make the view lie.
    Missing file -> [] (silent: no labels is the honest empty); an unreadable
    file -> one warning + []; skipped lines -> one warning + the decoded rows.
    """
    if not os.path.exists(path):
        return []
    rows = []
    skipped = 0
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except Exception as e:  # noqa: BLE001
        print(f'router_quota_accounting: unreadable quota table {path}: '
              f'{type(e).__name__}: {e} — no account labels applied',
              file=sys.stderr)
        return []
    if skipped:
        print(f'router_quota_accounting: quota table {path}: skipped '
              f'{skipped} unparseable line(s) — labels applied from the '
              f'{len(rows)} decoded row(s)', file=sys.stderr)
    return rows


def load_account_map(path: Any=None) -> dict:
    """{provider_id: account} for providers whose L0 rows declare a uniform,
    non-empty `account` (TR-216). A mixed or absent declaration is NOT a
    guessable identity, so that provider maps to nothing and its rows stay
    unlabeled — never a guessed 'default'."""
    by_pid = {}
    for r in _load_quota_table_rows(quota_table_path(path)):
        by_pid.setdefault(str(r.get('provider_id')), []).append(r)
    out = {}
    for pid, prows in by_pid.items():
        if not pid or pid == 'None':
            continue
        accts = {r.get('account') for r in prows}
        accts.discard(None)
        if len(accts) == 1:
            out[pid] = next(iter(accts))
    return out


def seat_folds(account_map: Any) -> dict:
    """{seat_provider: family_provider} for L0-declared sibling accounts.

    A provider id folds into its family when stripping a trailing -<digits>
    suffix names ANOTHER provider that ALSO declares an account in the map,
    and the two declarations differ. Both sides come from the committed
    table — the name shape alone is never trusted (a provider legitimately
    named foo-2 without a declaring foo sibling stays its own budget).
    """
    amap = account_map if isinstance(account_map, dict) else {}
    folds = {}
    for pid in amap:
        m = re.match(r'^(.*)-\d+$', str(pid))
        if not m:
            continue
        base_acct = amap.get(m.group(1))
        if base_acct is not None and base_acct != amap[pid]:
            folds[pid] = m.group(1)
    return folds


def load_alias_map(path: Any=None) -> dict:
    """{alias_provider_id: parent_provider_id} from the L0 table (TR-216).

    Spec §2: "an alias does not own quota" — gateway aliases carry `alias_of`
    and INHERIT the parent's windows, so the view must fold their rows into
    the parent's block or the same window is drawn down twice. The table is
    the authority (query time, never stored), so no second hand-maintained
    alias list can drift from it.

    Only a uniform, non-empty declaration per provider is a usable identity: a
    provider whose rows disagree, a blank parent, or a self-alias maps to
    nothing (never a guessed inheritance edge).
    """
    by_pid = {}
    for r in _load_quota_table_rows(quota_table_path(path)):
        by_pid.setdefault(str(r.get('provider_id')), []).append(r)
    out = {}
    for pid, prows in by_pid.items():
        if not pid or pid == 'None':
            continue
        parents = {str(r.get('alias_of')).strip() for r in prows
                   if r.get('alias_of') not in (None, '')}
        parents.discard('')
        if len(parents) != 1:
            continue
        parent = next(iter(parents))
        if parent != pid:
            out[pid] = parent
    return out


def canonical_provider(pid: Any, folds: Any=None, alias_of: Any=None) -> str:
    """The budget-owning provider id for `pid` (TR-216).

    Applies seat folds (xkiro-2 -> xkiro) and alias hops (gw-deepseek ->
    deepseek) until neither applies. Resolving each row on its own is what
    makes a chain (a -> b -> c) ORDER-INDEPENDENT: the old pop-and-append
    folding depended on dict iteration order, so which budget a chain landed
    in varied with declaration order. A cycle (a -> b -> a) folds NOTHING —
    the original id comes back, so a bad declaration cannot hang the view,
    pick an arbitrary winner, or drop the spend.
    """
    cur = str(pid)
    seen = {cur}
    while True:
        nxt = (alias_of or {}).get(cur) or (folds or {}).get(cur)
        if not nxt:
            return cur
        nxt = str(nxt)
        if nxt == cur:
            return cur
        if nxt in seen:
            # cycle (a -> b -> a): fold NOTHING. A bad declaration must never
            # pick an arbitrary winner, and it must not drop the spend either.
            return str(pid)
        cur = nxt
        seen.add(cur)


def load_aggregate_display(path: Any=None) -> dict:
    """{provider_id: declaration} of the L0 rows' `aggregate_display` field
    (whether docs permit summing limits across accounts — quota-layers-spec
    §2). Uniform per provider like the account map: a provider whose rows
    disagree, or that declares nothing, maps to nothing."""
    by_pid = {}
    for r in _load_quota_table_rows(quota_table_path(path)):
        by_pid.setdefault(str(r.get('provider_id')), []).append(r)
    out = {}
    for pid, prows in by_pid.items():
        if not pid or pid == 'None':
            continue
        decls = {json.dumps(r.get('aggregate_display'), sort_keys=True)
                 for r in prows if r.get('aggregate_display') is not None}
        if len(decls) == 1:
            out[pid] = json.loads(next(iter(decls)))
    return out


# ------------------------------------------------------------------- time ----

def parse_ts(value: Any):
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


def _iso(epoch: Any):
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
        .isoformat(timespec='seconds')


def window_bounds(kind: Any, now_ts: Any, window_hours: Any=None) -> tuple | None:
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


def _in_window(ts: Any, bounds: Any, now_ts: Any) -> bool:
    """Window membership — boundary semantics pinned by tests (AC2):
    rolling (resets None): start < ts <= now; calendar: start <= ts < resets."""
    start, resets, _seconds = bounds
    if ts > now_ts:
        return False            # future-dated rows never count
    if resets is None:
        return start < ts <= now_ts
    return start <= ts < resets


# --------------------------------------------------------------- conversion --

def _convert_usd(row: Any) -> tuple:
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


def _num_or_0(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v) if math.isfinite(v) else 0


# --------------------------------------------------------------- accounting --

def account_provider(provider: Any, rows: Any, cfg: Any, now_ts: Any, default_window: Any=DEFAULT_WINDOW,
                     account: Any=None):
    """The L2 block for ONE provider (or provider+account) over ITS rows.

    Rows are whatever the caller scoped to this provider — the grouping lives
    in account(). Every NULL carries its reason (Bane's null-with-reason law);
    unknown is never 0. `account` labels the block when the caller resolved a
    per-account scope (TR-216); it is reports-only here (the grouping itself
    lives in account())."""
    now = float(now_ts)
    cfg = cfg if isinstance(cfg, dict) else {}
    block = {
        'provider': provider,
        'account': account,
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
    where = f'provider {provider}' + (f' account {account}' if account else '')
    if limit_raw is None:
        block['remaining_reason'] = f'no limit configured for {where}'
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


def account(rows: Any, limits: Any, now_ts: Any, account_map: Any=None,
            aggregate_display: Any=None, alias_map: Any=None) -> list:
    """Per-provider (or per provider+account) blocks over ledger rows — the
    L2 VIEW (pure function).

    Grouping is by `provider` (spec §4: the lane count never multiplies a
    budget). When a provider's config entry declares an `account` key AND its
    ledger rows carry an `account` field, the view splits per account
    (TR-216, AC3): two plans of the same provider are two budgets. Rows
    without an `account` field fall in the block whose account the config
    declares (or the provider block itself when none is declared). Providers
    from the config appear even with zero rows, so a configured-but-idle
    budget is visible instead of missing.

    L0-derived resolution (TR-216, the real-ledger shape): `account_map` is
    {provider_id: account} loaded from the committed quota table by the
    caller (account_ledger; tests pass fixtures). Rows of a mapped provider
    are labeled with the declared account, and a SEAT provider id (xkiro-2)
    folds into its family provider (xkiro) per seat_folds() — one entry per
    account, never a summed row. Folded blocks carry `seats` (the absorbed
    ledger provider ids) and — when the L0 table declares it —
    `aggregate_display`, the doc-permission statement for summing limits
    across accounts. The view itself never emits a summed cross-account row.

    Alias inheritance (TR-216, spec §2): `alias_map` is {alias: parent} loaded
    from the L0 table's `alias_of` rows (account_ledger does this; tests pass
    fixtures). Alias rows land in the PARENT's block — the alias owns no
    budget, so the parent's window is drawn down once. A limits-config
    `alias_of` map overrides/extends it (the closer hand wins). Folded blocks
    carry `aliases` (the alias ids absorbed).

    The function never mutates `rows`: the same ledger list may be accounted
    against a different table between calls, and a leaked label would make the
    second answer depend on the first (the query must be pure, or "the view
    cannot drift" stops being true).
    """
    now = float(now_ts)
    amap = account_map if isinstance(account_map, dict) else {}
    folds = seat_folds(amap)
    # -- alias authority: L0 table first, the limit config's own map on top
    # (operator override). Invalid edges are dropped by load_alias_map; the
    # config side is filtered here for the same reasons.
    aliases = {}
    if isinstance(alias_map, dict):
        for a, p in alias_map.items():
            if p not in (None, '') and str(p) != str(a):
                aliases[str(a)] = str(p)
    cfg_alias = limits.get('alias_of') or {}
    if isinstance(cfg_alias, dict):
        for a, p in cfg_alias.items():
            if p not in (None, '') and str(p) != str(a):
                aliases[str(a)] = str(p)
    alias_targets = {a: canonical_provider(a, folds, aliases) for a in aliases}

    by_provider = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = r.get('provider')
        if p is None:
            continue        # unattributable usage cannot sit in a provider budget
        p0 = str(p)
        target = canonical_provider(p0, folds, aliases)
        # work on a COPY: the view is a pure query over the caller's ledger
        row = dict(r)
        # the account label comes from the id's OWN declaration, else from the
        # budget owner it resolved to (a seat/alias inherits the family's)
        acct = amap.get(p0)
        if acct is None:
            acct = amap.get(target)
        if acct is not None:
            row.setdefault('account', acct)
        row['_src_provider'] = p0      # provenance for the seats/aliases labels
        by_provider.setdefault(target, []).append(row)
    providers_cfg = limits.get('providers') or {}
    default_window = limits.get('default_window') or DEFAULT_WINDOW

    def _cfg_for(p: Any) -> dict:
        """Config for a canonical budget owner.

        The parent's own entry wins. An ALIAS/SEAT entry still describes the
        parent's window (neither owns a window of its own), so it is used as
        the fallback rather than leaving a configured alias silently
        limit-less.
        """
        cfg = providers_cfg.get(p)
        if isinstance(cfg, dict) and cfg:
            return cfg
        absorbed = sorted(a for a, owner in alias_targets.items() if owner == p) \
            + sorted(s for s, owner in folds.items() if owner == p)
        for a in absorbed:
            alt = providers_cfg.get(a)
            if isinstance(alt, dict) and alt:
                return alt
        return cfg if isinstance(cfg, dict) else {}

    def _blocks(provider: Any, prow: Any, cfg: Any):
        cfg = cfg if isinstance(cfg, dict) else {}
        declared = cfg.get('account')
        labeled = set()
        has_unlabeled = False
        for r in prow:
            a = r.get('account')
            if a is None:
                has_unlabeled = True
            else:
                labeled.add(str(a))

        def _annotate(block: Any, subrows: Any) -> Any:
            """Name the ledger ids this block ABSORBED (TR-216).

            Provenance comes from the rows themselves (`_src_provider`), so a
            seat/alias label lands on the account block that actually holds
            the spend — no post-hoc matching by account name, which could
            mislabel when a seat declares no account of its own.
            """
            srcs = {str(r.get('_src_provider')) for r in subrows
                    if r.get('_src_provider') not in (None, '')}
            srcs.discard(str(provider))
            if not srcs:
                return block
            seats = sorted(s for s in srcs if folds.get(s) == provider)
            als = sorted(a for a in srcs if alias_targets.get(a) == provider)
            if seats:
                block['seats'] = seats
            if als:
                block['aliases'] = als
            return block

        if not declared and len(labeled) < 2:
            # single-account shape: byte-identical pre-TR-216 behavior when
            # nothing declares an account; when the L0 table labels the rows,
            # the block carries that account name (explicit per-account
            # accounting instead of a bare null). A MIX of labeled and
            # unlabeled rows stays unlabeled — a label would attribute the
            # unlabeled spend to an account it never declared.
            single = (next(iter(labeled))
                      if len(labeled) == 1 and not has_unlabeled else None)
            return [_annotate(account_provider(provider, prow, cfg, now,
                                               default_window=default_window,
                                               account=single), prow)]

        paccount_cfgs = cfg.get('accounts') if isinstance(cfg.get('accounts'),
                                                          dict) else {}
        if declared:
            # config-declared shape: unlabeled rows belong to the DECLARED
            # account (the legacy-ledger contract, never the void)
            def _row_account(r: Any):
                a = r.get('account')
                return str(a) if a is not None else declared
            acct_names = sorted({str(r.get('account')) for r in prow
                                 if r.get('account') is not None}
                                | set(paccount_cfgs) | {declared})
        else:
            # L0-declared multi-account family: one block per labeled
            # account; (should-not-happen) unlabeled rows keep an honest
            # account=None bucket instead of being dropped or guessed.
            def _row_account(r: Any):
                a = r.get('account')
                return str(a) if a is not None else None
            acct_names = sorted(labeled) + ([None] if has_unlabeled else [])
        by_acct = {}
        for r in prow:
            by_acct.setdefault(_row_account(r), []).append(r)
        out = []
        for acct in acct_names:
            acfg = paccount_cfgs.get(acct) if acct is not None else None
            if acfg is None:
                # account not individually configured: inherit the provider's
                # own entry (window/limits), minus the account marker — spend
                # stays visible, remaining carries the no-limit reason.
                acfg = {k: v for k, v in (cfg or {}).items()
                        if k not in ('account', 'accounts')}
                if not acfg:
                    acfg = {}
            else:
                acfg = dict(acfg)
            out.append(_annotate(account_provider(provider,
                                                  by_acct.get(acct, []), acfg,
                                                  now,
                                                  default_window=default_window,
                                                  account=acct),
                                by_acct.get(acct, [])))
        return out

    out_blocks = []
    # A config entry naming a SEAT or an ALIAS is that family's window, not a
    # second budget: canonicalize the key so the family appears once.
    cfg_providers = {canonical_provider(c, folds, aliases) for c in providers_cfg}
    for p in sorted(set(by_provider) | cfg_providers):
        out_blocks.extend(_blocks(p, by_provider.get(p, []), _cfg_for(p)))

    # -- TR-216 annotation: the family's doc-permission statement for summing
    # limits (declared in the L0 table, "unknown" until a provider doc permits
    # it). The view never computes a cross-account sum itself. `seats` and
    # `aliases` were attached per block above, from the rows it absorbed.
    family_blocks = {}
    for b in out_blocks:
        family_blocks.setdefault(b['provider'], []).append(b)
    for p, blocks in family_blocks.items():
        split = len(blocks) > 1
        for b in blocks:
            decl = (aggregate_display or {}).get(p) \
                if isinstance(aggregate_display, dict) else None
            if decl is not None and split:
                b['aggregate_display'] = decl
    return out_blocks


def account_ledger(path: Any=None, limits: Any=None, now_ts: Any=None,
                   alias_map: Any=None) -> list:
    """The view over the ledger FILE — the only I/O path (read-only).

    `limits` may be a loaded config dict or a PATH to a config file (loaded
    here, still at query time — callers that query repeatedly pass the doc
    and re-load it themselves so an edit between queries is honored).
    The L0 account map, alias map and aggregate-display declarations are read
    from the committed quota table at query time too (TR-216) — the same
    no-second-store doctrine: the table is DATA, the view is a query.
    """
    path = path or router_outcomes.outcomes_path()
    if isinstance(limits, str):
        limits = load_limits(limits)
    limits = limits if limits is not None else load_limits(limits_path())
    rows = router_outcomes.load_outcome_rows(path)
    if alias_map is None:
        alias_map = load_alias_map()
    return account(rows, limits, now_ts if now_ts is not None else time.time(),
                   account_map=load_account_map(),
                   aggregate_display=load_aggregate_display(),
                   alias_map=alias_map)


# -------------------------------------------------------------------- CLI ----

def _fmt_usd(v: Any) -> str:
    return '-' if v is None else f'{v:.4f}'


def _print_human(blocks: Any, ledger: Any, limits_file: Any, as_of: Any) -> int:
    print(f'L2 quota accounting — view over {ledger}')
    print(f'  as of {as_of} · limits: {limits_file}')
    if not blocks:
        print('  no providers (empty ledger, empty limit config)')
        return 0
    for b in blocks:
        label = b['provider']
        if b.get('account'):
            label = f"{label}/{b['account']}"
        if b.get('seats'):
            label = f"{label} (+{','.join(b['seats'])})"
        if b.get('aliases'):
            label = f"{label} (~{','.join(b['aliases'])})"
        print(f"  {label:<30} {str(b['window']):<13} "
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


def main(argv: Any=None) -> int | None:
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
            # a SEAT id (xkiro-2) or an ALIAS id (gw-deepseek) names a block
            # that reports under its family/parent provider: match on what the
            # block ABSORBED first (TR-216), then fall back to the resolved
            # owner so an alias with no rows still resolves to its parent.
            target = canonical_provider(want, seat_folds(load_account_map()),
                                        load_alias_map())
            matched = [b for b in blocks
                       if b['provider'] == want
                       or want in (b.get('seats') or [])
                       or want in (b.get('aliases') or [])]
            if not matched and target != want:
                matched = [b for b in blocks if b['provider'] == target]
            blocks = matched
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

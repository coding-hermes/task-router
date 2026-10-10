#!/usr/bin/env python3
"""router_quota_pacing.py — quota L3: the pacing gate (TR-210).

WHY (docs/quota-layers-spec.md section 5): the quota plane can now SEE a
window (L0 declaration, L1 readback, L2 accounting view over the outcome
ledger) but nothing DECIDES with it. "N calls in a burst, then a 429 wall"
is a policy failure, not a visibility failure. This module is that policy,
and the one decision it makes is the spec's: admit a call only if

    spent_in_window + est_cost <= limit * safety_margin

with soft-spacing (DELAY a hop) preferred over refusal, an exhausted window
refusing EXPLICITLY with the window named, the refusal COUNTED, cross-account
rotation only to a sibling with its OWN headroom, and unknown-limit windows
contributing NO constraint while RECORDING that fact (silence stays visible).

LAYER DISCIPLINE (spec section 1): L3 consumes the L2 VIEW
(router_quota_accounting.account / account_ledger) — the same ledger-view
counters every other L3 consumer uses. NO SECOND STORE: this module WRITES
NOTHING; decisions are pure functions over (blocks, est_cost, now). The only
mutable state is the caller-supplied counters object, in memory, caller-owned
— the refusal-counting home the spec names ("_ADMISSION already has the
shape for this": scripts/router_server.py `_ADMISSION` counts
accepted/rejected on the request path in memory behind one lock; a caller
wiring this gate onto a real request path passes THAT object — or any dict /
object with accepted/rejected attributes — into PacingGate(counters=...) and
the gate increments the same counters beside the numbers that path already
publishes).

NEVER AN UNBOUNDED WAIT (AC5): the gate COMPUTES a delay, it does NOT sleep.
Waiting is the caller's decision — a Decision with outcome 'waited' carries
delay_s and the caller may absorb it, bounded by wait_s / wait_cap_s. A wait
that would exceed the cap is a refusal, not a stall. There is no sleep() in
this module.

LIMITS LIVE IN DATA, NEVER IN CODE (spec section 8): the gate reads its
window limits from the L2 limit config (data/quota_limits.json, read at
QUERY time — TR-209) — not from provider_quota.jsonl, and not hard-coded.
The L0 research table (data/tables/provider_quota.jsonl) remains the
limits-of-record for provider PLAN terms in token/credit units; this gate is
denominated in ledger dollars, so it keys off the L2 config exactly as the
accounting view does. A limit of null / absent config / unusable config
contributes NO constraint and the Decision records that fact
(outcome 'no_limit', no_limit_contribution=True + reason).

UNITS AND ESTIMATES (spec section 6): the L2 blocks carry remaining_usd plus
remaining_reason; when spent could not be priced the remaining is None and
the gate MUST NOT invent a budget — such a window contributes no constraint
and is recorded as an unknown contribution, never as headroom. An estimate
can never render as an observation: remaining_usd is taken VERBATIM from the
L2 view (which already labels its confidence), and the gate adds no
confidence it was not handed (basis: 'observed-limit' only where the L2
block actually carried a usable limit; 'no-limit' otherwise).

CROSS-ACCOUNT (AC3): accounts are per-account budgets (spec section 2). The
gate's decide() takes the block for the TARGET account; pick_account()
rotates ONLY to a sibling whose OWN window admits the spend. A sibling with
no headroom is passed over, and if none qualifies the decision refuses
naming the PREFERRED window — it never silently moves to a budget that
cannot take the spend.

CLI (operators; programmatic callers import PacingGate / decide directly):

    pacing --provider P --cost-c 0.01 [--accounts a,b] [--ledger PATH]
           [--limits PATH] [--now WHEN] [--margin 0.9] [--wait-s 0]
           [--wait-cap-s 30] [--json]

Account scoping: the L2 view is provider-keyed (spec section 4 — a
provider's window is ONE budget), so the CLI paces the provider block
directly. --accounts exists for the cross-account policy: the caller passes
distinct L2 blocks per account key (e.g. scoped ledger views) and the gate
rotates across them with pick_account(). Exit codes (router_quota.py
style): 0 ok, 2 usage/validation error (operator error — never masked), 1
unexpected failure (clean stderr message, no traceback).
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
DEFAULT_MARGIN = 0.9        # pacing line sits below the ceiling (spec section 5)
DEFAULT_WAIT_CAP_S = 30.0   # bounded-wait cap: never an unbounded wait (AC5)

OUTCOME_ADMITTED = 'admitted'
OUTCOME_WAITED = 'waited'
OUTCOME_REFUSED = 'refused'
OUTCOME_NO_LIMIT = 'no_limit'


# ------------------------------------------------------------ decisions ----

def _decision(outcome, **kw):
    """A JSON-ready dict — the Decision. Always carries the outcome and the
    window identity when a block was available."""
    d = {'outcome': outcome}
    d.update(kw)
    return d


def _finite_nonneg(v):
    return (not isinstance(v, bool) and isinstance(v, (int, float))
            and v >= 0 and v == v and v != float('inf'))


# --------------------------------------------------------------- helpers ----

def _remaining(block):
    """(remaining_usd | None, reason | None) from an L2 block — VERBATIM.

    remaining_usd is None whenever the limit was null/unusable OR spent was
    unpriceable; the L2 reason then names it. This gate never invents a
    number (spec section 6: no fake zeros, no infinity)."""
    r = block.get('remaining_usd')
    if _finite_nonneg(r):
        return float(r), block.get('remaining_reason')
    return None, (block.get('remaining_reason')
                  or 'remaining_usd missing from the L2 block')


def window_named(block):
    """The window identity a refusal must name (AC2, spec section 6)."""
    return (f"provider {block.get('provider')} "
            f"account {block.get('account') or 'default'} "
            f"window {block.get('window')}")


def parse_resets(block):
    """L2 resets_at (ISO) -> epoch seconds, or None (rolling, no rows yet)."""
    return acc.parse_ts(block.get('resets_at')) if block.get('resets_at') else None


def pacing_delay_s(remaining_usd, est_cost, resets_at_ts, now_ts):
    """The smooth-spend delay for one call, in seconds (AC2 soft-spacing).

    spec section 5: "the pacing rate is remaining / time_to_reset (smooth
    spend) rather than 'spend until 429'". A call costing a fraction f of the
    remaining budget waits f of the time left in the window. The gate
    COMPUTES this; it never sleeps. No reset time (rolling window with no
    in-window rows yet) or an exhausted window means delay 0 — the caller
    then refuses or resets instead of inventing a wait."""
    if (resets_at_ts is None or resets_at_ts <= now_ts
            or remaining_usd is None or remaining_usd <= 0 or est_cost <= 0):
        return 0.0
    frac = min(1.0, float(est_cost) / float(remaining_usd))
    return round((resets_at_ts - now_ts) * frac, 6)


# ------------------------------------------------------------------ gate ----

class PacingGate:
    """The L3 pacing policy over L2 blocks.

    decide(block, est_cost, now_ts) is a pure function of its arguments plus
    margin/wait_s/wait_cap_s. The ONLY mutable state is the caller-supplied
    counters object (AC5 — REUSE the existing admission counters; never a new
    store): a dict (optional 'lock' honored, keys mirror
    router_server._ADMISSION: accepted/rejected/waiting) or any object with
    accepted/rejected/waiting attributes.
    """

    def __init__(self, counters=None, margin=DEFAULT_MARGIN, wait_s=0.0,
                 wait_cap_s=DEFAULT_WAIT_CAP_S):
        if not 0.0 < float(margin) <= 1.0:
            raise ValueError(f'margin must be in (0, 1], got {margin!r}')
        self.counters = counters
        self.margin = float(margin)
        self.wait_s = max(0.0, float(wait_s))
        self.wait_cap_s = max(0.0, float(wait_cap_s))

    # -- the admission line -----------------------------------------------
    def _admits(self, block, est_cost):
        """spent + est_cost <= limit*margin, expressed on the L2 remaining.

        remaining = limit - spent, so the line is:
            remaining - est_cost >= limit*(1-margin)
        margin 1.0 degenerates to remaining >= est_cost — never above the
        ceiling (AC1's invariant)."""
        rem, _reason = _remaining(block)
        if rem is None:
            return False
        if self.margin >= 1.0:
            return (rem - est_cost) >= 0.0
        limit = block.get('limit_usd')
        if not _finite_nonneg(limit):
            return False
        return (rem - est_cost) >= float(limit) * (1.0 - self.margin)

    # -- unknown-limit visibility (AC4) -------------------------------------
    @staticmethod
    def _unknown(block):
        rem, reason = _remaining(block)
        if rem is None:
            return True, (reason or 'limit unknown')
        if not _finite_nonneg(block.get('limit_usd')):
            return True, (f'unusable limit_usd {block.get("limit_usd")!r} '
                          'in the L2 block')
        return False, None

    # -- counted outcomes (AC2/AC5) ------------------------------------------
    def _count(self, outcome):
        c = self.counters
        # `is None`, NOT a falsy test: an EMPTY dict is the natural caller
        # shape (`PacingGate(counters={})`) and a falsy guard silently made
        # every outcome uncounted — the AC2/AC4 counting the spec demands
        # ("an exhausted window refuses ... and is counted", "silence stays
        # visible") never fired for the default caller.
        if c is None:
            return
        key = {OUTCOME_ADMITTED: 'accepted',
               OUTCOME_REFUSED: 'rejected',
               OUTCOME_WAITED: 'waiting',
               OUTCOME_NO_LIMIT: 'no_limit_contribution'}[outcome]
        if isinstance(c, dict):
            # The _ADMISSION shape: plain dict under an optional lock; keys
            # mirror router_server._ADMISSION (accepted/rejected/waiting),
            # + this gate's own visibility counter (AC4: silence counted).
            lock = c.get('lock')
            ctx = lock if lock is not None else _NullCtx()
            with ctx:
                c[key] = int(c.get(key, 0) or 0) + 1
            return
        if hasattr(c, key):
            setattr(c, key, int(getattr(c, key) or 0) + 1)

    def stats(self):
        """Counters snapshot (dict form) or None when the caller passed an
        object — that path's owner publishes its own stats."""
        if isinstance(self.counters, dict):
            return {k: v for k, v in self.counters.items() if k != 'lock'}
        return None

    # -- the one decision ------------------------------------------------------
    def decide(self, block, est_cost=0.0, now_ts=None):
        """Pace ONE call against ONE window block (the target account's).

        Returns a JSON-ready Decision dict:
          outcome: 'admitted' | 'waited' | 'refused' | 'no_limit'
          delay_s: computed smooth-spend delay (waited only; the CALLER may
                   absorb it, bounded — this module never sleeps)
          reason:  names the window on every refusal (AC2)

        The outcome is COUNTED exactly once per call (AC2/AC4/AC5).
        `pick_account` therefore evaluates candidates through the counting-free
        `_evaluate` and counts only the decision it actually returns — probing
        siblings must never inflate the admission counters.
        """
        dec = self._evaluate(block, est_cost, now_ts)
        if not dec.get('operator_error'):
            self._count(dec['outcome'])
        return dec

    def _evaluate(self, block, est_cost=0.0, now_ts=None):
        """The decision itself — PURE: reads state, counts nothing."""
        est_cost = float(est_cost)
        if est_cost != est_cost or est_cost in (float('inf'), float('-inf')) \
                or est_cost < 0:
            # Operator/programming error, NOT a pacing outcome: refused, and
            # deliberately not counted as a window refusal.
            return _decision(OUTCOME_REFUSED, operator_error=True,
                             reason='est_cost must be a '
                                    'finite non-negative number')
        now = time.time() if now_ts is None else float(now_ts)
        unknown, unk_reason = self._unknown(block)
        if unknown:
            # AC4 / spec section 6: no constraint, and the fact is RECORDED
            # (counted by decide()) — silence stays visible, never treated as
            # capacity.
            return _decision(OUTCOME_NO_LIMIT,
                             provider=block.get('provider'),
                             account=block.get('account'),
                             window=block.get('window'),
                             no_limit_contribution=True,
                             reason=unk_reason)
        rem, _reason = _remaining(block)
        if self._admits(block, est_cost):
            return _decision(OUTCOME_ADMITTED,
                             provider=block.get('provider'),
                             account=block.get('account'),
                             window=block.get('window'),
                             remaining_usd=rem, basis='observed-limit')
        # Not admitted outright. Soft-spacing first (AC2: delay preferred
        # over refusal) — but only a BOUNDED, meaningful wait.
        resets = parse_resets(block)
        delay = pacing_delay_s(rem, est_cost, resets, now)
        if self.wait_s > 0 and 0.0 < delay <= self.wait_cap_s:
            return _decision(OUTCOME_WAITED,
                             provider=block.get('provider'),
                             account=block.get('account'),
                             window=block.get('window'),
                             remaining_usd=rem, delay_s=delay,
                             reason=(f'soft-spacing: {window_named(block)} '
                                     f'near the pacing line — delay '
                                     f'{delay:.1f}s (bounded, <= cap '
                                     f'{self.wait_cap_s:.1f}s)'))
        if self.wait_s > 0 and delay > self.wait_cap_s:
            # A wait that would exceed the cap is a refusal, not a stall
            # (AC5: never an unbounded wait on the request path).
            return _decision(OUTCOME_REFUSED,
                             provider=block.get('provider'),
                             account=block.get('account'),
                             window=block.get('window'),
                             remaining_usd=rem,
                             reason=(f'pacing delay {delay:.1f}s exceeds '
                                     f'wait cap {self.wait_cap_s:.1f}s; '
                                     f'{window_named(block)} is spaced out'))
        # Exhausted (or no wait wanted): EXPLICIT refusal naming the window.
        return _decision(OUTCOME_REFUSED,
                         provider=block.get('provider'),
                         account=block.get('account'),
                         window=block.get('window'),
                         remaining_usd=rem,
                         reason=(f'window exhausted: {window_named(block)} '
                                 f'remaining ${rem:.4f} < est ${est_cost:.4f} '
                                 f'(margin {self.margin:.2f})'))

    # -- cross-account rotation (AC3) ------------------------------------------
    def pick_account(self, provider, blocks_by_account, est_cost=0.0,
                     preferred=None, now_ts=None):
        """Rotate ONLY to a sibling whose OWN window admits the spend (AC3).

        blocks_by_account: {account_key: L2 block} — distinct blocks for the
        same provider (the L2 view is provider-keyed; a caller scopes one
        block per account, e.g. by pointint account-split ledger views at
        account()). Returns (block | None, Decision). Preferred account
        first (default 'default' or the first key); then every other account
        whose OWN remaining clears the margin line; else (None, refused)
        naming the PREFERRED window — never a silent move into a budget that
        cannot take the spend.
        """
        if not blocks_by_account:
            return None, _decision(OUTCOME_REFUSED, provider=provider,
                                   reason='no account blocks supplied')
        accounts = list(blocks_by_account)
        if preferred is None:
            preferred = 'default' if 'default' in blocks_by_account \
                else accounts[0]
        if preferred not in blocks_by_account:
            return None, _decision(
                OUTCOME_REFUSED, provider=provider, account=preferred,
                reason=f'preferred account {preferred!r} has no L2 block')
        order = [preferred] + [a for a in accounts if a != preferred]
        first_block = blocks_by_account[preferred]
        # Probes go through _evaluate (counting-free) and the ONE decision
        # actually returned is counted once (AC2/AC5): probing a sibling must
        # never inflate the admission counters.
        first = self._evaluate(first_block, est_cost, now_ts)
        if first['outcome'] in (OUTCOME_ADMITTED, OUTCOME_NO_LIMIT):
            self._count(first['outcome'])
            return first_block, first
        for acct in order[1:]:
            blk = blocks_by_account[acct]
            d = self._evaluate(blk, est_cost, now_ts)
            if d['outcome'] in (OUTCOME_ADMITTED, OUTCOME_NO_LIMIT):
                d['rotated_from'] = preferred
                self._count(d['outcome'])
                return blk, d
        # Nobody admits. Refuse naming the PREFERRED window — a paced-out
        # account never silently moves to a sibling with no headroom (AC3).
        self._count(OUTCOME_REFUSED)
        d = _decision(OUTCOME_REFUSED, provider=provider, account=preferred,
                      window=first_block.get('window'),
                      reason=(f'no account of provider {provider} has '
                              f'headroom: {window_named(first_block)} is '
                              f'paced out and no sibling account has its '
                              f'own headroom'))
        return None, d


class _NullCtx:
    """Context-manager no-op (a counters dict without a 'lock')."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------ CLI ----

def _print_human(dec, block, ledger, limits_file, as_of, gate):
    print(f'L3 quota pacing — gate decision over {ledger}')
    print(f'  as of {as_of} · limits: {limits_file} · margin '
          f'{gate.margin:.2f} · wait cap {gate.wait_cap_s:.1f}s')
    print(f"  {block.get('provider')} ({window_named(block)})")
    print(f"    -> {dec['outcome']}: {dec.get('reason') or '(admitted)'}")
    if dec.get('delay_s') is not None:
        print(f"    delay_s: {dec['delay_s']}")
    stats = gate.stats()
    if stats:
        print(f"    counters: {json.dumps(stats, sort_keys=True)}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='router_quota_pacing.py',
        description='Quota L3 pacing gate (TR-210): admit/delay/refuse per '
                    'provider window from the L2 accounting view (no second '
                    'store; soft-spacing preferred; explicit counted '
                    'refusals).')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    pg = sub.add_parser('pacing', help='the one verb: a pacing decision')
    pg.add_argument('--provider', required=True,
                    help='provider to pace (L2 view is provider-keyed)')
    pg.add_argument('--cost-c', dest='cost_c', type=float, default=0.0,
                    help='estimated cost of the call in USD (default 0)')
    pg.add_argument('--accounts', default=None,
                    help='comma-separated account keys for rotation '
                         '(combined with --blocks-json)')
    pg.add_argument('--blocks-json', dest='blocks_json', default=None,
                    help='path to a JSON dict of {account: L2 block} for '
                         'cross-account rotation (otherwise the block comes '
                         'from the ledger view)')
    pg.add_argument('--ledger', default=None,
                    help='outcome ledger (default: $ROUTING_OUTCOMES_FILE '
                         'or data/state/outcomes.jsonl)')
    pg.add_argument('--limits', default=None,
                    help='limit config JSON (default: '
                         '$ROUTER_QUOTA_LIMITS_FILE or data/quota_limits.json)')
    pg.add_argument('--now', default=None,
                    help='freeze the clock: ISO-8601 or epoch seconds')
    pg.add_argument('--margin', type=float, default=DEFAULT_MARGIN,
                    help=f'safety margin in (0, 1] (default {DEFAULT_MARGIN})')
    pg.add_argument('--wait-s', dest='wait_s', type=float, default=0.0,
                    help='bounded wait the caller may absorb (default 0 = '
                         'refuse instead of delay)')
    pg.add_argument('--wait-cap-s', dest='wait_cap_s', type=float,
                    default=DEFAULT_WAIT_CAP_S,
                    help=f'upper bound on any computed delay (default '
                         f'{DEFAULT_WAIT_CAP_S})')
    pg.add_argument('--json', action='store_true')

    a = parser.parse_args(argv)
    try:
        if a.command != 'pacing':
            parser.error('pacing is the one verb (TR-210)')
        now_ts = time.time()
        if a.now is not None:
            now_ts = acc.parse_ts(a.now)
            if now_ts is None:
                raise ValueError(f'--now {a.now!r} is not ISO-8601 or '
                                 'epoch seconds')
        gate = PacingGate(counters={}, margin=float(a.margin),
                          wait_s=float(a.wait_s),
                          wait_cap_s=float(a.wait_cap_s))
        lpath = acc.limits_path(a.limits)
        limits = acc.load_limits(lpath)
        if a.blocks_json:
            with open(a.blocks_json) as f:
                blocks_by_account = json.load(f)
            if not isinstance(blocks_by_account, dict):
                raise ValueError(f'--blocks-json {a.blocks_json} must hold a '
                                 'JSON object {account: L2 block}')
            block, dec = gate.pick_account(
                a.provider, blocks_by_account, est_cost=float(a.cost_c),
                preferred=(a.accounts.split(',')[0].strip()
                           if a.accounts else None), now_ts=now_ts)
        else:
            ledger = (os.path.abspath(os.path.expanduser(a.ledger))
                      if a.ledger else acc.router_outcomes.outcomes_path())
            blocks = acc.account_ledger(path=ledger, limits=limits,
                                        now_ts=now_ts)
            hits = [b for b in blocks if b['provider'] == a.provider]
            if not hits:
                raise ValueError(f'provider {a.provider!r} has no ledger '
                                 f'rows in {ledger} and no limit config in '
                                 f'{lpath}')
            block = hits[0]
            dec = gate.decide(block, est_cost=float(a.cost_c), now_ts=now_ts)
        as_of = acc._iso(now_ts)
        if a.json:
            print(json.dumps({'as_of': as_of,
                              'limits_file': lpath,
                              'margin': gate.margin,
                              'provider': a.provider,
                              'decision': dec,
                              'gate_stats': gate.stats()}, indent=1))
        else:
            _print_human(dec, block, a.ledger or
                         acc.router_outcomes.outcomes_path(), lpath, as_of,
                         gate)
        return 0
    except ValueError as e:
        # Operator input error: the parser's own channel (exit 2), so a typo
        # is DETECTABLE and never reads as success (router_quota.py style).
        parser.error(str(e))
    except Exception as e:  # noqa: BLE001  fail-open: clean message, no traceback
        print(f'router_quota_pacing error: {type(e).__name__}: {e}',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

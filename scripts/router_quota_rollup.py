#!/usr/bin/env python3
"""TR-415 — persisted per-provider window rollup for the RESOLVE hot path.

WHY
    `router_spawn.py` builds a per-provider WINDOW aggregate (the TR-213
    headroom term) on every resolve. TR-414 bounded that read to the widest
    configured window, but on the live store (592 MB / 500k rows, ~4.8 KB per
    recent row) the 26-day tail alone exceeds the 256 MiB bounded-scan cap, so
    every spawn paid BOTH parses: the capped scan and then the whole-store
    fallback (measured 9.8 s median, 2026-11-26).

WHAT
    A sidecar aggregate next to the ledger that answers "spend per provider per
    configured window as of now" in ONE small read, without touching the
    ledger. Shape (JSON, atomic writes):

        {"version": 1,
         "source": {"path","inode","size","mtime_ns","tail_ck"},
         "prices": {"inode","size","mtime_ns"},
         "cutoff_ts": <oldest ts kept, pruned below it>,
         "generated_at": <iso>,
         "providers": {"<provider>": [[ts, usd, tin, tout, treason, kind], ...]}}

    `kind` is how the row was priced (the ONLY thing the read side needs to
    reproduce the accounting view exactly):
        0 measured `cost_usd` on the row   1 price-table derived at build time
        2 unpriced (no declared price)     3 no token usage to price

    Entries are kept only for rows at/after the widest configured window start
    (plus the TR-414 margin) — an older row cannot move any provider's window,
    so dropping it is not a filter, it is the definition of the aggregate.

INVALIDATION (the card's AC4)
    Keyed by SOURCE-FILE IDENTITY: inode, size, mtime_ns and a sha1 of the
    trailing 4 KiB. An append keeps the prefix, so `size > stored_size` plus a
    matching tail checksum means the delta bytes are the ONLY new input —
    the rollup is updated incrementally from `stored_size` (a few KB) instead
    of re-scanning 592 MB. Any other shape (truncation, rewrite, inode change,
    a change to the price table the build priced with, a wider window than the
    pruned cutoff, a version bump) rebuilds from one streaming pass.

FAIL-OPEN IS SACRED
    Missing, unreadable, corrupt, partial or stale rollup -> `(None, stats)`
    and the caller keeps today's path. Nothing here raises on data problems,
    and nothing here can block or fail a resolve. Kill switch:
    `ROUTER_QUOTA_ROLLUP=0`.

    `refresh()` also RETURNS the in-window rows it saw, so a caller whose
    bounded scan tripped its byte cap can build the rollup from ONE full pass
    instead of paying for the capped scan AND the whole-store scan (the card's
    no-double-pay rule).

Stdlib only — the spawn runtime imports this with no dependencies installed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import tempfile
from typing import Any

VERSION = 1
ENV_SWITCH = 'ROUTER_QUOTA_ROLLUP'
#: Bytes at the end of the ledger whose sha1 proves "the prefix I already
#: consumed is still there" (append detection vs an in-place rewrite).
TAIL_BYTES = 4096
#: Hard ceiling on retained entries; the newest win when a store outgrows it.
DEFAULT_MAX_ENTRIES = 400000

_OFF_VALUES = ('0', 'false', 'no', 'off')


def enabled() -> bool:
    """The kill switch: ROUTER_QUOTA_ROLLUP=0 (false/no/off) disables the leg."""
    raw = os.environ.get(ENV_SWITCH)
    if raw is None:
        return True
    return str(raw).strip().lower() not in _OFF_VALUES


def rollup_path(ledger_path: Any) -> str:
    """The sidecar path — NEXT TO the ledger, so state-dir isolation (tests,
    --data-home, ROUTER_STATE_DIR) follows the ledger without extra wiring."""
    return str(ledger_path) + '.quota-rollup.json'


# --------------------------------------------------------------------- files --
def _stat(path: Any) -> dict | None:
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return {'path': str(path), 'inode': int(getattr(st, 'st_ino', 0)),
            'size': int(st.st_size),
            'mtime_ns': int(getattr(st, 'st_mtime_ns', 0))}


def _tail_ck(path: Any, size: Any, n: Any = TAIL_BYTES) -> str | None:
    """sha1 of the last `min(size, n)` bytes at offset `size` — i.e. of the
    prefix that ENDS at `size`, never of the live file's tail.

    The distinction is the whole invalidation contract: reading to EOF instead
    would hash bytes appended AFTER `size`, so every append would look like a
    rewrite and the incremental path could never fire."""
    try:
        size = int(size)
        if size <= 0:
            return None
        n = max(1, int(n))
        start = max(0, size - n)
        with open(str(path), 'rb') as f:
            f.seek(start)
            return hashlib.sha1(f.read(size - start)).hexdigest()
    except (OSError, ValueError):
        return None


def _prices_stat() -> dict | None:
    """The price table the build priced rows with (`router_outcomes
    ._registry_prices` reads this file). A price edit changes what a row from
    the price table is worth, so it invalidates the rollup."""
    # realpath, never abspath: this module ships to ~/.hermes/scripts/ as a
    # symlink, and the abspath idiom through a symlink resolves to the LIVE dir
    # (~/.hermes) instead of the repo (the TR-202 bug, guarded by
    # tests/test_symlink_path_resolution.py).
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return _stat(os.path.join(root, 'data', 'tables', 'models.jsonl'))


def _fp_key(st: Any) -> tuple:
    st = st if isinstance(st, dict) else {}
    return (st.get('inode'), st.get('size'), st.get('mtime_ns'))


# ------------------------------------------------------------------- pricing --
def _convert(row: Any) -> tuple:
    """(usd|None, kind) — the SAME pricing authority the accounting view uses
    (`router_quota_accounting._convert_usd`, TR-070). Never re-implemented here:
    a second price formula is a silent drift."""
    try:
        import router_quota_accounting as _rqa
        cost, klass, detail = _rqa._convert_usd(row)
    except Exception:  # noqa: BLE001 — unpriceable at build time -> kind 2
        return None, 2, None
    if cost is None:
        if klass == 'unknown-price':
            return None, 2, (detail or 'no declared price')
        return None, 3, None
    return float(cost), (0 if klass == 'row-cost' else 1), None


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _entry(row: Any, cutoff_ts: Any = None) -> tuple | None:
    """(provider, [ts, usd, tin, tout, treason, kind]) or None when the row
    cannot be windowed (no provider / unparseable ts — the accounting view
    counts those as outside-window, so dropping them here is identical)."""
    if not isinstance(row, dict):
        return None
    prov = row.get('provider')
    if prov in (None, ''):
        return None
    try:
        import router_outcomes as _ro
        ts = _ro.parse_row_ts(row.get('ts'))
    except Exception:  # noqa: BLE001
        ts = None
    if ts is None:
        return None
    if cutoff_ts is not None and not ts >= float(cutoff_ts):
        return None
    usd, kind, detail = _convert(row)
    return (str(prov), [ts, usd, _num(row.get('tokens_in')),
                        _num(row.get('tokens_out')),
                        _num(row.get('tokens_reasoning')), kind], detail)


# --------------------------------------------------------------- build/update --
def _empty_state(path: Any, src: Any, prices: Any) -> dict:
    return {'version': VERSION, 'source': dict(src or {}),
            'prices': dict(prices or {}), 'cutoff_ts': None,
            'generated_at': None, 'providers': {}, 'details': {},
            'path': path}


def _consume(handle: Any, providers: Any, cutoff_ts: Any, stats: Any,
             details: Any = None) -> int:
    """Parse complete JSONL lines from `handle`, appending entries. Returns the
    number of NEW entries; a torn final line is skipped loudly (the ledger's
    own tolerant semantics)."""
    added = 0
    for raw in handle:
        stats['bytes_read'] += len(raw)
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line.decode('utf-8', errors='replace'))
        except ValueError:
            stats['malformed'] += 1
            continue
        stats['rows_parsed'] += 1
        got = _entry(row, cutoff_ts)
        if got is None:
            continue
        prov, ent, detail = got
        providers.setdefault(prov, []).append(ent)
        if detail is not None and isinstance(details, dict):
            bucket = details.setdefault(prov, [])
            if detail not in bucket:
                bucket.append(detail)
        added += 1
    return added


def _prune(providers: Any, cutoff_ts: Any, max_entries: Any = DEFAULT_MAX_ENTRIES) -> int:
    """Drop entries older than the cutoff and trim to the newest max_entries."""
    dropped = 0
    keep_from = float(cutoff_ts) if cutoff_ts is not None else None
    total = 0
    for prov, entries in list(providers.items()):
        if keep_from is not None:
            kept = [e for e in entries if float(e[0]) >= keep_from]
            dropped += len(entries) - len(kept)
            providers[prov] = kept
        total += len(providers.get(prov) or [])
    if max_entries and total > int(max_entries):
        # keep the NEWEST entries overall (a window is a tail aggregate)
        flat = [(float(e[0]), prov, e) for prov, entries in providers.items()
                for e in entries]
        flat.sort(key=lambda t: t[0])
        over = total - int(max_entries)
        survivors = flat[over:]
        providers.clear()
        for _ts, prov, ent in survivors:
            providers.setdefault(prov, []).append(ent)
        dropped += over
    return dropped


def _write(path: Any, state: Any) -> bool:
    """Atomic write (same dir: tmp + fsync + os.replace). Any failure is a
    no-op that leaves yesterday's rollup in place — never a partial file."""
    tmp = None
    try:
        d = os.path.dirname(os.path.abspath(path)) or '.'
        fd, tmp = tempfile.mkstemp(prefix='.quota-rollup-', suffix='.tmp', dir=d)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, separators=(',', ':'), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f'quota-rollup: could not persist {path} '
              f'({type(e).__name__}: {e}) — rollup stays in memory this run',
              file=sys.stderr)
        try:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False


def load(path: Any) -> dict | None:
    """The persisted state, or None. A corrupt/partial/foreign file is None —
    the caller then rebuilds or falls back (never an exception)."""
    try:
        with open(str(path), encoding='utf-8') as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or st.get('version') != VERSION:
        return None
    if not isinstance(st.get('providers'), dict):
        return None
    for entries in st['providers'].values():
        if not isinstance(entries, list):
            return None
    if not isinstance(st.get('details'), dict):
        st['details'] = {}
    return st


def _rows_from_entries(providers: Any) -> list:
    """Ledger-shaped rows for the entries (the ONE-PASS rebuild return value:
    the caller gets today's rows without reading the store a second time)."""
    rows = []
    for prov, entries in (providers or {}).items():
        for e in entries:
            row = {'provider': prov, 'ts': e[0],
                   'tokens_in': e[2], 'tokens_out': e[3],
                   'tokens_reasoning': e[4]}
            kind = e[5]
            if kind in (0, 1) and e[1] is not None:
                row['cost_usd'] = e[1]
            rows.append(row)
    return rows


def refresh(ledger_path: Any, cutoff_ts: Any = None,
            persist: Any = True) -> tuple:
    """(state, stats) for the ledger's window rollup — hit, incremental delta,
    or rebuild. `state['rows']` carries the in-window ledger rows when a
    REBUILD read the store (single-pass handoff); `stats` reports the mode,
    bytes actually read and the entries held.

    Never raises for a data problem: an unreadable ledger or unwritable sidecar
    yields (None, stats) / a memory-only state.
    """
    stats = {'mode': 'none', 'bytes_read': 0, 'rows_parsed': 0, 'malformed': 0,
             'entries': 0, 'providers': 0, 'rebuilt': False, 'reason': None,
             'path': rollup_path(ledger_path), 'cutoff_ts': cutoff_ts,
             'pruned': 0, 'single_pass': False}
    if not enabled():
        stats['reason'] = 'disabled: ROUTER_QUOTA_ROLLUP=0'
        return None, stats
    src = _stat(ledger_path)
    if src is None:
        stats['reason'] = 'no ledger'
        return None, stats
    cut = None
    if cutoff_ts is not None:
        try:
            cut = float(cutoff_ts)
        except (TypeError, ValueError):
            cut = None
    path = rollup_path(ledger_path)
    prices = _prices_stat()
    state = load(path)

    reason = None
    if state is None:
        reason = 'no-rollup'
    elif (state.get('source') or {}).get('path') != src['path']:
        reason = 'ledger-path-changed'
    elif _fp_key(state.get('prices')) != _fp_key(prices):
        reason = 'price-table-changed'
    elif cut is not None and state.get('cutoff_ts') is not None \
            and float(state['cutoff_ts']) > cut + 1.0:
        reason = 'window-widened'

    if reason is None:
        old = state.get('source') or {}
        same = (_fp_key(old) == _fp_key(src)
                and old.get('tail_ck') == _tail_ck(ledger_path, src['size']))
        if same:
            entries = sum(len(v) for v in state['providers'].values())
            stats.update(mode='hit', entries=entries,
                         providers=len(state['providers']))
            if cut is not None:
                stats['pruned'] = _prune(state['providers'], cut)
                if stats['pruned']:
                    state['cutoff_ts'] = cut
                    state['generated_at'] = _now_iso()
                    if persist:
                        _write(path, state)
            return state, stats
        grown = (int(old.get('inode') or 0) == src['inode']
                 and int(src['size']) > int(old.get('size') or 0)
                 and old.get('tail_ck') == _tail_ck(ledger_path,
                                                    old.get('size')))
        if grown:
            effects = {'mode': 'delta', 'rebuilt': False}
            try:
                with open(ledger_path, 'rb') as f:
                    f.seek(int(old.get('size') or 0))
                    _consume(f, state['providers'], cut, stats,
                             state.get('details'))
                state['source'] = dict(src)
                state['source']['tail_ck'] = _tail_ck(ledger_path, src['size'])
                if cut is not None:
                    stats['pruned'] = _prune(state['providers'], cut)
                    state['cutoff_ts'] = cut
                persist_reason = None
            except OSError as e:                       # noqa: BLE001
                persist_reason = f'delta-unreadable: {type(e).__name__}: {e}'
                reason = 'delta-failed'
                stats['reason'] = persist_reason
            else:
                stats.update(effects)
                stats['entries'] = sum(len(v) for v in state['providers'].values())
                stats['providers'] = len(state['providers'])
                state['generated_at'] = _now_iso()
                if persist:
                    _write(path, state)
                return state, stats
        else:
            reason = 'ledger-rewritten'

    # ---- rebuild: ONE streaming pass over the store
    stats['rebuilt'] = True
    stats['mode'] = 'rebuild'
    stats['reason'] = reason
    fresh = _empty_state(path, src, prices)
    try:
        with open(ledger_path, 'rb') as f:
            _consume(f, fresh['providers'], cut, stats,
                     fresh.get('details'))
    except OSError as e:                               # noqa: BLE001
        stats.update(mode='unreadable', reason=f'{type(e).__name__}: {e}')
        return None, stats
    if cut is not None:
        stats['pruned'] = _prune(fresh['providers'], cut)
        fresh['cutoff_ts'] = cut
    fresh['source'] = dict(src)
    fresh['source']['tail_ck'] = _tail_ck(ledger_path, src['size'])
    fresh['generated_at'] = _now_iso()
    stats['entries'] = sum(len(v) for v in fresh['providers'].values())
    stats['providers'] = len(fresh['providers'])
    stats['single_pass'] = True
    if persist:
        _write(path, fresh)
    return fresh, stats


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc) \
        .isoformat(timespec='seconds')


# ------------------------------------------------------------------- read side --
def rows_from_state(state: Any) -> list:
    """Ledger-shaped rows for the entries a state holds (TR-415).

    The single-pass handoff: a caller that just built the rollup gets today's
    in-window rows without reading the store a second time. Rows carry `ts`,
    provider, tokens and — for entries the build could price — `cost_usd`, so
    `router_quota_accounting` sees exactly what it sees on a ledger walk.
    """
    return _rows_from_entries((state or {}).get('providers') or {})


def totals_by_provider(state: Any, limits: Any, now_ts: Any,
                       alias_target: Any = None) -> dict:
    """{canonical provider: window totals} for `router_quota_accounting
    .account_provider(..., totals=...)`.

    Grouping mirrors the spawn path exactly: rows are grouped by the CANONICAL
    provider (an alias id folds into its parent, TR-216) and the window config
    is the canonical provider's entry, so the totals and the ledger walk can
    never disagree about which window owns a row. A configured provider with no
    entries still gets totals (an empty window is a MEASURED zero).
    """
    import router_quota_accounting as _rqa
    doc = limits if isinstance(limits, dict) else {}
    cfg_all = doc.get('providers') or {}
    cfg_all = cfg_all if isinstance(cfg_all, dict) else {}
    default_window = doc.get('default_window') or _rqa.DEFAULT_WINDOW
    aliases = alias_target if isinstance(alias_target, dict) else {}
    groups: dict = {}
    for prov, entries in ((state or {}).get('providers') or {}).items():
        key = aliases.get(str(prov), str(prov))
        groups.setdefault(key, []).extend(entries)
    for prov in cfg_all:
        groups.setdefault(str(prov), [])
    out = {}
    for key, entries in groups.items():
        cfg = cfg_all.get(key) or {}
        cfg = cfg if isinstance(cfg, dict) else {}
        kind = cfg.get('window') or default_window
        if kind not in _rqa.WINDOW_KINDS:
            continue
        bounds = _rqa.window_bounds(kind, now_ts, cfg.get('window_hours'))
        if bounds is None:
            continue
        out[key] = _rqa.entries_totals(
            entries, bounds, now_ts,
            details=((state or {}).get('details') or {}).get(key))
    return out

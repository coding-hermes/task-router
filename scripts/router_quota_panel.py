#!/usr/bin/env python3
"""router_quota_panel.py — TR-212: the quota REMAINING panel's data plane.

WHY (docs/quota-layers-spec.md §3 rung ladder + §6 honesty contract): the data
command center must answer "how much quota does each provider/window have
left" WITHOUT ever letting an estimate render as a reading. This module
composes the three layers that already exist — it implements none of them:

  * L0 declaration  — data/tables/provider_quota.jsonl (TR-206): which windows
    a provider has, each limit in its OWN unit, NULL limits carrying a reason
    from the closed set {not-published, no-readback, plan-not-disclosed}.
  * L2 accounting   — scripts/router_quota_accounting.py (TR-209): a VIEW over
    the outcome ledger; spent per (provider, window); limit config read at
    query time (data/quota_limits.json).
  * rung-2 readback — scripts/router_quota_poller.py (TR-208) cache:
    usage/balance endpoints already polled, stored verbatim in
    $ROUTER_STATE_DIR/usage-cache.json.

RUNG LADDER (spec §3, strict priority — the rung is stamped on every number):
  rung-1  response headers   (source_rung "rung-1: response headers")  — not
          yet recorded on the proxy path, so the panel never emits it today;
          the vocabulary is here so rung-1 wiring can slot in without a UI
          change.
  rung-2  usage endpoint     (source_rung "rung-2: usage/balance endpoint")
          — OBSERVED: the poller cache answered. observed=True.
  rung-4  derivation         (source_rung "rung-4: derivation from the L2
          ledger") — limit − in-window spend, computed HERE, always
          observed=False with basis "derived-from-ledger". An estimate can
          never render as an observation (AC1): the observed flag is the only
          gate the UI is allowed to read, and only rung-2 may set it.

HONESTY CONTRACT (spec §6, enforced here):
  * unknown is NULL + reason — a NULL limit renders "unknown (<reason>)" in
    the UI, never 0 and never blank;
  * units without a ledger equivalent (credits, percent, energy_mwh,
    tokens_per_minute) do NOT get a fabricated conversion — the derivation is
    refused with the reason and the row still shows its declared limit;
  * per-minute windows are never "derived" from a ledger that has no
    per-minute resolution guarantee;
  * a provider absent from the limit config still shows its spend — remaining
    stays NULL with the config reason (the L2 rule, preserved verbatim);
  * the scan is bounded and reports rows_scanned / scan_limit /
    scan_truncated like every other UI endpoint (TR-151 rule).

BURN-SURPLUS (spec §5 consumer 3): delegated VERBATIM to
router_quota_burn.find_surplus over the same L2 blocks — burn entries are
labelled estimated with basis derived-from-ledger there, carry resets_at +
time_to_reset_s, and it never auto-spends.

WRITES NOTHING. Stdlib only, like the rest of the runtime. Read-only over:
the quota table, the ledger (tail-scanned), the limit config (query time),
the poller cache.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import deque
from pathlib import Path

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import router_quota_accounting as acc  # noqa: E402 (L2 view + window math)
import router_quota_burn as burn  # noqa: E402 (surplus projection)

REPO = os.path.dirname(_HERE)  # the repo root (same convention as the siblings)
DEFAULT_QUOTA_TABLE = os.path.join(REPO, "data", "tables", "provider_quota.jsonl")
DEFAULT_SCAN_LIMIT = 200_000

# rung-4 covers only the window kinds whose bounds the L2 view can compute AND
# whose ledger resolution we actually have. per_minute rows and their
# tokens_per_minute unit are DECLARED in L0 but can never be honestly derived
# from a ledger without sub-minute resolution — they stay NULL + reason.
DERIVABLE_WINDOW_KINDS = frozenset(acc.WINDOW_KINDS)  # rolling_5h .. monthly

# unit -> how in-window ledger spend is expressed for the derivation.
#   tokens   -> tokens_in+out+reasoning summed in-window
#   requests -> row count in-window
#   usd      -> the L2 dollar view (NULL-sum semantics preserved)
# every other unit has NO honest ledger equivalent.
LEDGER_UNIT_EQUIVALENT = {
    "tokens": "tokens_total",
    "requests": "n_rows",
    "usd": "spent_usd",
}

WINDOW_STATE_KINDS = ("rolling", "rolling_5h", "rolling_daily", "daily",
                      "weekly", "monthly")


# ------------------------------------------------------------------ input ----

def state_dir():
    """Same resolution the poller uses: env wins, else ~/.hermes/model-router."""
    try:
        import state_dir as _sd
        return _sd.resolve_state_dir(script_file=os.path.join(_HERE, "router_quota_poller.py"))
    except Exception:  # noqa: BLE001 — a UI read must not raise
        return os.environ.get("ROUTER_STATE_DIR",
                              os.path.expanduser("~/.hermes/model-router"))


def readback_cache_path(state_dir_arg=None):
    return os.path.join(state_dir_arg or state_dir(),
                        "usage-cache.json")


def load_quota_table(path=None):
    """Tolerant scan of the L0 table: returns (rows, stats). A torn trailing
    line is skipped and counted, never raised (live-append rule)."""
    path = Path(path or DEFAULT_QUOTA_TABLE)
    rows, malformed = [], 0
    if not path.exists():
        return rows, {"source": str(path), "exists": False, "rows": 0,
                      "malformed": 0}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows, {"source": str(path), "exists": True, "rows": len(rows),
                  "malformed": malformed}


def load_readback_cache(path=None):
    """The rung-2 poller cache, read verbatim. Missing/corrupt/old-version
    cache = absent, not empty-on-purpose — the caller reports which."""
    path = path or readback_cache_path()
    if not os.path.exists(path):
        return None, {"path": str(path), "exists": False, "updated": None,
                      "providers": 0, "error": "no rung-2 cache on this box "
                                               "(poller has not run)"}
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return None, {"path": str(path), "exists": True, "updated": None,
                      "providers": 0, "error": f"cache unreadable: {exc}"}
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return None, {"path": str(path), "exists": True, "updated": None,
                      "providers": 0, "error": "cache version mismatch"}
    provs = doc.get("providers") or {}
    return provs, {"path": str(path), "exists": True,
                   "updated": doc.get("updated"),
                   "providers": len(provs), "error": None}


def _scan_ledger_tail(path, scan_limit):
    """Tolerant TAIL scan (newest-N semantics, same as the nohops loader):
    a store bigger than the cap yields its NEWEST rows, never its oldest."""
    tail = deque(maxlen=max(1, int(scan_limit)))
    scanned = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                scanned += 1
                tail.append(line)
    except OSError:
        return [], {"rows_scanned": 0, "malformed": 0, "scan_limit": int(scan_limit),
                    "scan_truncated": False, "error": f"ledger unreadable: {path}"}
    rows, malformed = [], 0
    for line in tail:
        try:
            d = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows, {"rows_scanned": scanned, "malformed": malformed,
                  "scan_limit": int(scan_limit),
                  "scan_truncated": scanned > len(tail), "error": None}


def _parse_reset_field(v):
    """A rung-2 reset field (ISO string or epoch) -> epoch float | None."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# -------------------------------------------------------------- derivation ---

def derive_window_row(row, provider_rows, now):
    """One panel row for ONE L0 declaration, composed over the L2 view.

    row: the L0 dict. provider_rows: the provider's ledger rows (already
    provider-keyed). now: epoch. The `observed` flag is False on every branch
    of THIS function by construction — only a rung-2/1 readback may set it.
    """
    pid = row.get("provider_id")
    kind = row.get("window_kind")
    unit = row.get("unit")
    limit = row.get("limit")
    limit_is_num = isinstance(limit, (int, float)) and not isinstance(limit, bool)

    out = {
        "provider": pid,
        "account": row.get("account", "default"),
        "scope": row.get("scope", "account"),
        "window": kind,
        "unit": unit,
        "limit": float(limit) if limit_is_num else None,
        "limit_text": row.get("limit_text") or None,
        "remaining": None,
        "remaining_reason": None,
        "spent": None,
        "spent_reason": None,
        "n_rows": 0,
        # AC1: an estimate can never render as a reading. derived rows are
        # estimate-only; the rung names where the number came from.
        "observed": False,
        "source_rung": "rung-4: derivation from the L2 ledger",
        "basis": "derived-from-ledger",
        "confidence": row.get("confidence"),
        "resets_at": None,
        "reset_kind": row.get("reset_kind"),
        "resets_estimated": None,
        "sources": row.get("sources") or row.get("source"),
        "readback": None,
    }

    # -- window 1: the derivation is not meaningful for this window shape ----
    if limit is None:
        # L0 says the limit itself is unknown — the DECLARED reason travels
        # verbatim into the rendered "unknown (<reason>)" string. A real but
        # non-derivable window (per_minute) adds context; window "none" means
        # NO window at all, so the reason stands alone.
        reason = row.get("reason") or "not-published"
        if kind not in DERIVABLE_WINDOW_KINDS and kind != "none":
            reason += f"; no derivation exists for window kind {kind!r} either"
        out["remaining_reason"] = f"unknown ({reason})"
        return out

    if kind not in DERIVABLE_WINDOW_KINDS:
        out["remaining_reason"] = (
            f"no derivation for window kind {kind!r}: the ledger has no "
            f"per-minute resolution guarantee — the declared limit is shown "
            f"as declared, never a computed remainder")
        return out

    if unit not in LEDGER_UNIT_EQUIVALENT:
        # No honest ledger equivalent for this unit (credits, percent,
        # energy_mwh, tokens_per_minute, unknown): NULL + reason, never a
        # guessed conversion (spec §6).
        out["remaining_reason"] = (
            f"no in-ledger equivalent for unit {unit!r} — spending cannot be "
            f"summed in this unit, so remaining is unknown, not 0")
        return out

    if limit is None:
        # L0 says the limit itself is unknown — the DECLARED reason travels
        # verbatim into the rendered "unknown (<reason>)" string.
        out["remaining_reason"] = f"unknown ({row.get('reason') or 'not-published'})"
        return out

    # -- window 2: derive from the L2 view (limit − in-window spend) --------
    cfg = {"window": kind}
    block = acc.account_provider(pid, provider_rows, cfg, now)
    out["spent"] = block.get("spent_usd") if unit == "usd" else \
        block.get(LEDGER_UNIT_EQUIVALENT[unit])
    out["n_rows"] = block.get("n_rows", 0)
    out["resets_at"] = block.get("resets_at")
    # a rolling reset derived from the oldest in-window row is itself an
    # estimate (it moves with spend); calendar anchors are facts.
    out["resets_estimated"] = kind in ("rolling", "rolling_5h", "rolling_daily")

    if unit == "usd" and block.get("unpriced_rows"):
        out["spent_reason"] = block.get("unpriced_reason")
    if unit == "usd" and out["spent"] is None:
        # SQL-sum semantics preserved: an unpriceable row makes the sum NULL,
        # so the remainder must stay NULL too (an understated spend would
        # fake headroom).
        out["remaining_reason"] = (
            "spent unknown: " + (out.get("spent_reason")
                                 or "no row in window could be priced"))
        return out
    if out["spent"] is None:
        if str(block.get("remaining_reason") or "").startswith("unusable"):
            # the window itself is not computable (e.g. 'rolling' without
            # window_hours) — the L2 config reason travels verbatim, never a
            # fake "spent unknown"
            out["remaining_reason"] = block["remaining_reason"]
        else:
            out["remaining_reason"] = "spent unknown (window not computed)"
        return out

    out["remaining"] = round(float(limit) - float(out["spent"]), 8)
    return out


def readback_window_rows(cache_providers, quota_table, now):
    """Panel rows from the rung-2 poller cache — the only observed=True rows.

    One row per provider present in the cache. A cache row with status 'ok'
    contributes its mapped fields VERBATIM (unit truth lives at the endpoint);
    remaining is surfaced only when a numeric field names it, otherwise the
    row stays NULL with the reason. Degraded cache rows produce NO number at
    all — the degraded basis is the reason (never 0, never blank).
    """
    by_provider = {}
    for r in quota_table:
        if isinstance(r, dict) and r.get("provider_id"):
            by_provider.setdefault(str(r["provider_id"]), []).append(r)
    rows = []
    for pid in sorted(cache_providers or {}):
        c = cache_providers[pid] or {}
        l0 = by_provider.get(pid, [])
        conf = next((r.get("confidence") for r in l0 if r.get("confidence")),
                    None)
        out = {
            "provider": pid,
            "account": "default",
            "scope": "account",
            "window": "endpoint",
            "unit": None,
            "limit": None,
            "limit_text": (l0[0].get("limit_text") if l0 else None),
            "remaining": None,
            "remaining_field": None,
            "remaining_reason": None,
            "spent": None,
            "spent_reason": None,
            "n_rows": None,
            "observed": False,
            "source_rung": "rung-2: usage/balance endpoint",
            "basis": "observed" if c.get("status") == "ok" else "degraded",
            "confidence": conf,
            "resets_at": None,
            "resets_estimated": None,
            "reset_kind": None,
            "reset_field": None,
            "sources": l0[0].get("readback_docs_url") if l0 else None,
            "readback": {
                "endpoint_id": c.get("endpoint_id"),
                "status": c.get("status"),
                "basis": c.get("basis"),
                "fetched_at": c.get("fetched_at"),
                "age_s": c.get("age_s"),
                "from_cache": c.get("from_cache"),
                "mapped": c.get("mapped"),
                "fields": c.get("fields"),
                "detail": c.get("detail"),
                "http_status": c.get("http_status"),
            },
        }
        if c.get("status") == "ok":
            out["observed"] = True
            fields = c.get("fields") or {}
            # The cache fields are the ENDPOINT's mapped fields (keyed by the
            # L0 row's own field names: usage_remaining, limit_remaining,
            # balance, credits, ...). Probe by NAME for a remaining-like
            # numeric value; anything else is shown verbatim, never rewritten.
            def _num(v):
                if isinstance(v, str):
                    try:
                        v = float(v.replace(",", ""))
                    except ValueError:
                        return None
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return float(v)
                return None
            candidates = [k for k in sorted(fields) if "remaining" in k.lower()]
            candidates += [k for k in sorted(fields)
                           if k.lower() in ("total_balance", "balance", "credits")
                           and k not in candidates]
            for key in candidates:
                v = _num(fields.get(key))
                if v is not None:
                    out["remaining"] = v
                    out["remaining_field"] = key
                    out["unit"] = "endpoint-native"
                    break
            if out["remaining"] is None:
                out["remaining_reason"] = (
                    "endpoint answered but no numeric remaining/balance/credits "
                    f"field is mapped (mapped: {c.get('mapped')}) — the raw "
                    f"fields are shown as-is, never rewritten")
            reset_keys = [k for k in sorted(fields) if "reset" in k.lower()]
            for key in reset_keys:
                reset = _parse_reset_field(fields.get(key))
                if reset is not None:
                    out["resets_at"] = acc._iso(reset)
                    out["reset_kind"] = "endpoint-reported"
                    out["reset_field"] = key
                    break
        else:
            out["remaining_reason"] = (
                f"rung-2 degraded ({c.get('basis')}): "
                f"{c.get('detail') or 'no usable answer'}")
        rows.append(out)
    return rows


# ------------------------------------------------------------------ panel ----

# shortest-window-first: when a provider has several usd rows in L0, the L2
# view keys ONE (provider, window) — the binding constraint for pacing and the
# nearest reset for burn is the shortest declared window.
_WINDOW_PRIORITY = ("rolling_5h", "rolling_daily", "daily", "rolling",
                    "weekly", "monthly")


def limits_doc_for_view(operator_limits, quota_rows):
    """The limits doc the L2/burn view runs over: the operator config wins,
    the L0 table fills the rest.

    TR-209's data/quota_limits.json is the operator's dollar-config and stays
    authoritative where it speaks. Providers it does not declare get their
    limit from the L0 research table's own usd rows (the limits-of-record for
    plan terms, foreman doctrine: limits live in DATA) — shortest declared
    window wins, and the choice is recorded per provider in `limits_source`
    so the panel can say where every dollar limit came from.
    """
    doc = {
        "default_window": operator_limits.get("default_window") or acc.DEFAULT_WINDOW,
        "providers": dict(operator_limits.get("providers") or {}),
        "_sources": {p: "operator config (quota_limits.json)"
                     for p in (operator_limits.get("providers") or {})},
    }
    best = {}
    for r in quota_rows:
        if not isinstance(r, dict) or r.get("archive"):
            continue
        pid = r.get("provider_id")
        kind = r.get("window_kind")
        limit = r.get("limit")
        if not pid or kind not in _WINDOW_PRIORITY:
            continue
        if r.get("unit") != "usd":
            # ONLY dollar rows feed the dollar view — a 500M-tokens row is a
            # token budget, never a $500M budget
            continue
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            continue
        rank = _WINDOW_PRIORITY.index(kind)
        cur = best.get(pid)
        if cur is None or rank < cur[0]:
            best[pid] = (rank, kind, float(limit))
    for pid, (rank, kind, limit) in sorted(best.items()):
        if pid not in doc["providers"]:
            doc["providers"][pid] = {"window": kind, "limit_usd": limit}
            doc["_sources"][pid] = f"L0 usd row ({kind})"
    return doc


def panel_view(query=None, ledger_path=None, now=None, quota_table_path=None,
               limits_path=None, readback_cache=None, scan_limit=None):
    """The /api/ui/quota document. Pure over its inputs; injectable paths +
    clock keep it testable. WRITES NOTHING."""
    query = query or {}
    now = float(now if now is not None else
                 datetime.datetime.now(datetime.timezone.utc).timestamp())
    scan_limit = int(scan_limit or DEFAULT_SCAN_LIMIT)
    provider_filter = query.get("provider")
    if isinstance(provider_filter, list):
        provider_filter = provider_filter[0] if provider_filter else None
    provider_filter = (provider_filter or "").strip() or None

    # -- inputs (read-only) -------------------------------------------------
    quota_rows, quota_stats = load_quota_table(quota_table_path)
    cache_provs, cache_stats = load_readback_cache(readback_cache)
    operator_limits = acc.load_limits(acc.limits_path(limits_path))
    limits = limits_doc_for_view(operator_limits, quota_rows)
    ledger_path = ledger_path or acc.router_outcomes.outcomes_path()
    ledger_rows, scan = _scan_ledger_tail(ledger_path, scan_limit)

    by_provider = {}
    for r in ledger_rows:
        p = r.get("provider")
        if p:
            by_provider.setdefault(str(p), []).append(r)

    # -- one row per L0 declaration ----------------------------------------
    windows = []
    for row in quota_rows:
        pid = row.get("provider_id")
        if not pid:
            continue
        if provider_filter and pid != provider_filter:
            continue
        if row.get("archive"):
            continue
        windows.append(derive_window_row(
            row, by_provider.get(str(pid), []), now))

    # -- rung-2 observed rows (only when no narrower filter hides them) -----
    if cache_provs and (not provider_filter or provider_filter in cache_provs):
        sub = ({provider_filter: cache_provs[provider_filter]}
               if provider_filter else cache_provs)
        windows.extend(readback_window_rows(sub, quota_rows, now))

    windows.sort(key=lambda w: (str(w["provider"]), str(w["window"]),
                                str(w["account"] or "")))

    # -- burn surplus (the same L2 blocks the burn module would read) -------
    blocks = acc.account(ledger_rows, limits, now)
    if provider_filter:
        blocks = [b for b in blocks if b.get("provider") == provider_filter]
    entries = burn.find_surplus(blocks, now,
                                provider_cfgs=limits.get("providers") or {})
    candidates = [e for e in entries if e.get("verdict") == "burn"]
    burn_block = {
        "candidates": candidates,
        "summary": burn.summarize(entries),
        "note": (f"{len(candidates)} burn candidate(s) — every entry is "
                 f"estimated (basis derived-from-ledger) and burning stays "
                 f"the owner's call; reset countdown uses time_to_reset_s")
                 if candidates else
                (f"no burn candidate right now — "
                 f"{burn.summarize(entries)['unknown']} window(s) unknown "
                 f"(no limit_usd configured in the TR-209 limit config: set "
                 f"data/quota_limits.json or $ROUTER_QUOTA_LIMITS_FILE), "
                 f"{burn.summarize(entries)['no_burn']} will spend itself "
                 f"before reset"),
    }

    return {
        "as_of": acc._iso(now),
        "windows": windows,
        "windows_total": len(windows),
        "burn": burn_block,
        "generated_from": {
            "quota_table": quota_stats,
            "limits_file": acc.limits_path(limits_path),
            "limits_providers_configured": len(operator_limits.get("providers") or {}),
            "limits_sources": limits.get("_sources") or {},
            "limits_note": ("dollar limits: operator config "
                            "(data/quota_limits.json) wins; providers it does "
                            "not declare take the L0 table's own usd row "
                            "(shortest window) — the source of each limit is "
                            "named in limits_sources"),
            "ledger": str(ledger_path),
            "ledger_exists": os.path.exists(ledger_path),
            "rows_scanned": scan["rows_scanned"],
            "scan_limit": scan["scan_limit"],
            "scan_truncated": scan["scan_truncated"],
            "rows_malformed": scan["malformed"],
            "ledger_window": {
                "since": acc._iso(min((t for t in
                                       (acc.parse_ts(r.get("ts"))
                                        for r in ledger_rows)
                                       if t is not None), default=None)),
                "until": acc._iso(max((t for t in
                                       (acc.parse_ts(r.get("ts"))
                                        for r in ledger_rows)
                                       if t is not None), default=None)),
            },
            "readback_cache": cache_stats,
            "rung_vocabulary": [
                "rung-1: response headers (not recorded on the proxy path "
                "yet — never emitted by this panel)",
                "rung-2: usage/balance endpoint (observed=True)",
                "rung-4: derivation from the L2 ledger (observed=False, "
                "basis derived-from-ledger)",
            ],
        },
        "note": ("every remaining number names its rung; observed=True only "
                 "on rung-2 endpoint answers; NULL is 'unknown (<reason>)' "
                 "with the reason verbatim from the L0 table"),
    }


def _main(argv=None) -> int:  # pragma: no cover — CLI convenience only
    import argparse
    ap = argparse.ArgumentParser(
        description="TR-212: the quota remaining panel document (stdout only).")
    ap.add_argument("--provider")
    ap.add_argument("--ledger")
    ap.add_argument("--limits")
    ap.add_argument("--quota-table")
    ap.add_argument("--scan-limit", type=int, default=None)
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    doc = panel_view({"provider": a.provider}, ledger_path=a.ledger,
                     now=a.now, quota_table_path=a.quota_table,
                     limits_path=a.limits, scan_limit=a.scan_limit)
    print(json.dumps(doc, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

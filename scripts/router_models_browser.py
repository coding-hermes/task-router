#!/usr/bin/env python3
"""router_models_browser.py — TR-258: the models browser + provider rollups.

ONE query over data that is ALREADY on disk (no new pipeline, no cron, no
provider requests — a render must trigger zero provider network reads):

  * data/tables/models.jsonl  — the registry lanes (provider, model, prices, ctx)
  * ~/.hermes/model-router/health-state.json — the hourly probe (per-model and
    per-provider status + latency_ms)
  * data/state/outcomes-averages.jsonl — TR-049 rolling averages, bucketed by
    (source_system, provider, model, complexity_sig)

The JOIN fact that makes this subtle: averages are keyed by complexity_sig, so
one lane has MANY bucket rows (per backend x per signature). A lane's numbers
here are the SAMPLE-COUNT-WEIGHTED mean of its bucket rows (deterministic and
reconcilable; `bucket_rows_joined` exposes how many rows fed the lane). This
generalizes the feasibility join in scripts/hold_dash_preview.py
(avg_tokens_total_168h / n_samples / avg_wall_time_168h).

The null-with-reason law is the panel's spine (the same contract the quota
panel renders): every missing value names WHY it is missing instead of
rendering as 0 or blank:
  * tokens / samples / wall time — "no sample in window"
  * price — "not priced"
  * health status — "health unknown" (status=unknown, never a silent down)
  * latency — "no sample in window", PLUS a SOURCE LEVEL tag ("(provider)" vs
    "(model)") so a reader cannot mistake a provider-level latency for a
    per-model measurement.

Provider rollups are derived FROM the same lane list, so the sums reconcile by
construction — and the endpoint still PROVES it (totals.reconciles + both
sums) instead of asserting it.
"""
import datetime
import json
import os

#: Probe statuses that read as "the lane is up". Known-bad (DOWN/SLOW/DISABLED)
#: reads as "down"; unseen or EXCLUDED reads as "unknown" with its reason —
#: never folded into down (that would be a fabricated gate).
STATUS_UP = {"OK"}
STATUS_DOWN = {"DOWN", "SLOW", "DISABLED"}

REASON_NO_SAMPLE = "no sample in window"
REASON_NOT_PRICED = "not priced"
REASON_HEALTH_UNKNOWN = "health unknown"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


def mr_dir():
    """The model-router state dir (same surface router_health reads; env-resolved
    at CALL time so tests and a long-lived server follow an env change)."""
    return os.environ.get("ROUTER_MODEL_ROUTER_DIR") \
        or os.path.join(os.path.expanduser("~"), ".hermes", "model-router")


def load_health_state(mr_dir=None):
    """health-state.json as {} or the parsed dict. A missing/unreadable probe
    file degrades to {} -> every lane renders "health unknown", which is the
    honest answer, not an error page."""
    mr = str(mr_dir) if mr_dir is not None else mr_dir()
    try:
        with open(os.path.join(mr, "health-state.json"),
                  encoding="utf-8", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def load_averages(path=None):
    """outcomes-averages.jsonl rows; a torn line is skipped (read-only tool)."""
    if path is None:
        import router_outcomes
        path = router_outcomes.averages_path()
    rows = []
    try:
        with open(str(path), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        return []
    return rows


def _weighted(pairs):
    """Sample-count-weighted mean over [(value, weight)]; None when no value.

    Buckets with a NULL metric contribute their samples to sample_count but no
    weight to the mean — a metric the store never recorded stays missing
    (None + reason), never a fabricated 0.
    """
    wsum = xsum = 0.0
    for value, weight in pairs:
        if value is None:
            continue
        wsum += weight
        xsum += weight * float(value)
    return None if wsum == 0 else xsum / wsum


def _lane_stats(averages):
    """(provider, model) -> aggregated stats over ALL of the lane's buckets."""
    agg = {}
    for a in averages:
        p, m = a.get("provider"), a.get("model")
        if not p or not m:
            continue
        key = (str(p), str(m))
        slot = agg.setdefault(key, {"rows": 0, "weights": [], "tokens": [],
                                    "wall_ms": []})
        n = a.get("n_samples") or 0
        slot["rows"] += 1
        if n:
            slot["weights"].append(n)
            tok = a.get("avg_tokens_total_168h")
            if tok is not None:
                slot["tokens"].append((float(tok), n))
            wall = a.get("avg_wall_time_168h")
            if wall is not None:
                slot["wall_ms"].append((float(wall) * 1000.0, n))
    return agg


def _slot_summary(slot):
    """The per-lane sample/weighted-mean summary over one lane's buckets."""
    n = sum(slot["weights"])
    return {
        "n_samples": (n if slot["rows"] else None),
        "tokens": slot["tokens"],
        "wall_ms": slot["wall_ms"],
    }


def _lane_health(health, provider, model):
    """(status_raw, latency_ms, latency_level, ts) for one lane.

    The per-model probe result wins; the provider's own row is the fallback and
    is labelled as such (the "(provider)" vs "(model)" source tag).
    """
    provs = (health.get("providers") or {}) if isinstance(health, dict) else {}
    p = provs.get(provider)
    if not isinstance(p, dict):
        p = {}
    mrow = (p.get("models") or {}).get(model)
    if isinstance(mrow, dict) and mrow.get("status"):
        return (str(mrow.get("status")), mrow.get("latency_ms"), "model",
                mrow.get("ts"))
    if p.get("status"):
        return (str(p.get("status")), p.get("latency_ms"), "provider",
                p.get("ts"))
    return (None, None, None, None)


def _build_lane(r, health_st, stats_entry):
    """One registry lane joined with its health and averaged usage."""
    p, m = r.get("provider"), r.get("model")
    raw, lat, level, lat_ts = _lane_health(health_st, p, m)
    if raw is None:
        status = "unknown"
        sreason = REASON_HEALTH_UNKNOWN
    elif str(raw) in STATUS_UP:
        status, sreason = "up", None
    elif str(raw) in STATUS_DOWN:
        status, sreason = "down", None
    else:
        status = "unknown"
        sreason = ("probe status %r is neither up nor down (EXCLUDED/unprobed)"
                   % str(raw))
    priced = isinstance(r.get("public_price"), (int, float))
    summary = _slot_summary(stats_entry)
    n = summary["n_samples"]
    have_samples = bool(n)
    tokens = _weighted(stats_entry["tokens"])
    wall_ms = _weighted(stats_entry["wall_ms"])
    return {
        "provider": p, "model": m,
        "status": status, "raw_status": raw, "status_reason": sreason,
        "disabled": bool(r.get("disabled")),
        # latency: value + WHICH level measured it + the reason when absent
        "latency_ms": lat,
        "latency_level": (str(level) if (lat is not None and level) else None),
        "latency_ts": lat_ts,
        "latency_reason": None if lat is not None else REASON_NO_SAMPLE,
        # usage in the 168h window (sample-count-weighted across buckets)
        "tokens_168h": tokens,
        "tokens_reason": None if tokens is not None else REASON_NO_SAMPLE,
        "sample_count": (n if have_samples else None),
        "samples_reason": None if have_samples else REASON_NO_SAMPLE,
        "avg_wall_ms": wall_ms,
        "wall_reason": None if wall_ms is not None else REASON_NO_SAMPLE,
        # pricing
        "public_price_usd_1m": (float(r["public_price"]) if priced else None),
        "price_reason": None if priced else REASON_NOT_PRICED,
        "context_limit": r.get("context_limit"),
        "bucket_rows_joined": stats_entry["rows"],
    }


EPOCH_HEIGHT = None


def models_view(registry_rows, health, averages, now_s=None):
    """Every registry lane joined with health + averaged sampled usage.

    Response shape:
      {"ts", "counts": {"lanes","up","down","unknown"},
       "sources": {"health": {"updated", "provider_rows"},
                   "averages": {"rows"}},
       "providers": [provider ids, for the dropdown],
       "lanes": [{provider, model, status, raw_status, status_reason, disabled,
                  latency_ms, latency_level, latency_reason, latency_ts,
                  tokens_168h, tokens_reason, sample_count, samples_reason,
                  avg_wall_ms, wall_reason,
                  public_price_usd_1m, price_reason, context_limit,
                  bucket_rows_joined}]}
    """
    del now_s  # no decay math here; kept so callers may pass a clock
    health_st = health if isinstance(health, dict) else {}
    stats = _lane_stats(averages or [])
    empty = {"rows": 0, "weights": [], "tokens": [], "wall_ms": []}
    lanes_out = []
    counts = {"lanes": 0, "up": 0, "down": 0, "unknown": 0}
    providers = []
    for r in registry_rows or []:
        p = r.get("provider")
        if p not in providers:
            providers.append(p)
        counts["lanes"] += 1
        lane = _build_lane(r, health_st, stats.get((str(p), str(r.get("model"))),
                                                   empty))
        counts[lane["status"]] += 1
        lanes_out.append(lane)
    return {
        "ts": _utc_now(),
        "counts": counts,
        "sources": {"health": {"updated": health_st.get("updated"),
                               "provider_rows": len(health_st.get("providers")
                                                    or {})},
                    "averages": {"rows": len(averages or [])}},
        "providers": providers,
        "lanes": lanes_out,
    }


def provider_rollups(lanes):
    """Per-provider rollups derived FROM the lane list, with the reconciliation
    proven, not asserted:

      * sum of the providers' tokens equals the sum of the lanes' tokens
      * sum of the providers' sample_count equals the lanes'
      * sum of lanes_count equals the number of lanes

    avg_latency_ms is the mean over the lanes that CARRY a measurement; lanes
    without one stay out of the mean and are counted in `latency_lanes`.
    """
    roll = {}
    lanes = lanes or []
    for lane in lanes:
        r = roll.setdefault(str(lane.get("provider")), {
            "provider": str(lane.get("provider")), "lanes_count": 0,
            "up_count": 0, "down_count": 0, "unknown_count": 0,
            "latencies": [], "tokens": 0.0, "tokens_present": 0,
            "sample_count": 0, "samples_present": 0})
        r["lanes_count"] += 1
        st = lane.get("status")
        if st == "up":
            r["up_count"] += 1
        elif st == "down":
            r["down_count"] += 1
        else:
            r["unknown_count"] += 1
        if isinstance(lane.get("latency_ms"), (int, float)):
            r["latencies"].append(float(lane["latency_ms"]))
        if isinstance(lane.get("tokens_168h"), (int, float)):
            r["tokens"] += float(lane["tokens_168h"])
            r["tokens_present"] += 1
        if isinstance(lane.get("sample_count"), (int, float)):
            r["sample_count"] += int(lane["sample_count"])
            r["samples_present"] += 1

    out = []
    for r in roll.values():
        out.append({
            "provider": r["provider"], "lanes_count": r["lanes_count"],
            "up_count": r["up_count"], "down_count": r["down_count"],
            "unknown_count": r["unknown_count"],
            "avg_latency_ms": (round(sum(r["latencies"]) / len(r["latencies"]), 2)
                               if r["latencies"] else None),
            "latency_lanes": len(r["latencies"]),
            "tokens_168h": round(r["tokens"], 2),
            "tokens_lanes": r["tokens_present"],
            "sample_count": r["sample_count"],
            "sample_lanes": r["samples_present"],
        })

    lane_tokens = round(sum(l["tokens_168h"] for l in lanes
                            if isinstance(l.get("tokens_168h"), (int, float))), 2)
    provider_tokens = round(sum(p["tokens_168h"] for p in out), 2)
    lane_samples = sum(l["sample_count"] for l in lanes
                       if isinstance(l.get("sample_count"), (int, float)))
    provider_samples = sum(p["sample_count"] for p in out)
    totals = {
        "lanes": sum(p["lanes_count"] for p in out),
        "up": sum(p["up_count"] for p in out),
        "down": sum(p["down_count"] for p in out),
        "unknown": sum(p["unknown_count"] for p in out),
        "tokens_168h": provider_tokens,
        "sample_count": provider_samples,
        "lanes_tokens_168h": lane_tokens,
    }
    totals["reconciles"] = (provider_tokens == lane_tokens
                            and provider_samples == lane_samples
                            and totals["lanes"] == len(lanes))
    return {"ts": _utc_now(), "providers": out, "totals": totals}


# --------------------------------------------------------------------- served

def served_models_view(models_rows, mr_dir_override=None):
    """The /api/models surface: live loads + the pure join."""
    mr = str(mr_dir_override) if mr_dir_override is not None else mr_dir()
    return models_view(models_rows or [], load_health_state(mr),
                       load_averages())


def served_provider_rollups(models_rows, mr_dir_override=None):
    """The /api/provider-rollups surface (same loads; rollups of the SAME lanes)."""
    view = served_models_view(models_rows, mr_dir_override=mr_dir_override)
    out = provider_rollups(view["lanes"])
    out["sources"] = view["sources"]
    return out

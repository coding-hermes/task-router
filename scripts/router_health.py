#!/usr/bin/env python3
"""TR-087: control-plane health plane.

Three surfaces the fleet can health-check, all served by router_server.py:

- ``/health``  — cheap liveness + identity + registry freshness + chain counts
                + gate states. Answers "is this control plane alive and is its
                data fresh". Never raises; every block degrades to an error
                key (fail-open, like router_spawn).
- ``/``        — same payload plus a ``_links`` map (the human status surface).
- ``/model_status?provider=X`` — the per-model/provider STATUS LOOKUP: joins
                registry lanes, hourly probe results and circuit gates into
                one row per lane. A query, not a report.

Data sources (all read-only, all already on disk):
  data/tables/{models,providers}.jsonl — registry (via router_seed's writer)
  ~/.hermes/model-router/health-state.json — hourly provider_health_probe
  ~/.hermes/model-router/circuit-state.json — router_circuit gates
  data/state/chains/ — latest chain snapshot via router_maintain export
"""
import datetime
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_SCRIPTS = REPO / "scripts"
if str(SERVER_SCRIPTS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SERVER_SCRIPTS))
import router_circuit  # noqa: E402

MR_DIR = Path(os.environ.get("ROUTER_MODEL_ROUTER_DIR",
                             str(Path.home() / ".hermes" / "model-router")))


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _age_minutes(iso_ts):
    """Minutes since iso_ts, or None when unparseable."""
    if not iso_ts:
        return None
    try:
        ts = datetime.datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60, 1)
    except (ValueError, TypeError):
        return None


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path):
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return rows


def git_commit():
    # .git may be a DIR (normal checkout) or a FILE pointing at the real
    # gitdir (a linked worktree, wt/* branches) — handle both, else every
    # worktree run reports commit "unknown" and the health gate fails.
    gitdir = REPO / ".git"
    try:
        if gitdir.is_file():
            pointer = gitdir.read_text().strip()
            if pointer.startswith("gitdir: "):
                gitdir = Path(pointer[8:])
        head = gitdir / "HEAD"
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref_rel = ref[5:]
            # A worktree gitdir has no refs of its own — loose refs live in
            # the COMMON dir (gitdir/commondir, relative to the gitdir).
            ref_path = gitdir / ref_rel
            if not ref_path.exists():
                try:
                    common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
                    ref_path = common / ref_rel
                except OSError:
                    pass
            return ref_path.read_text().strip()[:12] if ref_path.exists() else "unknown"
        return ref[:12]
    except OSError:
        return "unknown"


VERSION = "task-router/1.0"


def latest_chains_snapshot():
    """Newest data/state/chains/<date>.md, as a freshness marker."""
    chains_dir = REPO / "data" / "state" / "chains"
    try:
        snaps = sorted(p for p in chains_dir.iterdir() if p.suffix == ".md")
        return {"latest": snaps[-1].name, "count": len(snaps)} if snaps else \
            {"latest": None, "count": 0}
    except OSError:
        return {"latest": None, "count": 0}


def health(mode="read-only", data_dir=None):
    """The /health payload. Fail-open: a broken block never kills the response."""
    data_dir = Path(data_dir) if data_dir else Path(
        os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
    out = {
        "status": "ok",
        "service": VERSION,
        "commit": git_commit(),
        "mode": mode,
        "ts": _utc_now(),
    }
    # --- registry freshness: newest valid_from + mtime of the tables ----------
    try:
        newest = None
        n_models = 0
        with (data_dir / "models.jsonl").open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_models += 1
                row = json.loads(line)
                vf = row.get("valid_from")
                if vf and (newest is None or str(vf) > newest):
                    newest = str(vf)
        out["registry"] = {
            "data_dir": str(data_dir),
            "models": n_models,
            "newest_valid_from": newest,
            "age_days": None,
        }
        if newest:
            try:
                d = datetime.date.fromisoformat(newest)
                out["registry"]["age_days"] = (datetime.date.today() - d).days
            except ValueError:
                pass
    except OSError as exc:
        out["registry"] = {"error": str(exc)}

    # --- hourly probe freshness + per-provider status counts ------------------
    hs = _load_health_state()
    if hs is None:
        out["probe"] = {"error": "health-state.json missing/unreadable"}
    else:
        providers = hs.get("providers", {})
        counts = {}
        for p in providers.values():
            counts[p.get("status", "?")] = counts.get(p.get("status", "?"), 0) + 1
        out["probe"] = {
            "updated": hs.get("updated"),
            "age_minutes": _age_minutes(hs.get("updated")),
            "providers": len(providers),
            "provider_status_counts": counts,
        }

    # --- circuit / gate states -------------------------------------------------
    try:
        st = router_circuit.load()
        pairs = st.get("pairs", {})
        now = router_circuit.now_iso()
        open_pairs = [k for k, s in pairs.items()
                      if router_circuit._is_open(s, now)]
        classes = {}
        for s in pairs.values():
            classes[s.get("class", "api_down")] = classes.get(
                s.get("class", "api_down"), 0) + 1
        breakers = st.get("v2", {}).get("provider_breakers", {})
        out["circuit"] = {"pairs": len(pairs), "open": len(open_pairs),
                          "open_pairs": sorted(open_pairs), "classes": classes,
                          "provider_breakers": len(breakers)}
    except Exception as exc:  # fail-open
        out["circuit"] = {"error": str(exc)}

    # --- chains snapshot freshness --------------------------------------------
    out["chains_snapshot"] = latest_chains_snapshot()
    return out


def _gate_open(state):
    """A pair is 'open' (gating active) while open_until is in the future."""
    ou = state.get("open_until")
    if not ou:
        return False
    try:
        return datetime.datetime.fromisoformat(str(ou).replace("Z", "+00:00")) \
            > datetime.datetime.now(datetime.timezone.utc)
    except (ValueError, TypeError):
        return False


def _load_health_state():
    hs = _load_json_safe(MR_DIR / "health-state.json")
    return hs if hs else None


def _load_json_safe(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def model_status(provider=None, data_dir=None):
    """TR-087: the per-model/provider status LOOKUP.

    One row per registry lane: identity + pricing + probe status/latency/ts +
    circuit gate + derived 'status' verdict. ?provider=<id> filters to one
    provider. This is a query over current state, not a report.
    """
    data_dir = Path(data_dir) if data_dir else Path(
        os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
    providers = {r.get("id"): r for r in _read_jsonl(data_dir / "providers.jsonl")}
    hs = _load_json_safe(MR_DIR / "health-state.json") or {}
    hs_providers = hs.get("providers", {})
    try:
        st = router_circuit.load()
        pairs = st.get("pairs", {})
        now = router_circuit.now_iso()
    except Exception:
        pairs, now = {}, None

    lanes = []
    for row in _read_jsonl(data_dir / "models.jsonl"):
        pid, mid = row.get("provider"), row.get("model")
        if provider and pid != provider:
            continue
        probe_p = (hs_providers.get(pid) or {})
        probe_m = (probe_p.get("models") or {}).get(mid) or {}
        gate = pairs.get(f"{pid}/{mid}") or {}
        gate_open = bool(now and router_circuit._is_open(gate, now)) if gate else False
        probe_status = probe_m.get("status") or probe_p.get("status") or "unprobed"
        if row.get("disabled"):
            status = "disabled"
        elif gate_open:
            status = "gated"
        elif probe_status == "DOWN":
            status = "down"
        elif probe_status == "SLOW":
            status = "slow"
        elif probe_status == "OK":
            status = "ok"
        else:
            status = "unprobed"
        lanes.append({
            "provider": pid,
            "model": mid,
            "status": status,
            "probe": {"status": probe_status,
                      "latency_ms": probe_m.get("latency_ms"),
                      "ts": probe_m.get("ts")} if probe_m else None,
            "circuit": {"open_until": gate.get("open_until"),
                        "class": gate.get("class"),
                        "failures": gate.get("failures")} if gate else None,
            "normalized_price": row.get("normalized_price"),
            "disabled": bool(row.get("disabled")),
            "valid_from": row.get("valid_from"),
            "provider_status": (providers.get(pid) or {}).get("status"),
        })
    return {"ts": _utc_now(), "provider": provider, "count": len(lanes),
            "updated": hs.get("updated"), "lanes": lanes}
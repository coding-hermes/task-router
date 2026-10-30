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
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_SCRIPTS = REPO / "scripts"
if str(SERVER_SCRIPTS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SERVER_SCRIPTS))
from typing import Any

import router_circuit  # noqa: E402
import router_validate  # noqa: E402  (TR-REVIEW-001: gate verdict + registry mtime)

MR_DIR = Path(os.environ.get("ROUTER_MODEL_ROUTER_DIR",
                             str(Path.home() / ".hermes" / "model-router")))


# ---------------------------------------------------------------------------
# TR-141: LOADED vs LIVE code identity.
#
# Measured 2026-09-25: the running proxy process started 2026-09-23 19:29 while
# scripts/router_server.py was last changed 2026-09-24 20:27 — 25 hours of stale
# code serving live calls — and /health reported the REPO's HEAD commit, so the
# service looked current. Deploy parity was invisible until someone remembered to
# compare `ps -o lstart=` against a file mtime.
#
# Fix: a module imported by the server captures what THIS PROCESS actually loaded
# (source hashes + the commit at boot). health() reports that as the identity and
# flags `stale` when the files on disk — or HEAD — no longer match. The live
# installs are symlinks, so a pull is not a deploy; this is how you tell.
# ---------------------------------------------------------------------------

#: Modules whose content defines the behaviour of the proxy/server process.
IDENTITY_FILES = ("router_server.py", "router_health.py")


def _file_sha(path: Any):
    """Short sha of one file, or None when it cannot be read (fail-open)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def source_sha():
    """Identity of the code ON DISK right now (the 'live' half of the compare)."""
    parts = [f"{name}:{_file_sha(Path(__file__).resolve().parent / name) or '-'}"
             for name in IDENTITY_FILES]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# Inline (not _utc_now()): this runs at import, before that helper is defined.
_LOADED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
_LOADED_SOURCE_SHA = source_sha()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _age_minutes(iso_ts: Any) -> float | None:
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


def _load_json(path: Any):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Any):
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



#: Commit this PROCESS was started from (captured once, at import).
_LOADED_COMMIT = git_commit()


def code_identity() -> dict:
    """LOADED (this process) vs LIVE (on disk / HEAD now) code identity.

    `stale` is the deploy-parity verdict: True means the code serving requests is
    NOT what a reader of the repo would expect — restart the service. Any field
    that cannot be computed is None with an `error` key; the caller never sees a
    fabricated match.
    """
    live_sha = source_sha()
    repo_commit = git_commit()
    err = None
    if live_sha is None:
        err = "source hash unavailable"
    if not _LOADED_COMMIT or _LOADED_COMMIT == "unknown":
        err = err or "loaded commit unknown at boot"
    return {
        "loaded_commit": _LOADED_COMMIT,
        "loaded_source_sha": _LOADED_SOURCE_SHA,
        "loaded_at": _LOADED_AT,
        "repo_commit": repo_commit,
        "live_source_sha": live_sha,
        # Only a real comparison yields a verdict; an uncomputable half is None.
        "stale": None if err else (live_sha != _LOADED_SOURCE_SHA or repo_commit != _LOADED_COMMIT),
        "error": err,
    }

try:
    import registry_path as _registry_path_mod
except ImportError:  # live byte-copy not yet synced (sync_runtime.sh) -> old default
    _registry_path_mod = None


def registry_path():
    """The registry.json this process reads, resolved AT CALL TIME.

    Same resolution order as router_spawn.py / router_seed.py /
    router_validate.py via scripts/registry_path.py (TR-REV-1012-1):
    ROUTING_REGISTRY wins, then TASK_ROUTER_HOME/registry.json, then
    <repo>/registry.json, then the XDG data home. Resolved per call rather
    than from an import-time constant so the reported path is the file the
    process will actually read if the env is set after import (test
    fixtures, and any wrapper that sets it lazily) — a frozen path is how
    /health ends up describing a file nobody reads.
    """
    if _registry_path_mod is not None:
        return Path(_registry_path_mod.resolve_registry_path(repo_root=str(REPO)))
    return Path(os.environ.get("ROUTING_REGISTRY", str(REPO / "registry.json")))


def registry_age():
    """Registry freshness as an MTIME-derived age (TR-REVIEW-001 criterion 1).

    The sibling `registry` block reports age_days from the newest
    `valid_from` DATE inside models.jsonl — that is DATA freshness (when the
    prices were last true). This block answers the other question a health
    check has to answer: when did this file's bytes last get WRITTEN, and are
    the data tables that feed it newer than it is.

    `stale` comes from router_validate.freshness_check() — the SAME predicate
    the gate's `freshness` check uses, content tiebreak included. Re-deriving
    it here from `lag_s` alone reported `stale: true` on a checkout the gate
    called valid (measured 2026-09-23: 37855s mtime lag, byte-identical
    tables, `gate.valid: true`), which is exactly the drift this endpoint
    exists to make visible rather than create.

    `exists: false` + age None is not stale — it is absent, and the gate's
    `registry.exists` check reports it as invalid. Never fabricates.

    TR-235: `state` makes the absent case LOUD instead of silent. It is the
    one-word verdict over this block: 'missing' (file absent — the gate is
    invalid because there is nothing to gate), 'stale' (the freshness
    predicate says so), 'ok' (fresh), or 'error' (the block could not run —
    also never silence). The 2026-09-28 incident: a serving instance had no
    registry.json at all, and because `stale` was null-with-no-comment on an
    absent file, every canary built on `stale OR gate.valid is false` had the
    raw material but no single honest field to alert on.
    """
    path = registry_path()
    out = {"path": str(path), "exists": path.exists(),
           "mtime": None, "age_s": None, "age_h": None,
           "newest_table": None, "newest_table_age_s": None, "lag_s": None,
           "content_match": None, "stale": None,
           # TR-235: the tri-state verdict. 'missing' FIRST — an absent file
           # is not a stale one, and conflating them is how absence read as
           # silence.
           "state": "ok"}
    if not out["exists"]:
        out["state"] = "missing"
        return out
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        out["error"] = str(exc)
        out["state"] = "error"
        return out
    now = time.time()
    out["mtime"] = datetime.datetime.fromtimestamp(
        mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    out["age_s"] = round(now - mtime, 3)
    out["age_h"] = round((now - mtime) / 3600, 3)
    data_dir = os.environ.get("ROUTING_DATA_DIR", str(REPO / "data" / "tables"))
    fresh = router_validate.freshness_check(str(path), data_dir)
    if fresh.get("lag_s") is not None:
        out["newest_table"] = fresh.get("newest_table")
        out["lag_s"] = round(fresh["lag_s"], 3)
        out["content_match"] = fresh.get("content_match")
        out["stale"] = fresh.get("stale")
        # TR-235: the SAME predicate decides the verdict — a stale flag here
        # IS the 'stale' state, no re-derivation (the content tiebreak stays
        # authoritative; a lagging-but-byte-identical registry stays 'ok').
        if fresh.get("stale"):
            out["state"] = "stale"
        newest = Path(data_dir) / str(fresh["newest_table"])
        try:
            out["newest_table_age_s"] = round(now - newest.stat().st_mtime, 3)
        except OSError:
            pass
    else:
        out["error"] = fresh.get("detail")
        out["state"] = "error"
    return out


def validate_gate() -> dict:
    """The `router validate` gate verdict, in-process (TR-REVIEW-001).

    In-process rather than a subprocess on purpose: /health is the endpoint a
    canary hits on a schedule, and router_validate.py is stdlib-only with no
    duckdb import — measured ~0.04 s for the full check set against the live
    registry. A subprocess would add ~30 ms of interpreter start for the same
    answer and one more failure mode (a spawn error reported as a gate FAIL).

    Failure to RUN the checks is reported as `error` with `valid: null`, never
    as `valid: false`: "the gate says the data is bad" and "the gate could not
    run" are different facts, and a health reader that conflates them will
    chase the wrong thing. Fail-open, like every other block here.
    """
    try:
        report = router_validate.run_checks_dict()
    except Exception as exc:  # noqa: BLE001 — a broken gate never kills /health
        return {"valid": None, "issues": [], "failed_checks": [],
                "checks": 0, "error": str(exc)}
    checks = report.get("checks") or []
    failed = [c.get("name") for c in checks if not c.get("ok")]
    return {
        "valid": report.get("valid"),
        "issues": (report.get("issues") or [])[:10],
        "issues_total": len(report.get("issues") or []),
        "failed_checks": failed,
        "check_names": [c.get("name") for c in checks],
        "checks": len(checks),
    }


def latest_chains_snapshot() -> dict:
    """Newest data/state/chains/<date>.md, as a freshness marker."""
    chains_dir = REPO / "data" / "state" / "chains"
    try:
        snaps = sorted(p for p in chains_dir.iterdir() if p.suffix == ".md")
        return {"latest": snaps[-1].name, "count": len(snaps)} if snaps else \
            {"latest": None, "count": 0}
    except OSError:
        return {"latest": None, "count": 0}


def health(mode: str="read-only", data_dir: Any=None):
    """The /health payload. Fail-open: a broken block never kills the response."""
    data_dir = Path(data_dir) if data_dir else Path(
        os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
    # TR-141 fail-open, like every other block in this payload: an identity that
    # cannot be computed must degrade to unknown-flags, never turn /health into a
    # 500 on the endpoint whose job is to REPORT the problem.
    try:
        identity = code_identity()
    except Exception as exc:  # noqa: BLE001
        identity = {"loaded_commit": _LOADED_COMMIT, "loaded_source_sha": _LOADED_SOURCE_SHA,
                    "loaded_at": _LOADED_AT, "repo_commit": None, "live_source_sha": None,
                    "stale": None, "error": f"identity block failed: {exc}"[:200]}
    out = {
        "status": "ok",
        "service": VERSION,
        # TR-141: this is the commit THIS PROCESS loaded, not the repo's HEAD —
        # reporting the latter is how a 25h-stale proxy looked fresh.
        "commit": identity["loaded_commit"],
        "stale": identity["stale"],
        "code": identity,
        "mode": mode,
        "ts": _utc_now(),
    }
    # --- registry freshness: newest valid_from + mtime of the tables ----------
    # TR-REVIEW-001: a torn/malformed line here used to propagate out of
    # health() and turn /health into a 500 — on the exact corruption this
    # endpoint is supposed to REPORT. Decoding is per-line (skip + count, the
    # same tolerance _read_jsonl already has) and every other error is caught
    # as an `error` key: fail-open, like every other block.
    try:
        newest = None
        n_models = 0
        n_bad = 0
        with (data_dir / "models.jsonl").open() as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                n_models += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    n_bad += 1
                    continue
                if not isinstance(row, dict):
                    n_bad += 1
                    continue
                vf = row.get("valid_from")
                if vf and (newest is None or str(vf) > newest):
                    newest = str(vf)
        out["registry"] = {
            "data_dir": str(data_dir),
            "models": n_models,
            "newest_valid_from": newest,
            "age_days": None,
        }
        if n_bad:
            # Never silent: the count is why `models` can exceed what parsed.
            out["registry"]["unparsable_lines"] = n_bad
        if newest:
            try:
                d = datetime.date.fromisoformat(newest)
                out["registry"]["age_days"] = (datetime.date.today() - d).days
            except ValueError:
                pass
    except Exception as exc:  # noqa: BLE001 — a bad table must not 500 /health
        out["registry"] = {"data_dir": str(data_dir), "error": str(exc)}

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

    # TR-356: silent prompt caps are distinct from ordinary provider health.
    # Publish the probe store even when it is empty so UNKNOWN is observable.
    try:
        out["context_fidelity"] = context_fidelity()
    except Exception as exc:  # noqa: BLE001 — status surfaces fail open
        out["context_fidelity"] = {"status": "UNKNOWN", "lanes": {},
                                   "error": str(exc)[:200]}

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

    # --- TR-REVIEW-001: the two things a canary has to be able to assert ------
    # registry_age: mtime-derived age of the file this process reads.
    # gate: the router_validate verdict, in-process (one source of truth for
    # both surfaces — a check added to the validator appears here for free).
    try:
        out["registry_age"] = registry_age()
    except Exception as exc:  # noqa: BLE001
        out["registry_age"] = {"error": str(exc), "state": "error"}
    out["gate"] = validate_gate()
    # TR-235: the one-word registry verdict as a TOP-LEVEL key, so a canary
    # alerts on `health.registry_state != 'ok'` instead of re-deriving the
    # verdict from registry_age internals (the re-derivation is exactly how
    # the 2026-09-28 incident's ABSENT registry read as silence: no file ->
    # no stale flag -> no alert). Mirrors registry_age.state; 'error' when
    # the block itself could not run. Never folded into `stale` (which stays
    # the code-identity deploy-parity verdict above).
    try:
        out["registry_state"] = out["registry_age"].get("state") or "error"
    except Exception:  # noqa: BLE001 — fail-open, never a 500 on /health
        out["registry_state"] = "error"
    # SCHED-GAP-16942 AC3: the chain's MEASURED fraction as a number, so "how
    # much of this chain is measured?" is answerable from /health alone. Same
    # fail-open contract as every other block: an absent store is a named error
    # beside a real lane count, never a 500 on the endpoint that reports problems.
    try:
        out["coverage"] = measurement_coverage(data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001
        out["coverage"] = {"basis": "price-first", "coverage": 0.0,
                           "error": f"coverage block failed: {exc}"[:200]}
    return out


def _gate_open(state: Any) -> bool:
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


def context_fidelity(provider: Any = None, model: Any = None) -> dict:
    """TR-356: per-lane prompt-preservation evidence for status surfaces.

    Missing or malformed evidence is explicitly UNKNOWN. The block is read-only
    and fail-open: a probe-store problem must not turn /health into a 500.
    """
    path = MR_DIR / "context-fidelity-state.json"
    try:
        import router_context_fidelity as _fidelity
        state = _fidelity.load_state(MR_DIR)
        lanes = state.get("lanes") or {}
    except Exception as exc:  # noqa: BLE001 — health must survive probe failure
        return {"status": "UNKNOWN", "updated": None, "lanes": {},
                "source": str(path), "error": str(exc)[:200]}
    if provider is not None and model is not None:
        key = f"{provider}/{model}"
        row = lanes.get(key)
        if not isinstance(row, dict):
            return {"status": "UNKNOWN", "provider": provider, "model": model,
                    "measured_at": None, "source": str(path), "reason": "unmeasured"}
        return {**row, "provider": provider, "model": model,
                "source": str(path), "status": row.get("status", "UNKNOWN")}
    return {"status": "OK" if lanes else "UNKNOWN", "updated": state.get("updated"),
            "lanes": lanes, "source": str(path),
            "measured": len(lanes), "error": None,
            "reason": None if lanes else "unmeasured"}


def _load_json_safe(path: Any):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# SCHED-GAP-16942 (AC2/AC3/AC4): MEASUREMENT COVERAGE on the status surfaces.
#
# The resolver already refuses to order a chain by measured cost-per-task unless
# enough of the chain carries measurement (router_spawn.MEASURED_MIN_COVERAGE).
# That fraction lived only inside a resolve response, so "how much of this chain
# is measured?" required reading the outcomes/averages store by hand — and on
# /model_status an unmeasured lane was indistinguishable from a lane that had
# been measured and never completed: neither carried an n at all.
#
# Two rules hold this block honest:
#   1. ONE definition. The coverage number is computed by the resolver's own
#      machinery (load_outcome_stats + lane_expected_value + the same sample
#      floor), never by a second definition invented for a status page — a page
#      that disagreed with the sorter would be worse than no page.
#   2. A null lane is n=0 with an UNKNOWN completion rate (None), never 0.0:
#      unmeasured is not failure. Only lanes with known outcomes enter a rate
#      denominator.
# ---------------------------------------------------------------------------
_COVERAGE_CACHE: dict = {"key": None, "value": None, "at": 0.0}
_COVERAGE_TTL_S = 60.0


def _resolver_mod():
    """The resolver module, imported lazily.

    router_health is imported by the read-only API server, which does not
    otherwise need the 4k-line resolver; /health must not pay for it at boot.
    """
    import router_spawn
    return router_spawn


def _coverage_defaults(rspawn: Any) -> tuple:
    """(window_h, sample floor) — the resolver's own knobs, read per call."""
    window = int(getattr(rspawn, "DEFAULT_WINDOW_H", 24) or 24)
    raw = os.environ.get("ROUTER_SORT_MIN_SAMPLES")
    floor = getattr(rspawn, "MEASURED_MIN_SAMPLES", 3)
    if raw not in (None, ""):
        try:
            floor = int(raw)
        except ValueError:
            pass
    return window, max(0, int(floor))


def _coverage_floor(rspawn: Any) -> float:
    """The bar a chain must clear before measured ordering applies.

    Read from the SAME knob the resolver reads (ROUTER_SORT_MIN_COVERAGE), per
    call, so a surface can never report a floor the sorter is not using.
    """
    raw = os.environ.get("ROUTER_SORT_MIN_COVERAGE")
    if raw not in (None, ""):
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(getattr(rspawn, "MEASURED_MIN_COVERAGE", 0.5) or 0.0)


def _no_sample(window: Any, floor: Any) -> dict:
    """The measurement block for a lane the store has never seen (AC2)."""
    return {"basis": "no-sample", "measured": False, "n_samples": 0,
            "n_completed": 0, "n_success_known": 0, "success_rate": None,
            "cost_per_task_window": None, "window_h": window,
            "floor_samples": floor, "match": None}


def lane_measurements(path: Any=None) -> tuple:
    """({(provider, model): measurement}, meta) — the resolver's own lane view.

    One entry per lane with ANY row in the rolling store. A lane ABSENT from the
    mapping has no sample at all; callers report it via `_no_sample` so an
    unmeasured lane is n=0 with rate None, never a 0% failure.
    """
    rspawn = _resolver_mod()
    index, meta = rspawn.load_outcome_stats(path=path)
    window, floor = _coverage_defaults(rspawn)
    ctx = {"index": index, "window_h": window, "keys": None}
    try:
        ceil_n, weight = rspawn._blend_cfg(None)
    except Exception:  # noqa: BLE001 — blend config is cosmetic here
        ceil_n, weight = getattr(rspawn, "BLEND_CEIL_SAMPLES", 0), \
            getattr(rspawn, "BLEND_WEIGHT", 0.0)
    out = {}
    for (pid, mid) in index:
        row, match = rspawn.lane_stats(index, pid, mid, None)
        try:
            value, basis = rspawn.lane_expected_value(
                {"provider": pid, "model": mid}, ctx, floor, ceil_n, weight)
        except Exception:  # noqa: BLE001 — one bad row never kills the surface
            value, basis = None, {"basis": "error"}
        row = row or {}
        kn = row.get("n_success_known")
        out[(pid, mid)] = {
            "basis": (basis or {}).get("basis") or "no-sample",
            "measured": value is not None,
            "n_samples": int(row.get("n_samples") or 0),
            "n_completed": int(row.get("n_completed") or 0),
            "n_success_known": int(kn or 0),
            # AC2: a rate exists only when outcomes are known.
            "success_rate": (row.get("success_rate") if kn else None),
            "cost_per_task_window": row.get(f"avg_cost_task_{window}h"),
            "window_h": window,
            "floor_samples": floor,
            "match": match,
        }
    return out, meta


def _store_view(path: Any=None) -> tuple:
    """(measurements, meta) with a short cache keyed on the file's identity.

    /health is polled; re-merging a multi-MB rolling store per request would
    make the cheap liveness endpoint expensive. Identity (path, mtime, size)
    means a rewritten store is picked up immediately and the TTL only bounds
    repeated reads of an unchanged file.
    """
    rspawn = _resolver_mod()
    p = Path(path or rspawn._averages_path())
    try:
        st = p.stat()
    except OSError as exc:
        window, floor = _coverage_defaults(rspawn)
        return {}, {"path": str(p), "rows": 0, "source": None,
                    "error": f"unreadable averages: {exc}"}
    key = (str(p), st.st_mtime_ns, st.st_size)
    cache = _COVERAGE_CACHE
    if cache["key"] == key and (time.time() - cache["at"]) < _COVERAGE_TTL_S:
        return cache["value"]
    val = lane_measurements(str(p))
    _COVERAGE_CACHE.update(key=key, value=val, at=time.time())
    return val


def _coverage_block(pairs: Any, meas: Any, meta: Any, floor: Any,
                    window: Any) -> dict:
    """The coverage block for a set of (provider, model) lane pairs (pure)."""
    lanes = list(dict.fromkeys(pairs))
    n_lanes = len(lanes)
    measured = sum(1 for k in lanes if (meas.get(k) or {}).get("measured"))
    known = sum(1 for k in lanes if (meas.get(k) or {}).get("n_success_known"))
    cov = round(measured / n_lanes, 4) if n_lanes else 0.0
    basis = "predicted_cost_per_task" if (n_lanes and cov >= floor) else "price-first"
    if not n_lanes:
        reason = "price-first: no lanes to measure"
    elif basis == "price-first":
        reason = (f"price-first: {measured} of {n_lanes} measured "
                  f"(coverage {cov:.3f} < floor {floor:g})")
    else:
        reason = (f"predicted_cost_per_task: {measured} of {n_lanes} measured "
                  f"(coverage {cov:.3f} >= floor {floor:g})")
    return {
        "basis": basis,
        "reason": reason,
        "coverage": cov,
        "floor": floor,
        "lanes": n_lanes,
        "measured": measured,
        "unmeasured": n_lanes - measured,
        # completion rates are computed only over lanes whose outcomes are known
        "completion_known": known,
        "denominator": "enabled-registry-lanes",
        "window_h": window,
        "source": {"path": (meta or {}).get("path"), "rows": (meta or {}).get("rows"),
                   "error": (meta or {}).get("error")},
        # Promoted to the top level, like every other block on this payload: an
        # absent store must be readable without descending into provenance.
        "error": (meta or {}).get("error"),
        "ts": _utc_now(),
    }


def measurement_coverage(provider: Any=None, data_dir: Any=None,
                         path: Any=None) -> dict:
    """AC3: 'how much of this chain is measured', as a number.

    Fails open like every other block: an absent store is a NAMED error beside
    a real lane count, never an exception on the endpoint whose job is to report
    problems.
    """
    rspawn = _resolver_mod()
    window, _floor = _coverage_defaults(rspawn)
    floor = _coverage_floor(rspawn)
    data_dir = Path(data_dir) if data_dir else Path(
        os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))
    pairs = [(r.get("provider"), r.get("model"))
             for r in _read_jsonl(data_dir / "models.jsonl")
             if isinstance(r, dict) and r.get("provider")
             and not r.get("disabled")
             and (provider is None or r.get("provider") == provider)]
    try:
        meas, meta = _store_view(path)
    except Exception as exc:  # noqa: BLE001 — fail-open on the status surface
        meas, meta = {}, {"error": f"coverage store failed: {exc}"[:200]}
    return _coverage_block(pairs, meas or {}, meta, floor, window)


def model_status(provider: Any=None, data_dir: Any=None) -> dict:
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
    # SCHED-GAP-16942: the lanes that could actually serve are the coverage
    # denominator — a disabled lane is not "unmeasured capacity".
    eligible = []
    try:
        meas, meas_meta = _store_view(None)
    except Exception as exc:  # noqa: BLE001 — fail-open: status outlives stats
        meas, meas_meta = {}, {"error": f"coverage store failed: {exc}"[:200]}
    rspawn = _resolver_mod()
    window_h, floor_n = _coverage_defaults(rspawn)
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
        elif probe_status == "DISABLED":
            # TR-247: provider row disabled in the probe registry (vendor
            # shutdown, dead key) — never render that as a mere 'unprobed'
            status = "provider-disabled"
        elif probe_status == "DOWN":
            status = "down"
        elif probe_status == "SLOW":
            status = "slow"
        elif probe_status == "OK":
            status = "ok"
        else:
            status = "unprobed"
        if not row.get("disabled"):
            eligible.append((pid, mid))
        lanes.append({
            "provider": pid,
            "model": mid,
            "status": status,
            # SCHED-GAP-16942 AC2: every lane states its own measurement. A lane
            # with no sample reports n=0 and rate None — unmeasured, not 0%.
            "measurement": meas.get((pid, mid)) or _no_sample(window_h, floor_n),
            # TR-356: context fidelity is independent of ordinary health and
            # always carries provenance; absent evidence is explicitly UNKNOWN.
            "context_fidelity": context_fidelity(pid, mid),
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
    # TR-235: the same loud registry verdict /health carries — an absent
    # registry must be visible on the per-lane surface too, not only on the
    # control-plane endpoint. Fail-open: a failing verdict block degrades to
    # 'error', never a 500 on the status surface.
    try:
        registry_state = registry_age().get("state") or "error"
    except Exception:  # noqa: BLE001
        registry_state = "error"
    return {"ts": _utc_now(), "provider": provider, "count": len(lanes),
            "updated": hs.get("updated"), "lanes": lanes,
            # SCHED-GAP-16942 AC3: the lookup also answers "how much of THIS
            # provider's chain is measured" as a number, in the resolver's terms.
            "coverage": _coverage_block(eligible, meas or {}, meas_meta,
                                        _coverage_floor(rspawn), window_h),
            "registry_state": registry_state}
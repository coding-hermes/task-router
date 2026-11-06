#!/usr/bin/env python3
"""router_metrics_store.py — hop-store rotation + retention (TR-372).

data/metrics.jsonl is the append-only per-hop store written by
router_spawn._append_metrics (TR-021). Until TR-372 it had no rotation and no
retention: it grew ~239 MB/day toward a full filesystem while every reader
opened exactly one path. This module owns the size-capped, generation-numbered
rotation of that store and the resolution of the RETAINED FAMILY so readers
keep seeing the same aggregates across a rotation boundary.

Layout after a rotation (generations N, current file always last):

    metrics.jsonl.N   oldest retained generation
    ...
    metrics.jsonl.2
    metrics.jsonl.1   newest closed generation
    metrics.jsonl     current file (appends land here)

Config (stdlib env reads, resolved at CALL time like every other store path
in this repo — a long-lived server follows an env change):

    ROUTER_METRICS_MAX_BYTES    size cap for metrics.jsonl; when the current
                                file is >= this at append time it is rotated
                                to metrics.jsonl.1 (default 536870912 = 512 MiB)
    ROUTER_METRICS_GENERATIONS  how many closed generations are retained
                                (default 3; lowering it prunes the stragglers
                                at the next rotation)

Fail-open by contract (docs/observability-spec.md §8 finding 1 is the reason
this module exists, and the append path is the reason it can never raise):
rotate_if_needed catches EVERY exception internally — a failed rotation must
never block or alter a resolve; it only delays retention until the next
append (or an explicit `router_metrics.py --rotate-now`).

Pure stdlib: os + re. No lock file, no dependencies (pyproject keeps runtime
dependencies empty on purpose).
"""
from __future__ import annotations

import os
import re

#: The repo root (this file lives in <repo>/scripts/).
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)

#: Env var names + defaults (documented in docs/configuration.md).
MAX_BYTES_ENV = "ROUTER_METRICS_MAX_BYTES"
GENERATIONS_ENV = "ROUTER_METRICS_GENERATIONS"
DEFAULT_MAX_BYTES = 536870912  # 512 MiB
DEFAULT_GENERATIONS = 3

#: `metrics.jsonl.<digits>` — the numbered-generation suffix this module owns.
_GEN_RE = re.compile(r"^(?P<base>.+)\.(?P<gen>\d+)$")


def metrics_path(repo: str | None = None, home: str | None = None) -> str:
    """The hop-store path: $TASK_ROUTER_HOME/metrics.jsonl wins, else
    <repo>/data/metrics.jsonl. Same rule (and same call-time env read) as
    router_spawn._metrics_path, router_metrics._metrics_path and
    router_prometheus._metrics_jsonl_path — one implementation, not a fourth
    copy. `home`/`repo` are explicit overrides for tests."""
    if home is None:
        home = os.environ.get("TASK_ROUTER_HOME")
    if home:
        return os.path.join(home, "metrics.jsonl")
    if repo is None:
        repo = _REPO
    return os.path.join(repo, "data", "metrics.jsonl")


def max_bytes() -> int:
    """The size cap in force. A missing/garbage/<=0 value falls back to the
    default (a cap of 0 would rotate on every append — never a valid
    configuration, so it is treated as unset rather than honoured)."""
    raw = os.environ.get(MAX_BYTES_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    try:
        val = int(raw.strip())
    except ValueError:
        return DEFAULT_MAX_BYTES
    return val if val > 0 else DEFAULT_MAX_BYTES


def generations() -> int:
    """The retained-generation count in force. Missing/garbage/<1 values
    fall back to the default (0 generations would mean keeping nothing)."""
    raw = os.environ.get(GENERATIONS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_GENERATIONS
    try:
        val = int(raw.strip())
    except ValueError:
        return DEFAULT_GENERATIONS
    return val if val >= 1 else DEFAULT_GENERATIONS


def retained_paths(path: str, n: int | None = None) -> list:
    """The retained family, OLDEST FIRST:
    [<path>.N, ..., <path>.2, <path>.1, <path>].

    Numbered generations that exist on disk are listed highest-first so a
    reader iterating this list walks old rows before new rows and `ts`
    ordering survives a rotation boundary. Only EXISTING numbered files are
    returned (a fresh store has none); the current path is always last, even
    when it does not exist yet (readers tolerate a missing file)."""
    if n is None:
        n = generations()
    family: list = []
    for gen in range(n, 0, -1):
        cand = f"{path}.{gen}"
        if os.path.exists(cand):
            family.append(cand)
    family.append(path)
    return family


def family_bytes(path: str, n: int | None = None) -> int:
    """Total bytes across the retained family (the gauge
    router_metrics_jsonl_family_bytes). Missing files count 0. Never raises."""
    total = 0
    try:
        for p in retained_paths(path, n):
            try:
                total += os.path.getsize(p)
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — a size probe must never raise
        return 0
    return total


def _prune_stragglers(path: str, n: int) -> None:
    """Remove numbered generations beyond the retained count (an operator
    who lowered ROUTER_METRICS_GENERATIONS must not leave .4/.5/... forever).
    Tolerates missing directories and unreadable dirs (fail-open)."""
    try:
        names = os.listdir(os.path.dirname(path) or ".")
    except OSError:
        return
    base = os.path.basename(path)
    for name in names:
        m = _GEN_RE.match(name)
        if not m or m.group("base") != base:
            continue
        if int(m.group("gen")) > n:
            try:
                os.remove(os.path.join(os.path.dirname(path), name))
            except OSError:
                pass


def rotate_if_needed(path: str, cap: int | None = None, n: int | None = None) -> int:
    """Size-capped rotation; returns 1 if the current file was rotated, 0 if
    not. NEVER raises (fail-open contract — see module docstring).

    When the current file is >= cap bytes: shift .1 -> .2 -> ... -> .N
    (oldest dropped), then os.replace(current, .1) and let the caller's next
    open('a') create a fresh current file. Every step tolerates missing
    files; lowering GENERATIONS prunes stale numbered files at this point
    too, so `--rotate-now` is a complete retention enforcement."""
    try:
        if cap is None:
            cap = max_bytes()
        if n is None:
            n = generations()
        # Retention first: stale generations beyond N go away on every run.
        _prune_stragglers(path, n)
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0  # no current file — nothing to rotate
        if size < cap:
            return 0
        # Drop the oldest generation FIRST, then shift the closed
        # generations up (.1 -> .2 -> ... -> .N-1 -> .N). Removing .N before
        # the shift (not after) is load-bearing: the shift overwrites each
        # slot, so deleting .N afterwards would destroy a retained
        # generation, not the dropped one.
        try:
            os.remove(f"{path}.{n}")
        except OSError:
            pass
        for gen in range(n - 1, 0, -1):
            src = f"{path}.{gen}"
            if os.path.exists(src):
                try:
                    os.replace(src, f"{path}.{gen + 1}")
                except OSError:
                    return 0
        try:
            os.replace(path, f"{path}.1")
        except OSError:
            return 0
        return 1
    except Exception:  # noqa: BLE001 — rotation is fail-open by contract
        return 0

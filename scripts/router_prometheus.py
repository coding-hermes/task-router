#!/usr/bin/env python3
"""Prometheus text exposition for the task-router serving listeners (TR-377).

stdlib-only BY CONTRACT: pyproject.toml keeps runtime dependencies empty on
purpose, so the exposition text format (version 0.0.4) is rendered by hand
here instead of pulling in prometheus_client.

Design (docs/observability-spec.md §4): the registry is populated by a
COLLECTING pattern — the resolve-path counters/histograms are incremented at
the real call sites in router_server.py (the /resolve operation), while state
the process does not already hold in memory (circuit breakers, lane coverage,
the data-plane file size) is read at SCRAPE time from the same files the
router itself reads. Nothing here caches a duplicate of state the process
already holds, and a collector failure can never fail the scrape (fail-open:
the error is counted on router_prometheus_scrape_errors_total instead).

Exposure contract (row AC): GET /metrics on every serving surface returns
this document with Content-Type text/plain; version=0.0.4; charset=utf-8,
loopback-scoped (the handler enforces it; the servers bind 127.0.0.1 by
default and this row must never widen the bind).

ALIAS MAP (one map, deliberate): every family defined here is emitted TWICE —
under its row-AC name (`router_*`) and again under the spec's product prefix
(`task_router_*`, docs/observability-spec.md §3 naming rule) with identical
samples. The alias list is exactly {f"task_router_{name}" for name in
families}: router_resolve_total            -> task_router_resolve_total
            router_chain_hops              -> task_router_chain_hops
            router_lane_excluded_total     -> task_router_lane_excluded_total
            router_lane_coverage           -> task_router_lane_coverage
            router_resolve_duration_seconds-> task_router_resolve_duration_seconds
            (plus every collector family below, same rule) so the spec's V1
check (`grep -c '^task_router_'` >= 20) and the row AC both hold against the
same scrape. The aliases are doubles, not a second writer: one registry, one
render pass, two name rows per family.

LABEL RULE (row AC): lane-scoped families (router_resolve_total,
router_lane_excluded_total, router_lane_coverage) carry provider and model;
chain-level histograms (router_chain_hops, router_resolve_duration_seconds)
carry profile/outcome AND a provider/model-labeled sample set wherever a hop
datum exists (chain_hops: the hop position of every lane in the chain;
duration: the resolve duration keyed by the top-ranked (hop-1) lane). A
failed resolve has no chain, so its router_resolve_total series carries
provider="" / model="" — stated here, not silently.

Deliberately NOT exported (docs/observability-spec.md §7): per-request
labels (session/task id, caller key, prompt text), per-lane one-hot gauges
for every registry lane, and the outcome store re-read as a metric (row
counting means reading a multi-GB append-only file on the scrape path; the
JSONL stores are the forensics tool, not a time series).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Spec §3: product prefix is task_router_. Every family is emitted under its
#: own name AND this prefix (see module docstring alias map).
ALIAS_PREFIX = "task_router_"

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Explicit buckets. Chain length has been observed up to 350 on a
#: 1202-lane registry, hence the 100/350 rungs; resolve duration is a
#: subprocess spawn + registry load, so sub-100ms is a cold-cache impossibility
#: and minutes are the degraded tail.
CHAIN_HOPS_BUCKETS = (1, 2, 3, 5, 8, 13, 21, 50, 100, 350)
RESOLVE_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)


def _escape_label_value(value: Any) -> str:
    return (str(value).replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace('"', '\\"'))


def _escape_help(text: Any) -> str:
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def _fmt_value(value: Any) -> str:
    """Render a sample value the way the exposition format expects."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    f = float(value)
    if f != f:
        return "NaN"
    if f == float("inf"):
        return "+Inf"
    if f == float("-inf"):
        return "-Inf"
    return repr(f)


def _fmt_le(bound: Any) -> str:
    if bound == float("inf"):
        return "+Inf"
    return repr(float(bound))


class _Family:
    """One metric family: name, type, help, ALLOWED labels, sample store.

    `labelnames` is the set of labels a sample of this family MAY carry; a
    sample may use any subset (chain-level histograms carry {profile,
    outcome} primary series and {provider, model} per-lane series in the
    same family — legal exposition 0.0.4). Unknown label NAMES are rejected:
    a typo must be loud, not a silent series.
    """

    def __init__(self, name: str, mtype: str, help: str,
                 labelnames: tuple) -> None:
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        for ln in labelnames:
            if not _LABEL_NAME_RE.match(ln):
                raise ValueError(f"invalid label name: {ln!r}")
        self.name = name
        self.type = mtype
        self.help = help
        self.labelnames = tuple(labelnames)
        # {(label values in declared order): store} — insertion-ordered for
        # deterministic output.
        self.series: dict = {}

    def _key(self, labels: dict) -> tuple:
        if not isinstance(labels, dict):
            raise TypeError("labels must be a dict")
        extra = set(labels) - set(self.labelnames)
        if extra:
            raise ValueError(
                f"{self.name}: unknown labels {sorted(extra)} "
                f"(allowed: {list(self.labelnames)})")
        # Key on the intersection so a {provider, model} sample and a
        # {profile, outcome} sample of one family never collide.
        present = [ln for ln in self.labelnames if ln in labels]
        return tuple(f"{ln}={labels[ln]}" for ln in present)

    @staticmethod
    def _labels_from_key(key: tuple) -> dict:
        return dict(entry.split("=", 1) for entry in key)


class Counter(_Family):
    def inc(self, labels: dict, value: Any = 1) -> None:
        if float(value) < 0:
            raise ValueError("counters only go up")
        key = self._key(labels)
        self.series[key] = self.series.get(key, 0) + value

    def render(self) -> list:
        rows = []
        for key in sorted(self.series):
            rows.append((self.name, self._labels_from_key(key),
                         self.series[key]))
        return rows


class Gauge(_Family):
    def set(self, labels: dict, value: Any) -> None:
        self.series[self._key(labels)] = value

    def render(self) -> list:
        rows = []
        for key in sorted(self.series):
            rows.append((self.name, self._labels_from_key(key),
                         self.series[key]))
        return rows


class Histogram(_Family):
    """Histogram with explicit buckets, rendered cumulatively per 0.0.4."""

    def __init__(self, name: str, help: str, labelnames: tuple,
                 buckets: tuple) -> None:
        super().__init__(name, "histogram", help, labelnames)
        self.buckets = tuple(sorted(float(b) for b in buckets))
        # key -> {"counts": [per-bound non-cumulative], "sum": f, "count": n}
        self.obs: dict = {}

    def observe(self, labels: dict, value: Any) -> None:
        key = self._key(labels)
        v = float(value)
        cell = self.obs.get(key)
        if cell is None:
            cell = {"counts": [0] * len(self.buckets), "sum": 0.0, "count": 0}
            self.obs[key] = cell
        cell["sum"] += v
        cell["count"] += 1
        # One observation increments exactly ONE bucket (the smallest bound
        # >= v); Registry render makes the series cumulative per 0.0.4.
        # Incrementing every bound >= v here would double-count once the
        # cumulative pass runs.
        for i, b in enumerate(self.buckets):
            if v <= b:
                cell["counts"][i] += 1
                break

    def render(self) -> list:
        rows = []
        for key in sorted(self.obs):
            cell = self.obs[key]
            labels = self._labels_from_key(key)
            cumulative = 0
            for b, c in zip(self.buckets, cell["counts"]):
                cumulative += c
                rows.append((f"{self.name}_bucket",
                             {**labels, "le": _fmt_le(b)}, cumulative))
            rows.append((f"{self.name}_bucket",
                         {**labels, "le": "+Inf"}, cell["count"]))
            rows.append((f"{self.name}_sum", labels, cell["sum"]))
            rows.append((f"{self.name}_count", labels, cell["count"]))
        return rows


class CallbackGauge:
    """A family whose samples come from a callable, evaluated at scrape time.

    The callable returns a list of (labels dict, value). This is the
    collecting pattern from the spec: the value is read from the source of
    truth WHEN the scrape happens, never cached here.
    """

    def __init__(self, name: str, mtype: str, help: str,
                 labelnames: tuple, fn) -> None:
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        for ln in labelnames:
            if not _LABEL_NAME_RE.match(ln):
                raise ValueError(f"invalid label name: {ln!r}")
        self.name = name
        self.type = mtype
        self.help = help
        self.labelnames = tuple(labelnames)
        self.fn = fn

    def render(self) -> list:
        out = []
        for labels, value in self.fn():
            labels = labels or {}
            extra = set(labels) - set(self.labelnames)
            if extra:
                raise ValueError(
                    f"{self.name}: unknown labels {sorted(extra)} "
                    f"(allowed: {list(self.labelnames)})")
            clean = {ln: str(v) for ln, v in sorted(labels.items())}
            out.append((self.name, clean, value))
        return out


def _render_row(name: str, labels: dict, value: Any) -> str:
    if labels:
        body = ",".join(f'{ln}="{_escape_label_value(v)}"'
                        for ln, v in sorted(labels.items()))
        return f"{name}{{{body}}} {_fmt_value(value)}"
    return f"{name} {_fmt_value(value)}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._families: list = []
        self._names: set = set()

    def _register(self, family) -> None:
        with self._lock:
            if family.name in self._names:
                raise ValueError(f"duplicate metric family: {family.name}")
            self._names.add(family.name)
            self._families.append(family)

    def counter(self, name: str, help: str, labelnames: tuple = ()) -> Counter:
        fam = Counter(name, "counter", help, labelnames)
        self._register(fam)
        return fam

    def gauge(self, name: str, help: str, labelnames: tuple = ()) -> Gauge:
        fam = Gauge(name, "gauge", help, labelnames)
        self._register(fam)
        return fam

    def histogram(self, name: str, help: str, labelnames: tuple,
                  buckets: tuple) -> Histogram:
        fam = Histogram(name, help, labelnames, buckets)
        self._register(fam)
        return fam

    def callback(self, name: str, mtype: str, help: str, labelnames: tuple,
                 fn) -> CallbackGauge:
        fam = CallbackGauge(name, mtype, help, labelnames, fn)
        self._register(fam)
        return fam

    def render(self) -> bytes:
        with self._lock:
            families = list(self._families)
        blocks = []
        for fam in families:
            try:
                rows = fam.render()
            except Exception:  # noqa: BLE001 — a collector must never fail the scrape
                scrape_errors.inc({"collector": fam.name})
                continue
            head = [f"# HELP {fam.name} {_escape_help(fam.help)}",
                    f"# TYPE {fam.name} {fam.type}"]
            lines = head + [_render_row(n, l, v) for (n, l, v) in rows]
            blocks.append("\n".join(lines))
            # ALIAS: same samples under the spec's product prefix (module
            # docstring, alias map). One registry, one render, two names.
            # The row prefix is stripped first so router_resolve_total
            # aliases to task_router_resolve_total — never a doubled
            # task_router_router_resolve_total.
            base = (fam.name[len("router_"):]
                    if fam.name.startswith("router_") else fam.name)
            alias = ALIAS_PREFIX + base
            alias_rows = [(alias + n[len(fam.name):], l, v)
                          for (n, l, v) in rows]
            blocks.append("\n".join(
                [f"# HELP {alias} {_escape_help(fam.help)}",
                 f"# TYPE {alias} {fam.type}"]
                + [_render_row(n, l, v) for (n, l, v) in alias_rows]))
        return ("\n".join(blocks) + "\n").encode("utf-8")


#: The process-wide registry. Imported by router_server at startup; the
#: resolve call sites mutate the first three families; collectors run only
#: inside render().
REGISTRY = Registry()

# --- resolve-path families (row AC 1/2/3/5) ----------------------------------

RESOLVE_TOTAL = REGISTRY.counter(
    "router_resolve_total",
    "Resolves served by the /resolve operation, by answered profile and "
    "outcome; provider/model label the top-ranked (hop-1) lane of the "
    "returned chain, empty on a failed resolve.",
    ("profile", "outcome", "provider", "model"))

CHAIN_HOPS = REGISTRY.histogram(
    "router_chain_hops",
    "Chain length in hops per resolve ({profile, outcome} series); the "
    "{provider, model} series observe every chain lane's 1-based hop index "
    "(spec §3.4 task_router_resolve_chain_length).",
    ("profile", "outcome", "provider", "model"), CHAIN_HOPS_BUCKETS)

LANE_EXCLUDED_TOTAL = REGISTRY.counter(
    "router_lane_excluded_total",
    "Lane exclusions, one increment per resolver exclusion code (the "
    "resolver's own EXCLUSION_REASON_CODES vocabulary — no parallel one).",
    ("reason", "provider", "model"))

RESOLVE_DURATION = REGISTRY.histogram(
    "router_resolve_duration_seconds",
    "Resolve wall time ({profile, outcome} series); the {provider, model} "
    "series carry the same duration keyed by the top-ranked (hop-1) lane "
    "of the returned chain (absent when the resolve served no lane).",
    ("profile", "outcome", "provider", "model"), RESOLVE_DURATION_BUCKETS)

RESOLVE_ERRORS = REGISTRY.counter(
    "router_resolve_errors_total",
    "Resolves that ended in a hard error (unknown project, failed "
    "subprocess) — the failure path a success-only exporter hides.",
    ("profile",))

RESOLVE_NOHOPS = REGISTRY.counter(
    "router_resolve_nohops_total",
    "Resolves whose chain came back empty — the no-hops incident shape "
    "(spec §3.4 task_router_resolve_nohops_total).",
    ("profile",))

PROXY_REQUESTS = REGISTRY.counter(
    "router_proxy_requests_total",
    "Proxy requests by route_outcome (served | failed | no-hops | "
    "rejected) and source driver (spec §3.1 "
    "task_router_proxy_requests_total).",
    ("route_outcome", "source"))

PROXY_NOHOPS = REGISTRY.counter(
    "router_proxy_nohops_total",
    "Proxy requests that dead-ended with no eligible hop, by lane_class "
    "(spec §3.1 task_router_proxy_nohops_total) — the rate the no-hops "
    "alert thresholds.",
    ("lane_class",))


def note_proxy_request(route_outcome: Any, source: Any,
                       lane_class: Any = None) -> None:
    """Instrument ONE finished proxy request at the _proxy_record call site.

    Fail-open like the ledger row itself: a metrics failure must never
    change a proxied request's outcome.
    """
    try:
        PROXY_REQUESTS.inc({
            "route_outcome": str(route_outcome or "unknown"),
            "source": str(source or "router-proxy")})
        if str(route_outcome or "") == "no-hops":
            PROXY_NOHOPS.inc({"lane_class": str(lane_class or "unknown")})
    except Exception:  # noqa: BLE001 — best effort, like the row
        pass

# --- collector error accounting (a NULL must carry a reason) -----------------

scrape_errors = REGISTRY.counter(
    "router_prometheus_scrape_errors_total",
    "Scrape-time collector failures by family; a family missing from the "
    "document is counted here, never silently dropped.",
    ("collector",))

# --- scrape-time collector families ------------------------------------------

def _state_file() -> str:
    """Circuit-state path, resolved the way router_circuit resolves it."""
    env = os.environ.get("ROUTER_STATE_DIR")
    if env:
        return os.path.join(env, "circuit-state.json")
    return os.path.join(os.path.expanduser("~/.hermes/model-router"),
                        "circuit-state.json")


def _collect_circuit():
    """Open circuit pairs, read at scrape time from the live state file.

    Reuses router_circuit's own loader + _is_open rule (the same source
    /health uses), so the gauge and the health plane can never disagree.
    Per spec §7 the pair-state gauge is limited to OPEN pairs.
    """
    import router_circuit
    st = router_circuit.load()
    now = router_circuit.now_iso()
    pairs = st.get("pairs", {}) or {}
    open_rows = []
    class_counts: dict = {}
    for key, s in pairs.items():
        if not isinstance(s, dict) or not router_circuit._is_open(s, now):
            continue
        provider, _, model = str(key).partition("/")
        cls = str(s.get("class", "api_down"))
        open_rows.append(({"provider": provider, "model": model,
                           "class": cls}, 1))
        class_counts[cls] = class_counts.get(cls, 0) + 1
    out = list(open_rows)
    out.extend(({"class": cls}, n) for cls, n in sorted(class_counts.items()))
    breakers = (st.get("v2", {}) or {}).get("provider_breakers", {}) or {}
    # Only breakers that are OPEN right now (the same _is_open rule as the
    # pairs) — the file can hold cooling history until the next write prunes
    # it, and a history row must never render as an open breaker.
    n_open_breakers = sum(1 for s in breakers.values()
                          if isinstance(s, dict)
                          and router_circuit._is_open(s, now))
    out.append(({}, n_open_breakers))
    return out


REGISTRY.callback(
    "router_circuit_pair_state",
    "gauge",
    "One-hot per OPEN circuit pair (spec §3.3; limited to open pairs per "
    "spec §7). Read from the circuit-state file at scrape time.",
    ("provider", "model", "class"),
    lambda: [r for r in _collect_circuit() if len(r[0]) == 3])

REGISTRY.callback(
    "router_circuit_open_pairs",
    "gauge",
    "Open circuit breaker pairs by failure class.",
    ("class",),
    lambda: [r for r in _collect_circuit() if set(r[0]) == {"class"}])

REGISTRY.callback(
    "router_circuit_provider_breakers",
    "gauge",
    "Provider-level circuit breakers currently open.",
    (),
    lambda: [r for r in _collect_circuit() if not r[0]])

# --- admission (in-process values; source wired by router_server) ------------

_ADMISSION_SOURCE = {"fn": None}


def register_admission_source(fn) -> None:
    """Wire the process's admission state (router_server._ADMISSION).

    fn() returns the admission dict (waiting/accepted/rejected/inflight...).
    router_server calls this at import; non-numeric keys (the dict carries
    its lock) are ignored.
    """
    _ADMISSION_SOURCE["fn"] = fn


def _admission_samples():
    fn = _ADMISSION_SOURCE.get("fn")
    if fn is None:
        return None
    raw = fn() or {}
    samples = {"inflight": raw.get("inflight"),
               "waiting": raw.get("waiting"),
               "rejected": raw.get("rejected")}
    return samples


def _cb_admission_inflight():
    s = _admission_samples()
    return [({}, s["inflight"])] if isinstance(s, dict) and \
        isinstance(s.get("inflight"), (int, float)) else []


def _cb_admission_queue():
    s = _admission_samples()
    return [({}, s["waiting"])] if isinstance(s, dict) and \
        isinstance(s.get("waiting"), (int, float)) else []


def _cb_admission_rejected():
    s = _admission_samples()
    return [({}, s["rejected"])] if isinstance(s, dict) and \
        isinstance(s.get("rejected"), (int, float)) else []


REGISTRY.callback(
    "router_admission_inflight",
    "gauge",
    "Proxy admission: requests currently in flight (the process's own "
    "_ADMISSION counter; resets on restart, hence a Prometheus counter "
    "primitive per spec §3.2).",
    (), _cb_admission_inflight)

REGISTRY.callback(
    "router_admission_queue_depth",
    "gauge",
    "Proxy admission: callers waiting in the bounded queue.",
    (), _cb_admission_queue)

REGISTRY.callback(
    "router_admission_rejected_total",
    "counter",
    "Proxy admission: refusals (queue full / no slot).",
    (), _cb_admission_rejected)


def _metrics_jsonl_path() -> str:
    """The hop-store path, mirroring router_spawn._metrics_path."""
    env = os.environ.get("TASK_ROUTER_HOME")
    if env:
        return os.path.join(env, "metrics.jsonl")
    return str(REPO / "data" / "metrics.jsonl")


def _cb_metrics_jsonl_bytes():
    try:
        return [({}, os.path.getsize(_metrics_jsonl_path()))]
    except OSError:
        return [({}, 0)]


REGISTRY.callback(
    "router_metrics_jsonl_bytes",
    "gauge",
    "Size of the CURRENT data/metrics.jsonl generation (one row per chain "
    "hop per resolve). Disk-growth early warning; stat only — the file is "
    "never read back on the scrape path. TR-372 added size-capped rotation; "
    "the whole retained set's footprint is router_metrics_jsonl_family_bytes.",
    (), _cb_metrics_jsonl_bytes)


def _cb_metrics_jsonl_family_bytes():
    """TR-372: total bytes across the retained rotation family
    (metrics.jsonl.N ... metrics.jsonl.1 + current). Fail-open to 0 like the
    current-file gauge."""
    try:
        import router_metrics_store
        return [({}, router_metrics_store.family_bytes(_metrics_jsonl_path()))]
    except Exception:  # noqa: BLE001 — a collector must never fail the scrape
        return [({}, 0)]


REGISTRY.callback(
    "router_metrics_jsonl_family_bytes",
    "gauge",
    "Total bytes across the RETAINED metrics.jsonl rotation family (all "
    "closed generations plus the current file; TR-372 "
    "ROUTER_METRICS_MAX_BYTES / ROUTER_METRICS_GENERATIONS). The real "
    "retention footprint; the current-file-only view is "
    "router_metrics_jsonl_bytes. Stat only — no file is read back.",
    (), _cb_metrics_jsonl_family_bytes)


def _data_dir() -> Path:
    return Path(os.environ.get("ROUTING_DATA_DIR", REPO / "data" / "tables"))


def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _collect_lane_coverage():
    """Coverage per category, read at scrape time (see family help).

    Live lanes: models.jsonl rows with a provider and not disabled — the
    same rule router_health.measurement_coverage applies. Category
    membership: model_tier.jsonl (joined on model, as the resolver does).
    Measured: the lane has evidence in the rolling averages store, via
    router_health._store_view (the cached wrapper /health uses — private
    use is precedented: router_server calls router_spawn._pub_prices).
    """
    import router_health

    data_dir = _data_dir()
    live = []
    for r in _read_jsonl(data_dir / "models.jsonl"):
        if r.get("disabled") or not r.get("provider") or not r.get("model"):
            continue
        live.append((str(r["provider"]), str(r["model"])))
    live_models = {m for (_p, m) in live}

    cat_lanes: dict = {}
    for r in _read_jsonl(data_dir / "model_tier.jsonl"):
        cat = r.get("category")
        model = r.get("model")
        if not cat or not model or model not in live_models:
            continue
        cat_lanes.setdefault(str(cat), set())
    for r in _read_jsonl(data_dir / "model_tier.jsonl"):
        cat = r.get("category")
        model = r.get("model")
        if not cat or not model or model not in live_models:
            continue
        cat_lanes[str(cat)].add(model)

    # Fail-open happens one level up (Registry.render): a collector raising
    # is counted on router_prometheus_scrape_errors_total, and the family is
    # omitted from that document rather than failing the scrape.
    meas, _meta = router_health._store_view()

    def measured_pair(pair):
        row = (meas or {}).get(pair) or {}
        return bool(row.get("measured"))

    out = []
    for cat in sorted(cat_lanes):
        models = cat_lanes[cat]
        pairs = [(p, m) for (p, m) in live if m in models]
        n_live = len(pairs)
        measured = [(p, m) for (p, m) in pairs if measured_pair((p, m))]
        ratio = round(len(measured) / n_live, 4) if n_live else 0.0
        out.append(({"category": cat, "kind": "ratio"}, ratio))
        out.append(({"category": cat, "kind": "unmeasured"},
                    n_live - len(measured)))
        for (p, m) in sorted(measured):
            out.append(({"category": cat, "kind": "lane",
                         "provider": p, "model": m}, 1))
    return out


REGISTRY.callback(
    "router_lane_coverage",
    "gauge",
    "Measured-with-evidence lanes / live lanes per task category "
    '(kind="ratio"), unmeasured live-lane count (kind="unmeasured"), and a '
    '1.0 marker per measured lane (kind="lane" with provider/model). '
    "Sources: models.jsonl + model_tier.jsonl live-lane rule (the same one "
    "measurement_coverage uses) and the rolling averages store. All-lanes "
    "0/1 series were rejected per spec §7 (scrape-budget warning).",
    ("category", "kind", "provider", "model"),
    _collect_lane_coverage)


# --- resolve-path instrumentation (called from router_server) ----------------

def record_resolve(result: Any, duration_s: Any = 0.0,
                   profile: Any = None) -> None:
    """Record one finished /resolve operation (success AND failure).

    result is the router_spawn.py JSON payload (chain / exclusions / error).
    A resolve that errors MUST land here with {'error': ...} so the failure
    counter moves — a success-only exporter is the defect this row names.
    Never raises: metrics must not change the resolve contract.
    """
    try:
        _record_resolve(result or {}, duration_s, profile)
    except Exception:  # noqa: BLE001 — fail-open contract
        pass


def _record_resolve(result: dict, duration_s: Any, profile: Any) -> None:
    error = result.get("error")
    if error:
        # The resolver's structured no-chain response (TR-383) carries
        # error="no chain ..." plus a `diagnosis`: that is the no-hops
        # incident shape, not a hard failure. Any other error key (unknown
        # project 400, failed subprocess) is a hard failure.
        outcome = ("no_hops" if (result.get("diagnosis") is not None
                                 or "no chain" in str(error)) else "failure")
    else:
        outcome = "success"
    prof = str(profile or result.get("profile") or "unknown")
    chain = [c for c in (result.get("chain") or [])
             if isinstance(c, dict)] if isinstance(result, dict) else []
    top = None
    for ent in chain:
        if ent.get("provider") and ent.get("model"):
            top = ent
            break
    top_labels = {"profile": prof, "outcome": outcome,
                  "provider": str(top["provider"]) if top else "",
                  "model": str(top["model"]) if top else ""}
    RESOLVE_TOTAL.inc(top_labels)
    # The failure path a success-only exporter hides: a hard error moves a
    # dedicated error counter, and the no-hops incident shape its own.
    if outcome == "failure":
        RESOLVE_ERRORS.inc({"profile": prof})
    if outcome == "no_hops":
        RESOLVE_NOHOPS.inc({"profile": prof})
    CHAIN_HOPS.observe({"profile": prof, "outcome": outcome}, len(chain))
    RESOLVE_DURATION.observe({"profile": prof, "outcome": outcome},
                             float(duration_s or 0.0))
    if top is not None:
        # Chain-level families carry provider/model wherever a hop datum
        # exists: chain_hops per-lane series observe the lane's 1-based hop
        # position; duration per-lane series carry the resolve duration
        # keyed by the lane the router ranked first.
        for ent in chain:
            if not (ent.get("provider") and ent.get("model")):
                continue
            CHAIN_HOPS.observe({"provider": str(ent["provider"]),
                                "model": str(ent["model"])},
                               ent.get("hop") if isinstance(
                                   ent.get("hop"), (int, float)) else 0)
        RESOLVE_DURATION.observe({"provider": str(top["provider"]),
                                  "model": str(top["model"])},
                                 float(duration_s or 0.0))
    if isinstance(result, dict):
        for ex in (result.get("exclusions") or []):
            if not isinstance(ex, dict):
                continue
            codes = ex.get("codes") or ["unknown"]
            for code in codes:
                LANE_EXCLUDED_TOTAL.inc({
                    "reason": str(code),
                    "provider": str(ex.get("provider") or ""),
                    "model": str(ex.get("model") or ""),
                })


def render_metrics() -> bytes:
    """Render the full exposition document (both name rows per family)."""
    return REGISTRY.render()

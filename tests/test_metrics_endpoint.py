"""TR-377 — /metrics Prometheus exposition tests.

Covers the row acceptance for the metrics endpoint: the route answers 200
with the exposition content type on EVERY surface; the five delivery-critical
families render with their labels after a synthetic resolve; histograms
render _bucket/_sum/_count; a failure path moves an error counter (and the
no-hops shape its own); hostile label values cannot break the document; and
the module imports stdlib-only (no prometheus_client at import time).

Hermetic like the rest of the suite: the tests boot the real RouterHandler on
an ephemeral loopback port and drive the renderer directly; no live service,
no live state file writes. Circuit-collector behavior is proven against a
fixture state dir via ROUTER_STATE_DIR.
"""
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
#: Subprocess interpreter: the fleet board venv when present, else the
#: running python (CI / fresh clone have no Bane-host venv).
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable
)
SERVER = REPO / "scripts" / "router_server.py"
# Import seam for router_prometheus / router_server regardless of which test
# runs first (the route tests must not be the ones to bootstrap the path).
sys.path.insert(0, str(SCRIPTS))

#: Every sample line must parse: name, optional {labels}, one value.
SAMPLE_LINE_RE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? "
    r"(NaN|[-+]?(Inf|\d+(\.\d+([eE][+-]?\d+)?)?|\d+[eE][+-]?\d+))$")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The five delivery-critical families from the row acceptance, verbatim.
ROW_FAMILIES = (
    "router_resolve_total",
    "router_chain_hops",
    "router_lane_excluded_total",
    "router_lane_coverage",
    "router_resolve_duration_seconds",
)


def _rp():
    import router_prometheus
    return router_prometheus


def _boot(surface):
    """Boot the REAL handler class on an ephemeral loopback port."""
    sys.path.insert(0, str(SCRIPTS))
    from router_server import RouterApplication, RouterHTTPServer, \
        set_surface_mode
    set_surface_mode(surface)
    app = RouterApplication("read-only", "", surface=surface)
    httpd = RouterHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode()


def _stop(httpd):
    httpd.shutdown()
    httpd.server_close()


#: A synthetic successful resolve payload shaped exactly like
#: router_spawn.py's JSON (chain entries carry hop/provider/model,
#: exclusions carry the resolver's own machine-readable `codes`).
SYNTH_SUCCESS = {
    "project": "tr377-test",
    "profile": "TR377A",
    "chain": [
        {"hop": 1, "provider": "prov-alpha", "model": "model-a:chat-latest",
         "usd_1m": 1.0},
        {"hop": 2, "provider": "prov-beta", "model": "model-b",
         "usd_1m": 2.0},
    ],
    "exclusions": [
        {"hop": 3, "provider": "prov-gamma", "model": "model-g",
         "why": ["quota GATED until reset"], "codes": ["quota-gated"]},
    ],
}


# (a) the route answers on every surface with the exposition content type ----

@pytest.mark.parametrize("surface", ("api", "proxy", "ui", "all"))
def test_metrics_route_on_every_surface(surface):
    httpd = _boot(surface)
    try:
        status, headers, body = _get(httpd.server_address[1], "/metrics")
    finally:
        _stop(httpd)
    assert status == 200
    assert headers["Content-Type"] == CONTENT_TYPE
    assert "# TYPE" in body


def test_metrics_route_before_dispatch_mechanics():
    """The route lives in do_GET before app.dispatch — a dispatch 404 for an
    unknown path must not shadow it, and the document is the real renderer."""
    httpd = _boot("all")
    try:
        port = httpd.server_address[1]
        status, headers, body = _get(port, "/metrics")
        assert status == 200
        assert "router_" in body
        try:
            _get(port, "/definitely-not-a-route")
            code = None
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404
    finally:
        _stop(httpd)


# (b) the five families appear with their labels after a synthetic resolve ---

def test_five_families_after_a_resolve():
    rp = _rp()
    rp.record_resolve(SYNTH_SUCCESS, duration_s=0.123)
    text = rp.render_metrics().decode()
    # (1) router_resolve_total{profile,outcome} (+ the served top hop)
    assert ('router_resolve_total{model="model-a:chat-latest",'
            'outcome="success",profile="TR377A",'
            'provider="prov-alpha"} 1') in text
    # (2) router_chain_hops histogram
    assert "# TYPE router_chain_hops histogram" in text
    # (3) router_lane_excluded_total{reason} under the resolver's own code
    assert ('router_lane_excluded_total{model="model-g",'
            'provider="prov-gamma",reason="quota-gated"} 1') in text
    # (4) router_lane_coverage{category} — repo fixture tables always
    # render at least one live category (value may be 0.0 in a fresh tree
    # with an empty rolling store: that 0.0 is a true measurement).
    assert "# TYPE router_lane_coverage gauge" in text
    assert re.search(r'^router_lane_coverage\{category="[^"]+",', text, re.M)
    # (5) router_resolve_duration_seconds histogram
    assert "# TYPE router_resolve_duration_seconds histogram" in text
    assert ('router_resolve_duration_seconds_count{outcome="success",'
            'profile="TR377A"} 1') in text


def test_spec_alias_prefix_rides_the_same_scrape():
    """V1 (spec §6): grep -c '^task_router_' >= 20 on the same document."""
    rp = _rp()
    rp.record_resolve(dict(SYNTH_SUCCESS, profile="TR377E"), duration_s=0.1)
    text = rp.render_metrics().decode()
    n = sum(1 for line in text.splitlines()
            if line.startswith("task_router_"))
    assert n >= 20, f"only {n} task_router_ sample lines"
    # the five row families each carry their alias, doubled-prefix-free
    for alias in ("task_router_resolve_total", "task_router_chain_hops",
                  "task_router_lane_excluded_total",
                  "task_router_lane_coverage",
                  "task_router_resolve_duration_seconds"):
        assert f"# TYPE {alias} " in text
    assert "task_router_router_" not in text


# (c) histogram renders _bucket / _sum / _count ------------------------------

def test_histogram_renders_buckets_sum_count():
    rp = _rp()
    payload = dict(SYNTH_SUCCESS, profile="TR377F")
    payload["chain"] = [
        {"hop": 1, "provider": "prov-hist", "model": "model-hist:only-here",
         "usd_1m": 1.0},
        {"hop": 2, "provider": "prov-hist-2", "model": "model-hist-2",
         "usd_1m": 2.0},
    ]
    rp.record_resolve(payload, duration_s=0.123)
    text = rp.render_metrics().decode()
    # chain-level series: chain length 2 lands in the le=2.0 bucket
    assert ('router_chain_hops_bucket{le="2.0",outcome="success",'
            'profile="TR377F"} 1') in text
    assert 'router_chain_hops_bucket{le="1.0",outcome="success",' \
           'profile="TR377F"} 0' in text
    assert ('router_chain_hops_bucket{le="+Inf",outcome="success",'
            'profile="TR377F"} 1') in text
    assert ('router_chain_hops_sum{outcome="success",'
            'profile="TR377F"} 2') in text
    assert ('router_chain_hops_count{outcome="success",'
            'profile="TR377F"} 1') in text
    # per-lane series (the provider/model sample set): lane hop-1 observed
    assert ('router_chain_hops_bucket{le="1.0",model="model-hist:only-here",'
            'provider="prov-hist"} 1') in text
    # duration histogram: 0.123 lands in le=0.25
    assert ('router_resolve_duration_seconds_bucket{le="0.25",'
            'outcome="success",profile="TR377F"} 1') in text
    assert ('router_resolve_duration_seconds_sum{outcome="success",'
            'profile="TR377F"} 0.123') in text


# (d) the failure path moves an error counter --------------------------------

def test_failure_moves_the_error_counter():
    rp = _rp()
    rp.record_resolve({"error": "nope not in registry"},
                      duration_s=0.0, profile="TR377B")
    text = rp.render_metrics().decode()
    assert 'router_resolve_errors_total{profile="TR377B"} 1' in text
    # the alias moves with it
    assert 'task_router_resolve_errors_total{profile="TR377B"} 1' in text
    # and the resolve itself is counted as a failure
    assert ('router_resolve_total{model="",outcome="failure",'
            'profile="TR377B",provider=""} 1') in text


def test_nohops_is_counted_but_not_as_a_hard_error():
    """The resolver's structured no-chain response (TR-383) is the no-hops
    incident shape: its own counter moves, the hard-error counter does not."""
    rp = _rp()
    rp.record_resolve({"error": "no chain — no eligible model and no "
                                "fallback lane could serve",
                       "profile": "TR377C", "diagnosis": {"summary": "x"},
                       "chain": [], "exclusions": []},
                      duration_s=0.4)
    text = rp.render_metrics().decode()
    assert 'router_resolve_nohops_total{profile="TR377C"} 1' in text
    assert ('router_resolve_total{model="",outcome="no_hops",'
            'profile="TR377C",provider=""} 1') in text
    assert 'router_resolve_errors_total{profile="TR377C"}' not in text


def test_proxy_request_counter_moves_at_the_record_site():
    """_proxy_record's one-call-per-request seam feeds the §3.1 families."""
    rp = _rp()
    rp.note_proxy_request("served", "tr377-test-driver")
    rp.note_proxy_request("no-hops", "tr377-test-driver", lane_class="qa")
    text = rp.render_metrics().decode()
    assert ('router_proxy_requests_total{route_outcome="served",'
            'source="tr377-test-driver"} 1') in text
    assert ('router_proxy_requests_total{route_outcome="no-hops",'
            'source="tr377-test-driver"} 1') in text
    # spec §3.1 alias names, prefix-doubled nowhere
    assert ('task_router_proxy_requests_total{route_outcome="no-hops",'
            'source="tr377-test-driver"} 1') in text
    assert 'router_proxy_nohops_total{lane_class="qa"} 1' in text
    assert 'task_router_proxy_nohops_total{lane_class="qa"} 1' in text


def test_error_counter_moved_through_http_route(tmp_path, monkeypatch):
    """The full path: an HTTP resolve that 400s leaves the error counter
    non-zero on the next scrape of the same process.

    Hermetic by construction (TR-175 doctrine): the registry is passed by
    EXPLICIT path — the suite never leans on a seeded <repo>/registry.json —
    and TASK_ROUTER_HOME keeps the spawn's hop-row write inside tmp_path so
    the suite grades the tree without writing into it.
    """
    registry = _seed_registry(tmp_path, [("prov-a", "model-a1", 0.1)])
    monkeypatch.setenv("ROUTING_REGISTRY", registry)
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "router-home"))
    httpd = _boot("all")
    try:
        port = httpd.server_address[1]
        # unknown project -> resolver error payload -> 400
        try:
            _get(port, "/resolve?project=tr377-not-a-real-project")
            raised = False
        except urllib.error.HTTPError as exc:
            raised = exc.code == 400
        assert raised, "unknown project must answer 400"
        status, _, body = _get(port, "/metrics")
        assert status == 200
        assert re.search(
            r"^router_resolve_errors_total\{profile=\"[^\"]*\"\} [1-9]",
            body, re.M), body[:2000]
    finally:
        _stop(httpd)


# (e) label escaping ----------------------------------------------------------

def test_hostile_label_values_cannot_break_the_document():
    rp = _rp()
    rp.record_resolve({
        "profile": "TR377D",
        "chain": [{"hop": 1, "provider": 'prov:"x\\y',
                   "model": "a:b-c\nsecond-line"}],
        "exclusions": [],
    }, duration_s=0.5)
    text = rp.render_metrics().decode()
    assert ('router_resolve_total{model="a:b-c\\nsecond-line",'
            'outcome="success",profile="TR377D",'
            'provider="prov:\\"x\\\\y"} 1') in text
    # and every sample line of the whole document still parses
    bad = [line for line in text.splitlines()
           if line and not line.startswith("#")
           and not SAMPLE_LINE_RE.match(line)]
    assert not bad, f"unparseable sample lines: {bad[:5]}"


# (f) stdlib-only at import time ----------------------------------------------

def test_module_imports_without_prometheus_client():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import router_prometheus\n"
        "leaked = [m for m in sys.modules if m.startswith('prometheus')]\n"
        "assert not leaked, leaked\n"
        "print('IMPORT_CLEAN')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "IMPORT_CLEAN" in out.stdout


def test_pyproject_stays_dependency_free():
    content = (REPO / "pyproject.toml").read_text()
    assert "prometheus" not in content.lower(), \
        "prometheus_client must NOT become a runtime dependency (TR-377)"


# loopback scoping ------------------------------------------------------------

def test_peer_is_loopback_fails_closed():
    sys.path.insert(0, str(SCRIPTS))
    from router_server import _peer_is_loopback
    assert _peer_is_loopback("127.0.0.1") is True
    assert _peer_is_loopback("::1") is True
    assert _peer_is_loopback("::ffff:127.0.0.1") is True
    for host in ("10.0.0.5", "192.168.1.10", "0.0.0.0", "::ffff:10.0.0.5",
                 "", None, "not-an-ip"):
        assert _peer_is_loopback(host) is False, host


# collectors read the sources of truth at scrape time -------------------------

def test_circuit_collector_reads_the_state_file(tmp_path, monkeypatch):
    state = {
        "version": 1,
        "pairs": {
            "prov-a/model-1": {"open_until": "2099-01-01T00:00:00+00:00",
                               "class": "api_down"},
            # cooling history: in the file, but NOT open — must never render
            "prov-b/model-2": {"open_until": "2000-01-01T00:00:00+00:00",
                               "class": "overload"},
        },
        "v2": {"provider_breakers": {
            "prov-a": {"open_until": "2099-01-01T00:00:00+00:00"},
            "prov-old": {"open_until": "2000-01-01T00:00:00+00:00"},
        }, "classes": {}},
    }
    (tmp_path / "circuit-state.json").write_text(json.dumps(state))
    sys.path.insert(0, str(SCRIPTS))
    import router_circuit
    # router_circuit resolves its state path at IMPORT time; the collector
    # reads it through load(), so point the module's STATE at the fixture.
    monkeypatch.setattr(router_circuit, "STATE",
                        str(tmp_path / "circuit-state.json"))
    rp = _rp()
    text = rp.render_metrics().decode()
    assert ('router_circuit_pair_state{class="api_down",model="model-1",'
            'provider="prov-a"} 1') in text
    assert 'provider="prov-b"' not in text
    assert 'router_circuit_open_pairs{class="api_down"} 1' in text
    assert 'router_circuit_open_pairs{class="overload"}' not in text
    assert "router_circuit_provider_breakers 1" in text


def test_metrics_jsonl_bytes_reports_the_hop_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    (tmp_path / "metrics.jsonl").write_text('{"x": 1}\n' * 3)  # 27 bytes
    rp = _rp()
    text = rp.render_metrics().decode()
    assert "router_metrics_jsonl_bytes 27" in text


def test_metrics_jsonl_family_gauge_and_alias(tmp_path, monkeypatch):
    """TR-372: the family gauge sums the RETAINED set (current + closed
    generations) and rides the same task_router_ alias mechanism as the
    existing current-file gauge — one registry, one render, two names."""
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    (tmp_path / "metrics.jsonl").write_text('{"x": 1}\n' * 3)   # 27 bytes
    (tmp_path / "metrics.jsonl.1").write_text("abcdefghij\n")   # 11 bytes
    (tmp_path / "metrics.jsonl.2").write_text("klmnopqrst\n")   # 11 bytes
    rp = _rp()
    text = rp.render_metrics().decode()
    assert "# TYPE router_metrics_jsonl_family_bytes gauge" in text
    assert "# TYPE task_router_metrics_jsonl_family_bytes gauge" in text
    assert "router_metrics_jsonl_family_bytes 49" in text
    assert "task_router_metrics_jsonl_family_bytes 49" in text
    # current-file gauge unchanged: it still reports ONLY the current file
    assert "router_metrics_jsonl_bytes 27" in text


def test_lane_coverage_uses_the_resolver_measurement_view(monkeypatch):
    """The ratio is computed by the resolver's own lane view
    (router_health._store_view — the same seam /health uses), not a parallel
    implementation: patch that ONE seam and prove the wiring picks it up."""
    sys.path.insert(0, str(SCRIPTS))
    import router_health
    fake = {("prov-alpha", "model-a"): {"measured": True},
            ("prov-beta", "model-b"): {"measured": False}}
    monkeypatch.setattr(router_health, "_store_view",
                        lambda path=None: (fake, {"path": "fixture"}))
    rp = _rp()
    text = rp.render_metrics().decode()
    assert "# TYPE router_lane_coverage gauge" in text
    assert re.search(r'^router_lane_coverage\{category="[^"]+",'
                     r'kind="ratio"', text, re.M)


# --- the real serving path end to end (subprocess, hermetic) -----------------
#
# The row's proof is "curl against the LIVE process"; the closest suite-side
# shape is a scratch server subprocess with a synthetic registry and tmp_path
# state. No live unit is touched, nothing writes into the tree.

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed_registry(tmp_path, models):
    """A minimal registry.json: project my-project -> P0_FORE (reasoning 1).

    `models` is a list of (provider, model, price) tuples; every model gets a
    reasoning tier so the profile's requirement is satisfiable. Price order
    is the resolver's default order, so the cheapest NON-gated lane is hop-1.
    """
    tables = {
        "providers": sorted(({"id": p} for (p, _m, _pr) in models),
                            key=lambda r: r["id"]),
        "projects": [{"id": "my-project", "profile": "P0_FORE"}],
        "task_profiles": [{"id": "P0_FORE"}],
        "task_profile_requirements": [
            {"task_id": "P0_FORE", "category": "reasoning", "level": 1},
        ],
        "category_levels": [{"category": "reasoning"}],
        "level_defs": [{"level": -5}, {"level": 5}],
        "models": [
            {"provider": p, "model": m, "normalized_price": pr,
             "token_factor": 1.0, "plan_tier": 1, "data_class": "zdr"}
            for (p, m, pr) in models
        ],
        "model_tier": [
            {"model": m, "category": "reasoning", "tier": 2}
            for (_p, m, _pr) in models
        ],
    }
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}, indent=1))
    return str(reg)


def _subproc_state_dir(tmp_path, open_pair=None):
    """Router state dir: quota OPEN everywhere, plus an optional OPEN pair.

    The open pair forces the resolver to drop that lane with its own
    'circuit OPEN ...' prose -> the resolver's own 'circuit-open' code, so
    router_lane_excluded_total gets a real exclusion to count.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    providers = ["prov-a", "prov-b"]
    (state / "quota-state.json").write_text(json.dumps(
        {"providers": {p: {"status": "open"} for p in providers}}))
    (state / "health-state.json").write_text(json.dumps({"providers": {}}))
    pairs = {}
    if open_pair:
        pairs[open_pair] = {
            "open_until": "2099-01-01T00:00:00+00:00",
            "failures": 1,
            "class": "api_down",
        }
    (state / "circuit-state.json").write_text(json.dumps(
        {"version": 1, "pairs": pairs,
         "v2": {"provider_breakers": {}, "classes": {}}}))
    return str(state)


def _averages_store(tmp_path, pair):
    """A rolling-averages store with ONE measured lane.

    `pair` must be a REAL (provider, model) lane from the copied tables —
    the coverage collector intersects measured lanes with live registry
    lanes, so a fixture-only lane would correctly render no kind="lane"
    sample (that intersection IS the product's coverage definition).
    """
    path = tmp_path / "outcomes-averages.jsonl"
    provider, model = pair
    row = {"source_system": "chain-run", "provider": provider,
           "model": model, "complexity_sig": None,
           "required_categories": None, "n_samples": 4, "n_completed": 4,
           "n_passed": 3, "n_success_known": 4, "success_rate": 0.75,
           "avg_cost_task_24h": 0.5, "avg_wall_time_24h": 1.0,
           "avg_turns_24h": 2.0, "avg_tokens_in_24h": 100,
           "avg_tokens_out_24h": 50, "avg_tokens_total_24h": 150,
           "cost_per_passed_task_24h": 0.6,
           "cost_per_passed_task_basis": "n_passed=3"}
    path.write_text(json.dumps(row) + "\n")
    return str(path)


def _pick_measured_lane(tables_dir):
    """First live (provider, model) lane in the copied tables whose model
    carries a tier — deterministic, so env and assertions agree."""
    tiered = set()
    with open(os.path.join(tables_dir, "model_tier.jsonl")) as fh:
        for line in fh:
            line = line.strip()
            if line:
                tiered.add(json.loads(line).get("model"))
    with open(os.path.join(tables_dir, "models.jsonl")) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("disabled") or not row.get("provider") \
                    or not row.get("model"):
                continue
            if row.get("model") in tiered:
                return (str(row["provider"]), str(row["model"]))
    raise AssertionError("no tiered live lane in the copied tables")


def _subproc_request(port, path, timeout=60):
    """(status, lower-cased headers, raw body bytes) for a loopback GET."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return (response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read())
    except urllib.error.HTTPError as exc:
        return (exc.code,
                {k.lower(): v for k, v in exc.headers.items()},
                exc.read())


def _subproc_env(tmp_path, models, open_pair="prov-a/model-a1"):
    data_dir = str(tmp_path / "tables")
    if not os.path.isdir(data_dir):
        shutil.copytree(REPO / "data" / "tables", data_dir)
    measured = _pick_measured_lane(data_dir)
    env = {
        **os.environ,
        "ROUTING_DATA_DIR": data_dir,
        "ROUTING_REGISTRY": _seed_registry(tmp_path, models),
        "ROUTER_STATE_DIR": _subproc_state_dir(tmp_path, open_pair=open_pair),
        "ROUTING_AVERAGES_FILE": _averages_store(tmp_path, measured),
        # every runtime write lands in tmp_path, never in the tree
        "TASK_ROUTER_HOME": str(tmp_path / "router-home"),
        "ROUTING_OUTCOMES_FILE": str(tmp_path / "outcomes.jsonl"),
    }
    # Stashed for tests that assert the measured-lane marker. NOT a real env
    # var: callers pop it before handing the dict to the subprocess.
    env["_MEASURED_LANE"] = measured
    return env


@contextlib.contextmanager
def _subproc_server(server_env, surface=None):
    # test-facing stash, never a process env var
    server_env.pop("_MEASURED_LANE", None)
    data_dir = server_env["ROUTING_DATA_DIR"]
    if not os.path.isdir(data_dir):
        shutil.copytree(REPO / "data" / "tables", data_dir)
    port = _free_port()
    argv = [PY, str(SERVER), "--mode", "read-only", "--host", "127.0.0.1",
            "--port", str(port)]
    if surface:
        argv += ["--surface", surface]
    proc = subprocess.Popen(argv, cwd=str(REPO), env=dict(server_env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        deadline = time.monotonic() + 25
        last = None
        healthy = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"scratch server died at boot (rc={proc.returncode}): "
                    f"{err[-800:] or out[-800:]}")
            try:
                status, _h, _b = _subproc_request(port, "/health", timeout=5)
                last = status
                if status == 200:
                    healthy = True
                    break
            except (urllib.error.URLError, socket.timeout, OSError):
                pass
            time.sleep(0.2)
        if not healthy:
            raise AssertionError(f"server never became healthy (last={last})")
        yield port
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def _sample_lines(body: bytes, name: str):
    """Rendered sample lines (not HELP/TYPE) of one family — histogram
    sub-samples (`_bucket` / `_sum` / `_count`) included."""
    return [ln for ln in body.decode().splitlines()
            if ln.startswith(f"{name}{{") or ln.startswith(f"{name}_")
            or ln == name or ln.startswith(f"{name} ")]


@pytest.mark.parametrize("surface", ("api", "proxy", "ui", "all"))
def test_metrics_route_on_every_surface_subprocess(tmp_path, surface):
    """(a) through the REAL process: 200 + exposition content type."""
    env = _subproc_env(tmp_path, [("prov-a", "model-a1", 0.1),
                                  ("prov-b", "model-b1", 0.3)])
    with _subproc_server(env, surface=surface) as port:
        status, headers, body = _subproc_request(port, "/metrics")
    assert status == 200, (surface, status, body[:200])
    assert headers.get("content-type") == CONTENT_TYPE, surface
    assert body.startswith(b"# HELP "), surface


def test_five_families_after_a_real_resolve(tmp_path):
    """(b) one forced resolve through the real path, then the scrape."""
    # prov-a/model-a1 is circuit-OPEN (state fixture) so the resolve produces
    # BOTH a served chain and a counted exclusion.
    models = [("prov-a", "model-a1", 0.1), ("prov-a", "model-a2", 0.2),
              ("prov-b", "model-b1", 0.3)]
    with _subproc_server(_subproc_env(tmp_path, models)) as port:
        status, _h, body = _subproc_request(
            port, "/resolve?project=my-project")
        assert status == 200, body[:400]
        resolved = json.loads(body)
        assert resolved.get("chain"), resolved
        status, _h, metrics = _subproc_request(port, "/metrics")
    assert status == 200
    doc = metrics.decode()

    for name in ROW_FAMILIES:
        assert f"# TYPE {name} " in doc, name
        # alias = task_router_<base> (the renderer strips the router_ prefix;
        # task_router_router_resolve_total must never exist)
        base = name[len("router_"):] if name.startswith("router_") else name
        assert f"# TYPE task_router_{base} " in doc, f"alias missing: {name}"
        assert _sample_lines(metrics, name), name

    # (1) resolve_total: success, answered profile, top-ranked lane labels
    # (price-first puts prov-a/model-a2 at hop-1; model-a1 is circuit-open)
    success = [ln for ln in _sample_lines(metrics, "router_resolve_total")
               if 'outcome="success"' in ln]
    assert success, doc
    assert any('profile="P0_FORE"' in ln for ln in success)
    assert any('provider="prov-a"' in ln and 'model="model-a2"' in ln
               for ln in success), success

    # (2) chain_hops counted the served resolve (2 live hops)
    hops = [ln for ln in _sample_lines(metrics, "router_chain_hops_count")
            if 'outcome="success"' in ln]
    assert hops and float(hops[0].rsplit(" ", 1)[1]) >= 1.0

    # (3) lane_excluded_total: the resolver's OWN code for the open pair
    excluded = [ln for ln in
                _sample_lines(metrics, "router_lane_excluded_total")
                if 'reason="circuit-open"' in ln]
    assert excluded, doc
    assert any('provider="prov-a"' in ln and 'model="model-a1"' in ln
               for ln in excluded)

    # (4) lane_coverage: ratio per category + a measured-lane marker with
    # provider/model for the fixture-measured lane (deterministically picked
    # from the same copied tables the server reads).
    measured = _pick_measured_lane(str(tmp_path / "tables"))
    ratios = [ln for ln in doc.splitlines()
              if ln.startswith("router_lane_coverage{")
              and 'kind="ratio"' in ln]
    assert ratios, doc
    assert any('category="reasoning"' in ln for ln in ratios)
    lanes = [ln for ln in doc.splitlines()
             if ln.startswith("router_lane_coverage{")
             and 'kind="lane"' in ln]
    assert any(f'provider="{measured[0]}"' in ln
               and f'model="{measured[1]}"' in ln
               for ln in lanes), (lanes[:5], measured)

    # (5) duration observed for the same resolve
    dur = [ln for ln in _sample_lines(
        metrics, "router_resolve_duration_seconds_count")
        if 'outcome="success"' in ln]
    assert dur and float(dur[0].rsplit(" ", 1)[1]) >= 1.0

    # Spec V1 check holds against this same scrape
    alias_lines = [ln for ln in doc.splitlines()
                   if ln.startswith("task_router_")]
    assert len(alias_lines) >= 20, len(alias_lines)


def test_failure_path_moves_an_error_counter(tmp_path):
    """(d) an unknown project (400) through the real path moves the
    failure counters on the next scrape of the SAME process."""
    models = [("prov-a", "model-a1", 0.1), ("prov-b", "model-b1", 0.3)]
    with _subproc_server(_subproc_env(tmp_path, models)) as port:
        status, _h, body = _subproc_request(
            port, "/resolve?project=not-a-project")
        assert status == 400, body[:300]
        _status, _h, metrics = _subproc_request(port, "/metrics")
    doc = metrics.decode()
    errors = [ln for ln in doc.splitlines()
              if ln.startswith("router_resolve_errors_total{")]
    assert errors, doc
    assert any(float(ln.rsplit(" ", 1)[1]) >= 1.0 for ln in errors)
    failures = [ln for ln in doc.splitlines()
                if ln.startswith("router_resolve_total{")
                and 'outcome="failure"' in ln]
    assert failures and float(failures[0].rsplit(" ", 1)[1]) >= 1.0


def test_label_escaping_survives_a_real_resolve(tmp_path):
    """(e) ':' / '-' / quote / backslash inside REAL registry model ids."""
    models = [("prov-a", "model:x-1", 0.1),
              ("prov-a", 'mod"el\\x', 0.2),
              ("prov-b", "model-b1", 0.3)]
    with _subproc_server(_subproc_env(tmp_path, models, open_pair=None)) \
            as port:
        status, _h, body = _subproc_request(
            port, "/resolve?project=my-project")
        assert status == 200, body[:400]
        _status, _h, metrics = _subproc_request(port, "/metrics")
    doc = metrics.decode()
    # ':' and '-' ride verbatim inside the quoted label value...
    assert 'model="model:x-1"' in doc
    # ...while the quote and the backslash are escaped per the format
    # (model id chars: mod"el\x -> mod\"el\\x in the document).
    assert 'model="mod\\"el\\\\x"' in doc
    # and the whole document still parses as exposition lines.
    for line in doc.splitlines():
        if not line.startswith("#") and line.strip():
            assert SAMPLE_LINE_RE.match(line), f"broken line: {line!r}"


def test_histogram_buckets_monotone_through_the_real_path(tmp_path):
    """(c) cumulative buckets non-decreasing and +Inf == _count, live."""
    models = [("prov-a", "model-a1", 0.1), ("prov-a", "model-a2", 0.2),
              ("prov-b", "model-b1", 0.3)]
    with _subproc_server(_subproc_env(tmp_path, models)) as port:
        _subproc_request(port, "/resolve?project=my-project")
        _status, _h, metrics = _subproc_request(port, "/metrics")
    doc = metrics.decode()
    for family in ("router_chain_hops",
                   "router_resolve_duration_seconds"):
        rows = []
        for line in doc.splitlines():
            if not line.startswith(f"{family}_bucket{{"):
                continue
            if 'outcome="success"' not in line or \
                    'profile="P0_FORE"' not in line:
                continue
            le = re.search(r'le="([^"]+)"', line).group(1)
            rows.append((le, float(line.rsplit(" ", 1)[1])))
        assert rows, family
        vals = [v for (_le, v) in rows]
        assert vals == sorted(vals), f"{family} not monotone: {rows}"
        inf = dict(rows).get("+Inf")
        count_line = [ln for ln in doc.splitlines()
                      if ln.startswith(f"{family}_count{{")
                      and 'outcome="success"' in ln
                      and 'profile="P0_FORE"' in ln]
        assert count_line and inf == float(
            count_line[0].rsplit(" ", 1)[1]), family

# Observability spec — task-router

Status: proposed (audit 2026-11-03). No service code is changed by this document.
Audit verdict before: **NONE** — zero `/metrics` endpoint on any listener, no
Prometheus client dependency, no metric emitted to the fleet.
Audit verdict after this spec is implemented: **SCRAPE-READY** (`/metrics` on
the existing API server, plus a textfile completion record for the short-lived CLI lanes).

---

## 1. What this service is, and what "healthy" means

`task-router` is the fleet's **model router**. Three long-running listeners run from
the same entrypoint file (`scripts/router_server.py`), with the mode chosen by CLI flags:

| Port | Unit | Role | Citation |
|------|------|------|----------|
| `:9092` | `task-router-server.service` | Read API / control plane: `/resolve`, `/health`, `/status`, `/api/v1/outcomes`, `/mcp` | `docs/health-plane.md:12` |
| `:9391` | `task-router-proxy.service` | Classified proxy: `POST /v1/chat/completions`, `POST /v1/responses`; walks the fallback ladder | `docs/health-plane.md:13` |
| `:9093` | (user-scoped) | Local web UI, read-only | `scripts/router_stale_watchdog.py:33` |

Plus one **short-lived CLI lane** invoked on every scheduler tick:
`scripts/router_spawn.py <project> --format json` — it builds the eligible chain and exits.

"Healthy" for this product means, in order of operational importance:

1. **The ladder delivers.** A resolve that finds no eligible hop ("no-hops") is the
   product failing at its one job. This is the incident class that already happened
   once (2026-10; see `scripts/router_nohops.py:4-6`).
2. **The chain is served by the lane the router chose**, not a distant fallback.
3. **Circuit breakers are not silently accumulating** open pairs.
4. **Admission is not shedding** — the proxy refuses callers when full by design
   (`scripts/router_server.py:3061`), but a rising shed rate is invisible today.
5. **The data plane is not filling the disk** — `data/metrics.jsonl` is already
   large and unrotated (see §2, finding 1).

## 2. Proven current state (evidence, not speculation)

| Question | Answer | Evidence |
|---|---|---|
| Is there a `/metrics` endpoint? | **No.** All three listeners return 404. | `curl -o /dev/null -w '%{http_code}' :9092/metrics` → `404`; same for `:9391`, `:9093`. Also in journald: `router_server: "GET /metrics HTTP/1.1" 404` |
| Is a Prometheus client used? | **No.** Absent from `pyproject.toml`/`uv.lock`; no `prometheus_client` import anywhere in `scripts/` or `task_router/`. | `grep -rn prometheus pyproject.toml uv.lock` → empty |
| Is there any HTTP surface? | **Yes**, four: `/health`, `/status`, `/resolve`, `/api/v1/outcomes`, `/api/ui/*`, `/v1/chat/completions`. Adding `/metrics` is a route addition, not new infrastructure. | `scripts/router_server.py:111` (`/health` descriptor), `:5447` (`do_GET`) |
| What is logged? | **Only one-line access logs** to journald, from `BaseHTTPRequestHandler`'s default `log_message`. There is **no `logging.*` call anywhere in `router_server.py`** — no structured fields, no error-level log for a failed hop. | `grep -n "logging\.\|basicConfig" scripts/router_server.py` → 0 hits; `journalctl --user -u task-router-server.service` shows `"GET /health HTTP/1.1" 200 -` |
| What is the durable output? | Two append-only JSONL stores: `data/metrics.jsonl` (one row per chain hop per resolve, TR-021) and `data/state/outcomes.jsonl` (one row per finished request). | `scripts/router_spawn.py:992-1080` (`_append_metrics`); `scripts/router_server.py:4405` (`_proxy_record`); `docs/outcomes-schema.md:13` |
| Is any of that exported? | **No.** It is local files only. The only consumer is a query CLI: `scripts/router_metrics.py:2-10`. | `scripts/router_metrics.py` header |
| Can the Alerter alert? | **Yes, in-process only.** `router_nohops.py check` computes a real verdict, but nothing ships it to Alertmanager. | see §5 |

### Live measurements taken during this audit

```
$ timeout 400 python3 scripts/router_nohops.py check --window-h 24
verdict=ALERT kind=rate+burst
no-hops rate 0.737327 over 24.0h (n=2387, threshold 0.227329 via baseline file threshold)
breach_reason=rate 0.737327 > threshold 0.227329; burst 62 no-hops in 1h >= ROUTER_NOHOPS_BURST_MIN=50
```

This is the single most important fact in this document: **the router is currently
breaching its own no-hops alert at 73.7% against a 22.7% threshold, and Prometheus
has no idea.** A 3am operator looking at Grafana sees a healthy host and a dead product.

```
$ stat -c %s data/metrics.jsonl          -> 6,326,911,883 bytes  (6.3 GB)
$ sample of 10,000 lines                 -> avg 614 bytes/line  (~10.3 M rows)
$ 20s growth window                      -> +55,230 bytes  ≈ 239 MB/day
$ df -h /                                -> 1.8T total, 177G free, 90% used
```

`data/metrics.jsonl` gets one row *per hop*, and one live row shows
`"chain_length": 350` — a single resolve on a 1202-lane registry can write hundreds of
rows. It grows ~239 MB/day with **no rotation and no retention cap**, on a filesystem
at 90%. That is a converging failure with no early-warning signal.

```
$ curl -s localhost:9092/health | jq -c '{admission, coverage, circuit, stale}'
{"admission": {"waiting":0,"accepted":0,"rejected":0,"peak_inflight":0,"inflight":0,
               "max_inflight":8,"queue_max":32,"queue_wait_s":20.0}, ...}
"coverage": {"basis":"price-first","reason":"price-first: 29 of 1202 measured
             (coverage 0.024 < floor 0.5)", "measured":29,"lanes":1202}
"circuit":  {"pairs":6,"open":4,"classes":{"api_down":4,"policy_gate":1,"overload":1},
             "provider_breakers":1}
"stale":    true
```

Note that these counters are **per-process and reset on restart** (`_ADMISSION` at
`scripts/router_server.py:3020-3022`), which is exactly the case where a Prometheus
counter is the right primitive and a JSON blob read on demand is not: you cannot compute
a *rate* from a value that resets and is only ever sampled by hand.

## 3. Metric catalogue

Naming: `<product>_<subsystem>_<thing>_<unit>`. Product prefix is `task_router`.
All labels are **low cardinality**: provider and model come from the registry (bounded,
thousands), project/profile come from the closed set in `data/tables/projects.jsonl`
and `task_profiles.jsonl`. **Never** a session id, task id, prompt, request path,
caller key or timestamp — those live in the JSONL stores, which are the right tool for
per-request forensics and the wrong tool for a time series.

### 3.1 Subsystem `proxy` — the serving path (`:9391`)

| Metric | Type | Unit | Labels | Question it answers |
|---|---|---|---|---|
| `task_router_proxy_requests_total` | counter | requests | `route_outcome{served,failed,no_hops,rejected}`, `source` | Is the proxy doing work, and how is it ending? |
| `task_router_proxy_hop_attempts_total` | counter | attempts | `provider`, `model`, `result{ok,error}` | Which lane is being tried, and which one is failing? |
| `task_router_proxy_fallbacks_total` | counter | events | `provider`, `model`, `from_hop` | Is one lane's outage quietly pushing all traffic to the fallback? |
| `task_router_proxy_request_duration_seconds` | histogram | seconds | `route_outcome` | How long does a served request take (p50/p95/p99)? |
| `task_router_proxy_hop_duration_seconds` | histogram | seconds | `provider`, `result` | Which hop is slow — the router's own timer, not the wall clock of the whole request? |
| `task_router_proxy_upstream_errors_total` | counter | responses | `provider`, `code_class{429,5xx,402,4xx_other}` | Rate limits and upstream breakage per lane. **The single most-needed view.** |
| `task_router_proxy_nohops_total` | counter | resolves | `lane_class` | The incident shape, as a rate you can alert on. |
| `task_router_proxy_cost_usd_total` | counter | USD | `provider`, `price_basis{list,plan_offset}` | What did we spend, and on which basis was it estimated? |

### 3.2 Subsystem `admission` — backpressure

| Metric | Type | Unit | Labels | Question it answers |
|---|---|---|---|---|
| `task_router_admission_inflight` | gauge | requests | — | How close are we to the ceiling right now? |
| `task_router_admission_rejected_total` | counter | requests | `reason{queue_full,no_slot}` | Are we shedding traffic? (state today: `_ADMISSION['rejected']`, `scripts/router_server.py:3061-3080`) |
| `task_router_admission_queue_depth` | gauge | waiters | — | Is a queue building? (`_ADMISSION['waiting']`, `:3053`) |
| `task_router_admission_limit` | gauge | requests | `kind{inflight,queue}` | What is the ceiling in force? (config is `ROUTER_PROXY_MAX_INFLIGHT`, default 8 — `:3033`) |

### 3.3 Subsystem `circuit` — breaker state

State source: `~/.hermes/model-router/circuit-state.json` (`scripts/router_circuit.py:105`).

| Metric | Type | Unit | Labels | Question it answers |
|---|---|---|---|---|
| `task_router_circuit_open_pairs` | gauge | pairs | `class{api_down,overload,quota_window,policy_gate}` | How many lanes are benched? (live now: 4 open, classes `{api_down:4, policy_gate:1, overload:1}`) |
| `task_router_circuit_pair_state` | gauge (0/1) | boolean | `provider`, `model` | Which exact lane is open? Expressed as a one-hot gauge so `sum by (provider)` works. |
| `task_router_circuit_provider_breakers_open` | gauge | breakers | — | Is a whole provider benched? (live: 1) |
| `task_router_circuit_transitions_total` | counter | transitions | `provider`, `to{open,closed}` | How often is a lane flapping? |

### 3.4 Subsystem `resolve` — the short-lived CLI lane (`router_spawn.py`)

| Metric | Type | Unit | Labels | Question it answers |
|---|---|---|---|---|
| `task_router_resolve_chain_length` | histogram | hops | `project`, `profile` | Did a profile's chain shrink to nothing? (one live row: `chain_length: 350`; TR-368 baseline: `P4_SECURITY` 0 hops) |
| `task_router_resolve_nohops_total` | counter | resolves | `project`, `profile` | Same incident shape, from the spawn side |
| `task_router_resolve_duration_seconds` | histogram | seconds | `project` | Chain resolution latency — a scheduler tick that took 40s starves the lane |
| `task_router_resolve_lanes_total` | gauge | lanes | `kind{eligible,excluded,chain}` | Chain anatomy at a glance |
| `task_router_metrics_jsonl_bytes` | gauge | bytes | — | **Disk-growth early warning** for `data/metrics.jsonl` (§2, finding 1) — the CURRENT file only |
| `task_router_metrics_jsonl_family_bytes` | gauge | bytes | — | Total bytes across the RETAINED rotation family (`metrics.jsonl.N … .1` + current; TR-372 `ROUTER_METRICS_MAX_BYTES`/`ROUTER_METRICS_GENERATIONS`) — the real retention footprint |

### 3.5 Subsystem `data` — data-plane freshness

| Metric | Type | Unit | Labels | Question it answers |
|---|---|---|---|---|
| `task_router_registry_age_days` | gauge | days | — | Are the prices stale? (`/health.registry.age_days`, already computed) |
| `task_router_outcomes_rows_total` | gauge | rows | — | Is the outcome store still growing (is the product being used)? |
| `task_router_coverage_measured_ratio` | gauge | ratio | — | What fraction of the chain is actually measured? **Live: 0.024 of a 0.5 floor** — the cost-optimisation promise is not being delivered and nothing alerts on it. |
| `task_router_code_stale` | gauge (0/1) | boolean | — | Is the running process behind the repo? **Live: `stale: true`** (`commit 4f43c4f8` vs repo `636397b2`). |
| `task_router_probe_providers_up` | gauge | providers | `status{OK,DOWN,DISABLED}` | Is the upstream fleet reachable? (live: 21 OK / 4 DOWN / 3 DISABLED) |
| `task_router_quota_remaining_ratio` | gauge | ratio | `provider`, `window` | Is a credential about to exhaust? (source: `data/tables/provider_quota.jsonl`) |

## 4. Exposure mechanism

**Decision: a `/metrics` route on the existing servers (all three listeners), via the
official `prometheus_client` library.** The `/health` and `/api/*` routes already
exist and are already scraped by the blackbox exporter, so this is the low-risk path.
The registry is populated by a *collecting* pattern, not by hand-incrementing globals in
ten places: existing per-process dicts (`_ADMISSION`, the circuit state file, the
outcome store) are read at scrape time by a single `collect()` callback. That keeps the
diff to the service small and keeps the numbers honest, because they are read from
the same source of truth the router itself uses.

```python
# scripts/router_server.py — shape of the change, not applied by this audit
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST

_REG = CollectorRegistry()
# ... counters incremented at _proxy_record() / _admission.__enter__ ...
# ... Gauges registered with a callback that reads circuit-state.json + _ADMISSION ...

# in RouterHandler.do_GET(), before app.dispatch():
if parsed.path == "/metrics":
    body = generate_latest(_REG)
    self.send_response(200)
    self.send_header("Content-Type", CONTENT_TYPE_LATEST)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
    return
```

**The short-lived lane (`router_spawn.py`) must NOT be scraped.** It runs for
~1 second and exits on every scheduler tick; a scrape would miss nearly every
invocation, and the process-local state dies with it. The right primitive is a
**textfile completion record**: write
`$TEXTFILE_DIR/task_router_resolve_<project>.prom` atomically (write to `.tmp`, then
`os.replace`) at the end of `_append_metrics()`, with the gauge/histogram summary for
that one resolve. `node_exporter --collector.textfile.directory` picks it up on its next
scrape. This preserves the existing fail-open contract (`scripts/router_spawn.py:1043`
swallows all errors) — a metrics write failure must never change the CLI's stdout or
exit code.

## 5. Collection path

| Step | Detail |
|---|---|
| Scrape job | New job `task-router` in `/root/fleet-monitoring/prometheus/prometheus.yml` on host `fleet-monitor`, targets `karaHermes-mde-7840hs-2:9092` (and `:9391` as a second target with `service="proxy"`). Strategy: `static_configs` + `host`/`product`/`service` labels applied at the job level. |
| Labels | `host="karaHermes-mde-7840hs-2"`, `product="task-router"`, `service::{api,proxy,web}`. Every metric name already carries the `task_router_` prefix, so job-level `product` is a redundant-but-cheap disambiguator. |
| Textfile lane | `router_spawn.py` writes to the node_exporter textfile directory; the existing `node` job picks it up with no new job. |
| Reachability | Both listeners bind `127.0.0.1` (`docs/health-plane.md:12-13`). **This must change before a remote scrape can work** — either bind `0.0.0.0` behind the existing auth, or front `:9092` with the `:9391`-style tailnet address. Flagged as an unverified prerequisite below. |
| What the fleet agent already does | Aliases push via remote_write (`bunker_*`, `fleet_*`, `alert_router_*`). task-router emits **nothing** today, so it contributes **0 of the ~20 product series**. |

## 6. Verification — what a reviewer runs to call this done

Each row is a proof, not a claim. `PROM=http://fleet-monitor:9090`.

```bash
# V1 — the endpoint exists and is scraped, on BOTH listeners
curl -s 127.0.0.1:9092/metrics | grep -c '^task_router_'   # >= 20
curl -s 127.0.0.1:9391/metrics | grep -c '^task_router_'
curl -s $PROM/api/v1/targets | jq '.data.activeTargets[]
       | select(.labels.product=="task-router") | {health,lastScrape}'  # health up

# V2 — up/doing-work
curl -s $PROM/api/v1/query --data-urlencode \
  'query=sum by (service) (rate(task_router_proxy_requests_total[5m]))'

# V3 — the 3am question: are we delivering hops, or dead-ending?
curl -s $PROM/api/v1/query --data-urlencode \
  'query=sum(rate(task_router_proxy_nohops_total[1h]))
         / ignoring(route_outcome) group_left sum(rate(task_router_proxy_requests_total[1h]))' \
  | jq '.data.result[0].value[1]'     # must be < 0.227 (nohops threshold)

# V4 — per-lane 429/5xx
curl -s $PROM/api/v1/query --data-urlencode \
  'query=sum by (provider,code_class) (rate(task_router_proxy_upstream_errors_total[15m]))'

# V5 — circuit breaker state
curl -s $PROM/api/v1/query --data-urlencode \
  'query=sum by (class) (task_router_circuit_open_pairs)'

# V6 — admission shedding
curl -s $PROM/api/v1/query --data-urlencode \
  'query=rate(task_router_admission_rejected_total[10m])'

# V7 — disk-growth early warning (metrics.jsonl)
curl -s $PROM/api/v1/query --data-urlencode \
  'query=task_router_metrics_jsonl_bytes'

# V8 — the short-lived lane (textfile record)
curl -s 127.0.0.1:9100/metrics | grep 'task_router_resolve_'

# V9 — cost
curl -s $PROM/api/v1/query --data-urlencode \
  'query=sum by (provider) (increase(task_router_proxy_cost_usd_total[24h]))'
```

**Done looks like:** V1 shows two healthy `task-router` targets; V2-V9 each return a
non-empty result; and Grafana has one dashboard whose first panel is
`nohops_rate` with the 0.227 threshold line drawn on it. The dashboard is not the
acceptance criterion — the PromQL returning data is.

### Alert rules (minimum set)

| Alert | Expression | For |
|---|---|---|
| `TaskRouterNoHopsHigh` | V3 > 0.227 | 30m |
| `TaskRouterDown` | `up{product="task-router"} == 0` | 2m |
| `TaskRouterShedding` | V6 > 0.1 | 10m |
| `TaskRouterCircuitMassOpen` | `sum(task_router_circuit_open_pairs) > 10` | 15m |
| `TaskRouterMetricsJsonlGrowth` | `deriv(task_router_metrics_jsonl_bytes[6h]) * 86400 > 5e8` | 1h |
| `TaskRouterCodeStale` | `task_router_code_stale == 1` | 2h |

### 6.1 The no-hops rule artifact (TR-371)

`TaskRouterNoHopsHigh` ships as a **repository-owned artifact**:
`monitoring/task-router-alerts.yml`. The live fleet rule set itself lives
outside this repo (§5: `/root/fleet-monitoring/prometheus/prometheus.yml` on
host `fleet-monitor`) and is **not verifiable from here** — the artifact plus
`tests/test_nohops_alert_rule.py` are the contract of record; deploying the
rule to the fleet rule set remains an **unverified** external step and no
curl against `fleet-monitor:9090`/`:9093` can be claimed from this tree.

- **Expression**: the V3 ratio —
  `sum(rate(task_router_proxy_nohops_total[1h])) / sum(rate(task_router_proxy_requests_total[1h])) > 0.227329`,
  `for: 30m`. The spec one-liner's `ignoring(route_outcome) group_left` is a
  no-op once both sides are `sum()`-aggregated; the artifact's plain `/` is
  the same ratio. The inputs are the TR-377 exposition families
  (`router_proxy_nohops_total` / `router_proxy_requests_total` and their
  `task_router_*` aliases). Provenance note: the board row cites "the
  Prometheus ratio from TR-369/TR-370"; TR-369/TR-370 are the
  capability-contract / perf-store-parity rows — the Prometheus ratio meant
  here is this §6 V3 expression over the TR-377 families. Named, not silently
  reinterpreted.
- **Threshold source**: `data/nohops-baseline.json` — the rule literal is that
  file's `threshold` key (measured baseline rate 0.177329 over a 168h window
  + headroom 0.05; recompute via
  `ROUTING_OUTCOMES_FILE=<live store> python3 scripts/router_nohops.py baseline --window-h 168 --write`).
  The same file feeds the CLI alert (`router_nohops.py`,
  threshold_source "baseline file threshold"), so the alert, the CLI and the
  UI share one threshold. The test pins rule threshold == baseline file: a
  recomputed baseline and the rule literal must land in the same commit.
- **Denominator semantics**: the proxy ratio counts every finished proxy
  request (admission sheds included); the CLI ratio excludes
  `route_outcome=rejected` from both sides. Same threshold over a larger
  denominator — the Prometheus arm is the conservative (lower) of the two.

## 7. Deliberately not instrumented

- **Per-session / per-task / per-caller-key labels.** Straight to a cardinality
  explosion — the outcome store already has ~450k rows in a 24h window. Those belong in
  `data/state/outcomes.jsonl` and the existing `/api/ui/flow?id=...` route, which is a
  query, not a time series.
- **Per-model one-hot gauges for all 2060 registry lanes.** `task_router_circuit_pair_state`
  is intentionally limited to *open* pairs (single digits live). All-lanes-as-series would be
  ~2000 series of constant 1 and would burn scrape budget for zero information.
- **Prompt text, prompt hashes, prices as labels.** `prompt_sha` and `price_usd_per_m` are
  recorded per row (`scripts/router_spawn.py:1052-1060`) and must stay there.
- **The metrics JSONL read back as a metric.** Re-reading a 6.3 GB file at scrape time
  would put the scraper's latency on the same disk as the writer. Only its *size* is exported.
- **`/health` body as a metric source via a scraping bridge (e.g. JSON exporter).** Tempting
  as a zero-code stopgap, rejected: it re-derives state the process already holds and hides
  the real gap (no rate/`histogram` capability). It is a legitimate *fallback* if the code
  change is blocked, but it is not the fix.

## 8. Findings a reviewer must not miss

1. **`data/metrics.jsonl` is 6.3 GB, ~10.3 M rows, ~239 MB/day, no rotation, on a 90%-full
   volume.** This is a disk-full incident in slow motion with no alert. (`stat`, 20s growth
   sample; `scripts/router_spawn.py:1012-1015` writes unconditionally.)
   **Addressed by TR-372** (`scripts/router_metrics_store.py`): the store is now
   size-capped and rotated (`ROUTER_METRICS_MAX_BYTES`, default 512 MiB;
   `ROUTER_METRICS_GENERATIONS`, default 3) before every append, readers resolve the
   retained family, and `router_metrics.py --rotate-now` enforces retention on demand.
   The historical 6.3 GB current file must still be trimmed once by hand or by a first
   `--rotate-now` (a single rotate moves it wholesale to `metrics.jsonl.1`).
2. **No-hops is at 73.7% against a 22.7% threshold right now** — a live, unshipped ALERT
   (`router_nohops.py check --window-h 24`).
3. **Chain coverage is 2.4% against a 50% floor** — cost-per-task routing is effectively
   off, published in `/health.coverage` and consumed by nothing.
4. **The running process is behind the repo** (`stale: true`), so any behaviour read from
   source may not be what is serving traffic.
5. **Admission counters are per-process** and reset on restart — they are only meaningful as
   Prometheus counters, which is why §3.2 is not optional.

## 9. Unverified in this audit

- Whether `karaHermes-mde-7840hs-2` is reachable from `fleet-monitor` on `:9092`/`:9391`.
  Both bind loopback; no scrape can succeed until that is resolved. Not tested (no network
  access from the audit).
- Whether `node_exporter` on this host already sets `--collector.textfile.directory`.
  Assumed yes (standard), not confirmed.
- The exact per-day row count of `metrics.jsonl` — the byte rate is measured, the row count
  is inferred from a 10k-line average, not counted.
- The `outcomes.jsonl` growth rate: a 20s sample showed **zero** growth, which means writes
  are bursty rather than absent; the daily rate was not measured.

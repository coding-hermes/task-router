# Configuration Reference

Complete reference for all `ROUTER_*` environment variables used by task-router.

This document supplements the Configuration table in [README.md](../README.md#configuration) with detailed entries for the remaining environment variables used in code.

## Table of Contents

- [Core paths and state](#core-paths-and-state)
- [Classifier](#classifier)
  - [Primary classifier](#primary-classifier)
  - [Classifier fallback](#classifier-fallback)
  - [Classifier cache](#classifier-cache)
  - [Classifier response shaping](#classifier-response-shaping)
- [Proxy](#proxy)
  - [Proxy endpoints](#proxy-endpoints)
  - [Proxy timeouts](#proxy-timeouts)
  - [Proxy concurrency](#proxy-concurrency)
  - [Proxy stats](#proxy-stats)
- [Spawn and sorting](#spawn-and-sorting)
- [Health and verification](#health-and-verification)
- [Hermes integration](#hermes-integration)
- [Ingress (TR-236)](#ingress-tr-236)
- [Miscellaneous](#miscellaneous)

---

## Core paths and state

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_ENV_FILE` | `router_probefix.py` | Path to the router environment file (`.env`) containing provider credentials | `~/.hermes/.env` |
| `ROUTER_OUTCOMES` | `router_tier_coverage.py` | Path to the outcomes ledger (JSONL) recording spawn outcomes | `data/state/outcomes.jsonl` (repo-relative) |
| `ROUTER_MODELSDEV_CACHE` | `router_lifecycle.py` | Path to the models.dev cache file | `data/modelsdev_cache.json` (repo-relative) |
| `ROUTERMODEL_ROUTER_DIR` | (unused) | Legacy variable, not referenced in current code | — |

---

## Classifier

### Primary classifier

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_CLASSIFIER_BASE_URL` | `router_classify.py`, `router_server.py` | Base URL for the primary classifier endpoint (OpenAI-compatible `/v1/chat/completions`) | `http://127.0.0.1:8642/v1` |
| `ROUTER_CLASSIFIER_MODEL` | `router_classify.py`, `router_server.py` | Model ID to use for classification requests | `deepseek-v4.1-flash` |
| `ROUTER_CLASSIFIER_KEY_ENV` | `router_classify.py` | Name of the environment variable holding the classifier API key | `API_SERVER_KEY` |
| `ROUTER_CLASSIFIER_KEY_VALUE` | `router_classify.py` | Literal API key value (overrides `ROUTER_CLASSIFIER_KEY_ENV` if set) | — |
| `ROUTER_CLASSIFIER_TIMEOUT_S` | `router_classify.py` | Timeout in seconds for primary classifier requests | `60.0` |
| `ROUTER_CLASSIFIER_RETRIES` | `router_classify.py` | Number of retry attempts for the primary classifier lane | `0` |

### Classifier fallback

When the primary classifier fails or is unavailable, the router can fall back to a secondary classifier endpoint.

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_CLASSIFIER_FALLBACK_BASE_URL` | `router_classify.py` | Base URL for the fallback classifier endpoint | — (disabled if unset) |
| `ROUTER_CLASSIFIER_FALLBACK_MODEL` | `router_classify.py` | Model ID for the fallback classifier | — (empty string) |
| `ROUTER_CLASSIFIER_FALLBACK_KEY_ENV` | `router_classify.py` | Name of the environment variable holding the fallback classifier API key | — (empty string) |
| `ROUTER_CLASSIFIER_FALLBACK_KEY_VALUE` | `router_classify.py` | Literal API key value for the fallback classifier (overrides `ROUTER_CLASSIFIER_FALLBACK_KEY_ENV` if set) | — |
| `ROUTER_CLASSIFIER_FALLBACK_TIMEOUT_S` | `router_classify.py` | Timeout in seconds for fallback classifier requests | `60.0` |

### Classifier cache

The server caches classification results to reduce redundant API calls.

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_CLASSIFY_CACHE_MAX` | `router_server.py` | Maximum number of classification results to cache | `512` |
| `ROUTER_CLASSIFY_CACHE_TTL_S` | `router_server.py` | Time-to-live in seconds for cached classification results | `900` (15 minutes) |

### Classifier response shaping

These options control the prompt version, output budget, structured JSON
response format, and reasoning mode for each classification call.

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_CLASSIFY_PROMPT_VERSION` | `router_classify.py` | Selects the classifier prompt file (`v1`, `v2`, or `v3`); `v1`/`v2` remain available for rollback | `v3` |
| `ROUTER_CLASSIFY_MAX_TOKENS` | `router_classify.py` | Positive integer completion-token budget; invalid/nonpositive values fall back to the code default | `16384` |
| `ROUTER_CLASSIFY_STRUCTURED` | `router_classify.py` | `auto` (default) uses the configured structured-response rung and steps down if rejected; `json_schema`/`json_object` force a rung; `off`/`none`/`false`/`0` disables structured output | `auto` |
| `ROUTER_CLASSIFY_STRUCTURED_MODE` | `router_classify.py` | Preferred structured-response rung used by `auto`; supported values are `json_schema` and `json_object`, with rejection remembered per process | `json_schema` |
| `ROUTER_CLASSIFY_THINKING` | `router_classify.py` | `off` (default) and `false`/`0`/`no`/`disabled` try reasoning-off parameters; `on`/`auto`/`default`/`true`/`1` sends no override and uses model defaults | `off` |
| `ROUTER_CLASSIFY_THINKING_MODE` | `router_classify.py` | Forces the reasoning-off rung: `none` sends `reasoning_effort=none`; `disabled` sends `thinking.type=disabled`; any other value uses `none` | unset (default ladder prefers `none`) |

With the default `off` setting, the classifier tries `reasoning_effort=none`
first, then `thinking.type=disabled` if rejected, then no override. The selected
structured/thinking rung is recorded in classifier call metadata when available.

---

## Proxy

### Proxy endpoints

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_PROXY_URL` | `proxy_e2e.py`, `proxy_acceptance.py` | Base URL for the classified proxy (Path B) | `http://127.0.0.1:9391` |
| `ROUTER_API_URL` | `proxy_e2e.py` | Base URL for the router API server | `http://127.0.0.1:9092` |
| `ROUTER_PROXY_KEY` | `drivers/deepseek_harness.py`, `drivers/hermes.py`, `drivers/openclaw.py` | Name of the environment variable holding the proxy API key | — (drivers reference this as the key env name) |

### Proxy timeouts

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_PROXY_HOP_TIMEOUT_S` | `router_server.py` | Timeout in seconds for each individual hop in the proxy fallback ladder | `180` |
| `ROUTER_PROXY_HOP_WALL_S` | `router_server.py` | Wall-clock timeout in seconds for the entire proxy request (all hops combined) | `3600` (1 hour) |
| `ROUTER_PROXY_IDLE_TIMEOUT_S` | `router_server.py` | Idle timeout in seconds for proxy connections | `1800` (30 minutes) |
| `ROUTER_PROXY_STREAM_HOPS` | `router_server.py` | Comma-separated list of hop indices that should stream responses (empty = none) | — (empty) |

### Proxy concurrency

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_PROXY_MAX_INFLIGHT` | `router_server.py` | Maximum number of concurrently executing proxy requests | `8` |
| `ROUTER_PROXY_QUEUE_MAX` | `router_server.py` | Maximum number of requests allowed to wait in the queue | `32` |
| `ROUTER_PROXY_QUEUE_WAIT_S` | `router_server.py` | Maximum time in seconds a request may wait in the queue before refusal | `20` |

### Proxy stats

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_PROXY_STATS_TTL_S` | `router_proxy_stats.py` | Time-to-live in seconds for proxy statistics cache | `60.0` |
| `ROUTER_STATS_WINDOWS` | `router_proxy_stats.py` | Comma-separated list of time windows (in seconds) for proxy stats rollup | — (empty, uses built-in defaults) |

---

## Spawn and sorting

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_SPAWN_QUIET` | `router_spawn.py` | Suppress stderr telemetry output when set to a truthy value (equivalent to `--quiet` flag) | — (disabled) |
| `ROUTER_MISS_VERBOSE` | `router_spawn.py` | Enable verbose telemetry for classification misses when set (overrides `ROUTER_SPAWN_QUIET`) | — (disabled) |
| `ROUTER_RETIRE_WARN_DAYS` | `router_spawn.py` | Warn when a provider/model has not been seen in the registry for this many days | `14` |
| `ROUTER_SORT_MIN_COVERAGE` | `router_spawn.py` | Minimum fraction of measured prices required before sorting by effective price (prevents sorting on sparse data) | `0.5` |
| `ROUTER_SORT_MIN_SAMPLES` | `router_spawn.py` | Minimum number of price samples required before sorting by effective price | `3` |
| `ROUTER_SORT_BLEND_CEIL` | `router_spawn.py` | TR-174 blend ceiling: lanes with fewer samples than this rank on the blended value (shrunk toward list price) instead of the raw mean | `9` |
| `ROUTER_SORT_BLEND_WEIGHT` | `router_spawn.py` | TR-174 blend weight W: the list-price pseudo-count in `(n*measured + W*list)/(n+W)` (0 restores raw means) | `5` |
| `ROUTER_SORT_COMPLETION_FLOOR` | `router_spawn.py` | TR-174 floor for the completion divisor: measured cost is divided by `max(success_rate, floor)` so cheap-but-failing lanes lose value | `0.1` |
| `ROUTER_SORT_EXPLORE_SHARE` | `router_spawn.py` | TR-174 exploration share of resolves that probe the stalest low-traffic lane (deterministic crc32 gate on the task key; `0` disables — the fleet default) | `0.0` |
| `ROUTER_SORT_EXPLORE_MIN_AGE_H` | `router_spawn.py` | TR-174 aging window: a lane whose last outcome is older than this may be selected for exploration | `24` |

---

## Health and verification

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_HEALTH_URL` | `router_health_probe.py` | Base URL for the health probe canary | `http://127.0.0.1:9092` |
| `ROUTER_VERIFY_GATEWAY` | `verify_proxy_deployment.py` | Base URL for the gateway to verify against | `http://127.0.0.1:8642` |
| `ROUTER_VERIFY_PROXY` | `verify_proxy_deployment.py` | Base URL for the proxy to verify against | `http://127.0.0.1:9391` |
| `ROUTER_VERIFY_KEY` | `verify_proxy_deployment.py` | API key for verification requests (overrides `API_SERVER_KEY` if set) | — |

---

## Hermes integration

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_HERMES_CONFIG` | `router_probefix.py` | Path to the Hermes configuration file (`config.yaml`) | `~/.hermes/config.yaml` |
| `ROUTER_HERMES_STATE_DB` | `router_server.py` | Path to the Hermes `state.db` SQLite database (for session tracking) | — (disabled if unset) |
| `ROUTER_HERMES_IDLE_TIMEOUT_S` | `router_server.py` | Idle timeout in seconds for Hermes session tracking | — (disabled if unset) |

---

## Chain execution

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_PROVIDER` | `router_chain_run.py`, `drivers/pi.py` | Provider name for the current chain hop (set by chain runner) | — |
| `ROUTER_MODEL` | `router_chain_run.py` | Model ID for the current chain hop (set by chain runner) | — |
| `ROUTER_KEY_ENV` | `router_chain_run.py` | Name of the environment variable holding the API key for the current hop | — |
| `ROUTER_HOP` | `router_chain_run.py` | Hop index (1-based) in the fallback chain | — |

---

## Ingress (TR-236)

The bus→router door: `scripts/router_ingress.py`. Contract and evidence in
[docs/tr236-ingress.md](tr236-ingress.md).

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_INGRESS_ENDPOINTS` | `router_ingress.py` | Path to the endpoint registry (JSONL; declarations are data) | `data/endpoints.jsonl` (repo-relative) |
| `ROUTER_INGRESS_LEDGER` | `router_ingress.py` | Path to the ingress ledger (one row per ingress-forward attempt) | `$ROUTER_STATE_DIR/ingress-ledger.jsonl` |
| `ROUTER_INGRESS_LANE_MAX_INFLIGHT` | `router_ingress.py` | Concurrent forwards allowed **per lane** (one lane's budget, not a shared pool) | `2` |
| `ROUTER_INGRESS_LANE_QUEUE_MAX` | `router_ingress.py` | How many may wait **per lane** before `lane-busy` (the per-lane amplification bound) | `8` |
| `ROUTER_INGRESS_LANE_QUEUE_WAIT_S` | `router_ingress.py` | How long a per-lane waiter may wait for its lane's slot | `20` |
| `ROUTER_INGRESS_CIRCUIT_FAILURES` | `router_ingress.py` | Consecutive lane failures before that lane's circuit opens (`endpoint-circuit-open`) | `3` |
| `ROUTER_INGRESS_CIRCUIT_OPEN_S` | `router_ingress.py` | Cooldown before a half-open probe; a failed probe doubles it (cap 1 h) | `30` |
| `ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT` | `router_ingress.py` | Optional global backstop across all lanes; `0` = off (never the first bound) | `0` |
| `ROUTER_INGRESS_MAX_INFLIGHT` | `router_ingress.py` | Legacy **shared** pool (`Admission` class) only: concurrent forwards | `4` |
| `ROUTER_INGRESS_QUEUE_MAX` | `router_ingress.py` | Legacy shared pool: how many may wait for a slot before the refusal | `16` |
| `ROUTER_INGRESS_QUEUE_WAIT_S` | `router_ingress.py` | Legacy shared pool: how long a waiter may wait for a slot | `20` |
| `ROUTER_INGRESS_TOKEN` | `router_ingress.py serve` | Bearer token required by the push door (alternative to `--token-file`) | — (refuses to serve unauthenticated without it) |
| `ROUTER_INGRESS_BUS_IDS` | `router_ingress.py poll/serve` | Comma-separated bus inbox identities this ingress drains / replies as | `task-router` |
| `ROUTER_INGRESS_BUS_URL` | `router_ingress.py` | Bus base URL (`CRIER_URL` is read as a fallback) | `http://100.97.236.14:8767` |
| `ROUTER_INGRESS_BUS_TOKEN_FILE` | `router_ingress.py` | File holding the bus bearer token (`CR_AUTH_TOKEN` wins when set) | `~/.hermes/secrets/crier-fleet.token` |
| `ROUTER_INGRESS_KEY_DIR` | `router_ingress.py` | Directory holding `<identity>.key` PKCS#8 ed25519 keys | `~/crier-fleet/keys` |
| `ROUTER_INGRESS_RESOLVE` | `router_ingress.py` | The model resolver the ingress asks for the provider+model pair | `scripts/router_spawn.py` |
| `ROUTER_INGRESS_RESOLVE_TIMEOUT_S` | `router_ingress.py` | Subprocess bound for one resolver call | `30.0` |
| `ROUTER_INGRESS_MAX_TOKENS` | `router_ingress.py` | `max_tokens` placed in an anthropic-messages request body | `4096` |

---

## Miscellaneous

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_WEB_URL` | `proxy_acceptance.py` | Base URL for the router web UI | `http://127.0.0.1:9093` |
| `ROUTER_SPAWN_SORT` | `router_spawn.py` | Default sort key for spawn results (can be overridden with `--sort`) | `price` |
| `ROUTER_MODEL_ROUTER_DIR` | `router_health.py` | Directory containing the model router configuration | `~/.hermes/model-router` |

---

## Internal constants (not env vars)

These `ROUTER_*` module-level constants in `scripts/router_server.py` are part of the timeout ladder but are not environment-tunable; they exist so the ladder math names its parts (TR-241).

| Constant | Value | Role |
|---|---|---|
| `ROUTER_CALLER_PATIENCE_S` | `1800.0` | The caller's per-turn tolerance (scheduler's 30m default); every inner wall budget must stay strictly below it |
| `ROUTER_HOP_LADDER_MARGIN_S` | `60.0` | Headroom below the caller patience that a clamped buffered hop budget must keep (not tunable) |

---

## See also

- [README.md § Configuration](../README.md#configuration) — core configuration variables
- [docs/health-plane.md](health-plane.md) — health canary contract
- [docs/integration.md](integration.md) — scheduler integration
- [SECURITY.md](../SECURITY.md) — credential handling policy
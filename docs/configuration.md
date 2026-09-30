# Configuration Reference

Complete reference for all `ROUTER_*` environment variables used by task-router.

This document supplements the Configuration table in [README.md](../README.md#configuration) with the remaining 39 keys used in code but not documented there.

## Table of Contents

- [Core paths and state](#core-paths-and-state)
- [Classifier](#classifier)
  - [Primary classifier](#primary-classifier)
  - [Classifier fallback](#classifier-fallback)
  - [Classifier cache](#classifier-cache)
- [Proxy](#proxy)
  - [Proxy endpoints](#proxy-endpoints)
  - [Proxy timeouts](#proxy-timeouts)
  - [Proxy concurrency](#proxy-concurrency)
  - [Proxy stats](#proxy-stats)
- [Spawn and sorting](#spawn-and-sorting)
- [Health and verification](#health-and-verification)
- [Hermes integration](#hermes-integration)
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

## Miscellaneous

| Variable | Used by | Effect | Default |
|---|---|---|---|
| `ROUTER_WEB_URL` | `proxy_acceptance.py` | Base URL for the router web UI | `http://127.0.0.1:9093` |

---

## See also

- [README.md § Configuration](../README.md#configuration) — core configuration variables
- [docs/health-plane.md](health-plane.md) — health canary contract
- [docs/integration.md](integration.md) — scheduler integration
- [SECURITY.md](../SECURITY.md) — credential handling policy

</content>
</invoke>
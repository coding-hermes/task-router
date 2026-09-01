# task-router

A deterministic model router for AI fleets: task profiles → eligible provider/model pairs → price-sorted fallback chains → runtime gates.

`task-router` makes model selection inspectable rather than incidental. A task declares signed capability requirements by category. The router admits only pairs that clear **every** requirement, sorts the eligible pairs by effective price, then filters that stored chain with current quota, health, circuit-breaker, diversity, and (when wired) concurrency state. The caller receives the first open pair and the full explanation of exclusions.

The runtime contract is **fail-open**: `router_spawn.py` always exits `0`. If resolution fails, it writes a JSON error so the consuming scheduler or application can use its own fallback instead of being blocked by routing infrastructure.

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Metrics](#metrics)
- [Data and namespaces](#data-and-namespaces)
- [Planned public interfaces](#planned-public-interfaces)
- [Contributing and security](#contributing-and-security)

## Quick start

The current source checkout works with Python 3. The resolver can read the committed tables directly; the seed command additionally requires DuckDB.

```bash
git clone https://github.com/coding-hermes/task-router.git
cd task-router

# Resolve a configured project to its gated, price-ordered chain.
python3 scripts/router_spawn.py <project> --format json

# Rebuild local registry.json from committed tables (DuckDB required).
~/.hermes/venvs/board/bin/python3 scripts/router_seed.py

# Run the repository tests.
~/.hermes/venvs/board/bin/python3 -m pytest -q tests/
```

Useful read-only checks:

```bash
python3 scripts/router_spawn.py --list-profiles
python3 scripts/router_spawn.py --profile-req 'reasoning=5 debug=3 vision=-2' --format json
python3 scripts/router_circuit.py status --json
python3 scripts/router_metrics.py --top-pairs 10 --since 24h --json
```

### Current source interface

There is not yet a released package or umbrella `router` executable. Use the repo-relative Python scripts listed below. This keeps the command surface honest while the installable interface is being prepared.

### Planned installable CLI — not shipped yet

The planned landing interface is intentionally documented here before release:

```bash
pip install -e .
router resolve <project> --format json
router circuit status --json
router metrics --top-pairs 10 --since 24h
```

The planned `router` command will expose one subcommand family per tool in [CLI reference](#cli-reference). It is **not available in the current checkout**; use `python3 scripts/<tool>.py` until packaging lands.

The planned data-home resolution is:

1. `TASK_ROUTER_HOME`, when explicitly set;
2. `$XDG_DATA_HOME/task-router`; otherwise
3. `~/.local/share/task-router`.

At that landing point, `registry.json`, circuit state, ledger data, and health state will resolve under the data home. Today, individual scripts retain their documented repository-relative or legacy state defaults; `TASK_ROUTER_HOME` currently controls metrics storage only. This distinction is deliberate so documentation does not imply a finished migration.

## Architecture

```text
configured project or ad-hoc requirements
                 |
                 v
     task profile (signed levels by category)
                 |
                 v
registry.json / committed JSONL tables
  eligibility: every category requirement clears
                 |
                 v
eligible pairs sorted by plan tier + effective price
                 |
                 v
runtime gates: quota | health | circuit | diversity | ledger
                 |
                 v
head pair + surviving chain + explicit exclusions
                 |
                 v
caller spawns, records outcome, and may advance to next hop
```

### Deterministic eligibility and ordering

A profile is a set of requirements such as `reasoning=5 debug=3 vision=-2`. A provider/model pair is eligible only if its signed tier is at least the required level for **all** categories. Capability in excess of a requirement is allowed; a shortfall in one category excludes the pair.

The resulting chain is sorted by plan tier and effective price. This chain is a durable decision artifact: health, quota, circuit, and concurrency signals do not rewrite its price order. They are applied later at resolve time and reported as exclusions, which keeps policy and transient operations auditable.

### Registry, seed pipeline, and state

- `data/tables/*.jsonl` is committed, generated text data used to rebuild the registry. Do not hand-edit it.
- `registry.json` is a gitignored local text database built by `scripts/router_seed.py`.
- The seed pipeline reads committed data, computes the category level layer, model tiers, and profile requirements, and writes the local registry. It can mirror generated data to an optional namespace when configured.
- Circuit breakers store per-pair failure history with exponential cooldowns. Open pairs are skipped; cooling history remains inspectable.
- The optional session ledger records `start` and `end` events. When a caller wires it around spawns, it supplies per-model in-flight counts for concurrency gates. Until then, the resolver reports that ledger gating is not wired.

### Fail-open contract

The router must not become a single point of failure. `router_spawn.py` serializes failures as `{"error": ...}` and exits successfully. Consumers must treat a missing head or error payload as a signal to use their independently configured fallback, while logging the routing condition for diagnosis.

## CLI reference

Run every command with `--help` for complete argument details. `router_*` names below are the current script names; the proposed `router` subcommands are planned only.

| Current command | Purpose | Key flags / subcommands |
|---|---|---|
| `python3 scripts/router_spawn.py` | Resolve a project or ad-hoc capability profile into a gated fallback chain. | `<project>`, `--profile`, `--profile-req`, `--list-profiles`, `--explain`, `--format`, `--no-health` |
| `python3 scripts/router_seed.py` | Rebuild `registry.json` and derived registry data from committed tables. | no runtime flags; `--help` is safe |
| `python3 scripts/router_circuit.py` | Inspect and update circuit-breaker state for provider/model pairs. | `record-failure`, `record-success`, `status`, `clear`; `--json`, `--all` |
| `python3 scripts/router_ledger.py` | Record routed spawn lifecycle events and inspect in-flight counts. | `start`, `end`, `status`; `--provider`, `--json` |
| `python3 scripts/provider_health_probe.py` | Probe configured providers and write health results. | `--config`, `--providers`, `--output` |
| `python3 scripts/router_maintain.py` | Coordinate registry repricing, seeding, export, snapshots, and commits. | `reprice`, `seed`, `export`, `snapshot`, `commit`, `all`; `--dry-run` |
| `python3 scripts/router_pricing.py` | Report or apply normalized pricing calculations. | `--dry-run`, `--json` |
| `python3 scripts/router_modelsdev.py` | Fetch or sync catalog data from models.dev into the text registry. | `fetch`, `sync`; `--dry-run`, `--seed`, `--commit` |
| `python3 scripts/router_probefix.py` | Find and repair provider model-ID gaps from health-probe logs. | `--runs`, `--dry-run`, `--sync-providers` |
| `python3 scripts/router_gaps.py` | Report registry data-quality gaps. | `--json`, `--lacking`, `--models`, `--top` |
| `python3 scripts/router_clinepass.py` | Synchronize the clinepass catalog and billing data. | `sync`; `--dry-run`, `--commit`, `--push` |
| `python3 scripts/router_plan_sweep.py` | Report or disable lanes outside configured flat plans. | `--apply`, `--json` |
| `python3 scripts/router_learn.py` | Inspect or add learning-loop lessons and provider doctrine. | `dump`, `lesson`, `doctrine`, `provider` |
| `python3 scripts/router_metrics.py` | Query append-only per-hop resolve metrics. | `--top-providers`, `--top-models`, `--top-pairs`, `--profile`, `--since`, `--json` |

Commands that can commit, push, write state, change provider configuration, or apply a plan sweep are operational tools. Review `--help` and use `--dry-run` where available before using them in automation.

## Configuration

The current scripts are designed to work from a fresh clone without environment configuration. The following variables override paths or integrations where noted.

| Variable | Used by | Effect |
|---|---|---|
| `ROUTING_REGISTRY` | seed, spawn, maintain | Path to the local `registry.json` text database. |
| `ROUTING_DATA_DIR` | seed, spawn, pricing, models.dev, clinepass, gaps, maintain | Directory containing committed/generated JSONL registry tables. |
| `ROUTING_NS` | seed, maintain | Optional namespace mirror used for seed exports and maintenance. |
| `ROUTING_DOCS_DIR` | maintain | Output directory for generated chain snapshots. |
| `ROUTING_BOARD_PY` | maintain | Python interpreter used by the maintenance seed subprocess. |
| `ROUTING_SPOT_CHECK` | maintain | Command path for the optional pricing spot-check helper. |
| `ROUTER_STATE_DIR` | spawn, circuit | Directory containing quota, health, circuit, and resolver ledger state. |
| `LEDGER_FILE` | ledger | Exact ledger JSONL path; overrides the ledger tool default. |
| `MODELSDEV_CACHE` | models.dev, probefix | Cache path for models.dev catalog input. |
| `TASKROUTER_NS` | maintain | Optional namespace mirror target for exported task-router data. |
| `TASK_ROUTER_HOME` | metrics today; broader data home planned | Current metrics file home; planned shared data home described above. |

Provider credentials are not configuration for this repository’s committed data. Supply them through a local environment or secret manager only; never add credentials to a file, issue, example, test fixture, or command transcript. See [SECURITY.md](SECURITY.md).

## Metrics

`router_spawn.py` emits one append-only metrics row per resolved or excluded chain hop. Each row records the provider, model, pair, chain order, effective price, result (`resolved`, `excluded`, or `error`), exclusion reason when applicable, and a configuration snapshot. This lets operators answer “what was considered?” rather than only “what was selected?”.

Query the data with `router_metrics.py`:

```bash
python3 scripts/router_metrics.py --top-providers 10 --since 7d
python3 scripts/router_metrics.py --top-models 10 --profile P1_CODING --json
python3 scripts/router_metrics.py --top-pairs 20 --since 24h
```

With `TASK_ROUTER_HOME` set, metrics live at `$TASK_ROUTER_HOME/metrics.jsonl`; otherwise they are read from `data/metrics.jsonl` in the checkout. Treat real metrics as operational data: they can reveal project identifiers, routing decisions, and deployment behavior.

## Data and namespaces

### Table anatomy

`data/tables/` holds the committed JSONL catalog and generated routing layer:

- Core catalog: `providers`, `models`, `benchmarks`, `archetypes`, and `projects`.
- Capability derivation: `level_defs`, `model_perf`, `category_levels`, and `model_tier`.
- Profile layer: `task_profiles` and `task_profile_requirements`.
- Provider operations: `provider_rules`, `probe_providers`, `probe_fixes`, `probe_excludes`, `probe_gaps`, `fallback_lanes`, and `plan_terms`.
- Catalog enrichment: `model_aliases`, `model_notes`, `model_catalog`, `temporary_discounts`, and `quality_estimates`.

The JSONL table set is generated and must be changed through the supported data/seed workflow rather than manually. A normal local rebuild is:

```bash
~/.hermes/venvs/board/bin/python3 scripts/router_seed.py
python3 scripts/router_spawn.py <project> --format json
```

Optional namespace mirroring is configured with environment variables and is not required for a fresh clone. Keep any deployment-specific namespace location outside committed documentation and source data.

### Existing design notes

- [Scheduler integration contract](docs/integration.md)
- [Registry maintenance and rebuild flow](docs/registry-maintenance.md)
- [Category data-quality notes](docs/category-data-quality.md)
- [Namespace and data anatomy](docs/duckbrain-namespace.md)
- [Historical chain snapshots](docs/chains-2026-08-31.md)
- [Model-selection research notes](docs/model-selection-cross-pollination.md)

## Planned public interfaces

The following interfaces are planned; they are not promises of a stable API in the current checkout.

### Local web UI — planned

A local web UI will provide settings inspection and a resolve preview: choose a project or ad-hoc profile, inspect eligible pairs, see price order, and understand gate exclusions without modifying runtime state by default.

### OpenAPI server — planned

A local OpenAPI server will expose a read-only mode for profile, chain, metrics, and explanation queries. Edit mode will be explicit and API-key-gated; it will be disabled by default and will record every mutation.

### OpenAPI-to-MCP bridge — planned

An MCP bridge derived from the OpenAPI description will let agents inspect and update approved catalog listings through controlled tools instead of ad-hoc file edits. Write tools will require the same edit-mode authorization as the API.

### Catalog and profile extensions — planned

- Import models.dev data for enabled providers, retaining provenance and reviewable diffs.
- Support provider/model mapping rules for prefix changes, pattern matching, and string-replace renames.
- Support Docker-style versioned, tagged profiles so callers can pin an immutable profile version or intentionally follow a tag.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, data discipline, tests, and commit expectations. See [SECURITY.md](SECURITY.md) for private vulnerability reporting and the repository’s key-handling policy.

## License

MIT. See [LICENSE](LICENSE).

# Contributing to task-router

Thank you for helping make model routing inspectable, deterministic, and safe to operate.

## Development setup

Use a virtual environment for development. The router's runtime lookup is standard-library Python, while the seed pipeline and tests use the board environment in the fleet setup.

```bash
git clone https://github.com/coding-hermes/task-router.git
cd task-router
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip pytest duckdb
python -m pytest -q tests/
./scripts/gitreins-guard-tests.sh
```

The project test command used by fleet automation is:

```bash
~/.hermes/venvs/board/bin/python3 -m pytest -q tests/
```

Run the relevant CLI help and a safe read-only command before documenting or changing a tool. For example:

```bash
python3 scripts/router_spawn.py --help
python3 scripts/router_spawn.py --list-profiles
python3 scripts/router_circuit.py status --json
```

## Contribution rules

### Data before code

Provider facts, model facts, aliases, plan terms, probe configuration, and profiles belong in the committed JSONL data layer—not in hard-coded Python constants. Keep the routing algorithm generic; put catalog knowledge in `data/tables/` through the supported data pipeline.

### JSONL tables are generated artifacts

The JSONL tables are versioned so a clone can rebuild the runtime registry, but they are generated materialized data. Do not hand-edit table rows. Update the authoritative data workflow, then rebuild with `scripts/router_seed.py` and review the resulting JSONL and `registry.json` behavior. The generated `registry.json` is a local runtime artifact and is not committed.

### Preserve router contracts

- `router_spawn.py` is fail-open: errors are returned as JSON and exit successfully so a caller can use its own fallback.
- Keep chain eligibility deterministic: models must satisfy every signed profile requirement before price ordering.
- Runtime gates apply at resolve time. Do not bake transient quota, health, circuit, or concurrency state into the stored price-ordered chain.
- Keep public documentation free of credentials, personal paths, private hosts, and internal operational identifiers.

## Scope discipline

Keep pull requests narrow. Do not combine catalog repricing, algorithm changes, generated-data refreshes, documentation cleanup, and unrelated formatting in one change. Avoid changing scheduler integrations or external deployment configuration from this repository.

Before opening a pull request, inspect the diff and confirm it only includes intentional files:

```bash
git diff --check
git status --short
```

## Commit and pull-request style

Use Conventional Commits for commit subjects, for example:

```text
feat: add provider alias normalization
fix: preserve price order after gate exclusions
docs: clarify fail-open resolution
```

Explain the user-visible behavior, data migration/rebuild steps, and verification performed in the pull request. Include tests for behavior changes. Generated data should be accompanied by the pipeline or source-data change that produced it.

## Reporting security issues

Do not open public issues for suspected vulnerabilities or credential exposure. Follow [SECURITY.md](SECURITY.md).

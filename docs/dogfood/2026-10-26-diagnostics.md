# Task-router dogfood diagnostics — 2026-10-26

Target identity: task-router f75e1bace20cdffb46ace69b5246fcf2415fc808.

Use an isolated HOME plus TASK_ROUTER_HOME and ROUTER_STATE_DIR for scratch runs. Install with a fresh venv and `uv pip install -e .`, then install duckdb before `router seed`. The seed command uses the committed `data/tables` inputs and writes the isolated registry. Verify `source=registry.json`, `fallback_used=false`, and `router validate` rc=0 before trusting a chain.

The real CLI entry point is `router`, not `toolsd`. The generic bunker QA battery currently checks for `toolsd` after installation, so its fresh-install result can say rc=0 but no artifact even when the package installed `task-router` successfully. A future QA fix should recognize the package's declared console script or run the README's smoke command directly.

The router's fail-open behavior is observable: an unknown project returns JSON error output with exit code 0. The API server can be isolated with `router server --mode read-only --host 127.0.0.1 --port <scratch-port>`. Health reports the serving commit; compare it with `git rev-parse HEAD` because the shared :9092 instance may be stale.

The bunker QA evidence for this run is `/tmp/task-router-bunker-20261026.jsonl`. Agent 2bd9809a was destroyed and no matching agent remained in `bunker list --server bunker-mvp` after collection. The native battery also reproduced the already-open QA-TASKROUTER-6 zero-tags refusal and the act leg lacked its requested Docker image; those are not new product findings from this run.

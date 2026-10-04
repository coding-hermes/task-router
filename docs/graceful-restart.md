# Graceful router restart

## Why this is a restart, not Python module reload

The router serves concurrent requests in `ThreadingHTTPServer` handler threads.
Reloading modules in-place can leave those threads holding old functions/classes
while global module state has already been replaced. The router therefore keeps
its imported code immutable for the life of a process and uses a bounded,
observable restart instead.

## What SIGTERM does

1. `/readyz` flips from HTTP 200 to HTTP 503 immediately; `/health` reports
   `runtime.draining=true` and the active handler count.
2. The server stops accepting connections. A connection that races the drain
   is closed without starting a new handler.
3. Existing handlers finish. The process waits up to
   `ROUTER_DRAIN_TIMEOUT_S` (default 60 seconds, clamped to 80) and logs one
   JSON `drain_finished` event with `drained` and any remaining in-flight count.
4. If a request outlives the deadline, the process exits rather than hanging
   systemd forever. Handler threads are daemonized; unfinished calls are
   deliberately cut off and the caller's existing retry/fallback logic applies.

The budget is intentionally below systemd's usual stop deadline. If the unit's
`TimeoutStopSec` is changed, keep it greater than the configured drain budget.

## Deploy and verify

Dry-run first; it compiles every script, fingerprints the runtime source, and
prints the ordered services. It does not call systemd:

```bash
python3 scripts/router_deploy.py
```

The deploy command refuses a dirty `scripts/` tree unless explicitly allowed.
After committing the change, execute:

```bash
python3 scripts/router_deploy.py --apply
```

It restarts `task-router-server` (`:9092`) and then `task-router-proxy`
(`:9391`) sequentially. Before touching the next service it requires all three
proofs from the newly answering process:

- PID changed from the pre-restart process;
- `/readyz` returns HTTP 200;
- `/health.runtime.source_digest` matches the Python source digest the deploy
  command computed before the restart.

A failed readiness or digest check stops the sequence and reports a partial
rollout; it does not silently restart the other service or claim success. The
scheduler and its gateway URL are not touched.

For an intentional uncommitted deploy, opt in loudly:

```bash
python3 scripts/router_deploy.py --allow-dirty --apply
```

To restart only one service, repeat `--service` as needed. `--apply` is always
required to perform a restart. The script does not deploy a rollback copy; if
readiness fails, it reports the failure and leaves the operator in control.

## Limits

This is a bounded graceful restart, not zero downtime: during the short interval
when the service socket is closed and systemd brings the process back, callers
can see connection failures. The router's retry/fallback handles those. True
zero downtime would need a stable front listener and blue/green backend switch,
which is a separate, higher-complexity project; do not add it unless the
measured restart gap causes actual failures.

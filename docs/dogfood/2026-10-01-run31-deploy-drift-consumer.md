# 2026-10-01 — deployment-drift + live-consumer angle (run 31)

## Angle

Runs 1-6 covered CLI/graph, MCP/web/seed, the TR-067 proxy ladder, and
versioned profiles + provider-mapping rename rules. This run took the two
surfaces nobody had touched: (1) deployment drift between the repo and the
live runtime the fleet actually executes every tick, and (2) the scheduler's
real consumer path (`router_spawn.py` et al. via the symlinks/copy-list of
`scripts/sync_runtime.sh`).

## What a real user session looked like

- `python3 scripts/router_spawn.py task-router --format json` — 0.073s warm,
  exit 0, real 2-hop chain (head xkiro-2/qwen3.7-plus:free, context 1M),
  lifecycle counts live=1530/retired=347. The documented bare-profile form
  also resolves.
- `router status` — registry.json fresh (generated 2026-09-30, 1877 rows,
  fallback_used=false).
- `router validate` — 14/14 checks pass, exit 0.
- `router_circuit.py status` — 2 open pairs, both human/policy-gated.
- Live API on :9092 `/health` — answered, but `code.stale=true` (see TR-255).

## Install leg (ephemeral bunker, bunker-las-03, agent 95500c74)

Direct control-host ssh to all bunker aliases is rejected (publickey), but
the bunker CLI control plane works, so the leg ran through CLI-managed spawn.
Debian / Python 3.13.5, no toolchains. README quickstart followed top-down:

| step | seconds |
|---|---|
| git clone --depth 1 (public GitHub, no credential) | 5 |
| python3 -m venv + pip install -e . | 5 |
| pip install duckdb | 0 (cached) |
| router seed (4179 KB registry, per-user data home) | 19 |
| first real spawn (P1_CODING, real chain, exit 0) | 0.1 |

`router validate` = 14/14 green at clone HEAD 72206c5. Agent destroyed and
verified gone. Total clone → first resolve ≈ 30s — the promise "stdlib-only
install, seed, resolve" holds on a fresh machine with zero deviations.

## Findings (filed as rows)

1. TR-255 (P1): serving router_server.py is 23 commits behind HEAD; the
   TR-194 degraded-path fix and the TR-235 loud registry-state verdict are
   on disk but not in the process. /health says stale:true honestly — but
   nothing consumes that verdict, so the drift persists silently.
2. TR-256 (P1): two of the six sync_runtime.sh copy-list files drifted on
   the LIVE side (fleet-cooldown-policy.py even re-introduced the unbounded
   pagination loop TR-205 fixed in the repo). The policy script's own hash
   guard compares live-vs-sidecar, never live-vs-repo.
3. TR-257 (P2, install leg record): control-host ssh key rejected on all
   bunker aliases; CLI path worked; full timing table above.

## Perf verdict

Headline operation (spawn resolve): 0.073s warm control host, 0.10-0.11s
cold on the fresh bunker box, 76-91ms in the 09-25 run. Nothing a user
would feel; no PERF row warranted. Install 30s to first resolve is fine.

## What was NOT done

- No repo code touched. No visibility/permission changes. Router state
  files untouched (read-only commands only). Foreman not woken (fleet 6h
  cooldown law; board already carries live audit rows).

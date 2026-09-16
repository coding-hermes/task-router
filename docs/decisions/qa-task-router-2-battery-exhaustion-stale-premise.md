# QA-TASK-ROUTER-2 — bunker-las-02 battery exhaustion: stale-premise verdict (2026-09-16)

## Row claim (filed 2026-09-15, post QA-TASK-ROUTER-1 pool fix)

7 fresh `run_battery` FAILs on bunker-las-02 after the port-pool resize (1→10 ranges,
09-12 20:09): `capacity full: 8/8 agents` (2 runs) and `port range allocation: no free
port ranges available (pool exhausted: 10 ranges)` (5 runs, 03:42–04:07Z). Probe at
filing time: 8 agents live — kara-lair (permanent) + 7 ephemeral QA agents, six created
08:14–09:18 PDT, idle at 1% disk. Claimed gap: no spawn-time reaping of stale QA agents
and no retry-elsewhere fallback, so the battery reproduces the same FAIL every cycle.

## Live evidence this tick (2026-09-16 ~00:30Z, probed by the foreman)

| Check | Command | Result |
|---|---|---|
| Pool occupancy | `bunker status --server bunker-las-02` | `Agents: 2/8`, host ONLINE (uptime 2d12h) |
| Agent inventory | `bunker list --server bunker-las-02 --status all` | kara-lair (permanent, 08-23) + `16c1feb7` (ephemeral QA agent, created 09-15 17:21 -07 — an in-flight battery), **nothing else** |
| The 7 stale ephemerals | same | GONE — TTL (4h default, `BUNKER_QA_TTL`) reclaimed them without any reaper |
| Preflight fix deployed | `/home/kara/.hermes/scripts/bunker-qa.sh` | Carries `QA-OFF-BY-ONE-9, 2026-09-15` capacity preflight: config → ssh → capacity gates run BEFORE any spawn; full pool ⇒ ONE actionable FAIL row in evidence + rc=2, no 3x retry ladder; `BUNKER_QA_SKIP_PREFLIGHT=1` documented bypass |
| Parser correctness | `parse_bunker_capacity` extracted and run against the LIVE `status` output | Returns `2 8` — correct used/max pair from the real host |
| Post-fix battery health | evidence files `/tmp/bunker-qa-evidence-*.jsonl` | 09-15 22:22Z run: toolchain-bootstrap OK, fresh-install OK, ci-pass OK (act run details visible); 09-16 00:23Z launch OK (`agent=16c1feb7 ttl=4h`); a SIGTERM-killed launch (00:01Z) still wrote its `write_fail_if_empty` FAIL row — the no-empty-evidence guard works |
| Exhaustion rows anywhere recent | grep evidence corpus | Only in pre-fix files (1788462xxx ≈ 09-14/15); zero post-fix |

## Verdict: STALE PREMISE — close, no further code change

Both halves of the row's premise no longer hold:

1. **The exhaustion self-healed.** The 7 accumulated ephemerals were TTL-reclaimed; the
   pool sits at 2/8 and a battery is running right now (launched 00:23Z, agent healthy).
2. **The deterministic-failure gap is already fixed.** The 09-15 capacity preflight
   (QA-OFF-BY-ONE-9) converts a full pool into ONE actionable FAIL row with no spawn
   attempt — exactly the "turns one actionable finding into a retry ladder" defect the
   row identified. Parser verified against the live host this tick.

The failure-mode transition the row described (1-range leak → capacity/pool exhaustion)
was real on 09-15; the fix that landed hours later addresses that class, and the TTL
mechanism addressed the accumulation.

## Residual gaps (honest record — not failures, watched)

- **No spawn-time reaping** still true, but TTL reaping proved sufficient (4h TTL vs
  QA cadence of hours). Revisit only if battery cadence ever approaches the TTL.
- **No retry-elsewhere failover**: battery host is the single `BUNKER_QA_SERVER`
  default. Four servers exist in `~/.bunker/config.yaml` (las-02/03/04 active);
  manual reroute remains a one-env-var operation, and the preflight now makes the
  reroute obvious (FAIL row names the condition). Auto-failover stays a possible
  future enhancement, not a defect.
- QA-TASK-ROUTER-3/4 (chaos-cell venv PATH) are NOT covered by the 09-15 fix and
  remain open, correctly.

## Lifecycle

- GitReins task `QA-TASK-ROUTER-2` created + started this tick; judge verdict in
  `.gitreins/history/`. Gate per repo precedent for infra/verification rows:
  Tier 1 guard + green suite; judge run for the closure criterion.
- Board closed via `boardctl` (legacy appender retired on this board 09-15).

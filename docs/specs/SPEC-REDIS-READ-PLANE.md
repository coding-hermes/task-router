# SPEC-REDIS-READ-PLANE — a Redis read plane over the JSONL registry

Status: **v0.4 (2026-11-04) — two quorum rounds merged + infrastructure deliverables added. Implementation authority, subject to the
R52 measurement gate. v0.3 has NOT yet been re-judged (round 3 owed with implementation).**
Board row: TR-388 (P1) · Owner directive: Bane, 2026-11-04
Quorum: `docs/reviews/TR-388-quorum-2026-11-04.md` (round 1) and §15 below (round 2)
Interacts with: TR-277 (`router_data_cache.py`), TR-046 (`source` provenance), TR-288
(runtime-sync copy list), TR-005 (`router_maintain.py`), AGENTS.md fail-open rule.

Normative statements **R1…R68** (R58–R68 are the infrastructure deliverables, §11.5). A statement marked `(FIX …)` was forced by a specific quorum
finding; §14 and §15 are the receipts. Cross-references are into this document only.

---

## 1. Problem and the measured baseline

| fact | measured 2026-11-04 (coordinator, this host) |
|---|---|
| `registry.json` | 4,792 KB, **12** tables (the 12 already include `fallback_lanes`) |
| biggest tables | models 2,064 · model_perf 3,944 · model_tier 3,896 · benchmarks 2,148 · projects 950 |
| parse `registry.json`, 100 runs | mean 49.0 ms, p50 45.1, min 33.3, max 180.2 |
| cold `router_spawn.py <project> --format json` | 0.28–0.39 s wall, ~73 MB RSS |
| `data/tables/` | **31** JSONL files; 12 are seed-derived artifact tables, 19 are not |
| in-process cache | TR-277: read-once per file identity, change-driven, single-flight, no TTL (AC0–AC6) |
| chain build | `DEFAULT_CHAIN_LIMIT = 1024` lanes (`router_spawn.py:85`); filters/sorts on plan_tier, price×token_factor, context_limit, lifecycle |
| worst-case profile | P0_FORE = 17 required categories (`task_profile_requirements.jsonl`), and P0_FORE is `DEFAULT_FALLBACK_PROFILE` (`router_spawn.py:93`) |

**R1.** TR-277 already removes repeated parsing inside a process. It cannot remove the cold start:
the dominant caller is a fresh `router_spawn.py` process per tick/probe/CLI call.

**R2.** The value claim is bounded and measured, not asserted: at most the registry read+parse
(~45 ms of ~300 ms wall) plus O(1) single-fact answers. The chain build cannot be made cheaper by
per-lane reads against a table already resident in memory — R36 fixes the read shape and R37 pins
the budget that proves it.

**R3.** The value does not come from caching JSON parsing inside a long-lived process (TR-277 does
that) and must not be sold that way in docs, board events, or memory.

## 2. Boundary

**R4.** `data/tables/*.jsonl` is the only source of truth; `registry.json` is the seed's build
artifact. Redis holds a derived, disposable projection.

**R5.** No write path, quota/circuit state, outcome stats, ledger, chain snapshot, or derived drift
moves from Redis back to JSONL. `router_spawn.py` gains no write verb against the cache or the
registry (its existing `data/metrics.jsonl` append is out of scope and asserted as the only write).

**R6.** Losing Redis (deleted container, reboot, `FLUSHALL`) is a non-event: the router serves from
`registry.json`/`data/tables` and the next `load` rebuilds. No Redis key is the only copy of any fact.

**R7.** No locks, leader election, queueing, or rate limiting in Redis. One scoped exception: the
publish CAS (R20) coordinates *publishers of this cache*, never fleet scheduling.

**R8. (FIX C16, A3) Data-home scope, its identity, and its lifecycle.**

- `<scope>` = first 12 hex of `sha256(realpath(DATA_DIR) + "|" + realpath(REGISTRY))`. Both paths
  matter: `ROUTING_REGISTRY` and `ROUTING_DATA_DIR` resolve independently (`registry_path.py:89`),
  so keying on one of them lets a hermetic run publish into the production plane.
- Every load/status refreshes `<scope>:meta:heartbeat` with `EXPIRE ROUTING_REDIS_SCOPE_TTL_S`
  (default 7 d). `load` deletes the keys of scopes whose heartbeat has expired.
- `status --json` reports `scopes_live`, `dbsize`, `used_memory_human` — capacity drift is a number,
  not a later `noeviction` surprise.
- A publisher whose `realpath(DATA_DIR)` is outside the repo git toplevel (scratch dirs, tests, CI)
  refuses to publish unless `ROUTING_REDIS_ALLOW_SCRATCH=1`.
- A reader whose scope has no keys gets `EMPTY` and serves from file; it can never see another
  scope's plane.

## 3. Version identity

**R9. (FIX C4, A8) Hash with multiplicity over an explicitly enumerated table set.**

- **Cached table set = exactly these 12 tables:** `providers`, `models`, `archetypes`,
  `benchmarks`, `projects`, `level_defs`, `category_levels`, `model_perf`, `model_tier`,
  `task_profiles`, `task_profile_requirements`, `fallback_lanes`. (The seed writes exactly these
  twelve files; `fallback_lanes` is folded into `doc['tables']` as a sidecar, so "12 + sidecar = 13"
  double-counted it. v0.2's arithmetic was wrong; this is the list.)
- `not_cached` = every other `data/tables/*.jsonl` file (19 today), **computed from the directory
  listing minus the 12 names at every run** — never hand-maintained.
- Per table: drop blank lines, strip trailing whitespace, take the sorted **list** of line strings
  *preserving duplicates*, `sha256("\n".join(sorted_lines))`. A set would collapse duplicates;
  `model_notes.jsonl` already carries 3 duplicate raw lines, so a multiplicity-blind hash is a
  demonstrated miss class.
- Aggregate: `sha256("\n".join(f"{name}:{hash}:{count}" for name in sorted(tables)))`.
- Stamp fields: `{jsonl_hash, tables, row_counts, not_cached, schema, generation, created_at,
  data_home, registry_path, checkout}` where `created_at` is unix seconds and `generation` is the
  Redis generation it describes (`created_at` is what R13 reports and R30 compares — v0.2 specified
  neither field and its age check was therefore unimplementable).
- **Stamp equality compares `{tables, row_counts, schema}` only.** `not_cached` is *reported*, not
  compared — dropping an unrelated 32nd file into `data/tables` must not force a republish.

**R10.** Publish reads ONCE: `load` parses the JSONL into memory, hashes *that in-memory set*, and
publishes *that same set*. No re-read window, therefore no TOCTOU (and no A→B→A blind spot).

## 4. States

**R11. (FIX C5, A5)** `status` prints exactly one state:

| state | condition | exit |
|---|---|---|
| `MATCH` | stamp equality (R9) + pointer's generation present | 0 |
| `STALE` | stamps disagree; names the drift class (`artifact-behind`, `cache-behind`, `cache-ahead`) and the drifted tables | 1 |
| `EMPTY` | pointer absent (never loaded / flushed / restarted empty) | 2 |
| `UNREACHABLE` | connect/auth/protocol failure | 3 |
| `GHOST` | pointer present, its generation's keys gone (partial GC, manual `DEL`, eviction) | 4 |
| `FOREIGN` | reachable, `run_id` differs, keys present without our owner key | 5 |
| `SCHEMA-AHEAD` | cache `schema` > reader's schema | 6 |
| `UNSTAMPED` | `registry.json` carries no `stamp` (today's artifact); compare JSONL↔cache only and report the artifact leg degraded; `cache_verified=false` | 0 if JSONL↔cache agree, else 1 |

`cache-ahead` is undetectable while `UNSTAMPED` — stated, not hidden. After **7 consecutive days**
in `UNSTAMPED`, `status` prints one line naming the missing artifact stamp so the degraded leg
cannot persist silently forever.

**R12. (FIX F2) In-flight is a real fact, not a guess.** The publisher writes
`<scope>:meta:publishing` = `{gen, started_at, pid, host}` before its write pass and deletes it after
the CAS. `status` reports `STALE (cache-behind, in-flight)` — quiet, exit 1 only when the intent key
is older than `ROUTING_REDIS_PUBLISH_STALE_S` (default 120 s) or its pid is gone on this host.
`status` itself never mutates.

**R13.** `status --json` carries all three stamps, `state`, `drift_class`, `drifted_tables`,
per-table counts, `created_at` ages, `data_home`, `registry_path`, `checkout`, `generation`,
`orphans`, `scopes_live`, `dbsize`, `used_memory_human`.

**R14. (FIX A6) The cron contract has teeth.** Silent on `MATCH`; one actionable line otherwise
(the `router-averages-refresh.sh` shape). Additionally: an alarm line when the cache `created_at`
is older than **2 × `ROUTING_REDIS_MAX_AGE_S`** (30 min) and when `redis-health.json` has not been
refreshed for 10 minutes while `ROUTING_REDIS != 0` — a correct-but-stale cache and a dead recovery
agent are both loud.

## 5. Diff reporting

**R15. (FIX C6, F3) Diff keys are the real columns, for every cached table.**

| table | key |
|---|---|
| models | `(provider, model)` |
| providers | `id` |
| archetypes | `id` |
| benchmarks | `(model, category, source)` |
| level_defs | `level` |
| model_perf | `(model, category)` — there is no `provider` column |
| model_tier | `(model, category)` — no `provider` column |
| category_levels | `(category, level)` |
| task_profiles | `id` |
| task_profile_requirements | `(task_id, category)` |
| projects | `id` |
| fallback_lanes | `order` |

Uniqueness of every key was measured against the committed tables (0 duplicates for all twelve);
a test re-asserts it, because a key that stops being unique silently mis-reports the diff.

**R16.** The diff compares full rows (models: all 39 columns) against full rows — never a
projection against a full row.

**R17.** `status --diff` = `+added −removed ~changed` per table; `--detail N --table T` prints
field-level deltas; default `--diff` under one second.

**R18.** The diff never mutates and never decides reload — the hash decides (R9).

## 6. Publish protocol

**R19.** All data keys live under `<scope>:g<N>:…`; readers reach data only through
`<scope>:meta:version`.

**R20. (FIX C7, A1) One connection, a fenced pointer, and orphan reclamation.**

1. open **one dedicated connection** owned by the publisher for the whole publish (WATCH is
   per-connection; a pooled or second connection makes the CAS vacuous);
2. `gen = INCR <scope>:meta:generation`; write `<scope>:meta:publishing` (R12);
3. write every `<scope>:g<gen>:…` key, including `<scope>:g<gen>:__ts` (unix seconds);
4. `WATCH <scope>:meta:version` → read it → if its generation > `gen`, `UNWATCH` + `DISCARD`, delete
   own keys, exit 4 → otherwise `MULTI; SET <scope>:meta:version <stamp>; EXEC` (the pointer can
   never regress);
5. delete `<scope>:meta:publishing`;
6. reap: generations with `M ≤ pointer_generation - 2`, **and** orphans `M > pointer_generation`
   whose `__ts` is older than `ROUTING_REDIS_PUBLISH_STALE_S` (a crashed publish's leftovers), are
   deleted. Reaping runs in `load` — never in `status` — and its count is reported as `orphans`
   in `status --json` and carried in the R23 record when nonzero.

A publish whose process dies before step 4 leaves an orphan that the next `load` reaps; a quiescent
system therefore cannot accumulate full-generation key sets inside a `noeviction` instance.

**R21.** If any key of the pinned generation is missing, the reader discards the Redis answer **for
the whole resolve** and re-resolves from the file backend. A resolve never mixes generations.

**R22. (FIX A6) Idempotence, refresh, and reap in one verb.** `load --if-stale`:

1. computes the JSONL stamp; if it differs from the cache stamp → full publish (R20);
2. if it matches and `created_at` is older than `ROUTING_REDIS_MAX_AGE_S / 2` → republish the
   *identical* data under a new generation solely to refresh `created_at`, so a living timer keeps a
   correct-but-unchanged cache inside the reader's window instead of driving every reader to the
   file arm at 900 s;
3. if it matches and is fresh → reap orphans (step 6 of R20) and return 0 without publishing.

**R23.** A failed or aborted publish appends one record to
`data/state/redis-publish-errors.jsonl` (`ts`, `scope`, `gen`, `reason`, `tables`, `orphans`) and
exits non-zero (4 = aborted by CAS, 5 = publish error). The seed stays fail-open (R24) but the
failure is a record, never only a log line.

**R24.** Triggers: (1) `router_seed.py` publishes in the same run and prints the stamp; a publish
failure leaves the seed exit 0 **and** writes the R23 record. (2) `router-redis-sync.timer`
(5 min) runs `load --if-stale` — it is also the recovery agent for R28. (3) `router redis load
[--force]`; `--force` refuses if the JSONL is unreadable.

**R25.** `flush --yes` requires the owner key to match this spec's owner id and touches only
`<scope>:*`.

## 7. Read path and fail-open

**R26.** No process-lifetime TTL memo: the pointer is resolved once per process and pinned for that
process's lifetime (TR-277 AC1 forbids a TTL in that module, and a parallel TTL here would be the
same defect with a different name). New generations are picked up by the next process, which is the
dominant caller.

**R27.** Every Redis call carries `ROUTING_REDIS_TIMEOUT_MS` (default **20 ms**) connect/read; a
timeout is a miss, never a surfaced error.

**R28. (FIX A2) The health file is written atomically, by writers only.** `data/state/redis-health.json`
(`{ok, checked_at, run_id, scope}`):

- written **only** by `status`, `load`, and the timer — never by the read path (a read-path `-ERR`
  must not take the plane down fleet-wide; that shared-fate failure was v0.2's);
- written atomically (tmp + `os.replace`); a missing or unparseable file is `UNKNOWN`, and an
  `UNKNOWN` reader makes **one** attempt under the R27 timeout rather than guessing;
- `ok:false` means a cold process makes zero connection attempts;
- recovery is owned: `status --json` (R14) alarms when the file has not been refreshed for 10 min
  while `ROUTING_REDIS != 0`, so a dead timer cannot silently disable the plane forever.

**R29.** Fallback order: Redis (healthy, fresh) → TR-277 cache / `registry.json` → `data/tables` →
empty tables + warning. `router_spawn.py` exits 0 with one warning line: never a traceback, never a
non-zero exit, never a blocked spawn.

**R30. (FIX A6) Stale is bounded.** The reader compares the cache stamp's `created_at` age with
`ROUTING_REDIS_MAX_AGE_S` (900): inside the window the cache is served; past it the file backend
wins and the warning names the age. R22's refresh keeps a live timer's cache inside the window
without a content change.

**R31.** Redis results equal file results by construction: one row-shaping path publishes and reads,
the models hash carries all 39 columns, and the category hashes carry every column the chain build
filters or sorts on (R34). No routing rule is re-implemented against Redis.

**R32.** Disable switches: `ROUTER_CACHE=0` (`false/no/off`) restores the pre-TR-277 read-per-call
path; `ROUTING_REDIS=0` disables only the Redis arm. `ROUTING_REDIS=redis` is the shipped default
**only if the R52 gate passes**; otherwise `file` is the default and the row says why. Parity is
functional, not byte-identical (every resolve carries `resolved_at` and appends metrics).

## 8. Key schema and access patterns

**R33. Point lookups** (single-fact questions):

| key | type | contents |
|---|---|---|
| `<scope>:g<N>:model:<provider>/<model>` | hash | all 39 model columns |
| `<scope>:g<N>:provider:<id>` | hash | all provider columns (incl. `plan`) |
| `<scope>:g<N>:project:<id>` | hash | `profile`, `sensitivity`, `board_type` |
| `<scope>:g<N>:profile:<id>` | hash | title/tag/version/caps |
| `<scope>:g<N>:profile:<id>:req` | hash | `category → level` |
| `<scope>:g<N>:leveldef:<level>` | hash | `lo`, `hi` |
| `<scope>:g<N>:levels:<category>` | string (JSON) | the 11 thresholds |
| `<scope>:g<N>:fallback` | hash | `order → provider/model` |

**R34. (FIX A4) Category scans carry the whole decision tuple.** `<scope>:g<N>:cat:<category>` is a
hash whose value is **not** the level alone but
`{level, plan_tier, normalized_price, token_factor, context_limit, archive, valid_to}` — every column
`_build_chain` filters or sorts on (`router_spawn.py:2800-2815`). One `HGETALL` per required category
therefore feeds the entire chain build and the reader never issues a per-model read.

**R35.** The models hash duplicates facts reachable through the category hashes; that duplication is
measured (R51) and is the first thing dropped if memory binds.

**R36. (FIX A4) Fixed read order, batched, count-independent.**

1 pointer, 1 `project:<id>`, 1 pipelined `{profile:<id>, profile:<id>:req}`, **1 pipelined batch of
all `cat:<category>` HGETALLs for the required categories (one round trip regardless of how many
categories the profile requires)**, 1 batch of `levels:*`/`fallback`. Total ≤ 6 round trips for any
profile, including P0_FORE's 17 categories.

**R37.** Round-trip budget pinned by test **against every live profile**, enumerated from
`task_profile_requirements.jsonl` at test time: ≤ 6 batch round trips per chain resolve, ≤ 2 per
point lookup. Exceeding the budget is a contract failure, not a performance curiosity.

**R38.** No price-derived ordering in Redis: the chain sort stays in the reader, from the columns it
just read. A price-sorted zset would make two components responsible for ordering.

## 9. Client

**R39.** Minimal **stdlib RESP2** client in `scripts/router_redis.py`: `AUTH`, `SELECT`, `PING`,
`GET`, `MGET`, `SET`, `HSET`, `HGET`, `HMGET`, `HGETALL`, `INCR`, `DEL`, `EXISTS`, `SCAN`, `INFO`,
`MULTI`/`EXEC`/`DISCARD`/`WATCH`/`UNWATCH`, pipelining, plus `EXPIRE`. No third-party `redis`
package (`pyproject.toml` declares `scripts/` stdlib-only).

**R40.** A check asserts no third-party imports (AST vs `sys.stdlib_module_names`).

**R41. (FIX F12) Registration goes in the COPY loop.** `router_redis.py` is added to
`scripts/sync_runtime.sh:65` — the byte-identical **copy** list where `router_data_cache.py` lives —
not the symlink list at `:28`; the TR-288 rule is that imported modules must ride the copy path or
cron invocations die on `ImportError` while the check "the file name appears in the script" still
passes. The check therefore asserts the file appears **in the copy loop specifically**.

**R42.** `-ERR` replies are a miss in the read path (no shared-file write, R28) and surfaced in
`load`/`status`.

## 10. Topology and security

**R43.** `ROUTING_REDIS_URL` (default `redis://127.0.0.1:6391/0`), loopback-bound,
`protected-mode yes`, `requirepass` from `~/.hermes/secrets/router-redis.pass` (mode 600), persistence
off (`save ""`, `appendonly no`), `maxmemory 256mb`, `maxmemory-policy noeviction` (a silently
evicted key is a correctness bug, not a capacity event).

**R44. (FIX C11, A8v) Guards are an allowlist, not a denylist.** Refuse any resolved port that is not
`ROUTING_REDIS_PORT` (6391) unless it is explicitly listed in `ROUTING_REDIS_ALLOW_PORT`; the known
product instances `{6379 dexdat-core-redis-1, 6380 asce-redis-1, 16379 helios-redis}` are documented
as the reason. Refuse if `AUTH` is not configured. On first connect record `INFO server.run_id` +
`DBSIZE` in `data/state/redis-instance.json`; later: same `run_id` → proceed; different `run_id` with
our owner key present → restart → reload; different `run_id` with keys but no owner key → `FOREIGN`,
exit 5, touch nothing.

**R45.** Provisioning is repo-tracked, idempotent (`scripts/router_redis_up.sh`), and names the owner
of the container/unit so a fresh or bunker host has an answer. The instance is disposable: no
backups, no volume; restore is `router redis load --force`.

## 11. Observability

**R46. (FIX C12, A5) Provenance is additive.** The resolve payload gains
`cache: {status, generation, created_at_age_s, scope, verified}` and keeps `source` untouched
(TR-046's answer must not be redefined). `verified=false` whenever the artifact leg is `UNSTAMPED`.

**R47.** `router redis status --json` is the machine contract; the `no_agent` cron runs it bare.

**R48.** The seed prints one line: `cache: <scope> g<N> jsonl=<hash12> tables=12` or
`cache: NOT PUBLISHED (<reason>)` plus the R23 record.

## 11.5 Infrastructure deliverables (added after the build-plan review asked "does this include the docker and infra work?")

R43–R45 state the *requirements*; these statements name the *artifacts and on-host changes* that
satisfy them. Every one is a deliverable of this row, not a follow-up, because a read plane that
only exists as a hand-typed `docker run` on one box is a demo.

**R58. Container definition lives in the repo.** `scripts/router_redis_up.sh` creates the instance
idempotently from an explicit spec (create if absent; verify + fix drift if present; never
`docker rm` a healthy instance, never touch any other container). It pins the **image digest**
(`redis@sha256:…`), publishes `127.0.0.1:6391:6379` only, and sets:

| flag | value | why |
|---|---|---|
| `--memory` / `--memory-swap` | `320m` / `320m` (equal) | `maxmemory 256mb` governs Redis's own accounting; RSS can exceed it via fragmentation and copy-on-write, and the host is the fleet's shared box. Equal swap disables swap rather than allowing a silent overcommit |
| `--restart` | `unless-stopped` | survives a reboot without a systemd unit for the container itself |
| `--health-cmd` | `redis-cli -a "$REDIS_PASSWORD" ping` | `redis:7-alpine` ships `redis-cli`; a process that is up but refusing writes must show `unhealthy` |
| `--log-opt max-size/max-file` | `10m` / `3` | `data/metrics.jsonl` already proved what unbounded logs do to this box (TR-372) |
| `--label` | `task-router.component=redis-read-plane`, `task-router.spec=SPEC-REDIS-READ-PLANE` | ownership is discoverable by `docker ps --filter label=…`, so "who owns this container" is answerable on a fresh host |

**R59. Secret lifecycle.** `requirepass` is generated by the provisioning script when
`~/.hermes/secrets/router-redis.pass` is absent (mode 600, dir 700), never logged, never in git, and
never printed by `status`. Rotation is: write the new secret file → recreate the container → the
next `status` reports `UNREACHABLE` until a `load --force` succeeds, which is the documented
observable sequence rather than a silent hang.

**R60. Reload timer ships as unit files in the repo.** `scripts/router-redis-sync.service` +
`.timer` (the `OnBootSec=2min` / `OnUnitActiveSec=5min` / `AccuracySec=30s` shape already used by
`worker-scope-reaper.timer`), installed to `~/.config/systemd/user/` by the provisioning script,
with `loginctl enable-linger` asserted because a user timer without linger does not run after a
reboot. The service runs `load --if-stale` and nothing else.

**R61. Alerting cron is registered, and *armed* is verified.** The `no_agent` cron job
(`router-redis-status.sh`, the `router-averages-refresh.sh` shape: silence on `MATCH`, one line
otherwise; `deliver` to the owner's channel) is created through the cron API, not hand-edited into
`jobs.json`. Arming is asserted: the provisioning script prints the job id, its `enabled` flag and
its `last_status`, because this fleet already has hourly router jobs sitting `enabled: false` — an
alerting cron that is quietly paused is worse than none.

**R62. Arming is a gated sequence, and every stage is reversible.**
`provision → load → status MATCH → read-path wiring (default still file) → timer → cron → measure →
(default flips only if R52 passes)`.
`scripts/router_redis_down.sh` reverses the *host* side in order: disable + remove the user timer and
the cron job, then stop the container (`docker rm -f tr388-redis`), then print the one-line
instruction (`ROUTING_REDIS=0`) that returns the code path to the file. Data loss is defined as a
non-event (R6), so teardown needs no backup step — and the teardown is tested, not described.

**R63. Metrics are opted into deliberately, never accidentally shared.** This host already runs
`svc-redis-exporter` against the product instance. Our instance is **not** added to that exporter's
target set unless a follow-up row says so; `status --json` + the cron line are the primary signal.
If an exporter is added later it must be a separate target with a `component` label, so a Redis
outage cannot be misread as a product outage.

**R64. CI stays honest about infrastructure.** Hosted runners have no Docker daemon and no Redis:
every infra-backed test skips with an explicit reason, and the `check:`-kind assertions (script
presence, digest pinning, flag set, unit-file shape, cron registration code path) run everywhere. A
skipped infra test must never be counted as a pass — the row reports the skip counts separately.

**R65. No host package install is required or performed.** The instance is a container on every
host, because `redis-server`/`redis-cli` are absent here (measured) and a per-host package would make
the plane's presence a property of the OS image rather than of this repo.

**R66. Fresh-host path is one command and is documented in `docs/registry-maintenance.md`.** The same
script provisions, loads, installs the units, registers the cron and prints the verification line;
a second host is a copy-run, not a runbook conversation.

**R67. Ownership and capacity are observable.** `docker ps --filter label=task-router.component=…`,
`status --json`'s `scopes_live`/`dbsize`/`used_memory_human`, and the container's health status are
the three answers; the provisioning script refuses to proceed if another container already publishes
loopback `:6391` (it is not "our" instance just because the port answers).

**R68. The infra work is part of the row's acceptance, in this order.** Provisioning script + unit
files + status script are reviewed and committed with the code (not "later"), the read path is not
wired until `status` reports `MATCH` against a live instance, and the default is not flipped until
R50/R51 are recorded and R52's gate has run.

## 12. Verification plan

**R49. (FIX C13, A7) Two kinds of assertion, one per statement.** Every normative R# carries exactly
one named assertion:

- `test:` — a pytest in `tests/test_redis_read_plane.py` for behaviour;
- `check:` — a script (`tests/check_spec_norms.py`) for prohibitions and prose claims: R3/R53
  (docs-grep over `docs/` and the board), R7/R54 (no lock/queue/pubsub verbs in the RESP allowlist),
  R56 (no key outside `<scope>:`), R57 (no per-lane `model:` reads in the chain path).

(the checker derives its range by parsing this document's largest R#, never a hard-coded count; R58–R68
are all `check:`-kind — infra shape, not behaviour) R55 is asserted by cross-reference to R31's parity test. The table below is the coverage map.

| test / check | proves |
|---|---|
| hash: multiplicity (duplicate add/remove moves it), explicit 12-name set, `not_cached` computed, equality excludes `not_cached` | R9 |
| two seeds → identical stamp; one-field edit → STALE naming the table; `generation`/`created_at` present | R9, R11 |
| state matrix ×8 incl. GHOST, FOREIGN, SCHEMA-AHEAD, UNSTAMPED (+7-day line) | R11 |
| in-flight: intent key aged by pid/age; `status` read-only | R12 |
| `status --json` field contract; exit codes 0–6; silence on MATCH; 30-min `created_at` alarm; 10-min health alarm | R13, R14 |
| diff: +1 −1 ~0; full-row compare; key uniqueness re-measured for all 12 tables | R15–R18 |
| publish: two concurrent publishers on one connection each; pointer monotonic; loser exits 4 + R23 record; orphan reaped by the next `load` after a simulated crash | R20, R23 |
| publish reads once (counting loader sees exactly one read per table) | R10 |
| pinned generation GC'd → whole resolve falls back (never mixed) | R21 |
| `--if-stale`: no-op when fresh; refresh-republish when age > MAX_AGE/2; reap-only path | R22 |
| scope: two data homes isolated; scratch publish refused without opt-in; expired-heartbeat scope swept; `scopes_live`/`dbsize` reported | R8 |
| fail-open: down / stale-within-window / beyond MAX_AGE / health UNKNOWN → resolve, exit 0, one warning | R27–R30, R32 |
| health: cold process with `ok:false` makes zero attempts; corrupt file → UNKNOWN → one attempt; read path never writes it | R28 |
| budget: ≤6 chain / ≤2 point round trips for **all 11 profiles** (counting client) | R36, R37 |
| parity: full-table + full-column parity for all 12 tables; chain resolves for `coding-hermes-scheduler` and `hivemind-work` (head + ordered members); `cat:<category>` values carry every sort/filter column | R31, R34 |
| guards: port allowlist, AUTH refusal, restart vs FOREIGN, flush owner guard | R44, R25 |
| registration: `router_redis.py` in the **copy** loop of `sync_runtime.sh`; no third-party import | R40, R41 |
| no-write: `router_spawn.py` writes only `data/metrics.jsonl` | R5 |
| seed wiring: publishes + prints the stamp; forced publish failure exits 0 **and** writes R23 | R24, R48 |

| infra: provisioning script exists, pins an image digest, publishes only `127.0.0.1:6391`, sets the R58 flag set, and refuses when another container owns `:6391` | R58, R67 |
| infra: secret file created 600 / dir 700, never printed by `status`, rotation sequence observable (`UNREACHABLE` → `load --force` → `MATCH`) | R59 |
| infra: unit files parse (`systemd-analyze verify` when available), linger asserted, the service runs only `load --if-stale` | R60 |
| infra: cron job registered through the API, `enabled` + `last_status` asserted at arm time, silence-on-MATCH shape | R61 |
| infra: teardown script disarms timer + cron before stopping the container, and the file path is proven after (`ROUTING_REDIS=0` resolve equals the pre-change tree) | R62 |
| infra: no new exporter target without a `component` label; `status --json` is the primary signal | R63 |
| infra: infra-backed tests skip with a reason and skip counts are reported separately; `check:` legs run on a bare runner | R64 |
| check: no `apt`/package-install path in the provisioning (container only) | R65 |
| check: `docs/registry-maintenance.md` carries the one-command fresh-host path | R66 |
| check: provision → `status` MATCH gate precedes read-path wiring; default flip blocked before R50/R51/R52 | R68 |

**R50. Perf gate.** Point-lookup p50/p95 over 200 distinct (model, category) queries, full
`router_spawn.py` wall, peak RSS, Redis `used_memory_human`, and round trips per resolve — file vs
Redis, before and after, recorded in the row's evidence.

**R51. Memory gate.** `used_memory_human` after load; the R35 duplication cost; the reverse index's
cost if kept.

**R52. (FIX C14, F9) The falsifier is a floor with defined legs.** Measure a **stub resolve** that
returns a canned chain with no registry read (the absolute floor any read plane can reach). The
stop rule: if `(cold_spawn_wall − floor) < 10 %` **and** `point_lookup_p95_redis` does not improve on
`point_lookup_p95_file` by ≥5× (file = cold parse + a full scan for the same fact, 200 distinct
queries, same process model), then publish that finding in the row and the Status line, ship
`ROUTING_REDIS=file` as the default, and stop. In practice the point-lookup leg is expected to pass
trivially on loopback, so **the stop decision hangs on the spawn-floor leg** — say so, do not imply
two live legs.

**R53. (FIX C14)** Any remote/bunker caller benefit is out of scope for this loopback topology: no
claim about it may appear in docs, evidence, or memory until a follow-up row defines that topology.

## 13. Non-goals

**R54.** Not a write plane, message bus, distributed lock, or quota store.
**R55.** No change to routing decisions, prices, tiers, ordering, or the seed's derivations (asserted
by R31's parity test; no second test).
**R56.** No new runtime dependency; no reuse of another product's Redis; no second canonical copy.
**R57.** No per-lane round trips; no JSON blob of the whole registry (it would keep the parse cost and
add a network cost).

## 14. Round-1 disposition (receipt)

Four seats, four families, identity proven from `session_model_usage`: `glm-5.3 @ api.z.ai`,
`kimi-k3 @ api.neuralwatt`, `deepseek-v4.1-flash @ api.neuralwatt`, `qwen3.6-35b @ api.neuralwatt`.
Two lanes were rejected on identity (Qwen3.8-Max @ commandcode → silently served by
`fireworks/deepseek-v4p1-flash-1017`; Qwen3.8-Max @ ollama-cloud → never served) and are recorded,
not counted. Nine claims at 4/4 CONTRADICTED: hash multiplicity, state table, diff columns, publish
atomicity, TOCTOU/exit codes, fail-open/switch name, instance guards, test coverage, missing risks
(multi-checkout + override poisoning). All merged. Full record:
`docs/reviews/TR-388-quorum-2026-11-04.md`.

## 15. Round-2 disposition (receipt)

Same four seats re-judged v0.2. Fix verdicts: 11 FIXED, 3 PARTIAL → all PARTIALs merged in v0.3.
The round found **five spec-breaking defects that v0.2 introduced**, each now fixed:

| id | defect (all 4 seats agreed unless noted) | v0.3 fix |
|---|---|---|
| A6 | age deadlock: `--if-stale` never advanced the stamp on unchanged content, so after 900 s every reader fell back forever while Redis was healthy and correct | R9 (`created_at` as a stamp field), R22 (refresh-republish at MAX_AGE/2), R14 (30-min alarm) |
| A4 | round-trip budget contradicted its own formula (P0_FORE = 17 categories ⇒ 18+ trips ≫ 8) **and** `cat:<category>` carried only the level, forcing per-lane reads for price/plan_tier/context/lifecycle | R34 (decision-tuple values), R36 (pipelined category batch = 1 trip), R37 (budget tested against all 11 profiles) |
| A1 | WATCH is per-connection (a pooled CAS is vacuous); a crash between the write pass and the CAS orphaned a full generation that no path ever collected inside a `noeviction` instance | R20 (one dedicated connection, UNWATCH/DISCARD on abort, `__ts`, orphan reap in `load`, `orphans` reported) |
| A2 | health file was text-as-state: non-atomic write, two writers, one read-path `-ERR` flipping `ok:false` fleet-wide, no owner of recovery | R28 (atomic write, writers-only, UNKNOWN semantics) + R14 (10-min alarm) |
| A3 | scope identity keyed on `DATA_DIR` alone while `REGISTRY` resolves separately (poisoning), and dead scopes accumulated with no GC (capacity) | R8 (both paths hashed, heartbeat + TTL, dead-scope sweep, scratch publish opt-in, `scopes_live`) |

Plus smaller merges: the 12-vs-13 table arithmetic and the explicit table list (F1/A8i); the
in-flight key that made R12 unimplementable (F2); missing diff keys for archetypes/benchmarks/
level_defs (F3); the phantom `R1–R64` range and the vacuous one-test-per-R rule (F10/A7 → R49);
`sync_runtime.sh` copy-loop vs symlink-loop (F12/R41); three wrong cross-references (R2→R52,
R33→R51, R34→R51); the undefined 5× metric and the "two live legs" implication (F9/R52); the
`UNSTAMPED`-forever silence (A5/R11/R14/R46); stamp equality excluding `not_cached` (A8iv);
port denylist → allowlist (A8v).

**v0.3 has not been re-judged.** Round 3 should run with the first implementation commit, against
the tests rather than the prose — the five defects above were all found by attacking a *fix*, which
is the class a third round of prose review stops catching.

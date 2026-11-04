# SPEC-REDIS-READ-PLANE — a Redis read plane over the JSONL registry

Status: **v0.2 (2026-11-04) — quorum-reviewed. Implementation authority, subject to R55's
measurement gate.**
Board row: TR-388 (P1) · Owner directive: Bane, 2026-11-04
Quorum evidence: `docs/reviews/TR-388-quorum-2026-11-04.md` (4 seats, 4 model families)
Interacts with: TR-277 (`scripts/router_data_cache.py`), TR-046 (`source` provenance),
TR-005 (`router_maintain.py`), TR-048/runtime-sync rules, AGENTS.md fail-open rule.

Normative statements **R1…R64**. Changed-from-v0.1 statements carry a `(FIX Cn)` tag naming the
quorum claim that forced the change; the disposition appendix (§14) is the receipt.

---

## 1. Problem and the measured baseline

| fact | measured 2026-11-04 (coordinator, this host) |
|---|---|
| `registry.json` | 4,792 KB, 12 tables |
| biggest tables | models 2,064 · model_perf 3,944 · model_tier 3,896 · benchmarks 2,148 · projects 950 |
| parse `registry.json`, 100 runs | **mean 49.0 ms, p50 45.1, min 33.3, max 180.2** (the v0.1 "~40 ms" was a single warm sample; a seat's 27.6 ms is not reproducible here) |
| cold `router_spawn.py <project> --format json` | 0.28–0.39 s wall, ~73 MB RSS |
| `data/tables/` | **31** JSONL files — only 12 are seed-derived artifact tables (+`fallback_lanes` sidecar) |
| in-process cache | TR-277: read-once per file identity, change-driven, single-flight, no TTL (AC0–AC6) |
| chain build | `DEFAULT_CHAIN_LIMIT = 1024` lanes (`router_spawn.py:85`), a **scan** with a per-lane price read — not a point lookup |

**R1.** TR-277 already removes repeated parsing *inside* a process. It cannot remove the cold
start: the dominant caller is a fresh `router_spawn.py` process per tick/probe/CLI call.

**R2. (FIX C14) The value claim is bounded and must be measured, not asserted.** The read plane
can remove at most the registry read+parse from a cold spawn (~45 ms of a ~300 ms wall, ≈15 %)
and can serve single-fact questions in O(1). It **cannot** make the chain build cheaper by
naive per-lane reads: a 1,024-lane loop over Redis round trips is a regression against a table
already resident in memory. Therefore R37 (round-trip budget) and R56 (floor measurement) are
part of the design, not a footnote.

**R3.** The value does not come from caching JSON parsing inside a long-lived process (TR-277
does that) and must not be sold that way in docs, board events, or memory.

## 2. Boundary

**R4.** `data/tables/*.jsonl` is the only source of truth; `registry.json` is the seed's build
artifact. Redis holds a derived, disposable projection.

**R5.** No write path, quota/circuit state, outcome stats, ledger, chain snapshot, or derived
drift ever moves from Redis back to JSONL. `router_spawn.py` gains no write verb against the
cache or the registry (its existing `data/metrics.jsonl` append is out of scope and stays).

**R6.** Losing Redis (deleted container, reboot, `FLUSHALL`) is a non-event: the router serves
from `registry.json`/`data/tables` and the next `load` rebuilds. No Redis key is the only copy
of any fact.

**R7.** No locks, leader election, queueing, or rate limiting in Redis. (One exception, scoped:
the publish CAS in R25 is a Redis-side compare-and-set on our own meta key — it coordinates
*publishers of this cache only*, never fleet scheduling.)

**R8. (FIX C5/C16) Namespace by data home.** Every key carries a data-home scope derived from
the resolved `DATA_DIR` realpath (`<scope>` = first 12 hex of `sha256(realpath(DATA_DIR))`), and
every stamp records `data_home`, `checkout` (git toplevel + HEAD), and `schema`. Two checkouts,
a scratch `ROUTING_DATA_DIR`, or a worktree therefore cannot read or publish each other's
plane: scratch runs get their own scope and are invisible to production.

## 3. Version identity

**R9. (FIX C4) Hash with multiplicity, over an explicit table set.**

- Cached table set = the 12 artifact tables + `fallback_lanes` = **13** named tables. The other
  18 JSONL files in `data/tables` are out of scope and MUST be listed as out of scope in the
  stamp (`not_cached: [...]`) so nobody assumes the hash covers them.
- Per table: read the file, drop blank lines, strip trailing whitespace, then take the **sorted
  list** of line strings *preserving duplicates* and `sha256("\n".join(sorted_lines))`. A set
  would collapse duplicates; `model_notes.jsonl` already carries 3 duplicate raw lines today,
  so a multiplicity-blind hash is a demonstrated silent-miss class.
- Aggregate: `sha256("\n".join(f"{name}:{hash}:{count}" for name in sorted(tables)))` — the row
  count is an input, so a duplicate add/remove moves the hash twice over.
- Stamps: same object written in three places — computed from JSONL (`status`, `load`), inside
  `registry.json` as `stamp` (seed), and in Redis at `<scope>:meta:version`.

**R10. (FIX C5/C8) Publish reads ONCE.** `load` parses the JSONL into memory, hashes *that
in-memory set*, and publishes *that same in-memory set*. There is no re-read window, so the
v0.1 before/after-hash TOCTOU dance (and its A→B→A blind spot) is deleted outright. If a seed
rewrites the tables mid-publish, the publish is internally consistent and simply carries an
older stamp — the next `load --if-stale` corrects it.

**R11. (FIX C5) States are exhaustive and machine-readable.** `status` prints exactly one:

| state | condition | exit |
|---|---|---|
| `MATCH` | JSONL stamp == artifact stamp == cache stamp, and the pointer's generation is present | 0 |
| `STALE` | stamps disagree; message names the drift class (`artifact-behind`, `cache-behind`, `cache-ahead` — a git rollback is a real case) and the drifted tables | 1 |
| `EMPTY` | pointer absent (never loaded, flushed, restarted empty) | 2 |
| `UNREACHABLE` | connect/auth/protocol failure | 3 |
| `GHOST` | pointer present but its generation's keys are gone (partial GC, manual `DEL`, `maxmemory`) | 4 |
| `FOREIGN` | reachable instance whose `run_id` differs **and** which holds keys without our owner key | 5 |
| `SCHEMA-AHEAD` | cache stamp `schema` > this reader's schema | 6 |
| `UNSTAMPED` | `registry.json` carries no `stamp` (today's artifact — measured: no `stamp` key); compare JSONL↔cache only and report the artifact leg as degraded | 0 if JSONL↔cache agree, else 1 |

**R12.** `status` never mutates, and is safe to run during a publish: a publish-in-progress is
reported as `STALE (cache-behind, in-flight)` with exit 1 only when the publish is older than
`ROUTING_REDIS_PUBLISH_STALE_S` (default 120). A live publish must not alarm.

**R13.** `status --json` carries all three stamps, `state`, `drift_class`, `drifted_tables`,
per-table counts, ages, `data_home`, `checkout`, and `generation`.

**R14.** Exit codes are the cron contract: silent on `MATCH`, one actionable line otherwise
(the shape of `~/.hermes/scripts/router-averages-refresh.sh`), **plus** a post-success age gate
— a cache whose stamp has not advanced in 7 days prints a line even while `MATCH`.

## 4. Diff reporting

**R15. (FIX C6) Diff keys are the real columns.** Declared once, in code and here:

| table | key |
|---|---|
| models | `(provider, model)` |
| providers | `(id)` |
| model_perf | `(model, category)` — **there is no `provider` column** (measured: 5 cols) |
| model_tier | `(model, category)` — measured: 6 cols, no `provider` |
| category_levels | `(category, level)` |
| task_profiles | `id` |
| task_profile_requirements | `(task_id, category)` |
| projects | `id` |
| fallback_lanes | `(order)` — measured cols `{key_env, model, note, order, provider}`; no `project`/`position` |

**R16. (FIX C6) The diff compares like with like.** Redis stores the **full row** (models: all
39 columns), so the diff compares full rows and never reports "every row changed" because of a
projection mismatch.

**R17.** `status --diff` = `+added −removed ~changed` per table; `--detail N --table T` prints
field-level deltas for up to N keys. Default `--diff` stays under one second.

**R18.** The diff never mutates and never decides reload: the hash decides (R9). The diff needs
a comparable resident set, so it cannot be the trigger.

## 5. Publish protocol

**R19.** All data keys live under `<scope>:g<N>:…`; readers reach data only through the
pointer `<scope>:meta:version`.

**R20. (FIX C7) Publish is a compare-and-set, not a blind SET.**

1. `gen = INCR <scope>:meta:generation`
2. write every `<scope>:g<gen>:` key
3. **`WATCH <scope>:meta:version` → if current generation > gen, abort (exit 4, GC own keys) →
   `MULTI; SET <scope>:meta:version <stamp>; EXEC`** — the pointer can never regress, so two
   publishers (seed + timer, or two checkouts in the same scope) cannot ping-pong.
4. GC generations older than `gen-2` (keep the previous two).

**R21. (FIX C7/C8) A reader that misses falls back for the WHOLE resolve.** If any key of the
pinned generation is missing (GC'd under a pinned reader, restart, eviction), the reader
**discards the Redis answer entirely** and re-resolves from the file backend. A resolve is
never assembled from mixed generations.

**R22. (FIX C7) Idempotence.** `load --if-stale` hashes, compares with the cache stamp, and
returns 0 without writing when they match. Safe to run every minute.

**R23. (FIX C8) Failure is representable.** A failed or aborted publish appends one JSONL record
to `data/state/redis-publish-errors.jsonl` (`ts`, `scope`, `gen`, `reason`, `tables`) and exits
non-zero (4 = aborted by CAS, 5 = publish error). The seed stays fail-open (R24) but the failure
is a record, not a log line: `status` reports `STALE`, and the record is how a repeated abort is
counted.

**R24. Reload triggers.** (1) `router_seed.py` publishes in the same run after its write pass and
prints the stamp; a publish failure leaves the seed exit 0 **and** writes the R23 record.
(2) `router-redis-sync.timer` (5 min) runs `load --if-stale` as the drift-catcher. (3) Manual
`router redis load [--force]`; `--force` refuses if the JSONL is unreadable.

**R25.** `flush --yes` requires the owner key to match this spec's owner id and touches only
`<scope>:*` keys.

## 6. Read path and fail-open

**R26. (FIX C3/C9) No process-lifetime TTL memo.** The pointer is resolved once per process and
the generation pinned for that process's lifetime; freshness is not re-checked mid-process by a
timer. A new generation is picked up by the next process (which is the dominant caller) and by
the server on its next cache-generation boundary. This respects TR-277 AC1 ("no TTL in this
module") instead of inventing a parallel TTL.

**R27.** Every Redis call carries a connect/read timeout (`ROUTING_REDIS_TIMEOUT_MS`,
default **20 ms** — v0.1's 50 ms was never justified and this host is documented I/O-starved).
A timeout is a miss, not an error.

**R28. (FIX C9) Health is cached out-of-process, so the latch problem disappears.** The read
path consults `data/state/redis-health.json` (written by `status`/`load`/the timer:
`{ok, checked_at, run_id}`). If `checked_at` is older than `ROUTING_REDIS_HEALTH_TTL_S` (60) or
`ok` is false, the process goes straight to the file backend **without attempting a connection**.
A cold process therefore pays zero extra latency while the cache is down — the v0.1 per-process
latch could not do that.

**R29.** Fallback order: Redis (healthy, fresh) → TR-277 cache / `registry.json` → `data/tables`
→ empty tables + warning. `router_spawn.py` exits 0 with one warning line; never a traceback,
never a non-zero exit, never a blocked spawn.

**R30. (FIX C9) Stale is bounded, not served indefinitely.** The reader compares the cache
stamp's age to `ROUTING_REDIS_MAX_AGE_S` (default 900): within the window the cache is served
(correctness is asserted by hash equality at load time); past it, the file backend wins and the
warning names the age. Serving a 5-minute-old cache is a deliberate availability choice;
serving an hour-old one is not.

**R31.** Redis results equal file results by construction: one row-shaping code path publishes
and reads, and the models hash carries all 39 columns (R16). No routing rule is re-implemented
against Redis.

**R32. (FIX C9) The disable switch is the existing one:** `ROUTER_CACHE=0` (`false/no/off`
alike) restores the pre-TR-277 read-per-call path; `ROUTING_REDIS=0` disables only the Redis arm.
Parity is asserted functionally, not "byte-identically" — every resolve carries `resolved_at`
and appends metrics, so byte-identity was never achievable (v0.1 was wrong).

## 7. Key schema and access patterns

**R33. (FIX C14) Two shapes, two access patterns, explicit.**

*Point lookups* (single-fact questions, `/v1` one-model queries):

| key | type | contents |
|---|---|---|
| `<scope>:g<N>:model:<provider>/<model>` | hash | **all 39 model columns** |
| `<scope>:g<N>:provider:<id>` | hash | all provider columns (incl. `plan`, the PAYG marker `_stamp_payg_providers` needs) |
| `<scope>:g<N>:project:<id>` | hash | `profile`, `sensitivity`, `board_type` |
| `<scope>:g<N>:profile:<id>` | hash | title/tag/version/caps |
| `<scope>:g<N>:profile:<id>:req` | hash | `category → level` |
| `<scope>:g<N>:leveldef:<level>` | hash | `lo`, `hi` (the `level_defs` rows the tier math reads) |

*Category scans* (the chain build and eligibility):

| key | type | contents |
|---|---|---|
| `<scope>:g<N>:cat:<category>` | hash | `<provider>/<model> → level` (3,896 fields total across 24 categories) |
| `<scope>:g<N>:levels:<category>` | string (JSON) | the 11 percentile thresholds |
| `<scope>:g<N>:tiers:<provider>/<model>` | hash | `category → level` (reverse index; optional, dropped if R55's memory leg says so) |
| `<scope>:g<N>:fallback` | hash | `order → provider/model` |

**R34.** The models hash duplicates model facts already reachable through `cat:<category>`'s
members. That duplication is measured (R57) and is the first thing to drop if memory binds.

**R35.** No price-derived ordering in Redis: the chain sort stays in the reader, from prices it
just read. A price-sorted zset would make two components responsible for ordering and would
need rebuilding on every price change.

**R36. (FIX C14)** The Redis plane serves the **whole chain build** as category scans: at most
`(#required categories × 1) + 1` bulk reads per resolve, never one read per candidate lane.

**R37. (FIX C14) Round-trip budget, pinned by test.** A resolve issues ≤ 8 Redis round trips for
a single-profile chain and ≤ 2 for a point lookup. A test counts them; exceeding the budget is
a contract failure, not a performance curiosity.

## 8. Client

**R38.** Minimal **stdlib RESP2** client inside `scripts/router_redis.py` (socket + parser):
`AUTH`, `SELECT`, `PING`, `GET`, `SET`, `MGET`, `HSET`, `HGET`, `HMGET`, `HGETALL`, `INCR`, `DEL`,
`EXISTS`, `SCAN`, `INFO`, `MULTI`/`EXEC`/`WATCH`/`UNWATCH`, pipelining. No third-party `redis`
package: `pyproject.toml` declares scripts/ stdlib-only.

**R39.** A test asserts no third-party imports (AST vs `sys.stdlib_module_names`).

**R40. (FIX C10) Registration is part of the deliverable.** `scripts/sync_runtime.sh` carries a
hard-coded file list (two `for f in` loops); `router_redis.py` must be added there, and a test
asserts the list contains it — an unregistered module silently fails open in live installs and
the plane never activates.

**R41.** `-ERR` replies are a miss + health-file flip in the read path; strict (surfaced) in
`load`/`status`.

## 9. Topology and security

**R42. (FIX C11) Dedicated instance, explicit URL, no discovery.** `ROUTING_REDIS_URL`
(default `redis://127.0.0.1:6391/0`), bound to loopback, `protected-mode yes`, `requirepass`
read from a local secret file (`~/.hermes/secrets/router-redis.pass`, mode 600), persistence
off (`save ""`, `appendonly no`), `maxmemory 256mb`, `maxmemory-policy noeviction` (a silently
evicted key is a correctness bug, not a capacity event).

**R43. (FIX C11) Guards, in order, all hard:**

1. refuse any port in the denylist `{6379, 6380, 16379}` — this host runs four product Redises
   (`dexdat-core-redis-1` 6379, `asce-redis-1` 6380, `helios-redis` 16379, `trouble-redis`
   internal) and v0.1 blocked exactly one of them;
2. refuse if `AUTH` is not configured;
3. on first connect record `INFO server.run_id` + `DBSIZE` in `data/state/redis-instance.json`;
4. on later connects: same `run_id` → proceed; different `run_id` **and** our owner key present
   → restart (keys may be gone) → reload path; different `run_id` **and** keys without our owner
   key → `FOREIGN`, refuse with exit 5 and touch nothing. (v0.1's single rule fired both ways
   and made exit 5 unreachable.)

**R44.** Provisioning is repo-tracked and idempotent (`scripts/router_redis_up.sh`), naming the
owner of the container/unit so a fresh or bunker host has an answer.

**R45.** The instance is disposable: no backups, no volume; restore is `router redis load --force`.

## 10. Observability

**R46. (FIX C12) Provenance is ADDITIVE.** The resolve payload gains `cache: {status, generation,
stamp_age_s, scope}` and keeps `source` untouched — TR-046's "seeded registry.json vs
data/tables" answer must not be redefined.

**R47.** `router redis status --json` is the machine contract; the `no_agent` cron runs it bare.

**R48.** The seed prints one line: `cache: <scope> g<N> jsonl=<hash12> tables=13` or
`cache: NOT PUBLISHED (<reason>)` plus the R23 record.

## 11. Test plan — one row per normative statement

**R49.** Every normative R# in R1–R64 has at least one named assertion in `tests/`; the suite
fails if a statement is added without one (the spec's own rule, now enforced: a table of
`R# → test` lives in `tests/test_redis_read_plane.py` and a checker asserts coverage).

| test | proves |
|---|---|
| hash determinism + multiplicity (add/remove a duplicate line moves the hash); 13-table set; `not_cached` list | R9 |
| two consecutive seeds → identical stamp; one-field edit → STALE naming the table | R9, R11 |
| state matrix: MATCH / STALE (3 drift classes) / EMPTY / UNREACHABLE / GHOST / FOREIGN / SCHEMA-AHEAD / UNSTAMPED | R11, R12 |
| `status` exit codes 0–6; silence on MATCH; 7-day age gate | R14 |
| diff: +1 −1 ~0 after a row add/remove; full-row compare (no false `~changed`); real keys per R15 | R15–R18 |
| publish: two concurrent publishers, pointer monotonic, loser aborts + records R23 | R20, R23 |
| publish reads once: rewrite the file between hash and publish is impossible by construction (no re-read; assert single read via a counting loader) | R10 |
| reader pinned to a GC'd generation → whole resolve falls back to file (never mixed) | R21 |
| identity: two `ROUTING_DATA_DIR` scopes cannot see each other; scratch publish never visible to production | R8 |
| fail-open: instance down / stale-within-window / beyond `MAX_AGE` / health-file miss → resolve, exit 0, one warning | R27–R30, R32 |
| health file: a cold process with `ok:false` makes **zero** connection attempts (socket-level counter) | R28 |
| round-trip budget: ≤8 for a chain, ≤2 for a point lookup (counting client) | R36, R37 |
| parity: full-table parity for models/providers/projects/profiles/tiers (not a 50-row sample) + chain resolves for `coding-hermes-scheduler` and `hivemind-work` | R31 |
| guards: denylist ports, no-AUTH refusal, FOREIGN vs restart, flush owner guard | R43, R25 |
| registration: `sync_runtime.sh` contains `router_redis.py`; no third-party import | R39, R40 |
| no-write: no write verb in `router_spawn.py` against cache/registry (`metrics.jsonl` append excluded and asserted as the only write) | R5 |
| seed wiring: a seed run publishes and prints the stamp; a forced publish failure still exits 0 **and** writes the R23 record | R24, R48 |

**R50. Perf gate.** Report: point-lookup p50/p95 (1,000 iterations), full `router_spawn.py` wall,
peak RSS, Redis `used_memory_human`, and **round trips per resolve** — file vs Redis, before and
after, in the row's evidence.

**R51. Memory gate.** `used_memory_human` after load; the R34 duplication cost; the R33 reverse
index's cost if kept.

**R52. (FIX C14) The falsifier is a floor, not an AND of three guesses.** Measure a **stub
resolve** that returns a canned chain with no registry read: that is the absolute floor any read
plane can reach. If `(cold_spawn_wall − floor) < 10 %` **and** point lookups do not improve by
≥5×, publish that finding in the row and the Status line, keep the file path as the default
(`ROUTING_REDIS=0` remains the shipped default), and stop building. A falsifier whose conditions
cannot all fire is not a falsifier.

**R53. (FIX C11/C14)** Any remote/bunker caller benefit is **out of scope** for this topology
(loopback instance): no claim about it may appear in docs, evidence, or memory until a follow-up
row defines the topology that would serve it.

## 12. Non-goals

**R54.** Not a write plane, message bus, distributed lock, or quota store.
**R55.** No change to routing decisions, prices, tiers, ordering, or the seed's derivations.
**R56.** No new runtime dependency; no reuse of another product's Redis; no second canonical copy.
**R57.** No per-lane round trips; no JSON blob of the whole registry (that would keep the parse
cost and add a network cost).

## 13. Open questions this spec deliberately leaves to measurement

- Q1: does the category-scan shape (R33/R36) actually beat the in-memory file path on this box?
  R52's floor measurement answers it; no document may claim it before then.
- Q2: is the `tiers:<model>` reverse index worth its memory once R36's scans exist?
- Q3: once `status` is a cron, is the 5-minute timer still needed, or does the alarm subsume it?

## 14. Quorum disposition (receipt)

Four seats, four families, identity proven via `session_model_usage` (not labels):
`glm-5.3 @ api.z.ai`, `kimi-k3 @ api.neuralwatt`, `deepseek-v4.1-flash @ api.neuralwatt`,
`qwen3.6-35b @ api.neuralwatt`. (A 5th intended family, Qwen3.8-Max @ commandcode, was
**misrouted** to `fireworks/deepseek-v4p1-flash-1017` and was not counted; Qwen3.8-Max @
ollama-cloud served no matching row and was not counted. qwen3.6-35b replaced it as the Qwen
family seat.)

| claim | verdict (4 seats) | disposition |
|---|---|---|
| C1 boundary | 4× CONFIRMED | kept, R4–R7 |
| C2 baseline | 3 CONFIRMED, 1 CONTRADICTED | **coordinator re-measured**: p50 45.1 ms / mean 49.0 (100 runs); the seat's 27.6 ms did not reproduce. §1 now states the range and method |
| C3 TR-277 composition | 2 CONFIRMED, 2 CONTRADICTED | fixed: R26 (no TTL memo, AC1 respected), R31/R33 (one row-shaping path), R16 (39-column parity) |
| C4 hash set-collapse | **4× CONTRADICTED** | fixed: R9 sorted list with multiplicity + count in the aggregate + explicit 13-table set (evidence: `model_notes.jsonl` has 3 duplicate lines) |
| C5 state table | **4× CONTRADICTED** | fixed: R11 adds GHOST / FOREIGN / SCHEMA-AHEAD / UNSTAMPED and `cache-ahead`; R12 in-flight rule |
| C6 diff keys | 3 CONTRADICTED, 1 measured | fixed: R15 real columns (measured: `model_perf`/`model_tier` have **no** `provider`; `fallback_lanes` has `order`), R16 full-row compare |
| C7 publish atomicity | 4 CONTRADICTED | fixed: R20 INCR+WATCH/CAS (pointer can't regress), R21 whole-resolve fallback, GC keeps two generations |
| C8 TOCTOU/exit codes | **4× CONTRADICTED** | fixed: R10 single-read publish (window deleted), R23 representable failure + exit 4/5, R24 seed fail-open with a record |
| C9 fail-open | **4× CONTRADICTED** | fixed: R32 real switch `ROUTER_CACHE=0` (verified `router_spawn.py:249-254`), R27 20 ms, R28 out-of-process health file, R30 `MAX_AGE` bound |
| C10 client | 4 CONFIRMED (with a deployment gap) | fixed: R40 sync_runtime registration + test |
| C11 topology/guards | **4× CONTRADICTED** | fixed: R42 AUTH+explicit URL, R43 ordered guards (denylist, AUTH, restart-vs-FOREIGN distinction), R44 ownership |
| C12 observability | 3 CONFIRMED, 1 UNRESOLVED | fixed: R46 additive `cache` field (TR-046 untouched), R14 age gate |
| C13 test coverage | **4× CONTRADICTED** (13 / 28 / 31 untested R#s) | fixed: R49 one-assertion-per-R table + coverage checker; §11 rewritten |
| C14 value/falsifier | 2 CONTRADICTED, 1 CONFIRMED, 1 UNRESOLVED | fixed: R2 honest bound, R36/R37 category scans + round-trip budget, R52 floor-based falsifier, R53 bunker claim withdrawn |
| C15 repeat-offense | 4× "yes, here is where" | each cited line is addressed by the fixes above; no item was waved through |
| C16 missing risks | **4× CONTRADICTED** | fixed: R8 data-home scope (multi-checkout + override poisoning), R11 SCHEMA-AHEAD, R21 restart-mid-resolve, R43/R44 instance identity + ownership |

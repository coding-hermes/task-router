QUORUM ROUND 2 — SPEC-REDIS-READ-PLANE v0.2 (TR-388) — seat 3, deepseek family
Artifact read in full from disk (spec v0.2 = 385 lines; round-1 record; my own seat3 round-1 verdict).
Read-only: 2 terminal commands used (cap 3), no writes, no servers.

════════════════════════════════════════════════════════════════
1. FIX VERDICTS
════════════════════════════════════════════════════════════════

F1 PARTIAL — R9: "Cached table set = the 12 artifact tables + `fallback_lanes` = **13** named tables" — the hash mechanism (sorted list preserving duplicates, count in the aggregate, `not_cached` list) is present, but the count is arithmetically wrong and the set is never enumerated: measured `registry.json` `.tables` has exactly 12 keys and already INCLUDES `fallback_lanes` (archetypes, benchmarks, category_levels, fallback_lanes, level_defs, model_perf, model_tier, models, projects, providers, task_profile_requirements, task_profiles), and `router_seed.py:1808-1810` `ALL_TABLES` is 11 duckdb tables + the `fallback_lanes` sidecar at :1836-1844 = 12, so "12 + fallback_lanes = 13" double-counts fallback_lanes and the 13 named tables exist nowhere in the text.

F2 PARTIAL — R11: "| `IN-FLIGHT`| ... | `GHOST` | pointer present but its generation's keys are gone ... | 4 |" and R12: "a publish-in-progress is reported as `STALE (cache-behind, in-flight)` with exit 1 only when the publish is older than `ROUTING_REDIS_PUBLISH_STALE_S` (default 120)" — GHOST / FOREIGN / SCHEMA-AHEAD / UNSTAMPED / `cache-ahead` all landed in the table, but R12 is unimplementable as written because R20's protocol (steps 1-4, spec:142-147) writes no in-flight/progress key, so nothing can age a publish: `status` has no fact to compare against 120 s and a live publish therefore reports plain `STALE` with exit 1 — exactly the alarm R12 was added to prevent.

F3 PARTIAL — R15: "model_perf | `(model, category)` — **there is no `provider` column** (measured: 5 cols)" / R16: "Redis stores the **full row** (models: all 39 columns)" — the three corrected keys verify exactly against the files (`model_perf` = {category,model,perf,source,source_ref}; `model_tier` = {category,model,perf,source_ref,tier,tier_source}; `fallback_lanes` = {key_env,model,note,order,provider}, `order` unique across 7 rows) and 39 model columns is right, but R15 declares keys for only 9 of the 12 artifact tables — `archetypes`, `benchmarks`, `level_defs` have no declared key while all three are cached and diffed.

F4 FIXED — R20: "`gen = INCR <scope>:meta:generation` … **`WATCH <scope>:meta:version` → if current generation > gen, abort (exit 4, GC own keys) → `MULTI; SET <scope>:meta:version <stamp>; EXEC`**" plus R21: "the reader **discards the Redis answer entirely** and re-resolves from the file backend" and step 4's "GC generations older than `gen-2` (keep the previous two)" — the fenced-pointer and whole-resolve fix landed in the text; the concrete holes in its execution are new-design items (A1 below), not a failed fix.

F5 FIXED — R10: "`load` parses the JSONL into memory, hashes *that in-memory set*, and publishes *that same in-memory set*. There is no re-read window" and R23: "exits non-zero (4 = aborted by CAS, 5 = publish error)" with "`data/state/redis-publish-errors.jsonl`" — the before/after window is deleted outright and failure is a durable record.

F6 FIXED — R32: "`ROUTER_CACHE=0` (`false/no/off` alike) restores the pre-TR-277 read-per-call path" is the real switch (`router_spawn.py:249-254` verified: `os.environ.get('ROUTER_CACHE','1') not in ('0','false','no','off')`), R27: "default **20 ms**", R28: "consults `data/state/redis-health.json`", R30: "`ROUTING_REDIS_MAX_AGE_S` (default 900)" — all four landed; whether those bounds are sane is A2/A6.

F7 FIXED — R42: "`ROUTING_REDIS_URL` (default `redis://127.0.0.1:6391/0`), bound to loopback, `protected-mode yes`, `requirepass` read from a local secret file"; R43: "refuse any port in the denylist `{6379, 6380, 16379}`" and "different `run_id` **and** keys without our owner key → `FOREIGN`" — explicit URL, mandatory AUTH and the restart-vs-FOREIGN split all landed; owner-key lifecycle is still undefined (residual C11, §3).

F8 PARTIAL — R36: "at most `(#required categories × 1) + 1` bulk reads per resolve" versus R37: "A resolve issues ≤ 8 Redis round trips for a single-profile chain and ≤ 2 for a point lookup" — measured `task_profile_requirements` gives **P0_FORE 17 requirement rows** (worst case in the table, and P0_FORE is `DEFAULT_FALLBACK_PROFILE`, `router_spawn.py:93`), so R36's own formula yields 18 reads before the profile/req/levels reads that A4 shows are unbudgeted: the two statements contradict each other on the default profile; R34's "That duplication is measured (R57)" also miscites — R57 is a non-goal ("No per-lane round trips; no JSON blob"), the measurement gates are R50/R51.

F9 PARTIAL — R52: "If `(cold_spawn_wall − floor) < 10 %` **and** point lookups do not improve by ≥5×" — the stub-resolve floor replaced two of the three guessed conditions, but a two-condition AND survives with an undefined second metric (5× of what: p50 of which query, on which corpus, against which file baseline?), so the falsifier still cannot fire unambiguously and still lets the build continue on a leg R2 itself calls non-dominant.

F10 PARTIAL — R49: "Every normative R# in R1–R64 has at least one named assertion in `tests/`" — the coverage claim is keyed to a range that does not exist: measured, the highest normative statement in the file is **R57** (R54-R57, spec:346-350); R58–R64 appear nowhere except this range and the header's "R1…R64" (spec:10), so a checker written to R49 either fails on seven phantom statements or silently passes them; §11's table also maps 17 test rows to ~30 R#s, not one per R#.

F11 PARTIAL — R8: "Every key carries a data-home scope derived from the resolved `DATA_DIR` realpath (`<scope>` = first 12 hex of `sha256(realpath(DATA_DIR))`) … a scratch `ROUTING_DATA_DIR`, or a worktree therefore cannot read or publish each other's plane" — the scope exists, but it is keyed on `DATA_DIR` alone while the artifact leg follows a different resolver (`registry_path.py:89-97`: `ROUTING_REGISTRY` > `TASK_ROUTER_HOME` > `<repo>`), so a hermetic run that sets `ROUTING_REGISTRY` without `ROUTING_DATA_DIR` publishes into the production scope — the exact isolation claim the fix makes (A3).

F12 PARTIAL — R40: "`scripts/sync_runtime.sh` carries a hard-coded file list (two `for f in` loops); `router_redis.py` must be added there" — registration is mandated but "there" is ambiguous between the symlink loop (`sync_runtime.sh:28-36`) and the byte-copy loop (`:65`), and the precedent that decides it is `router_data_cache.py`, which sits in the **copy** loop at `:65` because the cron realpath guard blocks symlinked imports (`:47-64`): picking the wrong loop reproduces the silent fail-open R40 exists to close.

F13 FIXED — R46: "The resolve payload gains `cache: {status, generation, stamp_age_s, scope}` and keeps `source` untouched — TR-046's 'seeded registry.json vs data/tables' answer must not be redefined" — additive field, `source` semantics preserved (cf. `router_spawn.py:3311`).

F14 FIXED — R53: "Any remote/bunker caller benefit is **out of scope** for this topology (loopback instance): no claim about it may appear in docs, evidence, or memory" — the round-1 remote-benefit claim is withdrawn; R44's "fresh or bunker host has an answer" is provisioning, not a benefit claim.

════════════════════════════════════════════════════════════════
2. NEW DEFECTS (severity-ranked)
════════════════════════════════════════════════════════════════

A8a CONFIRMED-DEFECT (highest severity — a fix that broke the state machine)
  Evidence: R8 (spec:59-61) "every stamp records `data_home`, `checkout` (git toplevel + HEAD),
  and `schema`"; R9 (spec:76-77) "Stamps: same object written in three places"; R11 (spec:89)
  "`MATCH` | JSONL stamp == artifact stamp == cache stamp"; R22 (spec:154) "hashes, compares with
  the cache stamp". Whole-object equality over a stamp that carries HEAD means the artifact stamp
  (written by the last seed, at commit N) can never equal a freshly computed JSONL stamp after any
  commit to the repo — including doc/board-only commits that do not touch data/tables. Result:
  permanent `STALE` on a data-identical cache after every commit, cleared only by re-seeding; the
  cron alarm becomes unconditional and therefore ignored, which is worse than having no alarm.
  Change R11 (and R22's comparator).
  Replacement wording: "`MATCH` compares the **table hash and per-table counts only**
  (`stamp.digest` + `stamp.counts`). `data_home`, `checkout`, `schema` and `generated_at` are
  provenance: they are reported in `status --json`, and a difference in any of them is surfaced as
  `provenance_drift` with a warning line — never as `STALE`, and never an input to `load
  --if-stale`'s equality check."

A6 CONFIRMED-DEFECT (kills the value claim outright)
  Evidence: R30 (spec:194-198) "The reader compares the cache stamp's age to
  `ROUTING_REDIS_MAX_AGE_S` (default 900): … past it, the file backend wins"; R22 (spec:154)
  "`load --if-stale` hashes, compares with the cache stamp, and returns 0 **without writing** when
  they match". The stamp only advances when the JSONL changes; the timer runs every 5 minutes and
  writes nothing when the data is stable. So for any dataset that does not change within 15
  minutes — which is the normal case for a seed that runs hourly or daily — every cold process
  declares the cache too old and takes the file path. The plane is therefore off ~all the time,
  by its own arbiter. R14's 7-day gate (spec:107) inverts the same way: a stable dataset prints
  "not advanced in 7 days" forever.
  Change R30 (and R14's gate definition).
  Replacement wording: "The cache's age is **time since the last successful verification**, not
  since the last write: `load --if-stale` refreshes `stamp.verified_at` (a `HSET` on the meta key,
  not a republish) whenever it proves hash equality, and the timer always refreshes it. A reader
  refuses the cache only when `now − verified_at > max(2 × timer period, seed period + timer
  period)`; `ROUTING_REDIS_MAX_AGE_S` defaults to **1800 s** and MUST be ≥ 2 × the timer period,
  asserted by a test. R14's age gate fires on the same `verified_at`, so a stable dataset is
  silent."

A2 CONFIRMED-DEFECT (text-file-as-state, and self-defeating arithmetic)
  Evidence: R28 (spec:183-188) "consult `data/state/redis-health.json` (written by
  `status`/`load`/the timer: `{ok, checked_at, run_id}`). If `checked_at` is older than
  `ROUTING_REDIS_HEALTH_TTL_S` (60) or `ok` is false, the process goes straight to the file
  backend **without attempting a connection**"; R24 (spec:165) "`router-redis-sync.timer` (5 min)
  runs `load --if-stale`". 60 s TTL against a 300 s writer cadence means the health file is stale
  240 of every 300 seconds, so four out of five cold processes skip Redis by design. Beyond that
  arithmetic nothing specifies what makes the file trustworthy: no atomic write (tmp+rename), no
  writer identity, no `run_id` cross-check against R43's instance, no monotonic guard against a
  clock jump, and an `ok:true` left by a process that then died is indistinguishable from a live
  one.
  Change R28.
  Replacement wording: "The health file is `data/state/redis-health.json`, written only by
  tmp-file + `os.replace` (atomic), holding `{ok, checked_at, wall_ms, writer_pid, instance_run_id,
  scope}`. A reader trusts it only when **all** of: `checked_at` is within
  `ROUTING_REDIS_HEALTH_TTL_S`, and `instance_run_id` equals the `run_id` recorded by R43's last
  successful connect, and `scope` equals its own. `ROUTING_REDIS_HEALTH_TTL_S` MUST be ≥ 2 ×
  `ROUTING_REDIS_TIMER_PERIOD_S` (default 660 s against the 300 s timer), asserted by a test;
  when the file is untrusted the reader attempts one bounded connect (R27) instead of refusing,
  because a 20 ms probe is cheaper than a wrongly disabled cache. An `ok:false` record older than
  one timer period is itself an alarm line in `status`."

A4 CONFIRMED-DEFECT (arithmetic contradiction inside the fix)
  Evidence: R36 (spec:240-241) "at most `(#required categories × 1) + 1` bulk reads per resolve";
  R37 (spec:243-245) "≤ 8 Redis round trips for a single-profile chain"; measured
  `task_profile_requirements.jsonl` = P0_FORE 17 rows (next: P9_REVIEW 8, P8_SYNC 8, P6_DEFAULT 8),
  and P0_FORE is the fallback profile (`router_spawn.py:93`). A resolve also needs
  `profile:<id>` and `profile:<id>:req` (R33, spec:220-221) — the requirement set is itself a
  Redis read, so the ordering dependency (pointer → profile req → category scans) is undefined and
  the profile read is not in the budget. Worst case is 17 + 1 + 2 + 1 + up to 17 `levels:<category>`
  reads = ~38 round trips against a pinned 8.
  Change R36/R37 (and add the ordering to R33).
  Replacement wording: "A resolve's read order is fixed and pipelined: (1) `GET
  <scope>:meta:version`, (2) **one pipeline** containing `HGETALL profile:<id>`, `HGETALL
  profile:<id>:req`, and for every required category `HGETALL cat:<category>` + `GET
  levels:<category>`, issued in a single write-then-read batch on one connection. The budget is
  therefore **≤ 2 round trips** (pointer, then pipeline) for any chain resolve and **≤ 2** for a
  point lookup, independent of the number of required categories — measured on P0_FORE (17
  categories). The test counts round trips of the client, not commands issued, and a chain resolve
  that exceeds 2 round trips fails the contract."

A1 CONFIRMED-DEFECT (WATCH affinity + uncollectable generations)
  Evidence: R20 (spec:142-147) lists INCR, the write pass, and `WATCH … MULTI; SET …; EXEC` as
  independent steps and never says they share a connection — WATCH is per-connection, so on a
  pooled or reconnecting client the watch is silently dropped and EXEC succeeds unguarded (the
  exact regression class R20 was written to kill); and a process that dies between step 2 and
  step 3 leaves a full generation whose pointer never moved, while step 4's GC is "generations
  older than `gen-2`", which can never match the newest abandoned generation. With R42's
  `maxmemory 256mb` + `maxmemory-policy noeviction`, leaked generations accumulate until writes
  fail: every later publish dies (R23 records it in a JSONL file nobody reads) and the failure is
  permanent until a human flushes.
  Change R20 (add R20a).
  Replacement wording: "**R20a. Connection affinity and leak collection.** The `INCR`, the
  guarded read, the `WATCH`, the `MULTI/EXEC` and the abort path MUST execute on ONE connection
  held for the whole publish (a dedicated, non-pooled socket); a reconnect during publish aborts
  it (exit 4). Step 3 writes `<scope>:meta:inflight` = `{gen, started_at, pid}` **before** the
  write pass and deletes it after `EXEC` or the abort. GC reclaims, at every publish, (a)
  generations older than `gen-2` and (b) any generation named by an `inflight` record whose
  `started_at` is older than `2 × ROUTING_REDIS_PUBLISH_STALE_S`, by `SCAN <scope>:g<M>:*` +
  `DEL`; a publish that finds its own `gen` missing is a hard error, not a retry."

A3 CONFIRMED-DEFECT (scope identity is the wrong identity)
  Evidence: R8 (spec:57-61) keys scope on `realpath(DATA_DIR)` only, while `DATA_DIR` is
  `ROUTING_DATA_DIR` or `<repo>/data/tables` (`router_spawn.py:166`) and the artifact leg is
  resolved by a different precedence — `ROUTING_REGISTRY` > `TASK_ROUTER_HOME` > `<repo>` >
  XDG (`registry_path.py:89-97`). A hermetic/scratch run that sets only `ROUTING_REGISTRY`
  (the documented test idiom, `registry_path.py:21-23`) therefore keeps the production scope and
  publishes a different artifact stamp into it. Conversely a moved or symlinked data home silently
  re-scopes: readers get `EMPTY` (exit 2), which is indistinguishable from "never loaded / flushed
  / restarted empty" (R11, spec:91), and the old scope's keys are never reclaimed because R20.4's
  GC is generation-scoped inside one scope — there is no scope GC anywhere.
  Change R8 (and R11's EMPTY row).
  Replacement wording: "**R8a. Scope identity.** `<scope>` = first 12 hex of
  `sha256(canonical_ref(DATA_DIR) + '|' + canonical_ref(REGISTRY))`, where `canonical_ref` is the
  `realpath` of the existing parent directory plus the basename. Both the data dir and the
  registry path are inputs, so `ROUTING_REGISTRY` alone cannot land in another plane. Every key
  also carries `<scope>`; `status` enumerates sibling scopes with one `SCAN
  taskrouter:scope:*` and reports, per scope, `{data_home, last_publish_at, generation}`.
  Re-scoping an existing data home is `EMPTY` **plus** a `re-scoped` note naming the previous
  scope and its last publish time; a scope unpublished for 30 days is a `stale_scope` line in
  `status` (never auto-deleted)."

A5 CONFIRMED-DEFECT (the day-one state exits 0)
  Evidence: R11 (spec:96) "`UNSTAMPED` | `registry.json` carries no `stamp` (today's artifact —
  measured: no `stamp` key); compare JSONL↔cache only and report the artifact leg as degraded |
  **0 if JSONL↔cache agree**, else 1". Measured `registry.json` top keys =
  `generated_at, source, tables, version`. So every install starts in a state where the artifact
  leg is unverified and the cron stays silent — while R29 (spec:190) makes `registry.json` the
  serving fallback, i.e. the reader falls back to precisely the file whose agreement was never
  checked. Green cron, stale file path.
  Change R11's UNSTAMPED row.
  Replacement wording: "`UNSTAMPED` | `registry.json` carries no `stamp`: compare JSONL↔cache and
  report the artifact leg as **degraded** | **1**, with one line: `cache UNSTAMPED: registry.json
  has no stamp — artifact leg unverified; run the seed`. `status --json` sets
  `artifact_verified=false`. `MATCH` is never reported while `artifact_verified=false`; the
  control is `ROUTER_REDIS_ALLOW_UNSTAMPED=1` for the transition window only, and it prints the
  same line."

A8b CONFIRMED-DEFECT (row-shaping mutation vs full-row diff)
  Evidence: R16 (spec:125-127) "Redis stores the **full row** (models: all 39 columns), so the diff
  compares full rows"; but every load runs `_stamp_payg_providers` (`router_spawn.py:1254, 1261,
  1285`), which sets `tables['_payg_providers']` (a Python `set`, `:1158`) and injects a boolean
  `_payg` into every model row (`:1159-1161`). Publishing "the same in-memory set" (R10) therefore
  ships 40 fields including `_payg`, while JSONL has 39: every model row diffs `~changed` on every
  run, and the stamped set is not JSON-serializable at all.
  Change R16/R31.
  Replacement wording: "Rows are published **pre-stamp**: `_payg` and `_payg_providers` are
  reader-side derivations and MUST NOT appear in any published key, in the diff, or in the hash.
  A test asserts that every published model hash has exactly the 39 JSONL columns (measured today:
  39) and that no key beginning `_` is written to Redis."

A8c CONFIRMED-DEFECT (R33 publishes columns that do not exist)
  Evidence: R33 (spec:222) "`<scope>:g<N>:leveldef:<level>` | hash | `lo`, `hi` (the `level_defs`
  rows the tier math reads)"; measured `level_defs.jsonl` columns = `{label, level, qcol}` — no
  `lo`, no `hi`. The tier math derives them: `lo, hi = (min(lv), max(lv))` over
  `level_defs.level` (`router_spawn.py:1099-1101`). A per-level hash keyed by `<level>` that
  contains the global `lo`/`hi` also duplicates the range on every key, and forces the reader to
  synthesize a value — breaching R31's "no routing rule is re-implemented against Redis".
  Change R33.
  Replacement wording: "`<scope>:g<N>:leveldef:<level>` | hash | the `level_defs` row verbatim
  (`label`, `level`, `qcol`), plus one `<scope>:g<N>:levelrange` string (JSON) = `{lo, hi}` = the
  published `min/max(level_defs.level)`; the reader reads the range once per resolve and MUST NOT
  compute it."

A8d CONFIRMED-DEFECT (field-key ambiguity on real data)
  Evidence: R33 (spec:217, 228) keys model facts as `<provider>/<model>` and category scans as
  `hash: <provider>/<model> → level`. Measured: **1,073 of 2,064** model ids contain `/`
  (`MiniMaxAI/MiniMax-M2.5`, `stealth/union-alpha`, `accounts/fireworks/models/...`). Splitting
  the hash field on the first `/` yields the wrong provider/model pair; a `provider` that itself
  contains `/` (vendor-prefixed lanes) makes it unresolvable.
  Change R33.
  Replacement wording: "Composite identities are delimited by a character that cannot appear in
  either half: `<provider>\\u001f<model>` for hash fields, and `HGETALL` results are split on
  `\\u001f` only. A test parses every one of the 2,064 published model ids round-trip and asserts
  field-count == 2; `/` is never a delimiter."

A7 CONFIRMED-DEFECT (the coverage claim is partly vacuous)
  Evidence: R49 (spec:302-304) "Every normative R# in R1–R64 has at least one named assertion in
  `tests/`; the suite fails if a statement is added without one". Measured: the highest normative
  statement is R57 — R58–R64 do not exist (only the range text at spec:10 and :302 mentions them).
  And several statements cannot be asserted by a test at all: R3 ("must not be sold that way in
  docs, board events, or memory"), R53 ("no claim about it may appear in docs, evidence, or
  memory"), R54 ("Not a write plane, message bus, distributed lock, or quota store" — assertable
  only as absence-of-code, i.e. a grep, not behavior), R1/R2 (measured baselines, not behavior),
  R55 (only via a golden-parity proxy).
  Change R49 (and the header at spec:10).
  Replacement wording: "Every normative R# in **R1–R57** carries one of three named artifacts in
  the coverage table in `tests/test_redis_read_plane.py`: `behavior` (a test that fails when the
  statement is violated), `grep` (an assertion over the source tree — used for absence claims:
  R7 no locks, R54 no write plane, R5 no write verb), or `doc-check` (an assertion over
  `docs/` + the board JSONL that no forbidden claim exists — R3, R53). The checker fails when an
  R# has none of the three, when the spec's maximum R# and the table's maximum disagree, or when a
  `behavior` row names a test that does not exist. A statement added without an artifact breaks
  the suite."

A8e CONFIRMED-DEFECT (smaller, still silent)
  (i) R33/R38 never define NULL encoding, but `models` is full of nullable numeric columns
  (`normalized_price`, `public_price`, `context_limit` are NULL on real rows — 2,064 rows carry
  the column, values are not all present) and Redis has no nil field: `HGETALL` returns `''`,
  and the reader's `float('')`/`tier >= level` path raises where the file path compared `None`.
  Change R33: "A NULL column is published as the sentinel `\\x00null` and decoded back to `None`
  on read; `''` and absent are distinct and MUST NOT be conflated."
  (ii) R11's exit 4/5 (GHOST/FOREIGN) are reused by R20/R23 for publish-abort/publish-error
  (spec:93-94 vs 158-159) — a cron keying on exit code alone cannot tell "status found a ghost"
  from "publish aborted". Change R23: "publish exits are **10** (aborted by CAS) and **11**
  (publish error); 0–6 are reserved for `status`."
  (iii) R21 says a miss "re-resolves from the file backend" but never says whether the pointer is
  re-read: a long-lived `router_server` whose pinned generation was GC'd (R20.4) stays on the file
  path for the rest of its life, and R26's "the server on its next cache-generation boundary" is
  never defined. Change R21: "on a miss the reader UNWATCHes nothing, re-reads the pointer exactly
  once per request, and re-pins if the generation changed; the pointer read is the same 1 round
  trip already budgeted in R37."
  (iv) R29's fallback order under `ROUTER_CACHE=0` and `ROUTING_REDIS=0` is not defined together
  (R32:204-207 gives two switches and one meaning): an operator who sets `ROUTER_CACHE=0` to debug
  the file path may still be served by Redis. Change R32: "`ROUTER_CACHE=0` wins over
  `ROUTING_REDIS=1`: the Redis arm is disabled whenever `ROUTER_CACHE=0`, and `status` prints
  `cache: redis disabled by ROUTER_CACHE=0`."

NOT-A-DEFECT notes (checked, no finding)
  - R15's corrected keys: verified against the files, unique where they exist (`fallback_lanes`
    order has 0 duplicates across 7 rows).
  - `model_perf`/`model_tier` genuinely have no `provider` column; R15 is right there.
  - R16's 39 model columns: counted 39 in `models.jsonl`.
  - R34's duplication claim is real; only its R57 citation is wrong (see F8).

UNRESOLVED (needs a write or a second machine; not asserted)
  - R43's restart-vs-FOREIGN behaviour against a real restarted instance (read-only review).
  - Actual `run_id` stability and `DBSIZE` behaviour on this host: no `redis-server`/`redis-cli`
    installed and no instance on 6391, so `INFO server.run_id` could not be sampled.
  - The R20 CAS race itself (would need two publishers writing).

════════════════════════════════════════════════════════════════
3. RESIDUAL ROUND-1 ITEMS
════════════════════════════════════════════════════════════════

1. C13/R49 — phantom statement range. Round-1 counted untested R#s mechanically; the fix now
   claims one test per R# over a range (R1–R64) that does not exist (max R57). The mechanical
   claim regressed from "wrong count" to "checkable against nothing".
2. C11/R43 — the owner key has no lifecycle. R25 and R43 both require "our owner key" but no
   statement says who writes it, when, or with what idempotence. A publish that crashes before
   writing it makes our own keys look FOREIGN on the next connect, and the recovery path is
   `flush --yes`, which itself requires the owner key to match — a deadlock.
3. C11/R43 — the denylist is port-literal only. A URL without an explicit port
   (`redis://127.0.0.1/0`) resolves to 6379 and the guard as written ("refuse any port in the
   denylist") does not say it applies to the *defaulted* port; the same Redis is reachable by a
   different path (host alias, IPv6, `unixsocket`) and is not denied.
5. C5/R11 — MATCH still does not verify key completeness. `MATCH` requires "the pointer's
   generation is present" (presence, not completeness) and `GHOST` fires only when the
   generation's keys are *gone*; a partially evicted/manually deleted key set satisfies neither,
   so `status` can report MATCH while resolves miss and silently fall back (R21). The round-1 ask
   ("a stamp match with missing keys is INCOMPLETE") is not implemented as a state — it survives
   only as GHOST, which is strictly narrower.
6. C6/R15 — three cached tables still have no declared diff key (`archetypes`, `benchmarks`,
   `level_defs`), so R17's `+added −removed ~changed` per table is undefined for them.
7. C7/R26+R20.4 — retention vs process-lifetime pin is still unresolved in the text: "keep the
   previous two" generations against a pin that lasts "that process's lifetime" (R26) and an
   undefined re-pin rule (A8e iii). Round-1 raised the 60 s pin; the pin got longer, not bounded.
8. C10/R40 — the registration gap is real but unpinned: "added there" does not name the byte-copy
   loop that `router_data_cache.py` needs for the cron realpath guard.
9. C3/AC2 — R46's `cache:{generation, stamp_age_s}` is close but not AC2: `router_data_cache.py`
   AC2 (lines 21-27) requires `cache_generation`, `cache_age_s`, `cache_stale`,
   `cache_reload_pending` on **every served meta**, and R46 puts three fields on the resolve
   payload only.
10. C14/R52 — value claim still survives its own falsification through an undefined second leg
    (F9); the round-1 correction asked for one floor measurement, and a second unmeasurable
    condition was added beside it.

════════════════════════════════════════════════════════════════
4. TOP 5 REMAINING RISKS FOR IMPLEMENTATION
════════════════════════════════════════════════════════════════

1. The falsifier can fire falsely, and that is the worst outcome available. With A2's 60 s health
   TTL against a 300 s writer and A6's 900 s max-age against an idempotent no-write load, the
   Redis arm is bypassed almost always; R52's floor measurement would then measure the file path,
   "prove" the plane worthless, and close the row on a measurement that never exercised the
   design. Fix A2/A6 before measuring, or the number is worthless.
2. Permanent-STALE alarm fatigue (A8a) plus R12's missing in-flight marker: the status cron will
   cry on every commit and go quiet nowhere, and the operator's rational response — ignore it —
   is the failure mode the whole spec exists to prevent.
3. The round-trip budget is impossible for the default profile (P0_FORE, 17 categories, A4), so
   implementation will either bust R37 on day one or pipeline and quietly redefine "round trip" —
   and the counting test will then assert whatever the implementation happens to do.
4. Aborted-publish generations are uncollectable under `noeviction` (A1): the plane degrades from
   "cache miss" to "publish permanently fails", with the only record in a JSONL file no component
   reads, and the recovery is a manual `flush` that itself needs the owner key.
5. Scope identity is keyed on the wrong input (A3): a hermetic `ROUTING_REGISTRY` run writes into
   the production plane, and a moved/symlinked data home silently orphans a plane while reporting
   `EMPTY` — "safe" per R11, indistinguishable from a fresh install, and invisible to every
   consumer.

════════════════════════════════════════════════════════════════
5. TRUNCATION DETECTOR
════════════════════════════════════════════════════════════════

PANEL REVIEW COMPLETE

session_id: local-f990e7a8100d6a9d8991c2ea4b4c8cc1deb589cfbda4108912bf5b9c3c0fa17a

│ cache/scratch/tr388-r2-probe.py
│ @@ -0,0 +1,26 @@
│ +#!/usr/bin/env python3
│ +"""TR-388 round-2 probe (read-only): registry keys, table columns, profile req counts."""
│ +import json, os, collections, statistics
│ +REPO = '/home/kara/task-router'
│ +TD = os.path.join(REPO, 'data', 'tables')
│ +d = json.load(open(os.path.join(REPO, 'registry.json')))
│ +print('registry top keys:', sorted(d.keys()), '| has stamp:', 'stamp' in d)
│ +files = sorted(f for f in os.listdir(TD) if f.endswith('.jsonl'))
│ +print('jsonl files in data/tables:', len(files))
│ +for name in ('model_perf', 'model_tier', 'fallback_lanes', 'models'):
│ +    with open(os.path.join(TD, name + '.jsonl')) as f:
│ +        r = json.loads(f.readline())
│ +    print(name, 'ncols:', len(r), 'cols:', sorted(r.keys()))
│ +reqs = collections.defaultdict(set)
│ +with open(os.path.join(TD, 'task_profile_requirements.jsonl')) as f:
│ +    for line in f:
│ +        line = line.strip()
│ +        if not line:
│ +            continue
│ +        r = json.loads(line)
│ +        reqs[r['task_id']].add(r['category'])
│ +counts = sorted(((len(v), k) for k, v in reqs.items()), reverse=True)
│ +vals = [len(v) for v in reqs.values()]
│ +print('profiles:', len(reqs), 'max req cats:', max(vals),
│ +      'mean:', round(statistics.mean(vals), 2))
│ +print('top:', counts[:12])
All evidence gathered. Verification commands used: 3 of 3 (grep of router_spawn.py/sync_runtime.sh; staged probe script over registry.json + data/tables; the probe run). Two heredoc probes were gateway-blocked and replaced by the staged-script run — no repo or system state touched.

1. FIX VERDICTS

F1 FIXED — R9 (spec:65-77): "take the sorted list of line strings preserving duplicates", aggregate "sha256(\"\\n\".join(f\"{name}:{hash}:{count}\" ...))" over "the 12 artifact tables + fallback_lanes = 13 named tables" with "not_cached: [...]" — multiplicity, count-as-input, explicit set, and exclusion list all present.

F2 FIXED — R11 (spec:85-96) adds GHOST / FOREIGN / SCHEMA-AHEAD / UNSTAMPED rows and the `cache-ahead` drift class inside STALE ("a git rollback is a real case"); R12 (spec:98-100) defines publish-in-flight with the 120 s `ROUTING_REDIS_PUBLISH_STALE_S` threshold.

F3 FIXED — R15 (spec:111-123) keys match my measured columns exactly: `model_perf (model, category)` (verified 5 cols, no `provider`), `model_tier (model, category)` (6 cols), `fallback_lanes (order)` (cols `{key_env, model, note, order, provider}`); R16 (spec:125-127) compares full rows, models verified at 39 columns.

F4 FIXED — R20 (spec:140-147): "gen = INCR … WATCH <scope>:meta:version → if current generation > gen, abort … MULTI; SET; EXEC — the pointer can never regress", plus "GC generations older than gen-2 (keep the previous two)"; R21 (spec:149-152) whole-resolve fallback, "never assembled from mixed generations".

F5 FIXED — R10 (spec:79-83): "load parses the JSONL into memory, hashes that in-memory set, and publishes that same in-memory set. There is no re-read window"; R23 (spec:157-161) the `data/state/redis-publish-errors.jsonl` record and exits 4/5.

F6 FIXED — R32 (spec:204-207) names `ROUTER_CACHE=0` — verified real at router_spawn.py:238,252 (`os.environ.get('ROUTER_CACHE', '1')`); R27 20 ms (spec:179-181); R28 out-of-process health file (spec:183-188); R30 `ROUTING_REDIS_MAX_AGE_S` 900 (spec:194-198).

F7 FIXED — R42 (spec:266-270) explicit `redis://127.0.0.1:6391/0`, `requirepass` from a mode-600 secret file; R43 (spec:272-282) ordered guards — denylist `{6379, 6380, 16379}`, AUTH refusal, and the restart-vs-FOREIGN split keyed on our owner key.

F8 FIXED — R33 (spec:211-231) the `cat:<category>` scan shape; R36 (spec:240-241) "(#required categories × 1) + 1 bulk reads per resolve, never one read per candidate lane"; R37 (spec:243-245) the ≤8/≤2 round-trip budget pinned by test; R34 (spec:233-234) duplication declared and measured. (Text landed; whether the budget survives reality is A4 below.)

F9 FIXED — R52 (spec:333-338): "Measure a stub resolve that returns a canned chain with no registry read: that is the absolute floor" — the AND-of-three-impossible-conditions construction is gone; the residual two-leg stop rule (cold-spawn gain <10 % AND point lookups <5×) can actually fire, since both legs are independent value claims in R2.

F10 FIXED — R49 (spec:302-304): "Every normative R# in R1–R64 has at least one named assertion in tests/ … a table of R# → test … and a checker asserts coverage", backed by the 17-row matrix (spec:306-324). (Whether the claim is vacuous for untestable R#s is A7.)

F11 FIXED — R8 (spec:57-61): "<scope> = first 12 hex of sha256(realpath(DATA_DIR))", stamp records `data_home`, `checkout` (git toplevel + HEAD), `schema`; scratch scopes "are invisible to production". (Scope-change side effects are A3.)

F12 FIXED — R40 (spec:256-259) names the two hard-coded `for f in` loops and mandates registration plus a test. Verified the loops exist at sync_runtime.sh:28 and :65 and `router_redis.py` is in neither — the spec statement accurately describes the deliverable's missing step.

F13 FIXED — R46 (spec:291-293): "The resolve payload gains cache: {status, generation, stamp_age_s, scope} and keeps source untouched — TR-046's … answer must not be redefined."

F14 FIXED — R53 (spec:340-342): "Any remote/bunker caller benefit is out of scope for this topology (loopback instance): no claim about it may appear in docs, evidence, or memory until a follow-up row defines the topology."

2. NEW DEFECTS (ranked by severity)

A6 CONFIRMED-DEFECT (highest — silently nullifies the entire feature in steady state) — R30 compares "the cache stamp's age" to MAX_AGE 900, but R22 makes `load --if-stale` write nothing when hashes match. The stamp therefore only advances when table CONTENT changes. A stable registry (the common case — seeds are change-driven) crosses 900 s fifteen minutes after the last content change, and from then on every reader takes the file backend forever: the Redis plane is built, healthy, correct, and never served. Change R30 to compare against a verification timestamp, not a content timestamp, and change R22 to maintain it. Exact replacement wording:
  R22 append: "When hashes match, load --if-stale rewrites ONLY meta:version's `verified_at` field (a touch, not a republish — generation and data keys unchanged); this is not a write for idempotence purposes."
  R30 replace the first sentence with: "The reader compares `now − verified_at` (the last moment the cache was hash-verified against the JSONL) to ROUTING_REDIS_MAX_AGE_S (default 900). Content age is irrelevant: a cache whose content has not changed in a week but which was verified 60 s ago is fresh."

A4 CONFIRMED-DEFECT (the round-trip budget is contradicted by measured data) — I measured `task_profile_requirements.jsonl`: 11 profiles, worst case P0_FORE = 17 required categories, mean 6.55, four profiles at ≥7. R36's own formula gives P0_FORE `(17 × 1) + 1` = 18 reads; R37 caps a chain resolve at ≤8 round trips. The spec never says category scans are pipelined (R38 lists pipelining only as a client capability), and the profile read (`profile:<id>` + `profile:<id>:req`, two separate keys per R33) is a serial dependency that must complete BEFORE the scan keys are even known, so it cannot fold into the scan batch. As written, the R49 budget test fails on the shipped data for at least P0_FORE. Change R36/R37 and the R49 budget row. Exact replacement wording:
  R36: "A resolve is exactly three pipelined round trips: (1) MGET profile:<id> + profile:<id>:req; (2) one pipeline of MGET/HGETALL across every required category's cat:<category> plus levels:<category>; (3) point reads for any model/provider hashes the chain math needs. Measured worst case on 2026-11-04 data: P0_FORE with 17 required categories — 19 commands in 3 round trips."
  R37: "A resolve issues ≤ 3 Redis round trips for a single-profile chain and ≤ 2 for a point lookup, with round trip = one pipeline flush; a test with a counting client asserts both, run against the P0_FORE profile (the measured worst case, 17 categories)."

A1 CONFIRMED-DEFECT (CAS silently voidable + abandoned-generation collection is publish-dependent) — (a) R20 never states that WATCH, the pointer read, and MULTI/EXEC run on ONE connection; WATCH is per-connection, so an implementation that EXECs on a different connection than it WATCHed has a CAS that never fires — exactly the pointer-regression class C7 fixed, reintroduced by omission. (b) If the process dies between step 2 (data keys written) and step 3 (CAS), generation N is orphaned; R20's GC runs only inside a publish and keeps gen-2, so the orphan survives until three subsequent SUCCESSFUL publishes — but R22 means a quiescent system (tables unchanged) never publishes again, so the orphan persists indefinitely inside a 256 mb noeviction instance, where it eventually blocks all writes. Exact replacement wording:
  R20 step 3 prepend: "The WATCH, the generation comparison, and MULTI/EXEC execute on a single dedicated connection held for the whole publish; a client that cannot guarantee this must use a Lua CAS instead."
  R20 step 4 replace with: "GC removes (i) generations older than gen-2 and (ii) any generation newer than the pointer's generation that has no in-flight marker younger than ROUTING_REDIS_PUBLISH_STALE_S — so a crashed publisher's abandoned generation is collected by the next load --if-stale even when no publish occurs."

A2 CONFIRMED-DEFECT (health file has no integrity story, and one error latches the plane off fleet-wide) — R28 introduces `data/state/redis-health.json` with three writers (`status`/`load`/timer) and no atomic-write requirement: a torn write during a crash makes the file unparseable, and the spec doesn't say what a reader does with corrupt JSON (presumably miss-to-file, but unstated). Worse, R41 makes a single `-ERR` in ANY process flip the shared file to `ok:false` — one transient error takes the plane down for every process on the host for 60 s, a new shared-fate failure class v0.2 introduced. The stale `ok:true` case is acceptably bounded (TTL 60 s, then one 20 ms timeout per process). Exact replacement wording:
  R28 append: "The file is written atomically (tmp + rename in the same directory); an unparseable or schema-mismatched file is treated as a miss (file backend, no connection attempt). Only load/status/timer write ok:false; the read path NEVER writes the file — a read-path -ERR or timeout is a per-process miss only, so one process's transient error cannot latch the plane off for the host."

A3 CONFIRMED-DEFECT (scope-change orphans vs noeviction; the safe direction is verified, the unsafe direction is unhandled) — Verified the safe half: a reader whose scope has no keys sees EMPTY (R11, exit 2) and falls back to file — never the wrong plane, since scope collision requires an identical realpath. The unhandled half: a checkout move, a `TASK_ROUTER_HOME` change, or a symlink retarget orphans the old scope's keys; R25's flush touches only `<scope>:*`, nothing cross-GCs scopes, and R42 sets `maxmemory-policy noeviction` — so orphaned scopes accumulate until the 256 mb instance refuses ALL writes, including the live scope's, and every publish becomes an R23 record with no named cause. Exact replacement wording:
  R8 append: "A scope change orphans the previous scope's keys. Because R42 sets noeviction, orphan accumulation is a write-blocking event: router redis status MUST warn when instance used_memory exceeds 50 % with keys outside the caller's scope present, and flush --yes gains --all-scopes for exactly this operator action."

A5 CONFIRMED-DEFECT (low — named but unbounded) — Verified: registry.json top keys today are `{generated_at, source, tables, version}`, no `stamp`, so every install is UNSTAMPED until the seed change lands. R11 does name the state and its exit rule, and R14's "one actionable line otherwise" covers it — but there is no sunset: if the seed change never lands (or silently fails — R24 keeps the seed exit 0 on publish failure), the artifact leg stays unverified FOREVER while status exits 0, and the cron line becomes noise operators filter. Exact replacement wording:
  R11 UNSTAMPED row append: "UNSTAMPED is a migration bridge, not a steady state: the first seed run that writes a stamp records `stamped_since` in meta:version, and any registry.json older than the first stamped seed re-raises UNSTAMPED as exit 1 (artifact-behind), not exit 0."

A7 CONFIRMED-DEFECT (wording-level, but it decides whether R49 is a gate or a garnish) — Enumerated R#s a test cannot assert: R1–R3 (problem framing/value honesty), R7 (no locks — only weakly grep-able), R45 (disposable instance — an ops property), R52 (the falsifier is a procedure, not an assertion), R53 (a docs/memory gag rule), R54–R56 (non-goals; only R56's no-dependency leg is covered, by R39). Under the current phrasing the coverage checker either forces vacuous asserts ("assert True  # R3") or fails on unaddable statements. Exact replacement wording:
  R49 replace the first sentence with: "Every normative R# in R1–R64 has at least one named gate in tests/ or in the spec's own inspection column: an automated assertion where one is possible, and a named manual/inspection gate (docs review, ops checklist, measurement procedure) where it is not; the checker fails the suite if any R# maps to no gate or to a gate of unstated type."

A8 CONFIRMED-DEFECT (two further silent surfaces; one UNRESOLVED sub-item) —
  (a) R9's embedded artifact stamp has a write-ordering hole: round-1 measured (seat 2, still current) that the seed writes registry.json FIRST and syncs data/tables FROM it — but R9 requires the stamp inside registry.json to be computed from the JSONL tables. That forces a two-pass write (registry → tables → hash → rewrite registry with stamp), which the spec never states; a naive one-pass implementation embeds a stamp of the PREVIOUS tables and every status is STALE-artifact-behind forever. Exact replacement wording, R9 stamps bullet append: "The seed writes registry.json without stamp, syncs data/tables, hashes the tables, then rewrites registry.json adding only the stamp key (a stamp-only rewrite does not dirty the tables, which derive from non-stamp content)."
  (b) R43 guard 3 is still trust-on-first-use: "on first connect record INFO server.run_id" blesses whatever answers on 6391 first; during provisioning, any local process that binds 6391 before the real instance is fingerprint-blessed and the read plane then serves its data. Loopback-only blasts radius, but silent. Replacement: "recorded on first connect ONLY when the instance holds no data (DBSIZE 0); a first connect to a non-empty instance without our owner key is FOREIGN, exit 5."
  (c) UNRESOLVED — R26 says a long-lived server picks up new generations "on its next cache-generation boundary" without defining that boundary for router_server; a server that pins a generation for days is saved only by GHOST-after-GC, i.e. by failing. Needs a write to exercise; marking UNRESOLVED per the budget rules.

3. RESIDUAL ROUND-1 ITEMS

- C11/round-1 correction 7 (explicit `bless` instead of first-connect recording): only partially adopted — the restart-vs-FOREIGN split landed, but first-connect blessing is still unconditional (see A8b). PARTIAL.
- C3 seat-2 composition gap (how the Redis layer composes inside long-lived router_server / TR-277 AC6's consumer): R26 gestures at it ("the server on its next cache-generation boundary") but never defines the boundary. PARTIAL — carried into A8c.
- C12 seat-2 provisioning gap (round-1 correction 10: name ALL provisioned units): R44 covers the container/unit, R24 names the timer, but the R47 `no_agent` status cron's provisioning is named nowhere. PARTIAL, minor.
- C8 seat-2 exit-code contradiction (seed-triggered publish abort: exit 4 vs seed exit 0): resolved — R24 now says the seed exits 0 AND writes the R23 record while direct load exits non-zero. CLOSED.
- All other round-1 items are closed by F1–F14 above; nothing was waved through a second time.

4. TOP 5 REMAINING RISKS FOR IMPLEMENTATION

1. Steady-state inertness (A6): MAX_AGE keyed to content age + idempotent no-write means the plane is never served 15 minutes after the last table change — the feature ships and silently does nothing. Fix before any implementation; it is a two-field change (verified_at touch).
2. Budget contradiction (A4): the R37 ≤8 budget is falsified by today's data (P0_FORE = 17 categories, measured) unless reads are redefined as pipelined round trips; if implemented naively the R49 contract test fails on day one or, worse, gets quietly loosened.
3. CAS connection discipline + orphan GC (A1): a stdlib client makes the WATCH-on-one-connection mistake easy, and crashed-publisher generations collect only if publishes keep happening — under noeviction this is a write-blocker, not a leak.
4. Health file as shared mutable state (A2/A8): torn writes unspecified, and one process's transient -ERR latches the plane off host-wide for 60 s — a derived-text-file failure class of exactly the kind this quorum exists to catch.
5. Scope-change orphans + TOFU blessing (A3/A8b): any data-home move strands keys in a noeviction instance until writes block, and the first connect on 6391 still blesses whatever answers — both silent, both discovered only when routing output looks wrong.

PANEL REVIEW COMPLETE

session_id: local-526ddaa8fb92acccb1534eff7dd9531d63d49aede6bf16ac3eb100798f70200b

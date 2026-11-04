│ cache/scratch/tr388-probe.py
│ @@ -0,0 +1,54 @@
│ +#!/usr/bin/env python3
│ +"""TR-388 quorum probe (read-only): registry parse timing + R15 diff-key uniqueness."""
│ +import json, time, os
│ +from collections import Counter
│ +
│ +REPO = '/home/kara/task-router'
│ +TD = os.path.join(REPO, 'data', 'tables')
│ +
│ +# --- baseline parse timing (3 runs) ---
│ +for i in range(3):
│ +    t = time.perf_counter()
│ +    with open(os.path.join(REPO, 'registry.json')) as f:
│ +        d = json.load(f)
│ +    print(f'json.load run{i+1}: {(time.perf_counter()-t)*1000:.1f} ms')
│ +
│ +def rows(name):
│ +    out = []
│ +    with open(os.path.join(TD, name + '.jsonl')) as f:
│ +        for line in f:
│ +            line = line.strip()
│ +            if line:
│ +                out.append(json.loads(line))
│ +    return out
│ +
│ +KEYS = {
│ +    'models': lambda r: (r.get('provider'), r.get('model')),
│ +    'model_perf': lambda r: (r.get('provider'), r.get('model'), r.get('category')),
│ +    'model_tier': lambda r: (r.get('provider'), r.get('model'), r.get('category')),
│ +    'category_levels': lambda r: (r.get('category'), r.get('level')),
│ +    'task_profiles': lambda r: r.get('id'),
│ +    'task_profile_requirements': lambda r: (r.get('task_id'), r.get('category')),
│ +    'projects': lambda r: r.get('id'),
│ +    'providers': lambda r: r.get('id'),
│ +}
│ +for name, kf in KEYS.items():
│ +    try:
│ +        rs = rows(name)
│ +    except FileNotFoundError:
│ +        print(f'{name}: FILE MISSING'); continue
│ +    keys = [kf(r) for r in rs]
│ +    dup = {k: c for k, c in Counter(keys).items() if c > 1}
│ +    print(f'{name}: rows={len(rs)} unique_keys={len(set(keys))} dup_keys={len(dup)}' +
│ +          (f' examples={list(dup.items())[:5]}' if dup else ''))
│ +
│ +# duplicate whole lines per table (R9 sorted-set blind spot probe)
│ +for fn in sorted(os.listdir(TD)):
│ +    if not fn.endswith('.jsonl'):
│ +        continue
│ +    with open(os.path.join(TD, fn)) as f:
│ +        lines = [l.rstrip() for l in f if l.strip()]
│ +    n_dup_lines = len(lines) - len(set(lines))
│ +    if n_dup_lines:
│ +        print(f'DUP-LINES {fn}: {n_dup_lines} identical duplicate line(s)')
│ +print('dup-line scan complete')
All three verification commands spent. Composing the mandated review output.

VERDICTS

C1 CONFIRMED — SPEC §2 R4-R7 and §12 R56-R58 contain no Redis→JSONL/registry write path and R52 (spec line 298) tests "no write verbs in router_spawn.py"; every key in §7 traces to a data/tables table. R22#1's seed publish is JSONL→Redis only, which R5 permits.

C2 CONFIRMED — measured: registry.json 4,907,215 B = 4,792 KiB, 12 tables (jq); models 2064 / model_perf 3944 / model_tier 3896 / benchmarks 2148 / projects 950 / category_levels 264 / task_profile_requirements 72 all match the spec table exactly; json.load 24.3-41.9 ms (3 runs) vs claimed ~40 ms. Spawn timing 0.28-0.31 s / 73 MB RSS not re-run (budget cap) but plausible: 40 ms parse is ~13% of 300 ms wall.

C3 CONFIRMED — router_data_cache.py:8-50 (AC0-AC6: identity invalidation, single-flight, "a reload NEVER empties a table", served meta) is real and consumed by router_spawn.py:1199-1232; R29's fallback order inserts Redis ABOVE the TR-277 cache without bypassing it, and R30's serve-stale-with-warning mirrors AC2's serve-last-good semantics. One clarity gap: the spec never says how the Redis layer composes inside long-lived router_server (AC6's _read_jsonl consumer) — only R26's 60 s pointer TTL gestures at it.

C4 CONTRADICTED — R9's sorted SET of lines (spec:76-77) misses two real change classes the spec does not name: (1) duplicate-row count changes (identical lines collapse in the set; measured live today: model_notes.jsonl contains 3 identical duplicate lines, so deleting one changes nothing in the hash); (2) file reorder (immune by construction). Worse, spec:81-83 misdescribes its own algorithm: "the cost is that pure formatting churn counts as a change" is false — trailing-whitespace and reorder churn are exactly what the normalisation hides.

C5 CONTRADICTED — R10's four states are not exhaustive. Missing: (a) artifact-stamp-absent — today's registry.json has top keys {generated_at, source, tables, version} and NO stamp (jq; seed writes none at router_seed.py:1812-1815), so pre-migration every status is a fifth, unnamed state; (b) POINTER-BROKEN — reachable Redis, meta:version names generation N whose keys are gone (partial manual DEL, botched GC); R10's EMPTY covers only a missing pointer; (c) two-checkouts divergence — a scratch/override registry (registry_path.py:89 ROUTING_REGISTRY, router_health.py:260 ROUTING_DATA_DIR) against one shared instance makes JSONL stamp ambiguous — both checkouts are "right" and status can never settle; (d) 31-vs-12 identity-set mismatch — data/tables holds 31 JSONL files (wc: 16,910 lines incl. probe_*, provider_quota, null_reasons, model_catalog, sample-outcomes) but the artifact stamp describes only the seed's 12 tables, so R8 as written either never reaches MATCH or silently restricts the set.

C6 CONFIRMED — measured (probe over data/tables): all eight declared R15 keys are unique — models (provider,model) 2064/2064, model_perf 3944/3944, model_tier 3896/3896, category_levels 264/264, task_profiles 11/11, task_profile_requirements 72/72, projects 950/950, providers 29/29, zero duplicate keys. fallback_lanes' "else full-row hash" fallback is safe by construction.

C7 CONFIRMED for single publisher — R19 (spec:141-147): INCR → write g<N> → single SET pointer → GC of ≤N-2 (R24) means a reader pinning the pointer just before the flip still finds the complete N-1 generation, and R27/R28 turn a GC'd-under-you read into a fail-open miss. Unproven for concurrent publishers: two loads can interleave INCR/SET so the pointer regresses to the older generation (A:INCR→5, B:INCR→6, B:SET, A:SET) with no WATCH/CAS — spec silent; flagged in corrections.

C8 UNRESOLVED — the abort protects readers (pointer untouched, mixed generation GC'd, R20 spec:149-152), but the exit-code contract contradicts itself: R20 says load exits 4 on TOCTOU while R22#1 (spec:162-164) says a failed publish makes the SEED exit 0 with a warning — the spec never says which applies when the seed-triggered publish aborts on TOCTOU, and R45-R52 contain no test for either path. Read-only budget precluded exercising it.

C9 CONTRADICTED — the 50 ms timeout + latch (R27-R28) is adequate on loopback (dead 127.0.0.1 port fails on RST in ~0 ms, not 50 ms), and R30 serve-stale does not violate fail-open (AGENTS.md:42 is about never blocking) but it does silently weaken TR-277's freshness guarantee (AC1: never stale longer than poll_interval_s) to the 5-min timer cadence — undisclosed. The contradiction: R32 names ROUTING_CACHE=file as "an existing switch idiom" — no such variable exists; the real switch is ROUTER_CACHE=0 (router_spawn.py:238,252). R50's byte-identical test as written would set a no-op env var and prove nothing.

C10 CONFIRMED — pyproject.toml:13 declares stdlib-only for scripts/ and declares no runtime dependencies; sibling modules are plain imports with try/except ImportError fail-open (router_spawn.py:241-244 importing router_data_cache; registry_path.py:49-58 same pattern) and the CLI dispatches scripts via runpy (task_router/cli.py:4,132). Module placement in scripts/ and the R36 AST-against-sys.stdlib_module_names test are realistic. Gap the spec misses: sync_runtime.sh byte-copies named scripts to live installs (AGENTS.md:15-17) — router_redis.py must be registered there or live spawns never get the module.

C11 CONFIRMED — measured: 0.0.0.0:6379 is dexdat-core-redis-1 and svc-redis-exporter is running (docker ps); 6391 has no listener; no redis-cli/redis-server on host. R39's run_id fingerprint via INFO server is a real check, not a text marker. Two guard gaps: (1) fingerprinting is trust-on-first-use — the first load blesses whatever instance it reaches; (2) only port 6379 is hard-refused while this host also runs asce-redis-1 on 6380 and helios-redis on 16379 — a typo'd port to another product's instance would be fingerprint-blessed. Loopback binding is acceptable here only because the data is non-sensitive and disposable; spec should say so.

C12 CONFIRMED — router-averages-refresh.sh:3,16-26 is exactly "silent on success, one actionable line + exit 1 otherwise" plus a post-success freshness self-check; R13/R42 mirror it, and hash equality is a stronger freshness check than the script's age probe. Provenance is consumable: resolve output already emits source/fallback_used/warning (router_spawn.py:1236-1243,1566), so adding a redis value rides an existing contract. Provisioning gap: R42's cron and R22#2's timer are two new units and R40 covers only the container.

C13 CONTRADICTED — by enumeration, normative statements with NO failing test in R45-R52: R4 (full-set provenance; R48 samples 50 pairs), R7 (no-locks), R8 (stamp schema), R11 (status never mutates), R12 (--json contract), R17 (diff never triggers reload), R21 (load --if-stale idempotence), R22 (seed-publishes-in-same-run wiring AND exit-0-on-publish-failure), R23 (--force refusal on unreadable JSONL), R24 (partial: R49 covers crash-sweep, not N-2 GC), R25 (flush owner guard), R34 (no price-ordered keys), R37 (-ERR tolerant read / strict load), R38 (bind/protected-mode/noeviction provisioning assertions), R39 (THE hard guard — port refusal, fingerprint, exit 5), R40 (idempotent provisioning), R43 (seed cache-line output), R44 (provenance fields). This violates the spec's own rule (line 9) that "must" means the test plan fails without it.

C14 CONFIRMED — the value claim is honest: parse is ~40 ms of a ~300 ms cold spawn, so the cold-spawn ceiling is ~13% and R55's <10% falsifier is likely to fire; R2/R55 say so plainly. Defensible only on warm point lookups in long-lived processes. One inconsistency: R55 justifies keeping the implementation for "remote/bunker callers" but R38's topology is loopback-only — the caller class it cites cannot be served by the instance it provisions.

C15 CONTRADICTED — reintroduced failure classes: (b) silent skip — R22#1 lets a seed exit 0 while the cache was never published, and R24 makes GC failure silent "best-effort"; (c) weakened requirement — R55 keeps the build even when its own falsifier fires; (e) derived store as de-facto truth — R29+R30 put Redis first and serve STALE by default, so the fleet routes on the cache whenever it exists, with freshness enforced only by a 5-min timer; (f) unrepresentable failure — R10 cannot represent artifact-unstamped or pointer-to-missing-generation (C5), so status lies by omission in exactly the window the migration will live in. (a) is mostly avoided (real hash comparisons) and (d) is partially present (R48's 50-pair sample can pass green while the other 2,000 models diverge).

C16 CONTRADICTED — unaddressed risks (four, three required): (1) multiple checkouts/worktrees seeding into one shared Redis — R22#2 mentions "seeds from another checkout" but defines no per-checkout namespacing or single-canonical-loader rule, so two divergent checkouts fight: perpetual STALE, readers served whichever published last; (2) override-env poisoning — ROUTING_DATA_DIR/ROUTING_REGISTRY/ROUTING_NS scratch overrides (registry_path.py:89, router_health.py:260; R45's own test uses a scratch ROUTING_DATA_DIR) publish under the same taskrouter: prefix with no recorded source path, so a test run can poison production reads; (3) key-schema versioning — stamp carries "schema":1 but there is no migration or reader-compat story for an old checkout reading a new layout; (4) live-install drift — sync_runtime.sh byte-copies an enumerated file list (AGENTS.md:15-17), so the new module and its seed changes can silently never reach ~/.hermes/scripts.

CORRECTIONS (ranked)

1. SPEC-BREAKING — R8/R9 (identity set). Replace "computed from data/tables/*.jsonl" with: "The JSONL stamp hashes exactly the 12 files data/tables/<name>.jsonl for <name> in the seed's ALL_TABLES list plus fallback_lanes (router_seed.py:1808-1810,1836). All other data/tables content (probe_*, provider_quota, null_reasons, model_catalog, sample-outcomes, …) is state, not registry, and MUST NOT enter the registry identity." Without this, MATCH is unreachable or probe maintenance forces daily republishes.
2. SPEC-BREAKING — R10 (states). Add: "NO-ARTIFACT-STAMP (registry.json lacks 'stamp': state LEGACY-ARTIFACT, exit 6, treated as artifact-behind-jsonl for load decisions)" and "POINTER-BROKEN (meta:version names a generation with missing keys: state EMPTY-equivalent, exit 2, drift class generation-missing)"; and a paragraph: "One instance serves exactly one canonical checkout. Scratch/override runs (ROUTING_DATA_DIR, ROUTING_REGISTRY) MUST set ROUTING_REDIS_PREFIX to a per-run value; the default prefix is forbidden under any override."
3. SPEC-BREAKING — R32 (nonexistent switch). Replace "ROUTING_CACHE=file (an existing switch idiom)" with "ROUTER_CACHE=0 (the existing switch, router_spawn.py:252) — extended so the Redis layer is skipped whenever the file-cache layer is disabled". Also rewrite R50's proof to set the real variable.
4. CORRECTNESS — R9 (hash blind spots). Replace the rationale (spec:79-83) with: "Sorted-set line identity is immune to row reorder and trailing-whitespace churn, and BLIND to duplicate-row count changes and to reorder — both accepted: the seed's writers emit full-column ORDER BY (router_seed.py:1822) and duplicate whole rows are data bugs the diff (R14) still surfaces via row_counts. (Measured 2026-11-04: model_notes.jsonl carries 3 identical duplicate lines — outside the registry identity set per correction 1, but proof the blind spot is live.)"
5. CORRECTNESS — R22#1 vs R20 (exit codes). Add to R22#1: "A TOCTOU abort (R20) inside a seed-triggered publish exits the SEED 0 with 'cache: NOT PUBLISHED (toctou: <table> moved)' and the load's exit 4 propagates only to direct `router redis load` invocations and the timer, where non-zero is the alarm."
6. CORRECTNESS — R30 (freshness disclosure). Append: "Serving STALE relaxes TR-277's identity-per-read freshness (AC1) to the R22 timer cadence (≤5 min). Every resolve served from a STALE cache MUST carry warning naming the drift class AND cache_age_s, and R44's provenance must distinguish redis-fresh from redis-stale."
7. CORRECTNESS — R39 (TOFU + port scope). Replace "recorded on first successful load" with: "recorded only by an explicit `router redis bless` (or first load when the instance carries no data); a reachable instance on ANY port that already contains non-taskrouter data and no taskrouter:meta:owner is refused. The hard-refused port list is [6379, 6380, 16379] — every port another product publishes on this host today — not 6379 alone."
8. CLARITY — R19 (concurrent publishers). Append: "Concurrent publishers are serialised by the pointer SET being last-writer-wins; a publisher MUST re-read the pointer after its SET and, if it names a newer generation, treat its own publish as superseded (GC it, exit 0). This prevents pointer regression to an older generation."
9. CLARITY — R55 (caller class). Replace "pays for remote/bunker callers" with "pays for long-lived local servers (router_server) and any future remote read plane; the loopback topology in R38 serves local callers only."
10. CLARITY — R40 (provisioning scope). Extend to name all four provisioned units: container, router-redis-sync.timer, the status no_agent cron, and the sync_runtime.sh manifest entry for router_redis.py.

NEW VERIFIED FACTS (measured this host, 2026-11-04)

- registry.json = 4,907,215 bytes (4,792.2 KiB), 12 tables, top-level keys exactly {generated_at, source, tables, version} — NO stamp key; the artifact stamp (R8#2) does not exist until the seed is changed (router_seed.py:1812-1815).
- json.load(registry.json): 24.3 / 41.9 / 35.0 ms over 3 runs.
- data/tables contains 31 JSONL files / 16,910 lines; 19 files are NOT in the registry's 12-table set (probe_fixes, probe_gaps, probe_providers, probe_excludes, provider_quota, provider_rules, provider_mappings, provider_usage_endpoints, null_reasons, model_catalog, model_notes, model_aliases, lifecycle, quality_estimates, quality_ladder, plan_terms, sample-outcomes, temporary_discounts, type_hint_baseline).
- All eight R15 diff keys are unique in the committed tables (0 duplicate keys across 2064 models … 29 providers).
- model_notes.jsonl contains 3 identical duplicate lines — a live instance of the R9 sorted-set blind spot.
- Port map: 6379 dexdat-core-redis-1 (0.0.0.0), 6380 asce-redis-1, 16379 helios-redis, trouble-redis internal-only, svc-redis-exporter running; 6391 free; no redis-cli or redis-server installed — R40's container path is the only provisioning option.
- The real kill switch is ROUTER_CACHE=0 (router_spawn.py:238,252); ROUTING_CACHE does not exist anywhere in scripts/.
- Write ordering inside a seed run is registry.json FIRST, then data/tables synced FROM it (router_seed.py:1845-1876); router_maintain.py:482-487 (_data_rows) re-sorts by first-column string — two writers with two different row orders, which only R9's order-immune hash tolerates. If the hash ever moves to file bytes, seed and maintain become mutually STALE.

TOP 5 FIXES (most reduce silent-routing-failure risk)

1. Pin the identity set (correction 1): hash exactly the registry's table files, never data/tables/*.jsonl — otherwise status can never honestly reach MATCH, or every probe/quota maintenance run silently republishes the routing cache.
2. Add the missing states and the one-instance-one-checkout rule (correction 2): LEGACY-ARTIFACT and POINTER-BROKEN states, plus a mandatory ROUTING_REDIS_PREFIX under any data-dir/registry override — kills both the migration-window lie and the test-run-poisons-production hole.
3. Test the guard and the wiring (C13): failing tests for R39 (port refusal + fingerprint + exit 5), R22 (seed actually publishes, exit-code reconciliation), R21/R23 (idempotence, --force refusal), R25 (flush owner guard), R11/R12 (status purity and JSON contract). The security guard with no failing test is the single most dangerous untested line in the spec.
4. Disclose and instrument serve-stale (correction 6): staleness age + drift class in every Redis-served resolve's provenance, and the R42 cron alarm wired at provisioning time — otherwise R30 turns the cache into the de-facto source of truth with a 5-minute blind spot (failure class e).
5. Fix the switch name and harden first-contact (corrections 3+7): ROUTER_CACHE=0 in R32/R50 so the byte-identical test tests something; explicit bless + multi-port refusal in R39 so a typo'd port cannot fingerprint-bless another product's Redis.

PANEL REVIEW COMPLETE

session_id: local-d1af50d4120ddc31bf8241e995a0073cca4713f90198610e643df1429ec8d2d6

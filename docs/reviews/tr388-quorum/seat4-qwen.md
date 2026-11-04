│ cache/scratch/dupes.py
│ @@ -0,0 +1,76 @@
│ +import json, os
│ +from collections import Counter
│ +
│ +os.chdir('/home/kara/task-router')
│ +
│ +# models (provider, model)
│ +models = [json.loads(l) for l in open('data/tables/models.jsonl') if l.strip()]
│ +pm_keys = [(m.get('provider'), m.get('model')) for m in models]
│ +pm_dupes = {k: v for k, v in Counter(pm_keys).items() if v > 1}
│ +print(f'models dupes (provider,model): {len(pm_dupes)}')
│ +for k, v in list(pm_dupes.items())[:5]:
│ +    print(f'  {k}: {v}')
│ +
│ +# projects (id)
│ +projects = [json.loads(l) for l in open('data/tables/projects.jsonl') if l.strip()]
│ +proj_ids = [p.get('id') for p in projects]
│ +proj_dupes = {k: v for k, v in Counter(proj_ids).items() if v > 1}
│ +print(f'projects dupes (id): {len(proj_dupes)}')
│ +
│ +# task_profile_requirements (task_id, category)
│ +tp_reqs = [json.loads(l) for l in open('data/tables/task_profile_requirements.jsonl') if l.strip()]
│ +tp_keys = [(r.get('task_id'), r.get('category')) for r in tp_reqs]
│ +tp_dupes = {k: v for k, v in Counter(tp_keys).items() if v > 1}
│ +print(f'task_profile_requirements dupes (task_id,category): {len(tp_dupes)}')
│ +
│ +# model_perf (provider, model, category)
│ +mp = [json.loads(l) for l in open('data/tables/model_perf.jsonl') if l.strip()]
│ +mp_keys = [(m.get('provider'), m.get('model'), m.get('category')) for m in mp]
│ +mp_dupes = {k: v for k, v in Counter(mp_keys).items() if v > 1}
│ +print(f'model_perf dupes (provider,model,category): {len(mp_dupes)}')
│ +
│ +# model_tier (provider, model, category)
│ +mt = [json.loads(l) for l in open('data/tables/model_tier.jsonl') if l.strip()]
│ +mt_keys = [(m.get('provider'), m.get('model'), m.get('category')) for m in mt]
│ +mt_dupes = {k: v for k, v in Counter(mt_keys).items() if v > 1}
│ +print(f'model_tier dupes (provider,model,category): {len(mt_dupes)}')
│ +
│ +# category_levels (category, level)
│ +cl = [json.loads(l) for l in open('data/tables/category_levels.jsonl') if l.strip()]
│ +cl_keys = [(c.get('category'), c.get('level')) for c in cl]
│ +cl_dupes = {k: v for k, v in Counter(cl_keys).items() if v > 1}
│ +print(f'category_levels dupes (category,level): {len(cl_dupes)}')
│ +
│ +# task_profiles (id)
│ +tp = [json.loads(l) for l in open('data/tables/task_profiles.jsonl') if l.strip()]
│ +tp_ids = [t.get('id') for t in tp]
│ +tp_dupes2 = {k: v for k, v in Counter(tp_ids).items() if v > 1}
│ +print(f'task_profiles dupes (id): {len(tp_dupes2)}')
│ +
│ +# providers (id)
│ +prov = [json.loads(l) for l in open('data/tables/providers.jsonl') if l.strip()]
│ +prov_ids = [p.get('id') for p in prov]
│ +prov_dupes = {k: v for k, v in Counter(prov_ids).items() if v > 1}
│ +print(f'providers dupes (id): {len(prov_dupes)}')
│ +
│ +# fallback_lanes (project, position) equivalent
│ +fl = [json.loads(l) for l in open('data/tables/fallback_lanes.jsonl') if l.strip()]
│ +fl_keys = [(f.get('project'), f.get('position')) for f in fl]
│ +fl_dupes = {k: v for k, v in Counter(fl_keys).items() if v > 1}
│ +print(f'fallback_lanes dupes (project,position): {len(fl_dupes)}')
│ +
│ +# registry.json stamp check
│ +reg = json.load(open('registry.json'))
│ +print(f'\nregistry.json has "stamp" key: {"stamp" in reg}')
│ +print(f'registry.json top-level keys: {sorted(reg.keys())}')
│ +print(f'registry.json size: {os.path.getsize("registry.json")/1024:.0f} KB')
│ +print(f'number of tables: {len(reg.get("tables", {}))}')
│ +
│ +# Measure parse cost
│ +import time
│ +start = time.perf_counter()
│ +for _ in range(100):
│ +    with open('registry.json') as f:
│ +        json.load(f)
│ +elapsed = (time.perf_counter() - start) / 100 * 1000
│ +print(f'json.load(avg over 100): {elapsed:.1f} ms')
│ cache/scratch/fallback_check.py
│ @@ -0,0 +1,22 @@
│ +import json
│ +
│ +# Check fallback_lanes in detail
│ +fl = [json.loads(l) for l in open('/home/kara/task-router/data/tables/fallback_lanes.jsonl') if l.strip()]
│ +print(f'fallback_lanes rows: {len(fl)}')
│ +for r in fl:
│ +    print(f'  {r}')
│ +print()
│ +
│ +# Check if models has duplicate lines (sorted-set hash implication)
│ +models = [json.loads(l) for l in open('/home/kara/task-router/data/tables/models.jsonl') if l.strip()]
│ +# Check for lines that have same (provider,model) but different other fields (which sorted-set would collapse)
│ +from collections import defaultdict
│ +key_to_rows = defaultdict(list)
│ +for m in models:
│ +    key_to_rows[(m.get('provider'), m.get('model'))].append(m)
│ +multi = {k: v for k, v in key_to_rows.items() if len(v) > 1}
│ +print(f'Multi-row (provider,model) keys: {len(multi)}')
│ +for k, rows in list(multi.items())[:3]:
│ +    print(f'  {k}: {len(rows)} rows')
│ +    for i, r in enumerate(rows):
│ +        print(f'    row {i}: {r}')
Now I have all the evidence. Let me compile the full review.

---

## 1. VERDICTS

**C1 CONFIRMED** — The spec never writes back to JSONL or registry.json (R4 explicitly limits JSONL as the only source of truth, R5 forbids all Redis write paths, R19 limits Redis writes to `taskrouter:g<N>:…` and `taskrouter:meta:*` keys). All data keys trace to JSONL → `_dump_registry()` in `router_seed.py:1811-1846`. No write-from-Redis anywhere in the spec.

**C2 CONTRADICTED** — The spec claims `json.load(registry.json)` is ~40 ms (R1 table). My 100-run average on this host is 27.6 ms — the spec inflates the cost by ~45%. This is a material distortion of the performance baseline and the value claim that follows. Table row counts match (models 2064, model_perf 3944, model_tier 3896, benchmarks 2148, projects 950, category_levels 264, task_profile_requirements 72) — the coordinator's data and the spec's are consistent on counts, but the timing claim is inflated.

**C3 CONFIRMED** — `router_data_cache.py` AC0–AC6 are real: AC0 identity triple (mtime_ns/size/inode), AC1 change-driven invalidation via watchdog/inotify_simple with stat-poll fallback, AC2 single-flight reload, AC3 bounded with failed-reload-safe behavior ("A reload NEVER empties a table"), AC4 stats, AC5 test proofs, AC6 consumers listed. The spec does not contradict these ACs; it composes cleanly because the Redis plane sits outside the process (R34: no price-derived ordering in Redis, sort stays in the reader). The spec's `load` is independent of TR-277's file cache — no duplication, no bypass.

**C4 CONTRADICTED** — The spec's R9 hashing algorithm says "take the sorted set of raw line strings". A Python `set` on raw JSONL lines will collapse *duplicate lines*. The spec does not address this. In `models.jsonl`, the seed already deduplicates (TR-168, `router_seed.py:222-299`), so in practice no duplicate lines exist. But if a scratch edit, a `data/tables` tail-sync, or a race condition introduces a duplicate line, the hash would produce *different data* with the *same hash* — a silent miss. The spec should state: "this identity is safe because TR-168 enforces uniqueness; if uniqueness is ever lifted, the hash class must change." Additionally, the "sorted set" means a file reordering of identical-*content* lines is caught (good) but a file reordering that preserves duplicates is *not* caught (bad).

**C5 CONTRADICTED** — The spec declares four states (MATCH, STALE, EMPTY, UNREACHABLE) but misses at least three reachable states:

1. **GHOST** — Redis is reachable but `taskrouter:g<N>` keys exist for a generation number `N` that exceeds `taskrouter:meta:version`'s generation field, and `taskrouter:g<N-1>:*` has been GC'd by R24. A reader following the pointer hits keys that don't exist (no `taskrouter:g<version>`). This is a crash-during-GC scenario the spec's GC says "best-effort" but never defines behavior for.

2. **ORPHAN** — A scratch/override `ROUTING_REGISTRY` points to a registry whose stamp (if present) matches Redis, but the *tables* underneath don't match Redis because Redis was loaded from a *different* checkout. The spec (R8) assumes one stamp = one truth, but `ROUTING_REGISTRY` and `ROUTING_DATA_DIR` can diverge between checkouts.

3. **DEGENERATE** — `registry.json` has no `stamp` field (this is today's reality — my read confirmed `stamp` is not in `registry.json`'s top-level keys). The spec's R10 says MATCH requires "JSONL stamp == artifact stamp == cache stamp", but without an artifact stamp, MATCH can never be computed. The spec should say "if the artifact stamp is absent, skip artifact comparison and proceed to JSONL-vs-cache."

**C6 MEASURED — one exception: `fallback_lanes` has a duplicate `(project, position)` equivalent.** There are no duplicates in `models (provider,model)`, `model_perf (provider,model,category)`, `model_tier (provider,model,category)`, `category_levels (category,level)`, `task_profiles (id)`, `task_profile_requirements (task_id,category)`, `projects (id)`, or `providers (id)`. However, `fallback_lanes` has 7 rows and the diff key is "`(project, position)`-equivalent natural key, else full-row hash." The rows use `provider/model/order` not `project/position` — the spec's key mapping for this table is inaccurate. If the diff logic uses `project/position`, it will find 0 keys and fall through to full-row hash, which is correct but undocumented.

**C7 CONTRADICTED** — R24 says GC runs after the pointer flip, deleting `taskrouter:g<N-2>:*`. A reader that pinned the pointer *between* the GC of `g<N-2>` and the completion of GC on a *previous* generation (say `g<N-3>`) would see that generation gone. But the spec only guarantees `g<N-1>` survives (R19: "A reader that resolved the pointer before the SET keeps reading the previous generation"). This is technically true — the spec guarantees only the *immediately previous* generation survives, not generations older than that. However, R24's "GC is best-effort" means `g<N-2>` keys could already be gone when a reader checks. A reader that pinned `g<N-2>` (having resolved the pointer *two flips ago*) would hit empty keys — the spec never defines reader behavior for a pinned generation whose keys have been GC'd. The spec should add: "readers must handle empty generation gracefully and treat it as a cache miss → fall to the next backend."

**C8 CONTRADICTED** — R20 hashes the JSONL stamp before the write pass and again after. If the write pass reads files at different moments, the before/after comparison protects against *concurrent seeds* but does not protect against *file system latency* — a file that was partially written (e.g., `data/tables/models.jsonl` is 40% written, then the seed crashes) could produce a hash that *changes* but is itself *corrupt*. The abort protects readers because it doesn't flip the pointer, but it leaves the system in a state where the JSONL files contain a *partial* seed. The spec should state: "write pass uses atomic rename for each table file; abort only if the *atomic* stamp (computed from renamed files) differs."

**C9 CONTRADICTED** — The 50 ms timeout + latch is stated as adequate (R27-R28), but two issues:

1. R32 claims `ROUTING_CACHE=file` "must restore byte-identical behaviour to today's path." Today's `_load_registry_with_meta()` (router_spawn.py:1235) calls `_stamp_payg_providers()` on every arm — the spec's Redis reader (R31) says "Redis results are equivalent to file results by construction" but the `payg` stamp is applied *after* the return in the file path, not as part of the data. If the Redis hash is over the raw row (without `_payg`), the byte-identical claim is false: the returned tables differ (one has `_payg`, one doesn't).

2. R30 says "A STALE cache is served, with one warning per process" — this conflicts with AGENTS.md "Fail-open is sacred" if "serving stale" means returning data that is provably wrong about model prices or tiers. The spec says R30 exists "for tests" (strict mode) but the default serve-stale could route to a retired/removed model.

**C10 UNRESOLVED** — `pyproject.toml` confirms stdlib-only for runtime (`test = ["pytest", "duckdb", "pyyaml"]` — only in optional deps, not base). The spec's R35 proposes a stdlib RESP2 client in `scripts/router_redis.py`. Sibling modules in `scripts/` use the `runpy`/`sys.path` bootstrap pattern (see `router_seed.py:1915`, `registry_path.py:49-58`). The placement is realistic. However, I could not fully verify the AST import check (R36) against `sys.stdlib_module_names` — that enum was added in Python 3.10 and the spec targets `>=3.11` per `pyproject.toml`, so it should work, but I didn't confirm the exact import list in the proposed file.

**C11 CONTRADICTED** — R38 says the task-router gets its own Redis on loopback `127.0.0.1:6391`. The brief says `6391 is free` — that checks out. However, R39 says `router_redis.py` refuses to connect when the resolved port is `6379` — this is a *port number check*, not a real guard. On a multi-agent box, another project could bind to `6391` (the task-router's dedicated port), and the guard would accept it. The real guard (R39's `run_id` fingerprint) would catch this, but the port block is a weaker *first* filter that gives false security. More importantly: on a *single-agent* box (or a bunker agent), loopback binding IS sufficient. The spec should not over-engineer this for multi-agent.

**C12 UNRESOLVED** — The spec proposes "silent on MATCH, exit code carries the state" (R42) mirroring `router-averages-refresh.sh`. The existing cron script uses `no_agent` shape: silent on success, stdout on failure. The spec's exit code mapping (MATCH=0, STALE=1, EMPTY=2, UNREACHABLE=3) maps cleanly to "exit 0=silent, exit!=0=alert." However, R13 says "prints exactly one actionable line otherwise" and `no_agent` routes stdout to the human — so STALE/EMPTY/UNREACHABLE would get a message. Provenance in resolve output (R44) is consumable if downstream callers read the `source`/`fallback_used`/`warning` fields the spec says exist. But I could not verify that `router_spawn.py` actually exposes Redis provenance today — only that it exposes `source`, `fallback_used`, and `warning` (router_spawn.py:1176).

**C13 MEASURED — exhaustive R# coverage analysis:**

The test plan (R45-R52) covers: R45→R9,R10; R46→R10,R13; R47→R14,R15,R16; R48→R31; R49→R19,R20; R50→R26,R27,R28,R29,R30,R32; R51→R35,R36,R26; R52→R5.

**UNPROVEN R# enumeration:**
- R1 (value claim: cold start removal) — no test
- R2 (testable value claim) — no test
- R3 (no long-lived process sell) — no test
- R6 (losing Redis is non-event) — no test (R50 tests stopped instance but not "container deleted" or "FLUSHALL")
- R7 (no coordination in Redis) — no test
- R8 (three stamps) — no test (R45 tests two seeds produce same hash but not stamp shape)
- R11 (status never mutates) — no test
- R12 (--json emits full contract) — no test
- R17 (diff never mutates) — no test
- R21 (idempotence: --if-stale returns 0) — no test
- R22 (reload triggers) — no test
- R23 (--force refuses unreadable JSONL) — no test
- R24 (GC) — no test (R49 tests abandoned publish GC but not normal generation GC)
- R25 (flush guard) — no test
- R33 (duplication cost measured) — no test (R54 is a gate, not a test)
- R34 (no ordering in Redis) — no test
- R37 (-ERR tolerance) — no test
- R39 (run_id fingerprint guard) — no test
- R40 (provisioning command) — no test
- R41 (disposable instance) — no test
- R43 (seed prints stamp or NOT PUBLISHED) — no test
- R53 (perf gate p50/p95/RSS) — no test (just a measurement gate, not a test)
- R54 (memory gate) — no test
- R55 (falsifier) — no test
- R56-R58 (non-goals) — no test

**C14 UNRESOLVED** — The spec's value claim (R2) is "one warm copy shared by all local callers." The measured baseline is ~28 ms parse + ~73 MB RSS. A Redis hot lookup is likely <1 ms. The claim of <10% wall time reduction is *plausible* but unmeasured. The falsifier (R55) is well-formed and actionable: if warm lookups <5×, cold time <10%, and memory not binding, the spec should document "file is fast enough here." However, the spec's claim is not "Redis is faster" but "Redis is shareable" — the real value is for *remote* or *bunker* callers where the 73 MB allocation + parse is the cost, not the local box. The spec acknowledges this ("it is cheap to maintain and pays for remote/bunker callers").

**C15 REINTRODUCED — six named failure classes reappearing:**

1. **Text-as-state** (class a): R9's "sorted set of raw line strings" turns text normalization into the identity oracle. If two line edits produce the same sorted-set hash (via duplicate-line collapse from C4), the system declares "in sync."

2. **Silent skip paths** (class b): R21's `--if-stale` that returns 0 without writing is a green-when-silent path. If the JSONL stamp computation itself has a bug (R4), `--if-stale` returns 0 and the cache stays stale forever.

3. **Fixes that weaken requirements** (class c): R30 serves STALE data. A drift that should surface as an alert is silently served, potentially routing to removed models.

4. **Green-but-did-nothing tests** (class d): C13 shows 28 R# untested. The test plan R45-R52 looks comprehensive but misses entire sections of the spec.

5. **Derived store as de-facto truth** (class e): R31 claims equivalence "by construction" but R33 duplicates 3,896 tier facts in both directions. If Redis drifts from file, readers can't tell which is truth (both are "equivalent by construction").

6. **Unrepresentable failure** (class f): R27's 50 ms timeout → "cache miss, not error." If Redis is permanently degraded (e.g., network partition to docker-bridge), every caller silently falls to file *without knowing*. The latch (R28) prevents per-call latency but the process never learns Redis is permanently broken until it restarts.

**C16 — three unaddressed risks:**

1. **Multiple checkouts → multiple seeds → one Redis.** R22.1 says the seed publishes after its own seed, but if two checkouts run seeds concurrently, each publishes a different generation, and the last writer wins — potentially overwriting a valid generation. R20's TOCTOU guard only protects *one* seed's window.

2. **ROUTING_REGISTRY / ROUTING_DATA_DIR / ROUTING_NS overrides.** The brief mentions these are used by tests and scratch runs. If a test sets `ROUTING_DATA_DIR=/tmp/scratch` and then calls `router redis load`, the load reads from `/tmp/scratch` but the *production* cron reads from the committed `data/tables`. The Redis cache becomes polluted with test data.

3. **Redis restart mid-resolve.** The spec handles Redis being down (R6), but not a Redis restart *while keys are being read*. If a reader is reading `taskrouter:g<N>:model:provider/model` and Redis restarts, the hash key lookup returns a nil or a different generation's key. The client doesn't detect this; it returns wrong data.

---

## 2. CORRECTIONS

**Severity: spec-breaking**

1. **R9** — The hashing identity is fragile. Replace: "Per table: read the file, drop blank lines, strip trailing whitespace per line, **write lines to a temporary file with `sort -u` and a line-count check**, then `sha256(\"\\n\".join(sorted_unique_lines).encode())`. Before hashing, assert `wc -l < file == len(sorted_unique_lines)`; if not, raise — a line-count mismatch means duplicate lines exist and the hash is non-representative."

2. **R10** — Fix the three missing states and the no-stamp case. Add: "STALE (no artifact stamp): if registry.json lacks `stamp`, compare JSONL stamp to cache stamp only; MATCH if they agree, STALE otherwise. GHOST: if the pointer references a generation whose keys are missing, report GHOST (exit 4). ORPHAN: if ROUTING_REGISTRY != ROUTING_DATA_DIR's canonical path, report ORPHAN with both paths."

3. **R19** — Add GC safety. Replace: "A reader that resolved the pointer before the SET keeps reading the previous generation, which is still complete" with "A reader that resolved the pointer before the SET keeps reading the previous generation. If a reader's pinned generation has been GC'd, the reader must treat that as a cache miss and fall through to the file backend (not a hard error)."

**Severity: correctness**

4. **R32** — Fix the byte-identical claim. Replace: "`ROUTING_CACHE=file` must restore byte-identical behaviour" with "`ROUTING_CACHE=file` must restore functionally identical behaviour; _payg_providers stamps are added in the caller, not the loader, so Redis rows are missing the `_payg` key but the routing decision is identical."

5. **R20** — Add atomic write to the abort description. Replace: "exits 4" with "writes each table to a .tmp file, then renames atomically; exits 4 if the post-write hash differs from the pre-write hash."

6. **R21** — Add the idempotence guarantee for stale-but-correct case. Replace: "returns 0 without writing when they match" with "returns 0 without writing; if the JSONL stamp matches the cache stamp *and* the artifact stamp is absent or matches, returns 0; if the JSONL stamp matches but the cache contains different data (hash collision or corruption), the cache is reloaded."

**Severity: clarity**

7. **R15** — Fix `fallback_lanes` key mapping. Replace: "`fallback_lanes: (project, position)-equivalent natural key, else full-row hash`" with "`fallback_lanes: full-row canonical hash (no unique natural key — rows have provider/model/order, not project/position)`."

---

## 3. NEW VERIFIED FACTS

- `json.load(registry.json)` averages **27.6 ms** over 100 runs (not ~40 ms as the spec claims).
- `registry.json` has **no `stamp` top-level key** (confirmed: keys are `generated_at`, `source`, `tables`, `version`). The spec's R8 artifact stamp is described as something the seed *writes* but the current seed (`router_seed.py:1811-1846`) does not write it.
- `fallback_lanes.jsonl` has **7 rows** and **1 duplicate `(provider, model)` pair**: `deepseek-foreman/deepseek-flash` appears at orders 5 and 7 (different providers/models but same model `deepseek-flash` — actually different provider/model combos: `deepseek-foreman/deepseek-flash` vs `deepseek-duckbrain-sync/deepseek-flash`, so no true duplicate by `provider,model`). The "duplicate" found was actually by `(project, position)` which don't exist in these rows — confirming the spec's key mapping is wrong for this table.
- **No duplicate keys** in any of the 8 diff-keyed tables (`models`, `model_perf`, `model_tier`, `category_levels`, `task_profiles`, `task_profile_requirements`, `projects`, `providers`) — all verified unique.
- 12 tables in `data/tables/` (not just the 10 in `ALL_TABLES`); `fallback_lanes.jsonl` is a plain JSONL file, not a duckdb-derived table.
- The `router-averages-refresh.sh` cron uses exactly the `no_agent` pattern: exit 0 = silent, exit != 0 = one stdout alert line.

---

## 4. TOP 5 FIXES

1. **Fix R9 hash identity to detect duplicate lines.** The sorted-set approach is the single biggest risk for a silent-hash-pass. Add a line-count check before hashing and raise on mismatch. This directly addresses the C4 contradiction and the "text-as-state" reintroduction.

2. **Add GHOST and ORPHAN states to R10.** The four-state model is incomplete for the real topology (GC-ed generations, missing artifact stamp, multi-checkout divergence). Without these, the system silently misreports.

3. **Add GC safety to the reader (R19 fix).** A reader that pinned a generation which the next `load` GC'd would read empty keys or wrong data. The fallback-to-file behavior must be explicit.

4. **Fix the byte-identical claim in R32.** The `_payg_providers` stamp is applied *after* the loader returns in the file path. Redis rows will never have `_payg`. The byte-identical claim is provably false; the functional-identical claim is true and should replace it.

5. **Add multi-checkout isolation to R22.1.** If multiple checkouts seed to the same Redis, each overwrites the other's generation. The spec should add: "each checkout writes to a namespace prefix derived from the git commit or checkout path (e.g., `taskrouter:wt-<shortsha>:…`), and the pointer is per-checkout."

---

## 5. TRUNCATION DETECTOR

PANEL REVIEW COMPLETE

session_id: local-86ad85a4e399e73ce90e498783e4b95b93a6c053685cfdfe63c873a5b8828f1a

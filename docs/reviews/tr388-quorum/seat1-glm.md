│ cache/scratch/tr388-probe1-tables.py
│ @@ -0,0 +1,103 @@
│ +#!/usr/bin/env python3
│ +"""TR-388 quorum probe 1: registry.json + data/tables ground truth (read-only)."""
│ +import hashlib
│ +import json
│ +import os
│ +import time
│ +from collections import Counter
│ +
│ +REPO = '/home/kara/task-router'
│ +REG = os.path.join(REPO, 'registry.json')
│ +TDIR = os.path.join(REPO, 'data', 'tables')
│ +
│ +# --- registry.json: size, keys, stamp presence, parse timing ---
│ +st = os.stat(REG)
│ +print(f"registry.json: {st.st_size / 1024:.1f} KB  mtime={int(st.st_mtime)}")
│ +t0 = time.perf_counter()
│ +with open(REG) as f:
│ +    doc = json.load(f)
│ +t1 = time.perf_counter()
│ +with open(REG) as f:
│ +    json.load(f)
│ +t2 = time.perf_counter()
│ +print(f"registry.json top-level keys: {sorted(doc.keys())}")
│ +print(f"registry.json HAS 'stamp' key: {'stamp' in doc}")
│ +print(f"registry tables: {len(doc.get('tables', {}))} -> {sorted(doc.get('tables', {}).keys())}")
│ +print(f"json.load timings: {(t1 - t0) * 1000:.1f} ms / {(t2 - t1) * 1000:.1f} ms")
│ +reg_counts = {k: len(v) for k, v in doc.get('tables', {}).items()}
│ +print(f"registry row counts: {reg_counts}")
│ +
│ +# --- data/tables: row counts + duplicate-key audit (R15 diff keys) ---
│ +KEYS = {
│ +    'models': lambda r: (r.get('provider'), r.get('model')),
│ +    'model_perf': lambda r: (r.get('provider'), r.get('model'), r.get('category')),
│ +    'model_tier': lambda r: (r.get('provider'), r.get('model'), r.get('category')),
│ +    'category_levels': lambda r: (r.get('category'), r.get('level')),
│ +    'task_profiles': lambda r: (r.get('id'),),
│ +    'task_profile_requirements': lambda r: (r.get('task_id'), r.get('category')),
│ +    'projects': lambda r: (r.get('id'),),
│ +    'providers': lambda r: (r.get('id'),),
│ +    'fallback_lanes': lambda r: (r.get('project'), r.get('position')),
│ +}
│ +
│ +files = sorted(f for f in os.listdir(TDIR) if f.endswith('.jsonl'))
│ +print(f"\ndata/tables files: {len(files)}")
│ +tables = {}
│ +for fn in files:
│ +    name = fn[:-6]
│ +    rows = []
│ +    with open(os.path.join(TDIR, fn)) as f:
│ +        for line in f:
│ +            line = line.strip()
│ +            if line:
│ +                rows.append(json.loads(line))
│ +    tables[name] = rows
│ +    dup_note = ''
│ +    if name in KEYS:
│ +        c = Counter(KEYS[name](r) for r in rows)
│ +        dups = {k: n for k, n in c.items() if n > 1}
│ +        dup_note = f"  DUPLICATE-KEYS: {len(dups)}" + (
│ +            f" e.g. {list(dups.items())[:3]}" if dups else "")
│ +    # full-row duplicate lines (the R9 sorted-set blind spot)
│ +    raw_lines = []
│ +    with open(os.path.join(TDIR, fn)) as f:
│ +        raw_lines = [l.rstrip('\n').rstrip() for l in f if l.strip()]
│ +    line_c = Counter(raw_lines)
│ +    exact_dups = {k: n for k, n in line_c.items() if n > 1}
│ +    dup_note += f"  exact-dup-lines: {len(exact_dups)}" + (
│ +        f" e.g. {sorted(exact_dups.items(), key=lambda x: -x[1])[:2]}" if exact_dups else "")
│ +    print(f"  {name}: {len(rows)} rows{dup_note}")
│ +
│ +# fallback_lanes shape (R15 natural-key question)
│ +if 'fallback_lanes' in tables and tables['fallback_lanes']:
│ +    print(f"\nfallback_lanes columns: {sorted(tables['fallback_lanes'][0].keys())}")
│ +    print(f"fallback_lanes first row: {tables['fallback_lanes'][0]}")
│ +    pc = Counter(r.get('project') for r in tables['fallback_lanes'])
│ +    multi = {k: n for k, n in pc.items() if n > 1}
│ +    print(f"fallback_lanes projects with >1 row: {len(multi)} e.g. {list(multi.items())[:3]}")
│ +
│ … omitted 25 diff line(s) across 1 additional file(s)/section(s)
All evidence is in hand — spec read in full, all cited code regions read, three verification commands executed (data probe; spawn timing ×3; topology). Delivering the mandated review.

════════════════════════════════════════════
1. VERDICTS
════════════════════════════════════════════

C1 CONFIRMED — R4–R7, R52 and R58 put no write path from Redis into JSONL/registry.json, and every data key in the R32 schema traces to a JSONL table (probe: registry.json row counts == data/tables row counts for all 12 tables today); the only non-data keys are meta:owner (config guard) and the R39 fingerprint in data/state/, neither of which is fleet routing data.

C2 CONFIRMED — measured: registry.json 4,792.2 KB, 12 tables, top-level keys {version, generated_at, source, tables}; all §1 row counts exact (models 2064, model_perf 3944, model_tier 3896, benchmarks 2148, projects 950, category_levels 264, task_profile_requirements 72); json.load 31.4–34.7 ms; cold spawn (board venv, 3 runs) 0.33/0.34/0.39 s wall, maxrss 73,232–73,648 kB — spec's 0.28–0.31 s is slightly optimistic but plausible.

C3 CONTRADICTED — the TR-277 claim is true (router_data_cache.py:6–50 states AC0–AC6; AC6 names router_spawn._load_registry_with_meta), but the spec's entire composition text is R29's one-line fallback ordering: it never defines whether the Redis arm enters the cached loader, and the AC0 identity is file-stat-based (router_spawn.py:274–286, tuple of mtime_ns/size/inode over registry.json + all data/tables files), so it structurally cannot validate a Redis-backed entry — the composition is undefined and either silently bypasses the cache or breaks the identity contract.

C4 CONTRADICTED — R9's "sorted set of raw line strings" is invariant to line multiplicity, and the repo contains a live counter-example today: model_notes.jsonl holds 3 exact-duplicate line pairs (258 rows, measured), so deleting one copy of each — a real content change — leaves jsonl_hash identical, falsifying R9's "catching every content change" (reorder-invisibility is a second, unstated miss).

C5 CONTRADICTED — at least four reachable states sit outside the table: DANGLING (pointer names a generation whose keys are gone — status would print MATCH because it never probes the generation, readers get per-key misses), ARTIFACT-UNSTAMPED (measured: registry.json has no 'stamp' key, and _dump_registry at router_seed.py:1811–1847 writes none, so the artifact leg of the three-way compare does not exist today and R10's MATCH/STALE conditions are undefined for it), FOREIGN-CACHE (two checkouts with different data/tables publishing alternating generations — no state, and R12's source_commit field is carried but never used to discriminate), and CORRUPT-POINTER (meta:version present but unparseable — matches none of UNREACHABLE's "TCP connect / auth / protocol failure").

C6 CONTRADICTED — measured against data/tables: model_perf columns are {category, model, perf, source, source_ref} and model_tier {category, model, perf, source_ref, tier, tier_source} — no provider column in either, so R15's declared (provider, model, category) keys reference a non-existent field; fallback_lanes columns are {key_env, model, note, order, provider} — no project, no position — so its declared key collapses all 7 rows into one diff key; the degenerate keys happen to be collision-free today (0 duplicates measured across all tables), but 3 of 9 declared keys do not match the committed schema, and R15's table list omits the 19 non-registry JSONL files (31 files total) that R9's hash does cover.

C7 CONTRADICTED — a single publish is atomic (pointer SET is one command, g<N-1> survives one GC round), but R24 lets publish N+1 delete g<N-1> while a reader still holds it pinned under R26's 60 s pointer TTL: with two publishes inside the window (seed + timer collision, or the C5 dual-checkout ping-pong) the reader's later lookups miss and fall back per-key to files, producing a mixed Redis-old/file-new resolve inside one process — silently, because a miss is not an error.

C8 CONTRADICTED — abandoning the publish protects readers only when the mid-window change persists: R20's before/after hash cannot detect an A→B→A revert inside the write pass (a torn generation gets published with a stamp that matches both ends), the spec never states which source the write pass reads (JSONL directly vs the registry.json artifact), and exit 4 has no named consumer — R13/R42 wire only `status` into cron while `load` runs under a systemd timer whose exit code lands unread in the journal.

C9 CONTRADICTED — 50 ms timeout + per-process latch is adequate for a dead instance, but R30's read-time warning "naming the drift class" has no defined mechanism (the reader would have to hash all 31 JSONL files — measured ~33 ms, i.e. the very cost the plane exists to avoid), and R32's "ROUTING_CACHE=file (an existing switch idiom)" names a switch that does not exist — the real one is ROUTER_CACHE with values '0'/'false'/'no'/'off' (router_spawn.py:249–254) — so R50's byte-identical leg as written tests a no-op variable and passes vacuously.

C10 CONFIRMED — pyproject.toml:13 ("stdlib only — scripts/ tools must run in the bare board venv"), the in-place runpy dispatch (pyproject [tool.setuptools] comment, router_spawn.py:145–146 sys.path bootstrap precedent), and router_data_cache.py's function-local optional imports make a stdlib RESP2 module in scripts/ realistic, and R36's AST gate against sys.stdlib_module_names is enforceable in the existing suite.

C11 CONTRADICTED — the 6379 claim verified (ss: 0.0.0.0:6379 LISTEN; docker: dexdat-core-redis-1 0.0.0.0:6379→6379, svc-redis-exporter present; 6391 absent from ss = free; no redis-server/redis-cli on host), but R39 is internally contradictory — a run_id change is simultaneously "state EMPTY, reload, rewrite the fingerprint" and "fingerprint mismatch → refuse loudly, exit 5", i.e. the guard's two rules fire on the same signal; the port guard refuses only 6379 while this host also runs asce-redis-1 on 6380 and helios-redis on 16379; the fingerprint blesses whatever instance the first successful load reached; and loopback binding without AUTH leaves the plane writable by any local process — insufficient on a multi-agent box.

C12 CONFIRMED — router-averages-refresh.sh is exactly the cited shape (silent on success, one actionable line + exit 1 on failure, lines 14–27), R13/R42 mirror it, R12's --json carries the ages the shell script recomputes by hand, and R44 rides the existing TR-025 source/fallback_used/warning fields already emitted by _load_registry_with_meta (router_spawn.py:1236–1243) so provenance is machine-consumable; residual gap: no post-success staleness alarm (the shell script has one) and EMPTY-after-restart alerts until the 5-min timer heals.

C13 CONTRADICTED — by enumeration against the §11 mapping (R45→R9,R10; R46→R10,R13; R47→R14–R16; R48→R31; R49→R19,R20; R50→R26–R30,R32; R51→R26,R35,R36; R52→R5; R53–R55→R2,R33), no test proves: R7, R11, R12, R17, R21, R22, R23, R25, R34, R37, R39, R43, R44 — thirteen normative statements, including the two guards (R25 flush guard, R39 instance guard) whose failure modes are exactly the silent ones.

C14 CONTRADICTED — measured, the plane can remove at most the ~35 ms parse of a ~350 ms spawn (~10%; interpreter startup dominates the wall time) and TR-277 already covers in-process repeats, so the cold-start value claim is marginal on this box, and R55's falsifier is structurally unfalsifiable: it requires lookup-win <5× AND wall-win <10% AND memory-not-binding, but the point-lookup leg passes 5× trivially (sub-ms HGET vs 35 ms parse), so the AND can never fire — no measurement the spec defines can kill it.

C15 CONFIRMED — the spec reintroduces all six named classes: (a) text-as-state in R30's unmechanized read-time drift-class warning; (b) silent skip in R22.1 (seed exits 0 with a warning when the publish failed — a step reporting green while doing nothing) and R24 (best-effort GC with no failure observability); (c) requirement-weakening in R30 (strict mode exists only behind an env var tests use but production never runs) and R55 (implementation kept even when the value claim dies); (d) green-but-did-nothing tests in R50's ROUTING_CACHE=file leg (non-existent switch → vacuous pass) and R48 (a 50-of-2064 = 2.4% sample); (e) derived-store-drift in R14/R33/R44 (Redis-side diff surface, reverse index, and enumeration set inviting direct consumers, with canon enforced only on spawn via R52); (f) unrepresentable failure in R10's non-exhaustive state table (C5's four states cannot be recorded, so every downstream count lies).

C16 CONFIRMED — four unaddressed risks: (1) multi-checkout/worktree split-brain — the fleet's standard isolation doctrine runs sibling checkouts, and nothing namespaces the Redis plane by data-home, so two 5-min timers publish alternating generations into one instance (permanent STALE flapping — the exact "permanent background noise" failure router_seed.py:1894–1896 warns about); (2) env-override poisoning — ROUTING_REGISTRY ("NEVER removed", registry_path.py:21–23), ROUTING_DATA_DIR (router_spawn.py:166, used by null_debt_invariants.py:243 for subprocesses), TASK_ROUTER_HOME and ROUTING_NS (router_maintain.py:446–452) all redirect the data plane while the Redis target stays shared, so scratch/CI runs publish scratch tables into production's cache; (3) first-load blessing — 6391 is free today, and any service that later binds it gets adopted as the plane by the first successful load (R39 records the fingerprint only after connecting); (4) ownership across hosts — R40 gives one idempotent command but names no owner for the container/timer on fresh or bunker hosts (the seven-year question), and connection-reset mid-HGET is neither R27's timeout nor R37's -ERR reply, so its handling is unspecified.

════════════════════════════════════════════
2. CORRECTIONS (ranked: spec-breaking → correctness → clarity)
════════════════════════════════════════════

S1. R10 — replace the four-state table with six states:
  | MATCH | JSONL stamp == cache stamp, artifact stamp equal when present, AND the pointer's generation passes an EXISTS probe on taskrouter:g<N>:index:models | 0 |
  | STALE | JSONL stamp != cache stamp — message MUST name drift class and differing tables | 1 |
  | ARTIFACT-UNSTAMPED | registry.json carries no stamp (all pre-spec artifacts) — reported on its own line, never alone a reload trigger | 1 |
  | EMPTY | reachable, no taskrouter:meta:version | 2 |
  | UNREACHABLE | TCP/auth/protocol failure, including an unparseable meta:version payload | 3 |
  | DANGLING | pointer present, named generation missing or incomplete | 6 |

S2. R9 — replace the per-table bullet: "Per table: read the file, drop blank lines, strip trailing whitespace per line, take the sorted LIST of raw line strings (duplicates preserved), then sha256 of the newline join. Reorder-only changes are invisible by design (the seed writes deterministic order, router_seed.py:1818–1820); duplicate-line count changes are visible. row_counts is a component of stamp equality (R21), never of the hash: a count that disagrees with a hash-match is an alarm, not silence."

S3. R15 — replace three rows and add scope: model_perf → (model, category); model_tier → (model, category); fallback_lanes → order (single global chain; no project column exists). Add: "The stamp (R8–R9) covers ALL files in data/tables/*.jsonl — 31 today, 19 beyond the registry's 12 — and every non-registry table uses the full-row canonical hash fallback."

S4. R24 + R26 — append to R24: "GC deletes only generations that are both older than the previous one AND untouched for longer than max(ROUTING_REDIS_POINTER_TTL_S, 120 s); a generation is never deleted while it is the pointer's target or immediate predecessor." Append to R26: "A miss on a key of the pinned generation re-resolves the pointer exactly once before falling back to files; a resolve that served some keys from Redis and some from files emits a named warning."

S5. R39 — replace with three independent checks: "(a) refuse when the resolved port is in the product set {6379, 6380, 16379} (measured on this host 2026-11-04; config-overridable); (b) after connect, if taskrouter:meta:owner exists with a different owner id → refuse, exit 5; if owner matches but run_id differs from the recorded fingerprint → state EMPTY, reload, rewrite the fingerprint (this is a restart of our own instance); (c) the fingerprint is written only by load, only after (b) passed — never by a read-path process."

S6. R32 (and R50's last leg) — replace: "ROUTER_CACHE=0 — the existing switch (router_spawn.py _cache_enabled, values '0'/'false'/'no'/'off') — disables the TR-277 cache AND the read plane and must restore byte-identical behaviour to today's path." R50: test ROUTER_CACHE=0, not ROUTING_CACHE=file.

S7. R55 — replace the conjunction: "If the cold router_spawn.py wall-time win is < 10 % (20 runs, including any reader-side stamp cost) OR the end-to-end scheduler tick time win is < 5 % over ≥ 100 ticks, then the honest conclusion is 'cache is correct but the file path is already fast enough here'…" — an OR of killers so the falsifier can actually fire; keep the "still maintained, no further perf work" clause.

S8. R30 — replace: "The reader never computes the JSONL hash. Read-time staleness is detected only from the pointer: a cache stamp older than ROUTING_REDIS_MAX_CACHE_AGE_S (default 3600) adds source='redis' plus a one-line age warning; the drift CLASS is reported only by status, the sole surface that compares stamps."

S9. R20 — replace: "The write pass records (path, mtime_ns, size, inode) per JSONL file as it reads it; the after-check re-stats every file AND recomputes the aggregate hash. Either failing abandons the publish (stat-identity catches the A→B→A revert that a pure hash before/after misses)." State explicitly: the write pass reads data/tables/*.jsonl, never the artifact.

S10. R21 — replace: "compares jsonl_hash AND the per-table hash dict AND row_counts between JSONL and cache stamps; equality of all three is required to skip the write."

C1. New R59 (clarity, multi-checkout isolation): "load refuses (exit 5) to publish when the cache stamp's recorded data-home hash differs from this invocation's unless --force; all keys carry that hash (taskrouter:<datahome>:g<N>:…), so two checkouts get two planes rather than one ping-ponging plane, and runs under ROUTING_REGISTRY/ROUTING_DATA_DIR/TASK_ROUTER_HOME never publish to a plane they do not match."

C2. R22.1 (clarity): the seed's "cache: NOT PUBLISHED (<reason>)" line must be a stable grep target counted by the same no_agent cron that watches status, so a persistently-unpublishing seed is an alarm, not journal text.

════════════════════════════════════════════
3. NEW VERIFIED FACTS (measured 2026-11-04, this host)
════════════════════════════════════════════

- registry.json: 4,792.2 KB, keys {version, generated_at, source, tables} — NO 'stamp' key; json.load 31.4–34.7 ms; row counts identical to data/tables for all 12 tables (zero artifact-vs-JSONL drift today).
- data/tables holds 31 JSONL files — 19 beyond the registry's 12 (largest: model_catalog 1358, null_reasons 564, benchmarks 2148, probe_gaps 241, model_aliases 234, quality_estimates 233); R9's hash scope must say which set it covers.
- Cold spawn (board venv, 3 runs): 0.33 / 0.34 / 0.39 s wall, maxrss 73,232–73,648 kB — spec's 0.28–0.31 s slightly optimistic.
- An R9-style hash over all 31 files computes in ~33 ms — reader-side stamp checking would cost roughly what the 35 ms parse costs, i.e. it cancels the win.
- model_notes.jsonl contains 3 exact-duplicate line pairs (2× each) — live instance of the sorted-set blind spot.
- Schema facts: model_perf = {category, model, perf, source, source_ref}; model_tier = {category, model, perf, source_ref, tier, tier_source}; fallback_lanes = {key_env, model, note, order, provider}, 7 rows, one global chain (xkiro order 1); all R15 keys collision-free today except fallback_lanes' declared key, which collapses all 7 rows.
- Host Redis topology: 0.0.0.0:6379 = dexdat-core-redis-1 (confirmed), plus asce-redis-1 :6380, helios-redis :16379, trouble-redis unpublished; svc-redis-exporter + svc-postgres-exporter run; 6391 free; no redis-server/redis-cli binaries on the host.
- The existing cache switch is ROUTER_CACHE ('0','false','no','off') at router_spawn.py:252 — no ROUTING_CACHE exists anywhere in scripts/.
- TR-277's composite identity spans registry.json + every data/tables file by stat triple (router_spawn.py:274–286) — it cannot represent a Redis generation.
- _dump_registry (router_seed.py:1811–1847) writes no stamp today; the spec's artifact stamp is a seed change the spec never lists as one.
- AGENTS.md:35–37 ("JSONL tables are generated — never hand-edit") and :42–43 (fail-open rule) confirmed as stated.

════════════════════════════════════════════
4. TOP 5 FIXES (most reduce chance of a silent routing failure)
════════════════════════════════════════════

1. Hash with multiplicity + row_counts as an equality component (S2/S10) — closes the measured silent-identical-stamp class (duplicate lines exist in model_notes today; a row removal would leave MATCH true).
2. Exhaustive state table with DANGLING, ARTIFACT-UNSTAMPED, and corrupt-pointer states plus a generation EXISTS probe in MATCH (S1) — kills the unrepresentable-failure class (f) before any downstream count starts lying.
3. GC retention ≥ pointer TTL + reader re-pin-on-miss with a mixed-source warning (S4) — closes the only interleaving that serves a mixed old-generation/file resolve inside one process with no signal.
4. Data-home-namespaced keys + publish refusal on data-home mismatch (S10-clararity/R59) — turns dual-checkout and env-override poisoning into a loud exit-5 instead of permanent generation ping-pong behind a MATCH-ing facade.
5. Real switch and decidable guard: R32/R50 on ROUTER_CACHE=0 and R39's three-check form with the product-port set {6379, 6380, 16379} (S5/S6) — makes the fail-open escape hatch actually tested and the wrong-instance refusal actually deterministic.

Budget note: exactly three verification commands were executed beyond file reads (data probe, spawn timing ×3, topology sweep); all work was read-only. The one blocked call (inline python -c) was rejected by the harness before execution and re-issued as a plain query.

PANEL REVIEW COMPLETE

session_id: local-61c43e290fb1017c43fa34f5b8c1124e02efdaa4d6856483f29f70119835885a

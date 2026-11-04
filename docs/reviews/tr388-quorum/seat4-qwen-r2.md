I now have all the evidence. Here is the complete round-2 verdict:

---

**1. FIX VERDICTS**

F1 FIXED — R9 (spec lines 65-77) states "sorted list of line strings preserving duplicates," "the 12 artifact tables + fallback_lanes = 13 named tables," and "`not_cached: [...]`" for the other 18 JSONL files; verified model_notes.jsonl has 3 duplicate raw lines and the registry has exactly 12 table keys.

F2 FIXED — R11 (lines 85-96) defines 8 states (MATCH/0, STALE/1, EMPTY/2, UNREACHABLE/3, GHOST/4, FOREIGN/5, SCHEMA-AHEAD/6, UNSTAMPED/0-or-1) and R12 (lines 98-100) defines publish-in-flight as STALE(cache-behind,in-flight) with age gate.

F3 FIXED — R15 (lines 111-124) declares real columns: model_perf key=(model,category) with 5 measured cols {category,model,perf,source,source_ref} no provider; model_tier key=(model,category) with 6 cols; fallback_lanes key=(order) with 5 cols {key_env,model,note,order,provider}; R16 (lines 125-127) mandates full-row comparison.

F4 FIXED — R20 (lines 140-147) uses INCR → write keys → WATCH → MULTI/EXEC in sequence with two generations retained and gen-2 GC; R21 (lines 149-152) mandates whole-resolve fallback on missing keys.

F5 FIXED — R10 (lines 79-83) deletes the TOCTOU window: "parse into memory, hash that in-memory set, publish that same in-memory set, no re-read window"; R23 (lines 157-161) exits non-zero (4=aborted, 5=publish) and writes to redis-publish-errors.jsonl.

F6 FIXED — R32 (lines 204-207) names ROUTER_CACHE=0; verified at router_spawn.py:252-253 the gate checks `('0','false','no','off')`; R27 (lines 179-181) sets 20 ms default; R28 (lines 183-188) defines the health file; R30 (lines 194-198) sets MAX_AGE 900s.

F7 FIXED — R42 (lines 266-270) defines explicit URL redis://127.0.0.1:6391, AUTH, denylist ports; R43 (lines 272-282) has three ordered guards: denylist → AUTH → run_id check with restart-vs-FOREIGN distinction.

F8 FIXED — R36 (lines 240-241) mandates category scans at most (#cats×1)+1 reads per resolve; R37 (lines 243-245) pins ≤8 chain, ≤2 point; R34 (lines 233-234) names duplication as measurable first-drop target.

F9 FIXED — R52 (lines 333-338) states "stub resolve that returns a canned chain with no registry read" as the floor, with explicit (cold_spawn - floor < 10% AND point lookups < 5×) falsifier conditions — single OR not AND.

F10 FIXED (text only) — R49 (lines 302-304) defines a coverage table mapping each R# to a named test and mandates a coverage checker; the spec text satisfies the requirement. `tests/test_redis_read_plane.py` does not yet exist (pre-implementation deliverable).

F11 FIXED — R8 (lines 57-61) derives scope from `realpath(DATA_DIR)` producing `<scope>` = first 12 hex of sha256, with stamp recording checkout and schema; two checkouts cannot share a plane.

F12 PARTIAL — R40 (lines 256-259) states `router_redis.py` must be added to `sync_runtime.sh` and a test asserts the list contains it; however `scripts/router_redis.py` does NOT exist and `router_redis.py` does NOT appear in either `for f in` loop in sync_runtime.sh (copy list at line 65 ends with `router_data_cache.py`). The spec mandates it as part of the implementation deliverable but the registration has not yet been applied to the repo file.

F13 FIXED — R46 (lines 291-293) adds `cache: {status, generation, stamp_age_s, scope}` to the resolve payload and states `source` is kept untouched; provenance is additive, not replacement.

F14 FIXED — R53 (lines 340-342) withdraws the remote/bunker caller benefit: "Any remote/bunker caller benefit is out of scope for this topology (loopback instance)."

**2. NEW DEFECTS**

A1 CONFIRMED-DEFECT — R20 does NOT state that WATCH, MULTI, and EXEC happen on the SAME connection. In RESP, WATCH binds per-connection but the spec never says "one pipeline." If the publish is split across connections, WATCH covers nothing. **Wording to add to R20 after "write every key":** "All Redis commands in the publish sequence (INCR, WATCH, MULTI, EXEC, key writes, DEL for GC) execute on a single connection, ideally pipelined." Additionally, the spec names GC of generations older than `gen-2` but does NOT define who collects the abandoned generation if the process dies between writing the keys and the EXEC — a generation with no pointer reference becomes orphaned data.

A2 CONFIRMED-DEFECT — R28's `redis-health.json` is a plain text file used as consensus state. A partial write (e.g. process killed mid-JSON) yields unparseable content. Two writers (status, load, timer) can race. A stale `ok:true` from a dead process persists until checked_at expires. **Wording to add to R28:** "Written atomically via write-to-tmp-then-rename. Consumers that fail to parse the file treat it as `ok:false` and fall back to file backend."

A3 PARTIAL — R8's `realpath(DATA_DIR)` scope is safe for readers with a different scope (they get EMPTY, not wrong data). However, when a checkout moves or a symlink is dereferenced differently, the old scope's keys are never collected — a resource leak. **Verdict:** Not a correctness defect but a named maintenance cost the spec should acknowledge. Add to R8: "Old-scoped keys are not automatically collected; a periodic `<scope>:` SCAN+DEL should be run when scopes change."

A4 CONFIRMED-DEFECT — R37's ≤8 round-trip budget for a chain resolve does NOT include the profile requirement reads from `profile:<id>:req` keys (R33's schema). If the chain's category scan requires N category reads plus M profile requirement reads, the total is N+M+1, not just N+1. A profile with 12 required categories could hit 13+ round trips. **Wording to add to R37:** "The budget counts all keys read per resolve including profile requirements (`profile:<id>:req`) and level definitions. If a profile's required categories exceed the budget, fall back to the file path."

A5 CONFIRMED-DEFECT — R11's UNSTAMPED exits 0 if JSONL↔cache agree, which means every installation today (where `registry.json` has no `stamp` key — verified at `/home/kara/task-router/registry.json`: top keys {version, generated_at, source, tables}, NO stamp) is functionally indistinguishable from MATCH. A cache built before a future seed change would report "MATCH-equivalent" while the artifact is unverified. **Wording to add to R11:** "When UNSTAMPED, the cache is served with `resolved_at=cache` and a `source` field carrying `cache_verified=false`. A consumer comparing against a future stamped registry must reject this state."

A6 NOT-A-DEFECT — R30's MAX_AGE is by design. The reader serves cache within the window and falls back to file past it. This is an intentional availability/correctness tradeoff, not a gap.

A7 CONFIRMED-DEFECT — R49's "one named test per normative R#" claim is vacuous for negative claims: R7 (no locks/leader election/queueing/rate limiting), R35 (no price-derived ordering in Redis), R54 (non-goals: not a write plane/message bus/etc.), R55 (no change to routing decisions), R56 (no new dependency), R57 (no per-lane round trips). These are absence claims that a test cannot positively assert. **Wording to add to R49's coverage table:** "Negative claims (no X, not a Y, non-goal Z) are verified by code-review assertion and static-analysis (AST for imports, grep for patterns), not by runtime test."

A8 UNRESOLVED — The spec says the `not_cached` list in R9 must name the 18 out-of-scope tables. This list must be manually maintained and kept in sync with `data/tables/`. A new JSONL added to the directory but omitted from `not_cached` silently changes the hash scope. **Recommendation:** The load command should compute `not_cached` automatically from the directory listing minus the 13 cached names.

**3. RESIDUAL ROUND-1 ITEMS**

C12 UNRESOLVED — The round-1 disposition table marked C12 "3 CONFIRMED, 1 UNRESOLVED." R46 and R14 were added but the original concern was that the age gate alone doesn't fully solve the "cache older than expected" problem — A5 (UNSTAMPED exits 0) is a residual part of this concern. The fix is partial: the age gate on R14 catches old caches, but the UNSTAMPED silent-pass-through (A5) means every current installation is "acceptable" while technically unverified.

**4. TOP 5 REMAINING RISKS FOR IMPLEMENTATION**

1. **Connection singlehood (A1):** The RESP2 client must pipeline WATCH→EXEC on one connection. A split-connection publish silently loses CAS protection.
2. **Health file atomicity (A2):** The health file must be written via tmp+rename, or the per-process latch problem that R28 was meant to solve reappears as a corruption risk.
3. **Round-trip budget overestimation (A4):** The ≤8 budget excludes profile requirement reads. Implementers who add profile reads without adjusting the budget will silently exceed the contract.
4. **`not_cached` list drift (A8):** Manual maintenance of the 18-file exclusion list is a latent bug — a new JSONL in `data/tables/` silently participates in the hash.
5. **Missing module deployment (F12):** `router_redis.py` must be created, tested, AND registered in `sync_runtime.sh`'s copy list. F12's PARTIAL verdict means the deployment gate is unproven — a missing module in the copy list means the Redis plane silently fails to activate on a fresh install, even if the code is correct.

PANEL REVIEW COMPLETE

session_id: local-60dbaa3dd8abd4018e00fb6132d8aee8a4c1493d8ebcef97c51972f9c7c7c90a

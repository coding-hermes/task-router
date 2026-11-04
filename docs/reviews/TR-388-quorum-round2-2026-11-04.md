# TR-388 — quorum ROUND 2 (fixes landed?) + attack on the new design

Date: 2026-11-04 · Same four seats / four families as round 1 · Artifact: SPEC-REDIS-READ-PLANE **v0.2**
Merged result: **v0.3** · Round-1 record: `docs/reviews/TR-388-quorum-2026-11-04.md`

## Seat identity (proven, not labelled)

| seat | requested lane | served (`session_model_usage`) | bytes | detector |
|---|---|---|---|---|
| 1 | `glm-5.3 @ zai-glm` | `glm-5.3` @ `api.z.ai` | 23,718 | `PANEL REVIEW COMPLETE` |
| 2 | `kimi-k3 @ neuralwatt` | `kimi-k3` @ `api.neuralwatt.com` | 17,799 | `PANEL REVIEW COMPLETE` |
| 3 | `deepseek-v4.1-flash @ neuralwatt` | `deepseek-v4.1-flash` @ `api.neuralwatt.com` | 31,309 | `PANEL REVIEW COMPLETE` |
| 4 | `qwen3.6-35b @ neuralwatt` | `qwen3.6-35b` @ `api.neuralwatt.com` | 9,431 | `PANEL REVIEW COMPLETE` |

## Job A — did the round-1 fixes land?

| fix | glm | kimi | deepseek | qwen | merged as |
|---|---|---|---|---|---|
| F1 hash multiplicity + table set | PARTIAL | FIXED | PARTIAL | FIXED | **PARTIAL accepted** — the set was never enumerated and "12 + fallback_lanes = 13" double-counted a table that is already inside the 12 (verified). v0.3 lists all 12 names, computes `not_cached` from the directory (19 today), and prints `tables=12` |
| F2 state table | FIXED | FIXED | PARTIAL | FIXED | **PARTIAL accepted** — the in-flight state had no fact to age (nothing wrote a progress key). v0.3 adds `<scope>:meta:publishing` |
| F3 diff keys | FIXED | FIXED | PARTIAL | FIXED | **PARTIAL accepted** — keys for `archetypes`/`benchmarks`/`level_defs` were missing. Added |
| F4 CAS publish | FIXED | FIXED | FIXED | FIXED | landed; holes in its execution became A1 |
| F5 single-read publish | FIXED | FIXED | FIXED | FIXED | landed |
| F6 real switch + bounds | FIXED | FIXED | FIXED | FIXED | landed |
| F7 instance guards | FIXED | FIXED | FIXED | FIXED | landed; strengthened to an allowlist in v0.3 |
| F8 category scans + budget | PARTIAL | FIXED | PARTIAL | FIXED | **PARTIAL accepted** → became A4 |
| F9 floor falsifier | FIXED | FIXED | PARTIAL | FIXED | **PARTIAL accepted** → the 5× leg was undefined; v0.3 defines it and states which leg decides |
| F10 coverage claim | PARTIAL | FIXED | PARTIAL | FIXED | **PARTIAL accepted** — range said R1–R64, document had R1–R57; negative claims unassertable → v0.3 two assertion kinds + range parsed at run time |
| F11 data-home scope | FIXED | FIXED | PARTIAL | FIXED | **PARTIAL accepted** → scope identity was keyed on `DATA_DIR` alone (A3) |
| F12 sync_runtime registration | PARTIAL | FIXED | PARTIAL | PARTIAL | **PARTIAL accepted** — the spec did not say WHICH list; must be the byte-copy loop (`:65`), TR-288 rule |
| F13 additive provenance | FIXED | FIXED | FIXED | FIXED | landed |
| F14 bunker claim withdrawn | FIXED | FIXED | FIXED | FIXED | landed |

11 FIXED · 3 PARTIAL (each merged) · 0 NOT-FIXED — **but the round's real yield was Job B.**

## Job B — new defects v0.2 introduced (all four seats, ranked by what they would have cost)

| id | defect | why it mattered | v0.3 |
|---|---|---|---|
| A6 | **age deadlock** — `load --if-stale` writes nothing when hashes match, so the stamp's age only advanced on content change; 15 minutes after the last change every reader fell back to the file while Redis was healthy and provably correct | the feature builds, runs, and is never served — a silent no-op with a green status | `created_at` is a stamp field (R9); `--if-stale` republishes identical data at MAX_AGE/2 (R22); alarm at 2×MAX_AGE (R14) |
| A4 | **budget contradicted its own formula** — P0_FORE has 17 required categories ⇒ 18+ round trips against a pinned ≤8; and `cat:<category>` carried only `level`, so price/plan_tier/token_factor/context_limit/lifecycle needed per-lane reads: exactly the pattern the spec forbids, over up to 1,024 lanes | either the budget test fails on first contact with real data, or it is written against a toy profile and lies | category hash carries the whole decision tuple (R34); category scans are one pipelined batch = 1 round trip (R36); budget tested against all 11 profiles (R37) |
| A1 | **WATCH is per-connection** — a pooled/EXEC-on-another-connection CAS is vacuous (reintroducing the pointer regression C7 fixed), the abort path never sent UNWATCH/DISCARD, and a crash between the write pass and the CAS orphaned a full generation that nothing collected inside a `noeviction` instance until writes failed | a crash loop bricks the plane and `status` reports MATCH | one dedicated connection for the whole publish, UNWATCH/DISCARD on abort, `__ts` per generation, orphan reap inside `load`, `orphans` in `status --json` (R20) |
| A2 | **health file was text-as-state** — no atomic write, three writers, a single read-path `-ERR` flipping `ok:false` for the whole host, and no owner of recovery | one transient error = fleet-wide plane outage; a dead timer = permanent outage nobody alarms on | writers-only + atomic replace, `UNKNOWN` semantics, recovery owned by the timer with a 10-min stale alarm (R28, R14) |
| A3 | **scope identity was wrong and unbounded** — keyed on `DATA_DIR` while `REGISTRY` resolves independently (a hermetic run could publish into production), and dead scopes accumulated forever | poisoning in one direction, capacity failure in the other | scope hashes both paths, heartbeat + TTL + dead-scope sweep, scratch publish opt-in, `scopes_live`/`dbsize` reported (R8) |

Smaller merges: stamp equality must exclude `not_cached` (else a stray file forces republish); port
denylist → allowlist; generation/created_at missing from the stamp field list; three wrong
cross-references (R2→R52, R33→R51, R34→R51).

## Coordinator re-verification of round-2 claims

| claim | verified how | outcome |
|---|---|---|
| registry.json has exactly 12 table keys and already includes `fallback_lanes` | parsed the artifact | TRUE — the "13 tables" arithmetic was mine and wrong |
| P0_FORE = 17 requirement rows; five of eleven profiles breach ≤8 | parsed `task_profile_requirements.jsonl` | TRUE |
| `_build_chain` filters/sorts on plan_tier, price×token_factor, context_limit, lifecycle | read `router_spawn.py:2798-2815` | TRUE — the `cat:` value shape was insufficient |
| `order` is unique across the 7 `fallback_lanes` rows | parsed the table | TRUE |
| `router_data_cache.py` sits in the COPY loop (`sync_runtime.sh:65`) | read the script | TRUE — the spec now names the loop |
| today's `registry.json` has no `stamp` | read the artifact | TRUE (already established in round 1) |

## Standing

v0.3 is merged but **unjudged**. Round 3 is recommended against the first implementation commit
(tests, not prose): five of the defects above were found by attacking a fix, which is the class prose
review stops catching. Raw outputs: `docs/reviews/tr388-quorum/seat*-r2.md`.

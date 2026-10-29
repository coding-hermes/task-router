# Task-router dogfood log

| date | verdict | promise | top findings | install_seconds | time-to-first-success | notes |
|---|---|---|---|---|---|---|
| 2026-09-12 | SHIPPABLE | A user resolves a task to a price-ordered, gate-filtered model chain via `pip install -e .` + `router spawn --format json`, state under a configurable data home, fail-open runtime | 1) ROUTER-MISS telemetry ~1004 lines/resolve, 724 tier=None — TR-039 "down to 0" does not reproduce (TR-043). 2) 7 subcommands ignore the data home; server/CLI heads disagreed (TR-044). 3) Quickstart `router seed` fails without undocumented duckdb; ROUTING_NS not exported → scratch seed stomps live DuckBrain ns (TR-045) | 3 (control) / 5 (bunker fresh install, Python 3.13, no toolchains) | ~4 min (clone→install→first resolve; seed adds 6s once duckdb installed) | bunker=las-bunker-03 agent=b6b57895 smoke=ok; circuit loop E2E PASS; API 403/15 paths ok; web 200; 290 tests green 220s; foreman NOT woken (cooldown 21600 already active, board has live work) |
| 2026-09-16 | SHIPPABLE | Same promise, now with MCP bridge + web UI + xKiro plan-tier economics on 115 lanes | 1) ROUTER-MISS flood improved to 760 lines/479 tier=None but far from 0 (TR-055). 2) status vs spawn read different registries from the same cwd — live TR-044 repro (TR-056); seeded data-home install FAILS router validate on a fresh box (TR-057, bunker-caught). 3) wrapper coerces argparse exits to 0 for ALL subcommands incl. validate (TR-058); bare profile id dead-ends without --profile hint (TR-059) | 5 (bunker agent fde15ade, Python 3.13; +14 duckdb +8 seed ≈ 27s to first real resolve) | ~5s control host (status+resolve); ~27s fresh box | bunker=las-bunker-03 agent=fde15ade smoke=ok (spawn 18-hop chain, head==production; validate [FAIL] = TR-057); MCP 15 tools E2E via tools/call; circuit+ledger loops PASS on synthetic pairs; web /api/preview == CLI == REST == MCP head; 321 tests green 321s; correction: seed-without-duckdb DOES exit 1 (09-12 pipe-masked reading); foreman NOT woken (fleet 6h law; foreman actively committing mid-run) |
| 2026-09-20 | PROMISING-BUT-ROUGH | Same promise (deterministic chain + gates + TR-067 zero-effort proxy) | 1) TR-081 P1 proxy ladder: max_hops=2 -> ONE attempt at hop 4 ($1.52/M) while hop 1 ($0.082/M) was never tried; ladder reads as a complete walk. 2) TR-082 router seed && router validate exits 1 on a fresh clone/box (fallback_lanes 0s newer than registry.json) - twice reproduced. 3) TR-083 data-home does NOT scope state writers: router quota set wrote the LIVE ~/.hermes/model-router gate (cleared same minute, verified); TR-084 soft-gate doc's circuit invocation exits 2; TR-085 bunker-qa launch wrote no .meta and collect probed a 2-day-old agent id | 5 (bunker fresh install, Debian 13 / Python 3.13.5, no toolchains) / 2.5 control host | ~8s control host (clone->install->first 43-hop chain); seed +19s / ~15s on the fresh box | bunker=bunker-las-02 agent=f413fd0b smoke=ok (clone 4s, venv+install -e 5s, duckdb 5s, spawn/seed/validate/circuit/ledger run); VERIFIED FIXED live: TR-055 (0 ROUTER-MISS lines), TR-056, TR-058 ($? real), TR-059; TR-067 proxy end-to-end 200 (ROUTER-DOGFOOD-OK via glm-5.3); API 9 reads 200 / 404 unknown / 403 mutations; MCP 16 tools; web 200 + 403 on write; harness: bunker-qa.sh launch SIGTERMed at the tool cap, no .meta, collect hit stale agent e3d3cd28 (TR-085), agent destroyed by hand; foreman NOT woken (cooldown 21600 s, board has live TR-070/071-079 rows) |
| 2026-09-25 | SHIPPABLE | Same promise (deterministic chain + gates), probing the two untouched surfaces: versioned/tagged profiles + provider-mapping rename rules | 1) TR-131 P1: retagging to v2 shadows the v1 row's exact id, no pin syntax — README "pinned old versions still resolve by version" is unimplementable as documented. 2) TR-132 P1: provider_mappings rename rules are seed-report-only; a renamed external lane never routes (registry keeps the external id). 3) TR-133 P2: --profile near-miss hint never fires (project-slot only). Perf: spawn 76ms±25 warm / 91ms cold — no perf row warranted | 18 (bunker fresh install, Debian 13 / Python 3.13.5) | ~40s control (venv+install -e), install→first resolve 35s bunker | bunker=bunker-las-03 agent=f7dc60b9 smoke=ok (seed 11s, spawn/estimate/adhoc green); angle=new: profiles-versioning + mappings (runs 1-6 did CLI/graph, MCP/web/seed, proxy ladder); isolated TASK_ROUTER_HOME/ROUTING_NS/ROUTING_DATA_DIR scratch — zero live-state writes; foreman NOT woken (cooldown 21600, board has live TR-129/130) |
| 2026-10-01 | SHIPPABLE (drift watch) | Same promise (deterministic chain + gates); angle = deployment drift + live consumer path | 1) TR-255 P1: serving router_server.py 23 commits behind HEAD (TR-194 fix + TR-235 verdict not in process); /health stale:true honest but consumed by nothing. 2) TR-256 P1: 2 of 6 sync_runtime.sh copy-list files drifted on the LIVE side (fleet-cooldown-policy.py re-has the unbounded pagination TR-205 fixed; router_health.py lacks TR-235). 3) TR-257 P2: control-host ssh keys rejected on all bunker aliases — install leg ran via bunker CLI; infra follow-up needed | 3 (bunker-las-03 agent 95500c74, CLI-managed) | ~30s clone→first resolve (clone 5 + venv/pip 5 + seed 19); spawn 0.10-0.11s cold | bunker=bunker-las-03 agent=95500c74 smoke=ok (spawn P1_CODING exit 0 real chain; validate 14/14; clone HEAD 72206c5); agent destroyed+verified; report: docs/dogfood/2026-10-01-run31-deploy-drift-consumer.md; foreman NOT woken (fleet 6h cooldown law, board carries live audit rows) |
| 2026-10-13 | SHIPPABLE | Same promise (deterministic chain + gates); angle = the un-dogfooded surfaces: TR-298 automatic board task intake, TR-236 bus ingress, TR-223 credential-acceptance fix | 1) TR-319 P2: intake cold-start with only TASK_ROUTER_HOME set → 48/48 resolves with head None (spawn's gate plane defaults to live ~/.hermes/model-router, not the scratch home) — silently empty, looks like success. 2) TR-320 P2: ingress openai-compatible door 400s against multi-model real targets both with model:'router' (unhosted id) and model:'none' (server demands a field). 3) TR-321 P2: forward-door circuit breaker is per-process — repeated forwards never trip. TR-223 verified live: CREDENTIAL-BLOCKED openrouter:274 + reason, JSON credential_blocked_unranked, 0 openrouter lanes in --next-wave (54 lanes/9 providers) | 21.6 (spawn) + 39.5 (clone→seed→spawn inside bunker agent 299229b7 on bunker-mvp; status+seed+spawn+validate all rc=0, validate 14/14) | ~5s control (clone HEAD 93ef98f→venv→spawn real chain); ~40s fresh box | bunker=bunker-mvp agent=299229b7 destroyed+0 residue; intake idempotence verified (new=0 replay; fresh row → exactly 1 resolve); ingress refusal ladder 6/6 (unknown-endpoint, endpoint-incomplete, auth-unresolved, no-prompt, duplicate, unauthorized×2); push door → gateway → bus reply ok 3.9s; circuit 3-strikes verified under serve; spawn 161ms±15 warm / 150ms cold — no perf row; foreman NOT woken (cooldown law, dogfood lane is read-only on scheduler state) |

| 2026-10-13 (run 33, second same-day run) | PROMISING-BUT-ROUGH | Same promise; angle happened to repeat run 32's surfaces (lane prompt misseed → resolved target by lane NAME = task-router) — re-proof on fresh evidence, no duplicate rows filed | 1) TR-322 NEW P1: intake on a fresh box with no scorer credentials (no ROUTER_CLASSIFIER_BASE_URL / OR_JEV) resolves NOTHING — task text found, both scorers degrade, no profile to degrade to, spawn NO_INPUT, stderr empty (premise distinct from TR-319's gate-plane split). 2) TR-323 NEW P1: ingress forward fires a doomed no-model call on a resolver error instead of refusing per 'the doomed call is never fired'; also forward exits 1 on this failure vs 0 on all other ledgered failures. 3) TR-320 re-proved with a third arm (resolver-error → same observable as model:'none'); recorded as event, row untouched. CONFIG finding: task-router-dogfood's stored prompt is the warpfs prompt verbatim (shared with 5 sibling lanes) | 40 (bunker-mvp agent ded45cd3: install 5s + seed 28s + probes ~7s; validate 14/14; spawn 0.19s) | ~6 min control (clone→install→first webhook accept) | bunker=bunker-mvp agent=ded45cd3 destroyed+0 residue; intake idempotence + crash-claim recovery re-verified verbatim; ingress refusal ladder 5/5 named (unknown-endpoint, transport-error, endpoint-http-501/400, auth set); perf: forward 96.5ms±8.8 warm, intake drained-once 48.7ms±2.1 — nothing user-noticeable, no PERF row; foreman NOT woken (read-only lane, cooldown law) |

| 2026-10-16 (run 34) | SHIPPABLE | Same promise (deterministic chain + gates); angle = the 64-commit delta since run 33, none of it dogfooded: TR-277 runtime data cache + cache kill-switch fixes (d024cc1, bb71603, 3926d14), TR-212 quota remaining panel, TR-213 quota headroom ordering | 1) TR-324 P2: /api/ui/quota 404s on the :9093 web door — the quota panel (page + route) exists only on the :9092 server; :9093's embedded page still claims 'the command center, same page, no second UI'. 2) TR-326 P1 re-proof of QA-TR-FRESH-001 on unpatched HEAD c16714d: fresh-venv collection ERROR reproduces (module-level import duckdb); the fix sits UNCOMMITTED in the main tree's working files with .gitreins QA-TR-FRESH-001 in_progress — needs landing. 3) TR-325 re-proof of TR-277 live: rewrite->reload->read-your-write under a running server (reloads increment, 0 stale, 0 errors, stat-poll fallback), ROUTER_CACHE=0 kill-switch chain-identical, 30/30 cache+quota-panel tests green | 36 (bunker-mvp agent 69f28085: clone 2s + venv+install 9s + duckdb+seed+first resolve+validate ~25s; validate 14/14; spawn 0.25s) | ~25s fresh box (clone->seed->first real chain); ~7s control | bunker=bunker-mvp agent=69f28085 destroyed+0 residue after 90s recheck; perf: spawn 113.1ms±7.2 warm (n=20), ~137ms first-cold, quota panel 10.7ms±0.8, registry browse 9.7ms±0.5 — nothing user-noticeable, NO PERF row; live-state clean: 0 writes to ~/.hermes/model-router or ~/.local/share/task-router from this run (the 15:29 mtimes there are the fleet's own :9092 server + a sibling lane's guard run); foreman NOT woken (read-only lane, cooldown law) |

## 2026-10-16 (run 35) — TR-251 mechanical-completeness surface (first drive)

| date | verdict | promise | top findings | install_seconds | time-to-first-success | notes |
|---|---|---|---|---|---|---|
| 2026-10-16 (run 35) | PROMISING-BUT-ROUGH (surface-scoped) | Same project promise; angle = the ONE untouched surface: TR-251 boundary-gate toolkit that landed today (a0c7fc6+c16714d) after runs 32/33 | 1) TR-339 = 5 sub-findings: [01] resolve_run accepts ANY 8-12 digit id as CI run (fabricated 9-digit id passes full false-completion ladder — the exact sin TR-251 kills); [02] zero command.json across 223 real verdict runs (no store writer exists — CI replay step is first-blank then doomed); [03] replay refusal carries no skip-arg for honest unreplayable verdicts (pre-push=block-push, CI=fail-run); [04] gitreins verdict fe0601db says passed=false at d0db679, fixes landed after, TR-251 board row still pending (correct — the accepted proof is evidence-route-launched, no re-run/verdict recommitted); [05] outcome_probes check-all wired to NO boundary and all 122 probes carry the author placeholder | 20 (bunker-mvp agent 44c65b6f, all pip paths refused PEP668 until venv — README quickstart assumes venv per line 69; first resolve ~14s after venv fix) | ~per-op < 100ms (dirty state. board_commit_boundary_gate 80.8ms±22.2 warm; evidence_replay 79.7ms±31.4; hyperfine on real repo, no cgroup churn) | bunker=bunker-mvp agent=44c65b6f destroyed + keys removed; config finding re-verified: task-router-dogfood's stored prompt is STILL warpfs's verbatim (3rd confirmation: run 33 10-13, this run; sibling run 34 same day re-reviewed the lane prompt as its own angle and did not file a fix either); install leg + real-store probe + ladder re-proof all ran read-only; no source/code mutated; foreman NOT woken (self-paused + read-only lane law + cooldown); rows filed to task-router board: TR-339 (+RUN-35-TR251-INSTALL/CONFIG/HYGIENE); docs/dogfood/2026-10-16-run35-tr251-boundary-gates.md + skills/task-router-tr251-usage/SKILL.md committed |
  sibling run note: run 34 (same day, committed 200c67b) covered TR-277 cache + TR-212 quota plane real-use (TR-324/325/326) — NO surface overlap with this run 35 (TR-251 boundary gates); run number taken by the sibling, this entry renumbered 34->35 and its rows RUN-34-*->RUN-35-* per the collision recipe.

## 2026-10-19 (run 36) — TR-281 quality-artifact surface (first drive) — 🟡 PROMISING-BUT-ROUGH

| date | verdict | promise | top findings | install_seconds | time-to-first-success | notes |
|---|---|---|---|---|---|---|
| 2026-10-19 (run 36) | PROMISING-BUT-ROUGH (surface-scoped) | "one machine-readable quality artifact, one verb, one authority — gitreins, the board and the UI read the same numbers" (TR-281 AC3) | 1) TR-1000976681059241 P1: the row is closed but its CONSUMER half does not exist — zero non-test readers of data/state/quality-artifact.json (no gitreins GR-142/143 guard, no board script, no UI route, no CI step, no Makefile target); the producer works and is honest (null+reason for coverage/wiring), it is simply read by nobody. 2) TR-1000976681059242 P2: two producers of one metric name — artifact type_hint_pct 52.2556 (type_hint_score.py: scripts/ + top-level packages, 1596 fns) vs the ladder's own definition_cmd quality_score.py = 52.47 (returns only, scripts/ only), same tree 42e4e60, reproduced on the control host AND a fresh bunker box. 3) TR-1000976681059243 P3: the verb is undiscoverable — no README/docs/Makefile mention. Recurrence event 20261019235704: lane prompt still the warpfs brief verbatim (4th confirmation), target resolved by lane NAME. | documented path: clone 2s + venv install 10s + duckdb 2s (verbatim `pip install -e .` rc=1, PEP 668) | ~45s fresh box (clone->seed->first spawn); artifact verb 0.50s warm / 1s cold; spawn 0.14-0.15s warm | bunker=bunker-mvp agent=df-tr-1019b destroyed + 0 residue after 90s; perf 504.3ms±26.8 (n=20) artifact verb, 460.3ms±32.1 ladder verb, spawn 150ms — nothing user-noticeable, NO PERF row; no code/existing-row/scheduler mutation; docs/dogfood/2026-10-19-run36-quality-artifact-consumers.md + diagnostics.md run-36 section + skills/task-router-usage patch; foreman NOT woken (read-only lane) |


## 2026-10-19b (run 37, same tick 2026-10-19-23-43-04, second angle) — TR-340 circuit-class re-proof — 🟡 PROMISING-BUT-ROUGH

Run 36 (same tick, committed 387f0e8) drove the TR-281 quality-artifact surface. This second angle drives a
DIFFERENT surface: the load-bearing one under the fleet's worst open P0 — `TR-340`, "router-proxy records
gateway 503 NO-OPEN-HOP as an api_down failure on the attempted pair — self-poisoning breaker cascade"
(filed 2026-10-19T21:06Z, still pending). No code, no row and no scheduler state was touched; TR-340's row is
byte-untouched and the evidence is an event.

| date | verdict | promise | top findings | install_seconds | time-to-first-success | notes |
|---|---|---|---|---|---|---|
| 2026-10-19b (run 37) | PROMISING-BUT-ROUGH (re-proof + bisect) | "A gateway/router-generated failure must not become a provider outage" (TR-340's own contract, TR-182's comment: "A timeout is not a provider outage, so it must not be recorded as one") | 1) CLOSED HALF: the fake identity is genuinely closed — `record-failure none none "no open hop for this request"` now prints `SKIPPED none/none — not a lane (no-hop ledger row); no circuit event recorded`, rc=0, state file stays empty (server guards router_server.py:3592-3606 + `_NOT_A_LANE`). 2) LIVE HALF, sharper than the row states: `_circuit_class()` (router_server.py:3372-3390) is a SUBSTRING test whose fall-through is HARD `api_down` (30m pair open, provider-wide at 3), and the failed-hop call site (router_server.py:4222-4224) feeds it the upstream payload's own error text — so **10 of 14** realistic failure shapes land HARD, including the router's own text (`no open hop for this request`, `503 no open hop for this request`, `502 bad gateway`, `no-hops`, `unservable-rating`, `registry-missing`, empty string). Only 429-shaped → quota_window and timeout-shaped → overload get the soft pair-only cooldown. 3) Fix direction sharpened: the identity filter cannot stop the cascade; the router-generated shapes must classify as NON-PROVIDER (record nothing / soft class), else 22 pairs across 12 providers is reachable again through REAL pairs. | n/a — install premise inherited (below) | probe setup < 2 min; classification probe is instant | evidence event `20261019235706` on TR-340; installer leg explicitly inherited, see below; nothing slow enough for a PERF row (the probe is a pure-function call) |

**Discriminating probes (exact commands).**

```bash
# 1. the fake identity (the half the row names first)
ROUTER_STATE_DIR=/tmp/df-tr36b-home python3 scripts/router_circuit.py \
  record-failure none none "no open hop for this request"
#  -> SKIPPED none/none — not a lane (no-hop ledger row); no circuit event recorded   rc=0

# 2. the class table the blast radius actually hangs on (pure function, no writes)
ROUTER_STATE_DIR=/tmp/df-tr36b-home python3 /tmp/tr-leg-1019/class_probe.py
#  -> 10 of 14 realistic reasons -> api_down (HARD); only '429 …' -> quota_window, '… timed out …' -> overload

# 3. the classification, live, through the CLI on a REAL pair
ROUTER_STATE_DIR=/tmp/df-tr36b-home python3 scripts/router_circuit.py \
  record-failure deepseek deepseek-v4-flash "no open hop for this request"
#  -> OPEN deepseek/deepseek-v4-flash class=api_down — 1 consecutive failures, cooldown 1800s
```

Probe 3 is the one to read twice: the reason recorded on a real pair is the ROUTER's own 503 text, stored with
the HARD class. That is TR-340's cascade with the fake identity removed — it needs no `none` pair at all.

**Why no new row.** The finding is the same defect the pending P0 already owns; per the dogfood skill the
bisect goes onto the board's EVENTS file as evidence for that row id, never as a sibling row. TR-340's row was
left byte-untouched (`git diff --stat -- .coding-hermes/board/tasks.jsonl` = empty).

**Install leg: EXPLICIT INHERIT, not a silent pass.** `git log -1` = 387f0e8 and the only delta since run 36's
leg is that commit (board + docs, no code). Run 36's leg already cloned and installed EXACTLY this commit
inside ephemeral bunker-mvp agent `df-tr-1019b`: documented clone 2s, verbatim `pip install -e .` rc=1 (PEP
668), venv rescue 10s, duckdb 2s, `router seed` 31s, `router spawn P1_CODING` resolves — PASS, agent destroyed,
0 residue. Re-spawning for an unchanged premise adds nothing, so this angle inherits that proof and says so.


## 2026-10-19c (run 38, same tick 2026-10-19-23-43-04, third angle) — CI red re-proof (INT-CI-5) with a local control — 🟡 PROMISING-BUT-ROUGH

Third angle of this tick: run 36 drove the TR-281 quality-artifact surface, run 37 re-proved TR-340's circuit
blast radius, and this angle drives the repo's CI gate itself — the one surface every other lane's landing
depends on, because a red main makes the pre-push gate refuse landings fleet-wide.

**Verdict:** the pending P1 `INT-CI-5` is RE-PROVED, now with a same-head local control, and it is not fixed.

| date | verdict | promise | top findings | install_seconds | time-to-first-success | notes |
|---|---|---|---|---|---|---|
| 2026-10-19c (run 38) | PROMISING-BUT-ROUGH (re-proof + control) | "A green local suite on the same tree means a green CI run" — the repo's own pre-push gate reads main's CI state | 1) FIFTH INSTANCE of the INT-CI-5 hang: run 39325031921 (head 42e4e60, job 118308514821, 21:07:49Z -> 21:31:15Z ≈ 23m26s) dies at step 7 'Full test suite (pure JSON — no duckdb needed)'; the frame chain is byte-for-byte the one the row cites (test_symlink_path_resolution.py:286 -> :155 -> :141 `runpy.run_path(<scratch>/contract-links/router_seed.py)` -> `router_seed.py:1386 <module> apply_overlay()` -> `:1339 con.execute("SELECT perf, source, source_ref FROM model_perf ...")` -> importlib `find_spec`/`_path_stat` -> `+++ Timeout +++`). 2) LOCAL CONTROL ON THE SAME HEAD: the identical test passes locally in **12.08s** (board venv, `--timeout=300 --timeout-method=thread`) — 12.08s vs >300s-capped on ubuntu-latest, so the class is CI-environment-specific. 3) The step's LABEL ('pure JSON — no duckdb needed') disagrees with what the probe executes (the seed's module-level duckdb `apply_overlay()` query) — sub-evidence of the same row. | n/a — install premise inherited from run 36 (same commit, unchanged code) | ~3 min (CI log fetch + one bounded local test run) | evidence event `20261019235707` on INT-CI-5; the pytest-timeout/faulthandler mitigation (INT-CI-20261015-01) is WORKING — the hang is reaped at 300s and NAMED, not silent; the row's cited red runs (39138425455/39180457070/39180634131/39195671258) now have a fifth sibling |

**Why this matters beyond the row.** Main's tip (42e4e60) is red, and the repo's own pre-push gate reads
exactly that: run 36's push was refused with `CI GATE: RED — coding-hermes/task-router@42e4e60f2 ... run
39325031921` and needed the documented `ALLOW_RED_PUSH=1` override (disclosed in that commit). Every lane that
lands board rows on this repo pays that toll until the hang is fixed. Two further runs sat in flight when this
was written — 39345056644 (387f0e8) and 39347124128 (f185b06) — and their outcome is NOT claimed here.

**Fix direction (unchanged, now better supported).** `_module_constants_through_symlink` (tests/
test_symlink_path_resolution.py:141) executes the target module with `runpy.run_path`, which runs the seed's
module-level `apply_overlay()` and its duckdb query during an import-scan — replace the execution with a static
parse (AST) of the constants the test actually asserts, which removes both the hang surface and the duckdb
dependency the step label claims not to have.


### Run 38 addendum (same tick, same angle) — the in-flight runs concluded: recurrence #6 lands on a board-only head

The run-38 entry above deliberately did not claim the outcome of the two in-flight runs. Both have now been
read, and one is decisive:

* **39345056644 (head 387f0e8 = this lane's run 36 commit; diff = 3 board/docs files, ZERO code) -> FAILURE,
  the same INT-CI-5 hang.** Job 118375540483: seed step finished at 00:01:18Z (`model_perf rows: 3944`), then
  `+++ Timeout +++` at 00:24:17Z (≈23m) with frames `test_symlink_path_resolution.py:286 -> :155 -> :141
  runpy.run_path(link)` -> `router_seed.py apply_overlay()` -> `con.execute("UPDATE model_perf SET perf=?,
  source='bench', source_ref=? ...")` -> `importlib._bootstrap_external._path_stat`.
* New detail: this instance hangs at `apply_overlay():1352` (an **UPDATE**) while 39325031921 hung at `:1339`
  (a **SELECT**) — the pathological loop is the overlay loop's duckdb calls in general, not one statement.
* Why it matters: the head carries no code, so the red cannot be a regression from a landing. The hang is
  environmental and reproducible, and it fails the repo's pre-push CI gate for **every** lane trying to land a
  board row. `INT-CI-5` therefore has six instances, one of them on a docs-only head — the cleanest possible
  "not our code" datum for whoever fixes it (reported as event `20261019235708`).
### Run 39 — tick task-router-dogfood-2026-10-23-00-27-32 (resumed turn; run 1 harvest + judge)

Split delivery: the scheduler delivered this tick TWICE into one session (session eb4ada7b, two identical
user rows 19:27:34 and 19:37:35). Run 1 (19:27→19:37, ~140 messages) executed the legs but ZERO write-back
(its model emitted no assistant prose over 100 chars; it died in a `process_manage` poll-argument loop).
This turn harvested run 1's artifacts, judged them, and landed the write-back.

**Angle (inherited + extended).** Run 1 drove the local fresh-install leg on a scratch
(/tmp/task-router-df-20261023-002732, repo HEAD 6376b1f) and began a bunker leg on agent tr-df-1023
(bunker-mvp). This turn re-verified the local artifacts on disk, re-drove an A/B on the same scratch, and
pulled the bunker transcript from the session DB (proc_1619eeb92484 log).

**Local leg (run 1 artifacts, verified ON DISK this turn):**
- venv 2.93s · pip -e . 3.70s (task_router-0.2.0 editable wheel built OK) · duckdb install 4.34s (2.0.0)
- router seed 12.91s (3944 model_perf rows → registry.json 4640.8 KB)
- spawn cold 0.23s / warm 0.24s / status 0.32s / validate 0.19s / raw-req 0.23s / unknown-project 0.21s rc=0 fail-open error json
- router validate: all 14 checks passed
- unknown-project error: proper fail-open JSON, rc 0, `{"error": "project definitely-not-a-project not in registry"}` — TR-235 contract holds.

**Bunker leg (run 1, transcript evidence — agent TTL expired, artifacts unrecoverable):**
- tr-df-1023 spawned 19:32 on bunker-mvp (100.115.67.116); first staged leg had a quoting EOF bug
  (unterminated `''` in the heredoc — rc 2 ×2); inline retry leg (proc_1619eeb92484) ran the documented
  path end-to-end on the agent: git clone → venv → pip -e → duckdb → seed 22.61s (3954 rows) → spawn 0.26s
  → validate 0.12s → **crashed at the final jq summary** (`jq: command not found`, rc 127).
- Harvest result: transcript carries the full per-step rc/time (all product steps green; the failure is the
  HARNESS's jq dependency, not the product). The spawn.json/validate.txt artifacts are GONE (agent left the
  registry at TTL; no key file at ~/.config/bunker/keys/tr-df-1023). This is an install leg SKIPPED-row
  candidate resolved as "ran, evidence partially harvested from transcript only."
- RESIDUES: agent name was tr-df-1023; on the shared bunker-mvp host the agent is no longer listed
  (`bunker list | grep tr-df` → no matches). Registry/key residue: ~/.bunker/keys/ does NOT carry the
  agent (it was in ~/.config/bunker/keys — also absent).

**A/B judgment on the empty-chain signal (run 1's most suspicious artifact).**
Run-1's local spawn returned `chain=[]`, `gate=NO-OPEN-HOP` with 26 hop-exclusions all citing
`policy-gate-missing-row: provider not listed in quota-state.json`. A/B on the SAME scratch tree this turn:

- ARM A (README env trio: TASK_ROUTER_HOME=$DATA + ROUTER_STATE_DIR=$DATA/state) → chain EMPTY, gate NO-OPEN-HOP. [run 1's arm]
- ARM B (TASK_ROUTER_HOME only; ROUTER_STATE_DIR unset, CLI sets it to data-home) → 26-lane chain, gate=OPEN, head xkiro/gpt-5.6-terra $0.04/task plan-effective, price-sorted with PAYG fallback lanes. /tmp/tr-ab-spawn.json
- ARM C (no env at all, HOME isolated to /tmp/tr-rsdhome) → bootstrap correctly wrote quota-state.json to $HOME/.local/share/task-router/, spawn resolved a real chain (rc 0, non-empty chain) — the DOCUMENTED behavior.

Arms B and C are the product's own convention, working. Arm A is the split-convention trap: ROUTER_STATE_DIR
pointed at a subdir the first-run bootstrap does not write to (it writes the data-home ROOT,
task_router/cli.py:443 → paths.quota_state_path() = resolve_data_home() = DATA root, NOT ROUTER_STATE_DIR),
while spawn's state resolver (scripts/state_dir.py) honors ROUTER_STATE_DIR. Result on fresh install: the
exclusion code names a file the install never received → every hop excluded → NO-OPEN-HOP. README.md:81
promises "first run bootstraps a starter state file ... instead of a silent zero-chain surprise" — on this
env combo the promise is false (the file exists, in a place nobody reads, and status reports present:false).
Filed as **TR-345** (row appended; evidence list preserved in the row body).

**Perf verdict (Step 2b):** no PERF row. Every CLI operation is 0.12–0.32s wall (spawn cold 0.23s, warm 0.24s,
validate 0.19s, status 0.32s) — nothing a user would feel; seed is a 12.9s one-shot admin op. A win nobody can
feel is not a finding.

**Value verdict:** 🟡 PROMISING-BUT-ROUGH leaning SHIPPABLE. The resolve engine works (arm B/C: profile →
eligibility → price sort → gates → head hop, all correct), error contracts hold (fail-open rc 0), fresh
install succeeds on both hosts, and the teaching layer (README, docs, skills) is dense and accurate with ONE
stale/incorrect promise (the ARM-A trap). Time-to-first-success on the documented path: ~25s from clone to
chain (clone+venv+install+seed+spawn), no friction, no source-reading needed.

**Left behind:** TR-345 on the board; the A/B artifacts at /tmp/tr-ab-spawn.json + run-1 scratch
/tmp/task-router-df-20261023-002732/* (logs 01–10 + run-local.sh + bunker-leg.sh); DuckBrain run record (key
/Dogfood/2026-10-23/task-router-dogfood-2026-10-23-00-27-32, namespace task-router). The dogfood-log entry and
board row landed THIS turn (git commit below). The bunker transcript was the install-leg evidence; no agent
survives (TTL'd), no key file residue.
### 2026-10-26 — task-router-dogfood-2026-10-26-05-26-13

Verdict: PROMISING-BUT-ROUGH. Promise: a fresh user can install task-router, seed the registry, and resolve a task profile into a gated, price-ordered chain with fail-open JSON errors. Control run at f75e1bace20cdffb46ace69b5246fcf2415fc808 passed: seed 11.85s; spawn 0.17s cold / 0.12s warm; status 0.18s; validate 0.06s; unknown-project rc=0 JSON error. Isolated API on :19192 returned 200 for health, OpenAPI, profiles and profile resolve; serving commit matched HEAD. No PERF row: interactive timings were below a noticeable threshold; seed was a one-shot admin operation.

Install leg: bunker-mvp reachable, bunker-qa launched agent 2bd9809a, evidence collected and agent destroyed. Fresh-install was FAIL from the generic harness: rc=0 installed duckdb/pyyaml/task-router but the harness looked only for a `toolsd` artifact. Native CI reproduced existing open QA-TASKROUTER-6; act lacked its Docker image; resource cell reproduced the same test failure. No new product row was filed.

Evidence: /tmp/task-router-dogfood-scratch-20261026/run.log; /tmp/task-router-bunker-20261026.jsonl; docs/dogfood/2026-10-26-integration.md; docs/dogfood/2026-10-26-diagnostics.md.


### 2026-10-29 — task-router-dogfood-2026-10-29-05-59-52

Verdict: PROMISING-BUT-ROUGH. Promise: a fresh user can install task-router, run `router seed`, validate the registry, and resolve P1_CODING to a gated chain with fail-open JSON errors. Target identity: canonical /home/kara/task-router, detached clean worktree at HEAD 46c7308fc36ab73bfe15696813e56caa794b1099; product checkout had unrelated staged board edits and .gitreins residue, so no source was run from the dirty canonical tree.

Real-use evidence: isolated venv editable install succeeded in 4.69s; duckdb install succeeded in 1.76s; `router --help` exited 0. `router seed` exited 0 in 32.47s, status exited 0 in 0.30s, and validate exited 0 with all 14 checks in 0.14s. `router spawn --profile P0_FORE --format json --quiet` returned rc=0 with a 50-hop chain and `source=registry.json`, `fallback_used=false`; cold 0.30s and warm 0.26s. The raw-level request returned a 93-hop chain. Unknown-project returned the intended JSON error with rc=0. The scratch API returned 200 for health, OpenAPI, profiles, and resolve; read-only execution-decision returned 403.

No new row: the observed bunker battery truncation is already covered by existing install/harness rows; this lane is read-only. No PERF row: measured interactive CLI/API operations were 0.13–0.30s and nothing user-noticeable; seed was a one-shot administrative operation.

Install leg: bunker-mvp was reachable and capacity was 3/50 agents, CPU 5.6%, memory 2.4/15.2 GB, disk 69%, Docker 49 containers. `bunker-qa.sh launch /home/kara/task-router` started agent d28dbe43 at HEAD 46c7308fc36ab73bfe15696813e56caa794b1099 and wrote `/tmp/task-router-bunker-20261029-055952.jsonl`; the battery was still RUNNING at close (`qa-run.log` had only HARNESS-COMPOSE=ok and HARNESS_NODE=ok, 12 evidence rows, no qa-run.finished). `bunker-qa.sh collect --evidence ...` returned `RUNNING` and left the agent intact. This is PARTIAL, not PASS or silent skip. Agent d28dbe43 remains live under its 4h TTL and requires a later collect/teardown; do not claim install completion.

Left behind: `/tmp/task-router-df-20261029-055952/run-summary.json`, `/tmp/task-router-df-20261029-055952/run/`, `/tmp/task-router-findings.json`, `/tmp/task-router-bunker-20261029-055952.jsonl`, and the usage-skill correction in `skills/task-router-usage/SKILL.md` documenting the fresh-mirror failure. The canonical board was restored to its pre-run bytes; pre-existing `boardctl validate` remained FAIL with 413 errors and 469 warnings in events.jsonl, and no new board row/event was left by this lane.

| 2026-10-29 (run 40) | PROMISING-BUT-ROUGH | A user can install task-router, seed its registry, and resolve a task profile into a gated, price-ordered provider/model chain; resolution failures remain fail-open JSON errors | 1) Local CLI/API workflow works at HEAD 46c7308: seed 32.47s, validate 14/14, spawn 0.30s cold / 0.26s warm, API resolve 0.13s. 2) Bunker fresh-install package cell passed, but the standard battery stopped in CI before full suite/chaos completion; installability is not a clean full-battery pass. 3) First detached-worktree helper attempt was rejected, then primary-root bunker-las-03 sync failed on port 2223; bunker-mvp corrected run completed install setup but was truncated | bunker-mvp agent=9d5d53d3: install cell OK, battery INCOMPLETE/TRUNCATED; bunker-las-03 agent=54ebefe7: sync failed, destroyed | ~36s local setup to first resolve; local interactive operations 0.14-0.30s | evidence=/tmp/task-router-dogfood-2026-10-29-055952/out; bunker evidence=/tmp/task-router-bunker-20261029-055952-primary.jsonl and /tmp/task-router-bunker-20261029-055952-mvp.jsonl; agent 9d5d53d3 destroyed and info=not_found; no new row, existing TR/QA install-harness rows corroborated; foreman NOT woken (read-only lane) |

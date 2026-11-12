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

Install leg: bunker-mvp was reachable and capacity was 3/50 agents, CPU 5.6%, memory 2.4/15.2 GB, disk 69%, Docker 49 containers. `bunker-qa.sh launch /home/kara/task-router` started agent d28dbe43 at HEAD 46c7308fc36ab73bfe15696813e56caa794b1099 and wrote `/tmp/task-router-bunker-20261029-055952.jsonl`; the first collect returned RUNNING. A later collect completed and destroyed it, with 21 evidence rows: fresh-install OK (`duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`), but ci-pass FAIL after 24m31s at `tests/test_classifier_lane_failover.py::test_429_still_retries_primary_first`; chaos-disconnect UNVERIFIED and chaos-resource INFO (`1 failed, 174 passed, 6 skipped` under 3145728KB cap). Install artifact PASS, overall fresh-machine battery FAIL/PARTIAL; teardown is recorded by the collector and must be rechecked before claiming zero residue.

Left behind: `/tmp/task-router-df-20261029-055952/run-summary.json`, `/tmp/task-router-df-20261029-055952/run/`, `/tmp/task-router-findings.json`, `/tmp/task-router-bunker-20261029-055952.jsonl`, and the usage-skill correction in `skills/task-router-usage/SKILL.md` documenting the fresh-mirror failure. The canonical board was restored to its pre-run bytes; pre-existing `boardctl validate` remained FAIL with 413 errors and 469 warnings in events.jsonl, and no new board row/event was left by this lane.

| 2026-10-29 (run 40) | PROMISING-BUT-ROUGH | A user can install task-router, seed its registry, and resolve a task profile into a gated, price-ordered provider/model chain; resolution failures remain fail-open JSON errors | 1) Local CLI/API workflow works at HEAD 46c7308: seed 32.47s, validate 14/14, spawn 0.30s cold / 0.26s warm, API resolve 0.13s. 2) Bunker fresh-install package cell passed, but the standard battery stopped in CI before full suite/chaos completion; installability is not a clean full-battery pass. 3) First detached-worktree helper attempt was rejected, then primary-root bunker-las-03 sync failed on port 2223; bunker-mvp corrected run completed install setup but was truncated | bunker-mvp agent=9d5d53d3: install cell OK, battery INCOMPLETE/TRUNCATED; bunker-las-03 agent=54ebefe7: sync failed, destroyed | ~36s local setup to first resolve; local interactive operations 0.14-0.30s | evidence=/tmp/task-router-dogfood-2026-10-29-055952/out; bunker evidence=/tmp/task-router-bunker-20261029-055952-primary.jsonl and /tmp/task-router-bunker-20261029-055952-mvp.jsonl; agent 9d5d53d3 destroyed and info=not_found; no new row, existing TR/QA install-harness rows corroborated; foreman NOT woken (read-only lane) |

### 2026-11-06 — task-router-dogfood-2026-11-06-12-08-29

Verdict: PROMISING-BUT-ROUGH. Target HEAD 586dcf6b16bde9a3b722a65a805bf4e090819a52 (canonical clean, HEAD...origin/main = 0 0); probed from a `git archive origin/main` copy and a `--no-hardlinks` clone of the same SHA.

Promise: install from a fresh clone, `router seed` the registry from committed tables, resolve a profile into a gated price-ordered chain, fail-open JSON on failure, `router validate` as the one-command verdict — and `make seed && make validate` "passes with no env vars at all" (README.md:54).

Real use: venv 2.63s + `pip install -e .` 5.72s + `pip install duckdb` 7.48s on Python 3.14.9. Pre-seed contract exactly as documented (spawn resolves 11 hops from data/tables with fallback_used=true; validate exits 1 naming registry.exists). Step 4 — `router seed` — exited 1 with "SATISFIABILITY FAIL: 6 of 264 rating(s) ... math: levels 0..5 ... blocked holder ollama-cloud/qwen3.5:397b (disabled, unpriced)" and then "Seeding REFUSED". The refusal is not a refusal: the run leaves a 6.0 MB registry.json (validate → {"valid": true}, 14/14) and rewrites two tracked tables. On the clean clone, `make seed` rc=2 in 50.4s, `git status --porcelain` shows M data/tables/category_levels.jsonl and M data/tables/model_tier.jsonl (22 lines, 11 SEMANTIC tier changes e.g. MiniMax-M2.7 mock -3 -> -2), and `make validate` exits 0. With the tool's own documented opt-out (`router seed --allow-unsatisfiable`, 27.886s ± 1.037 over 3 runs) everything downstream is correct: validate 14/14, spawn source=registry.json fallback_used=false 11 hops head opencode-go/deepseek-flash 0.029538 plan-effective, ad-hoc req 90 hops / 5 exclusions, unknown project rc 0 + JSON error. The ACP-ROUTER-001 receipt path is the strongest surface and it holds: after an isolated `--init-receipt-key`, decide → eligible=true, 42 candidates, valid_until +900s; verify → ok/signature_ok/issued; a receipt with head.model edited → rc 1 DIGEST_MISMATCH + RECEIPT_SIGNATURE_INVALID. Read-only server (:19192) serves /health 200 (90ms), /openapi.json 200 (2ms, 36 paths), /metrics 200 (248KB), /resolve 200 (451ms), /api/ui/chain 200 (482ms) and refuses POST execution-decision and POST ledger/start with 403; POST /api/v1/outcomes appends in read-only mode BY DESIGN (docs/integration.md:195) — observed, documented, not filed.

Findings: filed TR-1000976681059248 (P1) — refused seed writes registry.json + 2 tracked data/tables files because _dump_registry (router_seed.py:1946) and the tables sync (:1948-1957) run before the satisfiability check (:1991-2031, sys.exit at :2031); README.md:54's make-seed claim is false at this HEAD in the direction nobody guesses (seed red, validate green). Filed QA-TASKROUTER-14 (P2) — the fresh-machine suite is red on the interpreter a supported Debian ships: tests/test_metrics_endpoint.py:350 asserts `_peer_is_loopback("::ffff:127.0.0.1") is True`, which is False on python 3.12.3 and True on 3.14.9; the function (router_server.py:5431-5444) is the TR-377 loopback gate behind /metrics, so a widened bind (TR-378) would 403 an IPv4-mapped loopback client. Re-proved without siblings: TR-1000976681059246 (flagless seed refuses; rc 1 / rc 0 with the flag), QA-TASKROUTER-13 (scripts/router_mirror_account.py --commit → NameError: name 'os' is not defined at :71, rc 1, models.jsonl unchanged; --dry-run fine), QA-TASKROUTER-8 premise (pyproject [tool.ruff] + pinned ruleset present, ruff 0.15.22 in the board venv, battery's lint cell still N/A), QA-TASKROUTER-12 (collect reported AGENT-GONE on a live agent).

Perf: no PERF row. spawn 317.2ms ± 22.0 (15), status 324.3ms ± 29.2 (8), estimate 690.0ms ± 164.4 (8), decide 627.3ms ± 77.4 (6), HTTP /resolve 451ms, /health 90ms — all under loadavg 36.8/16 cores. seed 27.886s ± 1.037 (3) is the only felt wait and it is a one-shot admin/quick-start step at unchanged input scale (models 2023->2081, model_tier 3910->3896 since 10-26), so no regression is claimed; the cProfile attribution arm was BLOCKED by the harness and did not run.

Install leg: bunker-mvp live (/health 404 = live bunkerd; bunker-qa.sh status bunkerd=active docker=yes act=no uidmap=yes; bunker 0.2.0). launch → agent 301926f4, evidence /tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl. Cells harvested (collector could not reach the live agent — QA-TASKROUTER-12 again: `collect` said AGENT-GONE "cannot reach agent=301926f4 (ssh probe returned no state)" while `bunker info` reported Status: running and `bunker exec` answered ALIVE, so the last cells were read by hand from ~/qa-evidence.jsonl): launch OK; toolchain-bootstrap OK x5 (go 1.27.3, python venv+pytest on py3.12.3, gcc/g++, make, jq); fresh-install OK (duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0) — installability PROVEN on a clean box; lint N/A (detector reports "no supported lint trigger" though pyproject declares [tool.ruff]); tests FAIL (py3.12.3: tests/test_metrics_endpoint.py:350 ::ffff loopback); docker_build N/A, compose_config N/A (no triggers). The act/docker ci cell also failed (31m28s, "Job test failed", no assertion named — not attributed). Agent destroyed: rc 0, local key removed, fresh `bunker list --server bunker-mvp --status all` → "No agents found", `bunker info` → not_found.

Left behind: board rows TR-1000976681059248 (P1) and QA-TASKROUTER-14 (P2) with their task_created events (committed; committed-tree jq count = 1 each, 0 dup ids, boardctl validate error count unchanged by these appends — 422, all pre-existing legacy event-id ordering); docs/dogfood/2026-11-06-integration.md + 2026-11-06-diagnostics.md; skills/task-router-usage/SKILL.md now shows `router seed --allow-unsatisfiable` and warns that a refusal still writes. No source change, no scheduler mutation, no visibility/permission change, no credentials minted (the receipt key was bootstrapped inside an isolated TASK_ROUTER_HOME under scratch).

| 2026-11-06 (run 41) | PROMISING-BUT-ROUGH | A stranger can install, seed with the documented opt-out, and resolve a gated price-ordered chain; route receipts are tamper-evident and verify; the documented flagless seed is where trust breaks | 1) Engine + receipt contract correct end-to-end at 586dcf6 (spawn 317ms, decide 627ms, verify ok, tampered receipt rejected). 2) `router seed` REFUSED yet wrote registry.json (validate 14/14 green) and rewrote 2 tracked data/tables files with 11 semantic tier changes → TR-1000976681059248; `make seed` fails while `make validate` passes. 3) Fresh-machine suite red on python 3.12.3 at tests/test_metrics_endpoint.py:350 (::ffff loopback gate) → QA-TASKROUTER-14. 4) Four pre-existing rows re-proved (TR-1000976681059246, QA-TASKROUTER-13, QA-TASKROUTER-8 premise, QA-TASKROUTER-12) | cells: fresh-install OK, toolchain-bootstrap OK x5, lint N/A, tests FAIL (::ffff loopback on py3.12.3), docker_build/compose_config N/A; collector AGENT-GONE on a live agent (QA-TASKROUTER-12) → hand-harvested; agent 301926f4 destroyed, census empty | ~16s from clone to pre-seed chain; ~44s to a seeded validate-green registry with --allow-unsatisfiable; interactive ops 0.32-0.69s, seed 27.9s | evidence=~/.hermes/cache/scratch/tr-df-20261106-1208-ev/ (logs 01-46 + hyperfine JSON); bunker evidence=/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl agent=301926f4 (destroyed); foreman NOT woken (no scheduler mutation from this lane) |

### 2026-11-06b — DUPLICATE FIRE task-router-dogfood-2026-11-06-12-08-29

Records-only re-verification of the run already recorded under `### 2026-11-06 — task-router-dogfood-2026-11-06-12-08-29` (line 202). Nothing was re-measured and no rows were added; this fire is a scheduler duplicate dispatch of the same tick id, delivered ~55s after the closeout commit.

Duplicate-fire decision from two independent surfaces, both hit: body count `grep -c '^#\+ .*task-router-dogfood-2026-11-06-12-08-29'` = 1, and commit history `git log --oneline --fixed-strings --grep='-12-08-29'` = `065d590`. HEAD at fire time was that closeout commit itself.

Fresh re-verification against the current tip: `git fetch origin main` then `HEAD...origin/main` = `0	0`; HEAD = `065d5905a2d9b99da32c4404512a2191fc962c7f` = `origin/main`. The measured product SHA for the prior fire was `586dcf6b16bde9a3b722a65a805bf4e090819a52`; `git diff --stat 586dcf6 HEAD` touches only lane records (board tasks/events, this log, `docs/dogfood/2026-11-06-{integration,diagnostics}.md`, `skills/task-router-usage/SKILL.md`) and ZERO lines under `task_router/ tests/ scripts/ data/ Makefile pyproject.toml README.md` — so the prior PERF numbers are reused, not re-derived.

Committed-board proof: `git show HEAD:.coding-hermes/board/tasks.jsonl` carries `QA-TASKROUTER-14` and `TR-1000976681059248` exactly once each; the committed `events.jsonl` carries exactly one `task_created` for each (ids 1000889452787247 / 1000889452787246, actor foreman); `jq -r .id | sort | uniq -d` is empty.

Validator, no-NEW-harm gate: reconstructed both arms in scratch (`tasks/events/schema/fixtures/board.jsonl` at HEAD and at parent `586dcf6`) and ran `boardctl validate -C` on each. HEAD = FAIL 422 errors / 518 warnings; parent = FAIL 422 errors / 518 warnings — identical counts and identical class breakdown (415 non-ascending event ids, 7 `superseded` status rows), zero hits for either new row id. The repo-wide red is standing board-lint debt, unchanged by these appends.

Install leg — not launched this fire; census only. `bunker status --server bunker-mvp` → ONLINE, version 0.2.1, agents 0/50, `/tmp: private`, `Net mode: shared — NOT tenant isolation`; `bunker list --server bunker-mvp --status all` → "No agents found." (the prior fire's agent `301926f4` is gone, zero run-owned residue). A launch now would grade `install-proven / isolation-UNVERIFIED` because `Net mode: shared`. Host hygiene note (pre-existing, not run-owned): 20 orphan keys / 3 registered-agent residue entries. Installability itself was PROVEN this tick by the recorded run (fresh-install cell OK, duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0).

Prior evidence bundle read back and non-empty: 74 files under `~/.hermes/cache/scratch/tr-df-20261106-1208-ev/`, `/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl` 2508 B, both `docs/dogfood/2026-11-06-*.md` present in the HEAD tree, and `skills/task-router-usage/SKILL.md` carries the `--allow-unsatisfiable` correction.

No arm taken, deliberately: both recorded rows were walked — both `pending`, neither carries a withdrawal or contradiction note, and their subjects (refused-seed-still-writes P1; interpreter-dependent `::ffff` loopback gate P2) are fully owned by the prior fire. No un-owned half was found, so nothing was filed. Verdict carried forward: PROMISING-BUT-ROUGH.

### 2026-11-06c — DUPLICATE FIRE (2nd) task-router-dogfood-2026-11-06-12-08-29

Records-only re-verification of the run recorded under `### 2026-11-06 — …-12-08-29` (line 202) and its first duplicate line `### 2026-11-06b` (line 220). Nothing re-measured, no rows added, no install launched. This is the THIRD dispatch of one tick id inside ~4 minutes (full run 08:26:32, dup 1 08:28:46, this fire 08:29:41 local) — scheduler duplicate-dispatch evidence, not new work.

Duplicate-fire decision, two independent surfaces, both hit: body headings `grep -c '^#\+ .*2026-11-06-12-08-29'` = 2, and commit history `git log --oneline --fixed-strings --grep='-12-08-29'` = `065d590` + `3e330b2`. HEAD at this fire = `3e330b2` (dup-1's own records commit).

Fresh re-verification at the current tip (nothing carried from either prior fire): `git fetch origin main` then `git rev-list --left-right --count HEAD...origin/main` = `0\t0`; HEAD = origin/main = `3e330b2f130622a49a5a7b6b1671be56d778c060`; no REBASE_HEAD / MERGE_HEAD / CHERRY_PICK_HEAD / REVERT_HEAD / index.lock. Product surfaces have NOT moved since the measured SHA `586dcf6`: `git diff --stat 586dcf6 HEAD` = `.coding-hermes/board/{tasks,events}.jsonl`, this log, `docs/dogfood/2026-11-06-{integration,diagnostics}.md`, `skills/task-router-usage/SKILL.md` — zero lines under `task_router/ tests/ scripts/ data/ Makefile pyproject.toml README.md`, so the prior run's numbers stand and were not re-derived.

Committed-board proof (committed tree, not the working copy): `git show HEAD:.coding-hermes/board/tasks.jsonl` carries `QA-TASKROUTER-14` and `TR-1000976681059248` exactly once each, both `pending`; committed `events.jsonl` carries exactly one `task_created` for each; `jq -r .id | sort | uniq -d` empty.

Validator no-NEW-harm gate, fresh both arms: reconstructed `tasks/events/schema/fixtures/board.jsonl` at HEAD `3e330b2` and at parent `065d590` under fresh scratch `~/.hermes/cache/scratch/tr-df-20261106-1208c-249045/{head,parent}` and ran `boardctl validate -C` on each. HEAD = FAIL 422 errors / 518 warnings; parent = FAIL 422 errors / 518 warnings — identical counts and identical class breakdown (415 non-ascending event ids, 7 `superseded` status rows), zero hits for either new row id. Repo-wide red is standing board-lint debt, unchanged by these appends.

Install leg — not launched this fire; census only. `bunker status --server bunker-mvp` → ONLINE, version 0.2.1, uptime 10h08m, agents 0/50, `/tmp: private`, `Net mode: shared — NOT tenant isolation`, Disk 24%, Docker 3 containers, residue 21 orphan keys / 3 registered-agent entries (pre-existing, not run-owned). `bunker list --server bunker-mvp --status all` → "No agents found." (prior fire's agent `301926f4` gone; zero run-owned residue). A launch now would grade `install-proven / isolation-UNVERIFIED` because `Net mode: shared`; installability itself was PROVEN by the recorded run (fresh-install cell OK: duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0). Not SKIPPED — a host and an install path exist.

Prior evidence bundle re-read and non-empty: 74 files under `~/.hermes/cache/scratch/tr-df-20261106-1208-ev/`, `/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl` 2508 B, both `docs/dogfood/2026-11-06-*.md` in the HEAD tree, usage skill carries the `--allow-unsatisfiable` correction.

No arm taken: both open rows were walked again — both `pending`, neither carries a withdrawal/contradiction note, and their subjects (refused-seed-still-writes P1; interpreter-dependent `::ffff` loopback gate P2) are fully owned by the preceding fires. No un-owned half exists and the product code is unchanged, so a new arm would be re-measurement, not evidence. Verdict carried forward: PROMISING-BUT-ROUGH.

### 2026-11-06d — DUPLICATE FIRE (3rd) task-router-dogfood-2026-11-06-12-08-29

Records-only re-verification of the run recorded under `### 2026-11-06 — …-12-08-29` (line 202) and its duplicate lines `### 2026-11-06b` (line 220) and `### 2026-11-06c` (line 238). Nothing re-measured, no rows added, no install launched. This is the FOURTH dispatch of one tick id inside ~4m40s (full run 08:26:32, dup 1 08:28:46, dup 2 08:30:25, this fire 08:31:12 local) — scheduler duplicate-dispatch evidence, not new work.

Duplicate-fire decision, two independent surfaces, both hit: body headings of any level `grep -c '^#\+ .*task-router-dogfood-2026-11-06-12-08-29'` = 3 (bold-paragraph form = 0, so the earlier fires all used headings), and commit history `git log --oneline --fixed-strings --grep='task-router-dogfood-2026-11-06-12-08-29'` = 4 messages (`065d590` closeout, `3e330b2` dup-1, `833cfab` dup-2, plus `e91330f` whose body cites the tick), suffix-form `--grep='-12-08-29'` returning the same set. HEAD at this fire = `833cfab` (dup-2's own records commit).

Fresh re-verification at the current tip, nothing carried from any prior fire: `git fetch origin main` then `git rev-list --left-right --count HEAD...origin/main` = `0\t0`; HEAD = origin/main = `833cfab3adcf28a2795a8646dcde1b8740fba69a`; no REBASE_HEAD / MERGE_HEAD / CHERRY_PICK_HEAD / REVERT_HEAD / index.lock; working tree clean (bounded `--untracked-files=no`). Remote carrying the published branch is `origin` = git@github.com:coding-hermes/task-router.git (the only remote; no mirror to disambiguate).

Product surfaces have NOT moved since the measured SHA `586dcf6`: `git diff --name-only 586dcf6 HEAD` = `.coding-hermes/board/{tasks,events}.jsonl`, this log, `docs/dogfood/2026-11-06-{integration,diagnostics}.md`, `skills/task-router-usage/SKILL.md` — and `git diff --stat 586dcf6 HEAD -- task_router tests scripts data Makefile pyproject.toml README.md` is empty, so the prior run's measurements and PERF numbers stand and were not re-derived.

Committed-board proof (committed tree, not the working copy): `git show HEAD:.coding-hermes/board/tasks.jsonl` carries `QA-TASKROUTER-14` and `TR-1000976681059248` exactly once each, both `pending`; committed `events.jsonl` carries exactly one `task_created` for each (ids `1000889452787246` / `1000889452787247`, actor foreman — the event field is `event_type`, not `type`); `jq -r 'select(.id!=null)|.id' | sort | uniq -d` is empty.

Validator no-NEW-harm gate, fresh both arms in a per-fire scratch dir `~/.hermes/cache/scratch/tr-df-20261106-1208d-275440/{head,parent}` (tasks/events/schema/fixtures/board reconstructed from `git show`): `boardctl -C <arm> validate` → HEAD `833cfab` = FAIL 422 errors / 518 warnings; parent `3e330b2` = FAIL 422 errors / 518 warnings — identical counts and identical class breakdown (415 non-ascending `events.jsonl` ids, 7 `superseded`-status rows), zero hits for either new row id. The repo-wide red is standing board-lint debt, unchanged by these appends.

Install leg — not launched this fire; census only. `bunker status --server bunker-mvp` → ONLINE, version 0.2.1, uptime 10h10m, agents 0/50, `/tmp: private`, `Net mode: shared — NOT tenant isolation`, Disk 25%, Docker 3 containers, residue 20 orphan keys / 3 registered-agent entries (pre-existing, not run-owned). `bunker list --server bunker-mvp --status all` → "No agents found." (the recorded run's agent `301926f4` is gone; zero run-owned residue). A launch now would grade `install-proven / isolation-UNVERIFIED` because `Net mode: shared`; NOT `SKIPPED-install-bunker`, since a host and an install path exist. Installability itself was PROVEN this tick by the recorded run (fresh-install cell OK: duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0).

Prior evidence bundle re-read and non-empty: 74 files / 5.1M under `~/.hermes/cache/scratch/tr-df-20261106-1208-ev/`, `/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl` 2508 B, both `docs/dogfood/2026-11-06-*.md` in the HEAD tree, usage skill carries the `--allow-unsatisfiable` correction.

No arm taken, deliberately: both open rows were walked in full — both `pending`, neither carries a withdrawal or contradiction note (string scan for withdraw/contradict/supersede/not-reproduced = empty on both), and their subjects (refused-seed-still-writes P1; interpreter-dependent `::ffff` loopback gate P2) are fully owned by the preceding fires. With the product tree byte-identical to the measured SHA, no un-owned half can exist, so a new row would be re-measurement rather than evidence. Verdict carried forward: PROMISING-BUT-ROUGH. Escalating in chat, not on the board, because this is a scheduler duplicate-dispatch defect in another project's lane.

### 2026-11-09 — task-router-dogfood-2026-11-09-12-54-05 (run 42)

First fire of this tick id (`grep -c` on this log = 0, `git log --grep` = 0). Arm taken: a fresh
real-use walk of the CLI and both HTTP modes, because the product tree had moved since the last
measured SHA (`586dcf6` → `d9130116`) while those rows stayed `pending`.

Subject: `/home/kara/task-router` at `d9130116` (product diff `d9130116..8721ec08` over
`task_router tests scripts data Makefile pyproject.toml README.md` is empty — measurements stand
at the current tip). Binary: `~/.hermes/venvs/board/bin/router` (editable → the live tree), state
isolated with `TASK_ROUTER_HOME`/`ROUTER_STATE_DIR` under
`~/.hermes/cache/scratch/tr-df-20261109-1254-r1/`.

Walked: `seed` (refused, then forced), `validate`, `spawn` by profile / by project / unknown,
`estimate`, `decide` + `decide --verify` (good / tampered / wrong-parent), `status`, `gaps`, and
the HTTP surface in `--mode read-only` (GET matrix + decision + verify) and `--mode edit`
(auth matrix). Report `docs/dogfood/2026-11-09-integration.md`, trail
`docs/dogfood/2026-11-09-diagnostics.md`.

**New finding — filed `QA-TASKROUTER-22` (P2).** `router gaps` prints
`0 of 0 active models` + `no gaps` with rc 0 when the resolved `data/tables` does not exist, and
cwd decides which directory that is: from `~/.hermes/scripts` (the canonical install dir, which
has its own `scripts/router_spawn.py`) it reads nothing; from `/tmp` the same binary reports
`790 of 790` with gap lines. Cause: `task_router/cli.py:147-181` prefers cwd ancestors over the
package checkout (`cli.py:245` `_data_dir()`), and `scripts/router_gaps.py` treats an empty table
read as "nothing to check". The audit surface lies in the reassuring direction. Four acceptance
criteria filed, including the missing zero-data test and a cwd-independence test.

**Reproduced, not re-filed:** `QA-TASKROUTER-14` — clean box (bunker `b54d42ff`, Python 3.12.3)
`1 failed, 856 passed, 10 skipped in 731.79s`, failure `test_peer_is_loopback_fails_closed`
(`::ffff:127.0.0.1` is not `is_loopback` there). `TR-1000976681059246` — flagless `router seed`
still refuses on unsatisfiable math ratings.

**Cleared this run, so the board does not need them:** `TR-1000976681059248` is FIXED — refusal
now exits 1 and leaves a fresh `TASK_ROUTER_HOME` empty (no 7 MB `registry.json`). Receipt
verification fails closed: valid rc 0 / `signature_ok:true`, tampered rc 1 / `false`, wrong
`--expect-parent-run` rc 1 (the earlier `rc=0` reading of the tampered case was a `| jq` shell
artifact). Edit-mode HTTP auth enforces the key (`401`/`401`/`200`) with no state leak across
sequential keyed/unkeyed requests.

**Install leg — run, not skipped.** `bunker-qa.sh status` → `bunkerd=active docker=yes act=no
uidmap=yes`; agent evidence (`b54d42ff`, harvested over SSH because the collect path aborts with
`ssh probe returned no state` against a live agent — tooling defect, reported not filed here):
`toolchain-bootstrap OK`, `fresh-install OK` (`duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`,
artifact `~/tools/venv/bin/router`), `tests FAIL` (the one loopback test), `lint`/`docker_build`/
`compose_config` N/A. A decoy `collect --evidence` path returns `COLLECT-FAIL no-launch-record`,
which is how the live-agent-only failure mode was separated from a bad path.

**Performance (Step 2b) — nothing a user would feel, so no PERF row.** `spawn P1_CODING` 0.232 s
real, `validate` 0.124 s real, HTTP verify 2.1 ms server-side. Only observation worth a number:
the full suite costs 731.79 s on a 2-vCPU clean box (≈0.85 s/test), which is contributor friction
with no profiled hot path — stated, not filed.

**Board write verified, not asserted:** id from the minter
(`board_next_id.py … QA-TASKROUTER` → `QA-TASKROUTER-22`); `grep -c '"id": "QA-TASKROUTER-22"'`
on `tasks.jsonl` = 1 and on `events.jsonl` = 1 (`task_created`, event id
`1794082887258446142` = prior max + 1); `git diff --stat -- .coding-hermes/board/` =
`2 files changed, 2 insertions(+)`; `boardctl -C /home/kara/task-router validate` still FAILs on
standing debt (448 errors / 565 warnings) with **zero** lines mentioning the new id, so no new
lint harm.

Left behind: this entry, the two reports, a usage-skill correction (run the CLI from the repo
root or with `TASK_ROUTER_REPO`; `router gaps` is not trustworthy from the install dir until
`QA-TASKROUTER-22` lands), and the one row + one event above. Product code untouched; repo
visibility and permissions untouched.

Verdict: **WORKS-BUT-AUDIT-SURFACE-LIES** (carried-forward `PROMISING-BUT-ROUGH` upgraded — the
core promise now holds end to end and fast; the trust defect is confined to `gaps`).

### 2026-11-09b — DUPLICATE FIRE | tick=task-router-dogfood-2026-11-09-12-54-05 (run 42b)

Duplicate dispatch of a tick already worked (run 42, committed `0ddfc606`). Attribution: the log BODY
carries the run-42 heading at line 276 (`grep -c '^#\+ .*task-router-dogfood-2026-11-09-12-54-05'` = 1 on the
pre-append log); the commit-subject grep for the tick id prints 0 only because that message names the run
ordinal ("dogfood(run 42): file QA-TASKROUTER-22 …"), which is the known blind spot of that surface. Per the
duplicate-fire rule the deliverable is this fire's own read-only proof plus this line — no re-measurement, no arm.

**Re-verified this fire, fresh commands (HEAD-bound):** `git fetch origin main` then
`git rev-list --left-right --count HEAD...origin/main` = `0	0`; HEAD = origin/main = `0ddfc606`
(`0ddfc606db972685033d358ff25a1b404e28bda8`). Product diff `d9130116` (run 42's measured SHA) → `0ddfc606`
over `task_router tests scripts data Makefile pyproject.toml README.md docs/skills` is EMPTY, so no product
surface moved since that walk and no un-owned half can exist. Board: 493 rows / 996 events with the working
tree byte-equal to the committed blobs (`git status --short -- .coding-hermes/board/` silent), 0 duplicate ids,
`QA-TASKROUTER-22` present once with exactly one `task_created` event. Validator: `boardctl -C
/home/kara/task-router validate` → `RESULT: FAIL (448 error(s), 565 warning(s))`, the same standing counts as
run 42's parent reading, with **zero** lines mentioning `QA-TASKROUTER-22` — no new lint class.

**Open-row re-proof (premise liveness only, no re-filing):** `QA-TASKROUTER-22` REPRODUCED at `0ddfc606` —
`router gaps` run from `~/.hermes/scripts` (where `data/tables` does not exist) prints
`== registry data-quality gaps (0 of 0 active models) ==` / `no gaps …` with rc 0, while the resolved data dir
there has no tables. `QA-TASKROUTER-14` and `TR-1000976681059246` carried forward: their product tree is
byte-identical to the one that proved them, so re-running them here would be re-measurement, not evidence.

**Install leg — not launched this fire; census restated, not escalated.** `bunker status --server bunker-mvp`
→ ONLINE, version 0.2.1, uptime 3d10h28m, agents 5/50 (none created or destroyed by this fire), `/tmp: private`,
`Net mode: shared — NOT tenant isolation`, Disk 39%, Docker 11 containers. A launch now would grade
`install-proven / isolation-UNVERIFIED` because of the shared network namespace — NOT `SKIPPED-install-bunker`,
since host and install path both exist; installability was already proven (fresh-install cell OK:
duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0).

**Judging decision:** no GitReins task and no judge ran — this is a bookkeeping-only fire with no row of its
own to bind a verdict to. Verdict unchanged: PROMISING-BUT-ROUGH. Nothing filed, nothing left dirty. The
duplicate delivery of this tick id is scheduler duplicate-dispatch evidence, reported in chat, not a board row.

### 2026-11-09c — DUPLICATE FIRE (third delivery) | tick=task-router-dogfood-2026-11-09-12-54-05 (run 42c)

Third dispatch of a tick id already worked twice (run 42 committed `0ddfc606`, run 42b committed `f2bd9981`).
Both duplicate surfaces hit before discovery: log body `grep -c '^#\+ .*task-router-dogfood-2026-11-09-12-54-05'`
= 2 (run 42 heading + run 42b heading) and commit-subject `git log --oneline --fixed-strings --grep=<tick-id>` = 1
(`f2bd9981 dogfood(run 42b) … tick -12-54-05`), as does the clock-suffix grep `--grep=12-54-05`. Per the
duplicate-fire rule the deliverable is this fire's own read-only proof plus this line: no re-measurement, no
arm, no new row. Consecutive dispatch of one tick id is scheduler duplicate-dispatch evidence, raised in chat.

**Re-verified this fire, fresh commands (HEAD-bound, `~/.hermes/cache/scratch/tr-df-20261109-1254-r3-3424404/`):**
`git fetch origin main` then `git rev-list --left-right --count HEAD...origin/main` = `0\t0`; HEAD = origin/main =
`f2bd99814cbec9a33a8051299216ac7af37d8d72`. No Git operation marker: `REBASE_HEAD`/`MERGE_HEAD`/`CHERRY_PICK_HEAD`/
`REVERT_HEAD`/`index.lock`/`rebase-merge`/`rebase-apply` all absent, `git status --short --untracked-files=no` shows
only a sibling's `.gitreins/tasks.yaml` (not this lane's file, left untouched).

**Arm decision — none earned, product surface did not move.** `git diff --name-only d9130116..f2bd9981` (run 42's
measured SHA → now) names only records: `.coding-hermes/board/{tasks,events}.jsonl`, `.coding-hermes/dogfood-log.md`,
`docs/dogfood/2026-11-09-{integration,diagnostics}.md`, `skills/task-router-usage/SKILL.md` — every one written by
run 42/42b. The product-path diff over `task_router tests scripts data Makefile pyproject.toml README.md docs/skills`
is EMPTY, so no user-visible surface moved and no un-owned half can exist; re-timing would be a re-measurement of
run 42's numbers (spawn 0.232 s, validate 0.124 s, HTTP verify 2.1 ms — all subsecond, nothing to feel).

**Board proof (working tree byte-equal to committed blobs):** 493 rows / 996 events, `git status --short --
.coding-hermes/board/` silent, `jq -r .id | sort | uniq -d` = 0 duplicate ids, `QA-TASKROUTER-22` present exactly
once in `tasks.jsonl` with exactly one `task_created` event in `events.jsonl`. Unchanged from run 42b, as expected
for a fire that wrote nothing.

**Validator (the gate that actually ran):** `boardctl -C /home/kara/task-router validate` → `RESULT: FAIL (448
error(s), 565 warning(s))` and `boardctl: validation failed`, exit 1 — byte-identical standing counts to runs 42
and 42b (whole-board non-ascending `events.jsonl` id debt, pre-existing), with ZERO lines mentioning
`QA-TASKROUTER-22`, so no new lint class and no new harm from this fire.

**Open-row premise liveness (anchor probed, not re-measured):** `QA-TASKROUTER-22` (pending, P2) cites
`task_router/cli.py:147-181`; at `f2bd9981` the anchor is LIVE at `task_router/cli.py:169`
(`candidates.extend(_parents(os.path.abspath(os.getcwd())))` still runs before the package-dir candidate at :170,
first-match on `scripts/router_spawn.py`) and `_data_dir()` still at :245 — the citation drifted a few lines, the
premise did not. Product tree is byte-identical to the tree that reproduced the defect, so the reproduction is
carried forward rather than re-run (`QA-TASKROUTER-14`, `TR-1000976681059246` likewise).

**Install leg — not launched this fire; census restated, not escalated.** `bunker status --server bunker-mvp`
→ ONLINE, version 0.2.1, uptime 3d10h32m, agents 5/50, `/tmp: private (per-session pam_namespace instance)`,
`Net mode: shared — host network namespace … NOT tenant isolation`, Disk 43%, Docker 11 containers, 20 orphan keys.
Transport checked independently of the client's exit status: `curl -s -o /dev/null -w '%{http_code}'` on
`http://100.115.67.116:19090/health` = `404` (live daemon; also `404` on the `:18080` port `~/.bunker/config.yaml`
lists — the client prints the advertised port, both answer), while the CLI's DEFAULT server `bunker-las-03`
answers `000` (connection refused) — a bare `bunker status` therefore reads OFFLINE and only `--server bunker-mvp`
is meaningful here. `BUNKER_QA_SERVER=bunker-mvp bunker-qa.sh status` → `bunkerd=active docker=yes act=no
uidmap=yes`. A launch now would grade `install-proven / isolation-UNVERIFIED` because of the shared network
namespace — NOT `SKIPPED-install-bunker`: host, credential and install path all exist, and installability was
already proven (fresh-install cell OK: duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0).

**Judging decision:** no GitReins task and no judge ran — this is a bookkeeping-only fire with no row of its own to
bind a verdict to (sibling judges share this checkout and must be serialised). The gate that did run is the repo's
own pre-commit hook (`core.hooksPath=.githooks`) plus `boardctl validate` above. Verdict unchanged:
PROMISING-BUT-ROUGH. Nothing filed, nothing left dirty, product code untouched, repo visibility and permissions
untouched.

### 2026-11-09d — DUPLICATE FIRE (fourth delivery) | tick=task-router-dogfood-2026-11-09-12-54-05 (run 42d)

Fourth dispatch of a tick id already worked three times (run 42 committed `0ddfc606`, run 42b `f2bd9981`, run 42c `68ee7bf4`).
Both duplicate surfaces hit before any discovery: log body `grep -c '^#\+ .*task-router-dogfood-2026-11-09-12-54-05'` = 3 (run 42,
42b, 42c headings) and commit-subject `git log --oneline --fixed-strings --grep=<tick-id>` = 2 (`f2bd9981`, `68ee7bf4`, both naming
the clock suffix). Per the duplicate-fire rule the deliverable is this fire's own read-only proof plus this line — no re-measurement,
no arm, no new row. Four dispatches of one tick id is scheduler duplicate-dispatch evidence, raised in chat, not a board row.

**Re-verified this fire, fresh commands (HEAD-bound, scratch `tr-df-20261109-1254-r4-fire4`):** `git fetch origin main` then
`git rev-list --left-right --count HEAD...origin/main` = `0\t0`; HEAD = origin/main =
`68ee7bf4f92259b46725568a30a8f003561904a2`. No Git operation marker: `REBASE_HEAD`/`MERGE_HEAD`/`CHERRY_PICK_HEAD`/`REVERT_HEAD`/
`index.lock`/`rebase-merge`/`rebase-apply` all absent; `git status --short --untracked-files=no` shows only a sibling's
`.gitreins/tasks.yaml`, left untouched.

**Arm decision — none earned, product surface did not move.** `git diff --name-only d9130116..68ee7bf4` (run 42's measured SHA →
now) names only records — `.coding-hermes/board/{tasks,events}.jsonl`, `.coding-hermes/dogfood-log.md`,
`docs/dogfood/2026-11-09-{integration,diagnostics}.md`, `skills/task-router-usage/SKILL.md` — all written by runs 42/42b/42c.
The product-path diff over `task_router tests scripts data Makefile pyproject.toml README.md docs/skills` is EMPTY, so no
documented user-visible surface moved and no un-owned half can exist. Re-timing would only re-measure run 42's subsecond numbers
(spawn 0.232 s, validate 0.124 s, HTTP verify 2.1 ms) that a user cannot feel.

**Board proof (working tree byte-equal to committed blobs):** 493 rows / 996 events, `git status --short -- .coding-hermes/board/`
silent, `jq -r .id | sort | uniq -d` = 0 duplicate ids, `QA-TASKROUTER-22` present exactly once (`pending`) with exactly one
`task_created` event. Unchanged from run 42c, as expected for a fire that wrote nothing.

**Validator (the gate that actually ran):** `boardctl -C /home/kara/task-router validate` → `RESULT: FAIL (448 error(s), 565
warning(s))` / `boardctl: validation failed` — byte-identical standing counts to runs 42/42b/42c (whole-board non-ascending
`events.jsonl` id debt, pre-existing), with ZERO lines mentioning `QA-TASKROUTER-22`: no new lint class, no new harm.

**Open-row premise liveness (anchor probed, not re-measured):** `QA-TASKROUTER-22` (pending, P2) cites `task_router/cli.py:147-181`;
at `68ee7bf4` the anchor is LIVE at exactly the cited lines — `cli.py:169 candidates.extend(_parents(os.path.abspath(os.getcwd())))`
runs before the package-dir candidate, and `_data_dir()` at `cli.py:245`; `task_router/cli.py` sha256
`fde44383…6a67e` is byte-identical to the file that reproduced the defect. Reproduction carried forward. `QA-TASKROUTER-14` and
`TR-1000976681059246` likewise carried forward on a byte-identical product tree.

**Install leg — not launched this fire; census restated, not escalated.** `bunker status --server bunker-mvp` → ONLINE, version
0.2.1, uptime 3d10h38m, agents 5/50 (none created or destroyed by this fire), `/tmp: private (per-session pam_namespace instance)`,
`Net mode: shared — host network namespace … NOT tenant isolation`, Disk 43%, Docker 11 containers, 20 orphan keys. Transport
checked independently of the client's exit status: `curl -s -o /dev/null -w '%{http_code}'` → `404` on
`http://100.115.67.116:19090/health` and `404` on `:18080` (live daemon; `000` would be down). `BUNKER_QA_SERVER=bunker-mvp
bunker-qa.sh status` → `bunkerd=active docker=yes act=no uidmap=yes`. A launch now would grade `install-proven /
isolation-UNVERIFIED` because of the shared network namespace — NOT `SKIPPED-install-bunker`: host, credential and install path all
exist and installability was proven by run 42's fresh-install cell (duckdb-2.0.0 / pyyaml-6.0.3 / task-router-0.2.0).

**Judging decision:** no GitReins task and no judge ran — bookkeeping-only fire with no row of its own to bind a verdict to (sibling
judges share this checkout and must be serialised). The gate that did run is the repo's own pre-commit hook (`core.hooksPath=.githooks`)
plus `boardctl validate` above. Verdict unchanged: PROMISING-BUT-ROUGH. Nothing filed, product code untouched, repo visibility and
permissions untouched.

---

## run 43 — tick `task-router-dogfood-2026-11-12-13-41-25` (2026-11-12 13:41Z, executor: hand-run of skill `coding-hermes-dogfood`)

**Verdict: PROMISING-BUT-ROUGH.** The CLI is genuinely usable — `router spawn --effort <minimal|low|default|high|max>` answers in
well under a second, rejects garbage effort values, and `router status` / `GET /resolve` behave. Four findings came out of USING it,
not from reading tests: two of them are silent-wrong-answer classes a user would never see as an error.

**Filed (4 rows, ids minted by `board_next_id.py`, 515 → 519 board lines):**
- `TR-1000976681059252` (P1) two breaker planes. `router spawn` reads `~/.local/share/task-router/circuit-state.json`
  (346 B, `pairs={}`, zero `open_until`, two Sept fixtures) while `:9092` and `scripts/router_spawn.py` read
  `~/.hermes/model-router/circuit-state.json` (3.85 MB, 5 open breakers). Caught live: at 13:5xZ the CLI named
  `xkiro/openai/gpt-6-luna` while the service skipped it — that pair was `api_down` 13:32:30Z→14:02:30Z. After expiry all
  three entry points agreed again, so the divergence is intermittent and silent.
- `TR-1000976681059253` (P2) `GET /resolve` silently drops effort: `?effort=high`, `?reasoning_effort=high` and a POST body
  all return 200 with `effort={requested:null,source:absent}`, no warnings, chain identical to the no-effort call
  (`scripts/router_server.py:1003-1076` never passes `--effort`).
- `TR-1000976681059254` (P2) served plane is a day-old boot: `:9092`/`:9391` started Nov 11 14:31, the effort exporter landed
  `db40f5ba` at 02:37 -0500 today → `curl :9092/metrics | grep -c effort` = **0** vs **354** from a fresh HEAD server on `:19092`.
  `/health` says ok either way, and `/metrics` carries no build/version label, so nothing distinguishes stale from current.
- `TR-1000976681059255` (P2) install leg dies after the fresh-install cell: `COLLECT-ADOPT reason=recent tick-death-orphan
  agent=c688aa9c` / `AGENT-GONE rows=7` — tests/ui never ran and no per-cell SKIPPED rows explain the gap.

**Not filed (deliberately):** no PERF row. `hyperfine -w 2 -r 8` on `router status` and the headline `router spawn` put every
interactive operation inside tolerance and `GET /resolve` answered in tens of ms; nothing a user would feel.

**Install leg — install PROVEN, battery incomplete (not a SKIPPED row).** `bunker-qa.sh launch` on bunker-mvp gave agent
`c688aa9c`; ledger `/tmp/bunker-qa-evidence-20261112T135003Z-4101880.jsonl` records `launch OK`, `toolchain-bootstrap OK` ×5
(go 1.27.4, venv+pytest, gcc/g++, make 4.3, jq 1.8.2) and `fresh-install OK` — *Successfully installed duckdb-2.0.0
pyyaml-6.0.3 task-router-0.2.0* on a clean host. Then the agent vanished before the test leg. `collect c688aa9c` →
`COLLECT-FAIL no-launch-record` (the record is keyed by evidence path, not agent id — 10 minutes of the tick lost to that);
`collect --evidence <ledger>` → the AGENT-GONE result above. Installability is therefore proven, the rest is filed as a row.

**Write-proof:** `tasks.jsonl` 515 → 519; `boardctl validate .coding-hermes/board` → `RESULT: FAIL (458 error(s), 603 warning(s))`,
identical to the same counts on a copy of the committed HEAD board (`validate-baseline.sh`), and zero lines mention my ids —
the standing red is pre-existing `events.jsonl` id-order debt, my rows added no new lint class. Trail + bunker ledger committed
to `.coding-hermes/evidence/DOGFOOD-2026-11-12-134125{,-bunker.jsonl}`. Product code untouched; repo visibility/permissions untouched.

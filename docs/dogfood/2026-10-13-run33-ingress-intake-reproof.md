# Dogfood run 33 — 2026-10-13 (second same-day run; surfaces already swept by run 32 — re-proof + new contract-edge findings) — bus ingress (TR-236) + board task intake (TR-298)

Angle: first real use of the two surfaces none of runs 1-31 touched — the
inbound door (`scripts/router_ingress.py`) and the automatic board intake
(`scripts/board_task_intake.py`). All work isolated from live state:
`/tmp/df-tr/*` endpoints/ledger/state, and a throwaway bunker agent for the
install leg. No repo code or board state was modified by this lane.

## Promise under test

TR-236: "a Crier bus message addressed to the router is translated into the
request the target endpoint's protocol requires, with the model choice left to
the router" — data-declared endpoints, named refusals, one ledger row per
attempt. TR-298: "a new pending row on the board IS the task-submitted event;
each new id resolves exactly once via the real `--from-task` path."

## What I did (and what happened)

### TR-236 ingress

1. `router_ingress.py endpoints` — validates the 3 committed declarations, all
   `ok`. Adding my own 2-line endpoints file needed zero code — the "data, not
   code" claim holds.
2. `translate` (no forward) with `model: router`: honest model resolution per
   profile — P1_CODING → glm-5.3-flash@xkiro, P0_FORE → qwen3.7-plus:free,
   P7_MOCK → muse-spark-1.3:free; unknown profile → `resolver error: profile X
   not in registry` recorded in `model_reason`. The translate view is excellent.
3. `forward` happy path (webhook, 200 sink): `outcome=accepted`,
   `reason="reply rule 'webhook-separate' …"`, ledger row complete, exit 0.
4. Failure paths, every one a NAMED refusal or failure with a stated reason:
   connection refused → `transport-error: URLError … Connection refused`;
   HTTP 501 → `endpoint-http-501: <body>` (with `http_status` on the ledger);
   unknown endpoint → `refused / unknown-endpoint: 'no-such-endpoint' is not
   declared (known: …)`; auth-unresolved likewise (contract-doc'd, spot-read).
   NULL ledger fields carry `null_reasons` everywhere. This is exactly the
   documented refusal vocabulary working.
5. FINDING (TR-236-DF1, P1): when the resolver cannot pick a model
   (`resolver error: profile X not in registry`), `forward` still FIRES the
   HTTP call with no model field — a doomed call. My LM Studio sink answered
   400 "Multiple models are loaded. Please specify a model" and the ledger
   showed `outcome=failed / model_chosen=null` with the resolver error buried
   in `null_reasons`. `docs/tr236-ingress.md` says a refusal means "the doomed
   call is never fired"; a resolver error is not in the refusal set. Fix
   direction: treat a null model choice on a `model: router` endpoint as
   `refused / model-unresolved: <reason>` before build_hop — or document the
   fire-anyway as intended (then translate is the only honest preview).
6. FINDING (TR-236-DF2, P3): `forward` on a resolver error exits 1, while the
   documented contract (and every other failed forward I drove) is fail-open —
   a webhook `failed` outcome exits 0. Consumers scripting `forward` cannot
   distinguish "endpoint refused" from "CLI crashed". Fix direction: exit 0
   whenever a ledger row was written; nonzero only for CLI-level failures
   (unreadable message file, bad args).

Perf: `forward` = 96.5ms ± 8.8ms warm (hyperfine, 20 runs, webhooksink path);
happy-path webhook forward 0.01s server-side. Nothing here is slow enough that
a user would notice — no PERF row warranted.

### TR-298 intake

1. Control host, isolated synthetic board: backlog `--once` drain resolves 2
   new pending ids exactly once (jev-scored, real chains: muse-spark-1.3
   free lane), malformed line skipped non-fatally, duplicate id NOT
   re-resolved (seen-state idempotence verified: second `--once` →
   `new=0 resolved=0`, ledger unchanged). Crash-recovery probe: pre-seeded a
   dangling claim, re-ran → the doc's contract holds verbatim:
   `resolve interrupted before completion (daemon restart); claim found in
   intake state`, loop continued.
2. Fresh bunker box (bunker-mvp agent ded45cd3): README install verbatim —
   venv + `pip install -e .` (5s) + `pip install duckdb` + `router seed`
   (28s) — spawn P1_CODING resolves xkiro/glm-5.3-flash in 0.19s,
   `router validate` 14/14. Intake on the fresh box with a one-row board:
   **FAILED to pick a model** → see TR-298-DF1.
3. FINDING (TR-298-DF1, P1): on any box without the scorer credentials the
   control host has (ROUTER_CLASSIFIER_BASE_URL, OR_JEV), `--from-task` can
   find the task text (verified: `task_text_for` returns the title) but
   `complexity_requirements` degrades with BOTH scorers unavailable, leaving
   no profile and no adhoc matrix — so spawn answers `NO_INPUT: no
   project/profile given`, and the intake records `resolve FAILED` + error
   row. Result: on a fresh machine, "every time we have a test go in we pick
   a model" resolves NOTHING, and the only signal is a 300-char stderr tail
   in the resolves ledger (stderr was in fact EMPTY on my run — the degrade
   WARNING goes to stderr only when a profile existed to degrade to). Fix
   direction: when task-text scoring degrades and the row carries no declared
   profile, fall back to a documented default profile (or the PAYG-last
   default chain) with `degrade_reason` in the ledger row, instead of
   NO_INPUT. Secondarily: mirror `complexity_meta.problems` into the
   resolves row so the cause is visible without reproducing it by hand.
4. Perf: intake `--once` on a drained backlog = 48.7ms ± 2.1ms (hyperfine,
   5 runs). Nothing a user would feel — no PERF row.

### Install leg

PASSED (not skipped): bunker-mvp reachable via ssh + bunkerd active
(designated las-bunker-03/02/04 all ssh-unreachable this run — used the
skill's host-substitution rule). Agent ded45cd3 spawned, repo delivered via
`git archive HEAD` tarball (repo-visibility rules respected — no new
credentials, no clone widening), install + seed + spawn + validate + intake
probes run, agent DESTROYED and destruction verified (0 matching agents in
list). install_seconds ≈ 40 to first real resolve (5 install + 28 seed + ~7
probes), smoke=ok.

## Verdict

🟡 PROMISING-BUT-ROUGH — both surfaces deliver their contracts' happy paths
and genuinely excellent failure vocabulary, but each fails its own promise in
the exact case a fresh user hits first: ingress fires a doomed call when the
router can't pick a model, intake picks nothing when the scorers aren't
configured. Neither is a code-quality problem — both are contract edges that
the docs state the opposite of what the code does.

Time-to-first-success: ~6 min control host (clone → install → first webhook
accept), ~40s fresh-box install→first resolve (seed dominates).

## Left behind

- This file + `docs/dogfood/diagnostics.md` update
- `skills/task-router-usage/SKILL.md` update (ingress + intake sections)
- Board rows TR-236-DF1, TR-236-DF2, TR-298-DF1, CONFIG-DF-LANE (misseeded
  prompt), INSTALL-TR-20261013 note — via the task-router board
- `.coding-hermes/dogfood-log.md` entry (this run)

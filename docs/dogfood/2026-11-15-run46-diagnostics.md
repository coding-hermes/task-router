# Dogfood run 46 — diagnostics trail (2026-11-15, tick `task-router-dogfood-2026-11-15-15-59-32`)

Every claim in `2026-11-15-run46-integration.md` traces to one of these commands. Scratch root:
`S=~/.hermes/cache/scratch/dogfood-task-router-2026-11-15-15-59-32`. Target was never modified;
all execution happened in `$S/pass1` (an installed `git archive` of HEAD) or against the live
read-only service on `127.0.0.1:9092`.

## 0. Target state

```
cd /home/kara/task-router
git rev-parse --short HEAD          -> 326d36ec   (branch main)
git log -1                          -> 326d36ecf4a093b0f2f6297beb70e7c58ce94997 2026-11-15 06:19:38 -0500
                                       router: arrival-day gate on the 2026-11-15 models.dev wave …
git rev-list --left-right --count HEAD...origin/main -> 0  0
git status --porcelain | wc -l      -> 0            (before the board append)
nproc -> 16 ; uptime -> load 3.51 5.05 8.35 ; df -h / -> 93% (135G free)
```

## 1. Install in a hermetic tree

```
mkdir -p $S/pass1 && git -C /home/kara/task-router archive HEAD | tar -x -C $S/pass1
cd $S/pass1 && python3 -m venv .venv && . .venv/bin/activate
/usr/bin/time -f 'ELAPSED=%e' pip install -e .        -> exit 0
pip install duckdb                                     -> exit 0
command -v router                                      -> $S/pass1/.venv/bin/router
```

## 2. Seed — refusal, and what it mutates

```
router seed                                  -> SATISFIABILITY FAIL, math domain
                                                (ollama-cloud/qwen3.5:397b disabled + unpriced)
                                                owned by TR-384 — not re-filed
router seed --allow-unsatisfiable            -> exit 0, writes registry.json
```

Control experiments (does the seed mutate committed data?) — three trees, all byte-exact exports of
`326d36ec`, hashed over `data/tables/*.jsonl` (32 files each):

```
pass2a  clean control, no seed
pass2b  `python3 -m scripts.router_seed` (no flag)
          -> rc 1, NO registry.json, 32/32 tables identical to the control
pass3   `python3 -m scripts.router_seed --allow-unsatisfiable`
          -> rc 0, registry.json 7,973,096 B, 32/32 tables identical to the control
pass4   copy of pass3, then `router validate --json` -> `router status --format json`
          -> `router spawn task-router --format json` -> `router decide --request req.json`
          -> 0 files changed at every step, 0 vs baseline
```

So neither the refusal nor a successful seed writes a tracked table, and no read command does
either — the premise of the pending `TR-1000976681059257` (and of the completed
`TR-1000976681059248`) does not reproduce at this HEAD. Filed as `TR-1000976681059261`.

The one delta observed this tick was in the long-lived `pass1` tree, which ran a bare `router seed`
*before* the flagged one and then the whole CLI battery:

```
join of pass1 vs control hashes:
  data/tables/category_levels.jsonl  c537627f…  vs  235d56b6…
  data/tables/model_tier.jsonl       9adc7354…  vs  c7f3c2e8…
every other one of the 32 files identical
```

That writer is **unattributed** — the controlled replays (pass2b/3/4) rule out the seed and the read
commands but do not reproduce the delta, so the row asks the worker to bisect the
bare-seed-then-flag sequence before changing `scripts/router_seed.py`.

## 3. CLI use

```
router validate --json          -> registry valid, agrees with registry.json
router status --format json     -> live catalog; gaps 798/798 "active models" (status.py:15)
router spawn task-router --format json
    -> resolved head provider/model + usd_1m, sorted chain with effective_price, exclusions list
router spawn P1_CODING --format json       -> resolved_as = profile P1_CODING
router spawn not-a-project --format json   -> json error doc, rc=0
    README.md:7 documents exactly that fail-open contract ("always exits 0 … so the consuming
    scheduler can use its own fallback") -> contract, NOT a defect. No row filed.
router estimate task-router --tokens-in 1000 --tokens-out 500   -> documented form works
    (an undocumented `--in/--out` is rejected; README documents the correct flags)
```

## 4. Decision path (CLI)

```
router decide --init-receipt-key
# req.json: version 1, parent_run_id run-dogfood-1115, step_id step-1, generation 0,
#           brief_digest sha256(brief.txt), requester foreman@task-router,
#           required_levels {code_gen: 3}, required_context_limit 200000
router decide --request req.json --format json   -> receipt.json
    receipt: receipt_id, eligible, head{provider,model}, valid_until, valid_for_s,
             candidates/exclusions counts, key_id, signature_alg, registry, receipt_digest
router decide --verify receipt.json --expect-parent-run run-dogfood-1115 \
                              --expect-step step-1 --expect-generation 0   -> ok
router decide --verify receipt.json --expect-…            (head.model edited to openai/gpt-6-sol)
                                                          -> rejected (signature/digest)
```

Binding and tamper rejection hold. Note for future runs: the receipt key is per-state-dir, so a
receipt minted in a scratch tree is not verifiable by an instance that lacks that key — the server's
answer for a foreign/missing key is `ok=false`, never a silent pass.

## 5. HTTP surface on the live read-only instance (`127.0.0.1:9092`, PID 2029853)

```
GET  /health                                   -> 200 ; .registry, .build, .gates present
GET  /resolve?project=task-router&effort=low   -> 200
GET  /resolve?project=task-router&effort=high  -> 200      (effort handling owned by TR-1000976681059253)
GET  /gaps                                     -> 200 ; total_models 798, gapped 798
GET  /listings/models?limit=2                  -> 200
GET  /openapi.json                             -> 200 ; paths include /health, /resolve,
                                                  /api/v1/execution-decision(+ /verify)
POST /decide                (legacy guess)     -> **404** {"error":"not found","path":"/decide",
                                                   "hint":"this is not a POST endpoint; GET
                                                   /openapi.json lists the contract","surface":[…]}
                                                   — /decide is not in /openapi.json; the hint names
                                                   /api/v1/execution-decision. Good error UX.
POST /api/v1/execution-decision  (req.json)    -> **403** {"error":"read-only mode",
                                                   reason: … restart with --mode edit …}
                                                    the resolver is never reached
POST /api/v1/execution-decision/verify         -> 200, no key required
                                                  ok=false, problems[]:
                                                    RECEIPT_UNBOUND_EXPECTATION, RECEIPT_KEY_UNAVAILABLE
```

Code proof for the 403 being deliberate rather than a bug:

```
scripts/router_server.py:1109   if path not in READ_ONLY_POST_PATHS:  -> 403 read-only mode
scripts/router_server.py:1368   "# H1-F4: /api/v1/execution-decision is deliberately NOT here.
                                 It appends an [issuance record] …"
scripts/router_server.py:1374-1377  READ_ONLY_POST_PATHS = ("/api/v1/execution-decision/verify", …)
```

…and the contradiction that became `TR-1000976681059260`:

```
scripts/router_decision.py:30   " * HTTP:    POST /api/v1/execution-decision (+ /verify), read-only mode OK"
```

## 6. Install leg (bunker estate)

```
~/.hermes/scripts/bunker-qa.sh resolve /home/kara/task-router   -> /home/kara/task-router (rc 0)
~/.hermes/scripts/bunker-qa.sh status
    -> server bunker-mvp ; bunkerd=active docker=yes act=no uidmap=yes
BUNKER_QA_SERVER=bunker-las-02 … status -> bunkerd=active docker=yes act=no uidmap=yes
BUNKER_QA_SERVER=bunker-las-01 … status -> UNREACHABLE
BUNKER_QA_SERVER=bunker-las-04 … status -> UNREACHABLE
BUNKER_QA_SERVER=cube-las-00   … status -> UNREACHABLE

~/.hermes/scripts/bunker-qa.sh launch /home/kara/task-router
    -> PREFLIGHT FAIL: target host bunker-mvp disk usage 97% exceeds 94% launch threshold
BUNKER_QA_SERVER=bunker-las-02 … launch /home/kara/task-router
    -> PREFLIGHT FAIL: target host bunker-las-02 disk usage 100% exceeds 94% launch threshold
```

Zero cells executed, no evidence file, no agent id. Filed as the explicit SKIPPED-install-bunker
row `TR-1000976681059255-R3`. The disk condition belongs to the bunker lane, not to task-router:
it is recorded here so a red install column is not mistaken for a product regression.

## 7. Performance

```
cd $S/pass1 && . .venv/bin/activate
/usr/bin/time -f 'COLD=%es RSS=%MKB' router spawn task-router --format json > /dev/null
    -> COLD=0.19s RSS=49812KB
hyperfine --warmup 2 --runs 8 --style basic 'router spawn task-router --format json > /dev/null'
    -> Time (mean ± σ): 197.9 ms ± 9.4 ms   [User 165.6 ms, System 32.3 ms]
cat /proc/loadavg -> 3.61 5.21 8.49

for i in 1..6: curl -s -o /dev/null -w '%{time_total}' \
      'http://127.0.0.1:9092/resolve?project=task-router&effort=high'
    -> 0.299859 0.298920 0.317323 0.296499 0.290509 0.300414
for i in 1..3: curl -s -o /dev/null -w '%{time_total}' http://127.0.0.1:9092/health
    -> 0.189333 0.125148 0.130527
```

No operation is slow enough to be felt → **no PERF row**. The served-vs-CLI delta (~300 ms vs
~198 ms for the same resolver) is logged as an observation, unprofiled on purpose.

## 8. Board write verification (a "filed: N" is not proof)

```
python3 ~/.hermes/scripts/board_next_id.py .coding-hermes/board/tasks.jsonl TR
    -> TR-1000976681059260
python3 ~/.hermes/scripts/board_next_id.py .coding-hermes/board/tasks.jsonl TR --recur TR-1000976681059255
    -> TR-1000976681059255-R3
python3 ~/.hermes/scripts/board_next_id.py .coding-hermes/board/tasks.jsonl TR      (re-minted after the
    -> TR-1000976681059261                                                          seed re-measurement)
python3 ~/.hermes/scripts/board_append.py .coding-hermes/board/tasks.jsonl "$(cat $S/rowA.json)" "$(cat $S/rowB.json)"
    -> APPENDED=2 PRIOR=527 TOTAL=529
python3 ~/.hermes/scripts/board_append.py .coding-hermes/board/tasks.jsonl "$(cat $S/rowC.json)"
    -> APPENDED=1 PRIOR=529 TOTAL=530
jq -r '.id' .coding-hermes/board/tasks.jsonl | tail -3
    -> TR-1000976681059260, TR-1000976681059255-R3, TR-1000976681059261
grep -c '"id": "TR-1000976681059260"'      -> 1
grep -c '"id": "TR-1000976681059255-R3"'   -> 1
grep -c '"id": "TR-1000976681059261"'      -> 1
boardctl validate .coding-hermes/board
    -> RESULT: FAIL (455 errors, 590 warnings) — pre-existing damage in tasks.jsonl/events.jsonl rows
       that predate this tick; owned by TR-1000976681059258. None of the three new rows is named
       in the report.
```

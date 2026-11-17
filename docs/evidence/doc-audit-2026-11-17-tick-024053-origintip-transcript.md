TICK: task-router-docs-2026-11-17-02-40-53 (re-validation sweep, published-tip re-derivation)
TIP: c8d61cfa9686af324e5c3ad0e73182ea6312e7ca  (docs(evidence): 02:40:53 docs-audit re-fire — re-verified DOC-22/DOC-23 at the tip, filed 0 rows)
DATE: 2026-11-17T05:00:32Z

=============== P1: docs/failure-contract.md inbound links ===============
$ git grep -n "failure-contract" HEAD -- "*.md" | grep -v "^HEAD:docs/failure-contract.md"
(no output)
$ git grep -l "failure-contract" HEAD -- "*.py"
HEAD:tests/test_failure_contract.py
$ sed -n "1,8p" docs/failure-contract.md
# Failure contract — class → caller action (TR-242)

One table, enforced by tests (`tests/test_failure_contract.py`). Every failure
class the router can report maps to EXACTLY ONE caller action — the same class
must mean the same thing in the envelope (`_router`), the ledger row
(`failure_reason`), and on the client. This document is the arbitration point:
when code, ledger and this table disagree, the table wins and the code is the
bug.

=============== P2: seed --allow-unsatisfiable / ROUTING_ALLOW_UNSATISFIABLE ===============
$ python3 scripts/router_seed.py --help
usage: router_seed.py [-h]

Seed the task-router routing registry

options:
  -h, --help  show this help message and exit
rc=0
$ grep -n "allow-unsatisfiable\|ALLOW_UNSATISFIABLE" scripts/router_seed.py
2323:            '--allow-unsatisfiable', action='store_true',
2333:            os.environ.get('ROUTING_ALLOW_UNSATISFIABLE') == '1'
2398:                    print('\n--allow-unsatisfiable: seeding anyway — these ratings '
2402:                          'categories above, or re-run with --allow-unsatisfiable '
$ grep -n "help" scripts/router_seed.py | head -4   # --help intercept
122:# (opt-in style: never done at import time, TR-029 keeps --help write-free).
126:# TR-029: --help must be safe and exit 0 with ZERO file writes. The full seed
127:# body is module-level (legacy shape); intercept --help before any table loads
129:if '--help' in sys.argv or '-h' in sys.argv:
$ grep -rn "unsatisfiable" README.md docs/configuration.md CONTRIBUTING.md
(no output)
$ grep -rln "allow-unsatisfiable" . --include="*.md" | grep -v "^./.git"
./docs/decisions/TR-1000976681059248-seed-publish-atomicity.md
./docs/unmeasured-categories.md
./docs/dogfood/2026-11-12-run44-integration.md
./docs/dogfood/2026-11-15-run46e-diagnostics.md
./docs/dogfood/2026-11-12-run44-diagnostics.md
./docs/dogfood/2026-11-15-run46-diagnostics.md
./docs/dogfood/2026-11-06-integration.md
./docs/dogfood/2026-11-12-run45-diagnostics.md
./docs/dogfood/2026-11-12-run45-integration.md
./docs/dogfood/2026-11-15-run46-integration.md
./docs/dogfood/2026-11-15-run46e-integration.md
./docs/dogfood/2026-11-09-diagnostics.md
./docs/dogfood/2026-11-06-diagnostics.md
./docs/dogfood/2026-11-09-integration.md
./skills/task-router-usage/SKILL.md
./.coding-hermes/dogfood-log.md
$ grep -n "allow-unsatisfiable" skills/task-router-usage/SKILL.md
43:router seed --allow-unsatisfiable           # build registry.json from data/tables
310:At `af84c2e6`, a fresh isolated clone installed with `pip install -e .` and `pip install duckdb`, then `router seed --allow-unsatisfiable` completed in 12.80s and `router validate --json` returned `valid=true`. Effort-aware CLI resolves returned the requested `reasoning_effort` for `low`, `high`, and `max`, with `source=registry.json` and `fallback_used=false`. An unknown `--effort bogus` is fail-open: exit 0, a stderr warning says the value was ignored, and the result is effort-absent; callers that need strict input rejection must validate before invoking the router. The prescribed bunker leg was not run because bunker 0.3.0 had no configured server; see board row `TR-1000976681059256` and `docs/dogfood/2026-11-12-run44-diagnostics.md`. Warm `router spawn --effort high` measured 62.4ms ± 2.6ms (n=6) after a 0.06s cold process; no PERF row was warranted.
334:`router seed --allow-unsatisfiable` (registry.json ≈ 6.99 MB) before `validate`/`spawn` mean
396:   `--allow-unsatisfiable` it writes `registry.json` (~8 MB) and leaves all 32 tracked

=============== P3: ROUTING_TIER_LADDER_IGNORE ===============
$ grep -n "TIER_LADDER_IGNORE" scripts/router_seed.py scripts/router_tier_calibrate.py
scripts/router_seed.py:686:_LADDER_IGNORED = os.environ.get('ROUTING_TIER_LADDER_IGNORE') == '1'
scripts/router_seed.py:1822:    print(f'tier ladder: IGNORED (ROUTING_TIER_LADDER_IGNORE=1) — '
scripts/router_tier_calibrate.py:13:  2. runs the seed there with ROUTING_TIER_LADDER_IGNORE=1, i.e. the pure
scripts/router_tier_calibrate.py:69:                   ROUTING_TIER_LADDER_IGNORE='1',
$ grep -rn "TIER_LADDER_IGNORE" README.md docs/configuration.md docs/registry-maintenance.md docs/duckbrain-namespace.md
(no output)

=============== P4: TASK_ROUTER_REPO ===============
$ grep -rn "TASK_ROUTER_REPO" README.md docs/configuration.md CONTRIBUTING.md AGENTS.md docs/integration.md
(no output)
$ grep -n "TASK_ROUTER_REPO" task_router/cli.py
156:      1. TASK_ROUTER_REPO / TASK_ROUTER_HOME -- an operator override wins.
165:    explicit = os.environ.get("TASK_ROUTER_REPO") or os.environ.get("TASK_ROUTER_HOME")
535:        # Installed-from-wheel case (or a wrong TASK_ROUTER_REPO): say what to do
543:            "    * point at one explicitly: TASK_ROUTER_REPO=/path/to/task-router router "
$ sed -n "536,548p" task_router/cli.py
        # instead of dying with a site-packages path nobody can act on.
        print(
            "router: cannot find the task-router checkout.\n"
            f"  looked for: {script}\n"
            "  The scripts/ tools ship with the checkout, not with the installed wheel.\n"
            "  Fix one of these ways:\n"
            "    * run `router` from inside a checkout (any subdirectory works), or\n"
            "    * point at one explicitly: TASK_ROUTER_REPO=/path/to/task-router router "
            f"{cmd} ..., or\n"
            "    * use the checkout's own entry point: python3 scripts/"
            f"{COMMANDS[cmd]} ...",
            file=sys.stderr)
        raise SystemExit(USAGE_ERROR)
$ grep -n "TASK_ROUTER_REPO" skills/task-router-usage/SKILL.md
314:**Run the CLI from the repo root, or pin `TASK_ROUTER_REPO`.** `task_router/cli.py:147-181`
322:export `TASK_ROUTER_REPO=/home/kara/task-router` and re-check the count.

=============== P5: ROUTER_BASE_URL ===============
$ grep -n "ROUTER_BASE_URL" scripts/record_outcome.py
53:DEFAULT_BASE = os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:9092")
$ grep -rn "ROUTER_BASE_URL" README.md docs/ skills/ --include="*.md"
(no output)
$ sed -n "174,196p" docs/integration.md
**3. Write back the outcome** (closes the loop; required for the averages to
learn). The normal path is the session REPORTING ITSELF — one command, no
cron: `scripts/record_outcome.py` reads the session's own meter and posts it
(safe to re-run; rows are keyed, so a duplicate is skipped):

```bash
# the session reports itself when it ends
python3 scripts/record_outcome.py --from-state-db <session_id> --profile-id P1_CODING
```

The wire path is the same endpoint if a host wants to post its own numbers:

```bash
curl -X POST localhost:9092/api/v1/outcomes -H 'Content-Type: application/json' -d '{
  "source_system":"hermes","session_id":"s-123","profile_id":"P1_CODING",
  "required_categories":{"code_gen":2,"test":1},
  "provider":"deepseek","model":"deepseek-v4-flash",
  "turns":7,"tokens_in":52000,"tokens_out":3100,"cost_usd":0.0112,
  "wall_time_s":184.5,"success":true}'
```

`POST /api/v1/outcomes` is deliberately served in read-only mode without the
edit key: it appends to the local measured-outcome store that only readers

=============== B: board baseline at published tip ===============
$ boardctl validate | tail -2
RESULT: FAIL (455 error(s), 594 warning(s))
boardctl: validation failed
errors:
455
warns:
594
$ tail -1 events.jsonl (last event id, for append ordering)
1794115000000000075

=============== C: DOC rows already on the published board ===============
$ jq -r ... DOC ids/status/priority
DOC-5|pending|P3|P4 — Correct the ROUTER_MODEL_ROUTER_DIR configuration reference
DOC-6|pending|P3|P4 — Document the live API readiness, model-browser, rollup, and listing routes
DOC-7|pending|P3|P4 — Add ROUTING_OUTCOMES_FILE and ROUTING_AVERAGES_FILE to the configuration re
DOC-8|pending|P3|P4 — Correct the classifier Default column in docs/configuration.md (ROUTER_CLAS
DOC-9|pending|P3|P4 — Fix the ROUTER_MODELSDEV_CACHE default in docs/configuration.md (doc data/m
DOC-10|pending|P3|P4 — Correct the ROUTER_* count claim at README.md:637 (states 44; docs/configur
DOC-11|pending|P3|P4 — Document the five ROUTING_* store overrides read by seed/maintain/pricing-a
DOC-12|pending|P3|P5 — De-duplicate the README CLI reference table (chain-run/lifecycle/outcomes/p
DOC-13|pending|P3|P4 — Document router context-call in the README CLI reference
DOC-14|pending|P3|P4 — Document context-call options and four-surface read-only contract
DOC-15|pending|P3|P4 — Document API readiness, model-browser, rollup, outcome, listing, and servin
DOC-16|pending|P3|P4 — Add source-backed ROUTING_* store overrides to the configuration reference
DOC-17|superseded|P3|P4 — Correct the ROUTER_MODEL_ROUTER_DIR configuration reference
DOC-18|pending|P3|P4 — Document the router outcomes apply-verdicts and deadline-report child comma
DOC-19|pending|P3|router chain-run refuses to run outside a checkout (rc 2) while README:632 claim
DOC-22|pending|P3|P4 — docs/health-plane.md §2 documents 14 of the 26 blocks the published /health
DOC-23|pending|P3|P4 — No documented refresh path for data/tables/benchmarks.jsonl (scripts/router

END OF TRANSCRIPT

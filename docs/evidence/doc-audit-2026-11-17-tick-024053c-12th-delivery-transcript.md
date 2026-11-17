PROBE ROOT: /home/kara/.hermes/stand-in/docs/task-router/probe-d12
TIP: bd51394a630d9fe475bbe9f7b6b5115b189270cf
REMOTE TIP: bd51394a630d9fe475bbe9f7b6b5115b189270cf
DATE: 2026-11-17T04:25:13-05:00
WORKTREE DIRTY LINES: 0

=============== P1: the repo's own marker validator, at the published tip ===============
$ PYTHONPATH=. /home/kara/task-router/.venv/bin/python scripts/trace_context.py --validate-only --roots .
rc=2   (non-zero = the validator REFUSES the tree as it stands)
--- validator summary lines (markers scanned / invalid) ---
2:files scanned: 1051 | markers matched: 26 | invalid: 8
205:8 invalid marker(s):
--- the invalid-marker block, verbatim ---
205:8 invalid marker(s):
206-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/reports/tr253-prompt-load.txt:27  missing required `row`
207-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/docs/capability-flag-precedence.md:3  neither witness= nor evidence= present
208-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/docs/evidence/doc-audit-2026-11-17-tick-024053c-4th-delivery-transcript.md:32  neither witness= nor evidence= present
209-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/docs/evidence/doc-audit-2026-11-17-tick-024053c-4th-delivery-transcript.md:35  neither witness= nor evidence= present
210-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/docs/evidence/tr343-ci-replay.md:79  missing required `row`; neither witness= nor evidence= present
211-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/tests/test_quota_gate.py:785  neither witness= nor evidence= present
212-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/tests/test_routing_quality_corpus.py:119  neither witness= nor evidence= present
213-  /home/kara/.hermes/stand-in/docs/task-router/probe-d12/tests/test_circuit_reset_aware.py:1  verdict= without commit=

--- how many ch:trace marker lines exist in tracked files (denominator) ---
$ git grep -c -F "ch:trace" HEAD | wc -l   # files carrying a marker
67
$ git grep -F "ch:trace" HEAD | wc -l       # marker lines
278

--- the reader-facing doc page carrying an invalid marker ---
$ grep -n 'ch:trace' docs/capability-flag-precedence.md
3:ch:trace row=INT-CI-21 test=tests/test_routing_quality_corpus.py

--- tests that carry an invalid or multi-line marker ---
$ grep -n 'ch:trace' tests/test_quota_gate.py
785:# ch:trace row=TR-345 spec=README.md:81,148 test=tests/test_quota_gate.py
$ sed -n '785,787p' tests/test_quota_gate.py
# ch:trace row=TR-345 spec=README.md:81,148 test=tests/test_quota_gate.py
#   evidence="AC-A gate=OPEN chain=133 | AC-B 0 policy-gate-missing-row | AC-C
#   gate=OPEN chain=83 | AC-D 35+ passed" witness=run verdict=CONFIRMED
$ grep -n 'ch:trace' tests/test_routing_quality_corpus.py
119:ch:trace row=TR-350 spec=board-row-TR-350 test=tests/test_routing_quality_corpus.py
$ grep -n 'ch:trace' tests/test_circuit_reset_aware.py
1:# ch:trace row=TR-346 spec=none:row-reasoning-is-the-contract wave=none:serial-tick test=tests/test_circuit_reset_aware.py::test_weekly_cap_429_gates_pair_until_stated_reset doc=none:no-doc-surface-changed prompt=none:worker-brief-external evidence=commit witness=tests/test_circuit_reset_aware.py verdict=none:pending-foreman-trace

--- evidence artifact whose marker the validator flags ---
$ grep -n 'ch:trace' docs/evidence/tr343-ci-replay.md reports/tr253-prompt-load.txt
docs/evidence/tr343-ci-replay.md:76:ch:trace row=TR-343 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-worker test=tests/test_tr339_ci_wiring.py
docs/evidence/tr343-ci-replay.md:79:EVIDENCE: tests/test_tr339_ci_wiring.py (7 TR-343 cases, focused run + full suite in the worker report); commit = this commi
reports/tr253-prompt-load.txt:27:  ceability-doctrine.md in the repo that owns the work. One recursive grep must find every marker. MARKER (one line, wherever t

--- does ANY test, gate or hook run the validator? ---
$ git grep -ln -e "validate-only" -e "trace_context" HEAD -- "tests/*.py" "scripts/*.py" ".githooks/*"
HEAD:scripts/trace_context.py

--- is the validator reachable from reader-facing markdown? ---
$ git grep -n -F trace_context HEAD -- "*.md" | grep -v "docs/evidence"
(no hits)

=============== P2: docs/quota-layers-spec.md status header vs shipped L1/L2/L3 ===============
--- the status block, verbatim ---
$ sed -n '3,5p' docs/quota-layers-spec.md
Status: L0 (declaration table + validator) IMPLEMENTED — TR-206, 2026-10-01
(`data/tables/provider_quota.jsonl` + `scripts/validate_provider_quota.py`).
L1–L3 are not implemented yet; the research wave filled the data table.

--- the SAME page's per-layer implemented statements ---
$ grep -n 'Implemented 20' docs/quota-layers-spec.md
75:  opencode-go/-2 are explicitly `unknown` with the reason. *(Implemented 2026-11-01, TR-216.)*
87:   *Implemented 2026-10-02 (TR-207): `scripts/router_quota_readback.py` — one adapter per header mechanism,
117:*Implemented 2026-10-09 (TR-209): `scripts/router_quota_accounting.py` — the view over
134:   *Implemented 2026-10-10 (TR-210): `scripts/router_quota_pacing.py` — the gate as a PURE decision over
141:   *Implemented 2026-10-10 (TR-211): `scripts/router_quota_burn.py` — the projection as a PURE
154:   *Implemented 2026-10-13 (TR-213): the demote slot lives inside `router_spawn._legacy_sort_key` AFTER the

--- shipped modules for the three 'not implemented' layers ---
$ ls -1 scripts/router_quota_readback.py scripts/router_quota_accounting.py scripts/router_quota_pacing.py scripts/router_quota_burn.py
scripts/router_quota_accounting.py
scripts/router_quota_burn.py
scripts/router_quota_pacing.py
scripts/router_quota_readback.py

--- tests that pin the layers ---
$ ls -1 tests/test_quota_accounting*.py tests/test_quota_pacing.py tests/test_quota_burn.py 2>/dev/null
tests/test_quota_accounting.py
tests/test_quota_burn.py
tests/test_quota_pacing.py

--- the operator CLI surface ---
$ PYTHONPATH=. /home/kara/task-router/.venv/bin/python -m task_router.cli quota --help
usage: router_quota.py [-h] COMMAND ...

Plan-window quota gates (429 exhaustion) in quota-state.json. A gate excludes
the provider until its reset_at passes.

positional arguments:
  COMMAND
    set         record a plan-window gate for a provider
    clear       remove a plan-window gate (re-enable the lane)
    status      show recorded plan-window gates
    accounting  L2 quota accounting: spent/remaining/resets_at per provider
                account, derived from the outcome ledger (TR-209; per-account
                since TR-216 — multi-account providers report one entry per
                account, never a summed row; no second store)
    pacing      L3 quota pacing gate: admit / soft-space / refuse one call
                against the provider window derived from the outcome ledger
                (TR-210)
    burn        L3 quota burn-surplus finder: windows whose quota will expire
                unused, projected from the outcome ledger (TR-211; report
                only, never auto-spends)

options:
  -h, --help    show this help message and exit
rc=0

$ PYTHONPATH=. /home/kara/task-router/.venv/bin/python -m task_router.cli quota accounting --json --provider zai-glm   # L2 view, real run
{
 "as_of": "2026-11-17T09:25:15+00:00",
 "ledger": "/home/kara/.hermes/stand-in/docs/task-router/probe-d12/data/state/outcomes.jsonl",
 "limits_file": "/home/kara/.hermes/stand-in/docs/task-router/probe-d12/data/quota_limits.json",
 "limits_present": true,
 "providers": []
}
rc=0

$ PYTHONPATH=. /home/kara/task-router/.venv/bin/python -m task_router.cli quota burn --json --provider zai-glm        # L3 report, real run
{
 "as_of": "2026-11-17T09:25:15+00:00",
 "ledger": "/home/kara/.hermes/stand-in/docs/task-router/probe-d12/data/state/outcomes.jsonl",
 "limits_file": "/home/kara/.hermes/stand-in/docs/task-router/probe-d12/data/quota_limits.json",
 "limits_present": true,
 "summary": {
  "burn": 0,
  "no_burn": 0,
  "unknown": 0,
  "total_surplus_usd": 0
 },
 "windows": []
}

--- L0 declaration table ---
$ wc -l data/tables/provider_quota.jsonl
93 data/tables/provider_quota.jsonl

--- the page IS reachable (so this is not DOC-24's inbound-link finding) ---
$ git grep -n -F quota-layers-spec HEAD -- docs/configuration.md docs/ui/data-command-center.md
HEAD:docs/configuration.md:149:| `ROUTER_QUOTA_ORDER_GUARD` | `router_spawn.py` | TR-213 quota-ordering demote guard: a lane whose window he
HEAD:docs/ui/data-command-center.md:190:(docs/quota-layers-spec.md §3), rung stamped on EVERY number:

--- boundary vs DOC-30: the two pages DOC-30 names -- do they cite the spec? ---
$ git grep -c -F quota-layers-spec HEAD -- README.md docs/quota-gate.md
(no hits: DOC-30 owns that omission, not this row)

--- the exact stale sentence ---
$ grep -n 'not implemented' docs/quota-layers-spec.md
5:L1–L3 are not implemented yet; the research wave filled the data table.

=============== END ===============

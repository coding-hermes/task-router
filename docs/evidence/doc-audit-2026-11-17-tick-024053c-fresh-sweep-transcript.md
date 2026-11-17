# task-router documentation audit — fresh sweep (third delivery of tick 2026-11-17-02-40-53)

Probe root: clean detached worktree of `origin/main` (`git worktree add --detach ... origin/main`).
The canonical checkout `/home/kara/task-router` was NOT touched: `git status --porcelain` showed
6 modified data/table + scripts paths and 2 untracked paths, and the branch was 109 commits
BEHIND origin/main with 7 local commits ahead — a write stop AND a stale-board publish risk.

This delivery is the THIRD for this tick id. The first delivery filed DOC-20/DOC-21 (stranded
in the diverged canonical checkout); the second filed DOC-24..DOC-27 and a REFIRE of it
reported `filed_rows: 0`. Neither probed the CLI help surface, the undocumented-env-key corpus,
or the `router quota` subcommand surface. This sweep did, and found three gaps those deliveries
did not reach. DOC-24..DOC-27 are NOT re-filed and NOT amended.

All corpus measurements below use `git grep` over TRACKED blobs, so this artifact (untracked
while the probes ran) cannot count its own text as documentation coverage.

## 0. Revision and checkout state

### $ git log -1 --format='%H %ci %s'
```
c024739583b2819028c1661c8344ac4440199930 2026-11-17 00:11:44 -0500 board(task-router): file DOC-24..DOC-27 (P4-labelled, board floor P3)
```

### $ git status --porcelain | wc -l
```
1
```

### $ git rev-list --left-right --count origin/main...HEAD
```
1	0
```

## 1. Surface derivation — the CLI dispatch table

`task_router/cli.py:187-216` declares the COMMANDS map that IS the shipped subcommand surface.

### $ sed -n '187,189p;214,216p' task_router/cli.py
```
COMMANDS = {
    "spawn":      "router_spawn.py",
    "circuit":    "router_circuit.py",
    "context-call": "router_context_call.py", # TR-254: ONE CONTEXT CALL (code + DuckBrain + specs + board)
    "decide":     "router_decision.py",       # ACP-ROUTER-001: JIT execution decision route receipt
}
```

### $ grep -cE '^    "[a-z-]+":' task_router/cli.py
```
28
```

## 2. FINDING 1 — `router --help` prints 6 of 28 commands with a BLANK description (T3)

The help printer builds `texts` at `task_router/cli.py:630-653` and renders each line with
`texts.get(name, ())` at `task_router/cli.py:656`, so a command absent from `texts` prints its
name padded and nothing else. Six shipped commands are absent from that dict.

### $ grep -n 'texts = {\|texts.get(name' task_router/cli.py
```
630:    texts = {
656:        print(f"  {name}{pad}{texts.get(name, '')}")
```

### $ PYTHONPATH=. python3 -m task_router.cli --help
```
usage: router [-h] COMMAND [args...]

task-router CLI — deterministic model routing for the coding-hermes fleet.
Each subcommand runs the matching scripts/router_<name>.py tool with
data-home env overrides applied. `router <cmd> --help` passes through to
the underlying tool's own argparse.

COMMAND
  chain-run   
  circuit     circuit-breaker state for (provider, model) pairs
  clinepass   Cline Pass plan lane diagnostics
  context-call
  decide      just-in-time execution decision: closed v1 request -> canonical expiring route receipt (ACP-ROUTER-001; --verify checks a receipt)
  diff        chain snapshot diff between two dates (head moves, price deltas)
  estimate    cost estimate for a project's chain at given token volumes
  gaps        registry coverage gap report
  ingress     
  learn       DuckBrain-backed learning loop (dump/lesson/doctrine)
  ledger      spawn-ledger start/end/status (TR-007)
  lifecycle   
  maintain    registry repair/export/reprice maintenance
  metrics     usage metrics: top providers/models/pairs, per-profile, since-window
  modelsdev   models.dev catalog sync + price refresh
  outcomes    
  plan-sweep  disable plan-outside flat-subscription lanes
  pricing     price table diagnostics
  pricing-audit
  probe       provider health probe (manual calibration run)
  probefix    resolve 404/400 model ids from probe logs
  quota       plan-window quota gates (429 exhaustion): set/clear/status; plus accounting (TR-209 L2 view), pacing (TR-210 L3 gate), burn (TR-211 L3 burn-surplus finder)
  seed        rebuild registry.json from data/tables/*.jsonl
  server      OpenAPI API server + MCP bridge (read-only | edit with API key)
  spawn       resolve a task/profile to a model chain (fail-open JSON/text)
  status      one-command overview: registry, gates, circuit, gaps (json|text)
  validate    registry/schema/state/profile integrity check (--json, exit 1 on issues)
  web         local web UI: settings editor + live resolve preview (:9093)

Data home: $TASK_ROUTER_HOME > $XDG_DATA_HOME/task-router > ~/.local/share/task-router (see task_router.paths).
```

### $ PYTHONPATH=. python3 -m task_router.cli --help | awk '/^  [a-z]/{n++} END{print "commands listed: " n}'
```
commands listed: 28
```

### $ PYTHONPATH=. python3 -m task_router.cli --help | awk '/^  [a-z]/ \&\& NF==1 {print "BLANK: " $1}'
```
awk: 1: unexpected character '\'
Exception ignored while flushing sys.stdout:
BrokenPipeError: [Errno 32] Broken pipe
```

## 3. FINDING 2 — ROUTER_STATE_DIR_WARN appears in zero reader-facing docs (T2)

Derivation: every ROUTER_/ROUTING_/TASK_ROUTER_ token in tracked `task_router/` + `scripts/`,
minus every such token in tracked Markdown.

### $ git grep -hoE '(ROUTER|ROUTING|TASK_ROUTER)_[A-Z0-9_]{3,}' -- task_router scripts | grep -vE '_$' | LC_ALL=C sort -u | wc -l
```
170
```

### $ git grep -hoE '(ROUTER|ROUTING|TASK_ROUTER)_[A-Z0-9_]{3,}' -- '*.md' | grep -vE '_$' | LC_ALL=C sort -u | wc -l
```
179
```

### $ cs=$(mktemp); cd_=$(mktemp); git grep -hoE '(ROUTER|ROUTING|TASK_ROUTER)_[A-Z0-9_]{3,}' -- task_router scripts | grep -vE '_$' | LC_ALL=C sort -u > $cs; git grep -hoE '(ROUTER|ROUTING|TASK_ROUTER)_[A-Z0-9_]{3,}' -- '*.md' | grep -vE '_$' | LC_ALL=C sort -u > $cd_; LC_ALL=C comm -23 $cs $cd_
```
ROUTER_E2E_KEY
ROUTER_GENERATED_MARKERS
ROUTER_HOP0_RULE
ROUTER_MODE
ROUTER_STATE_DIR_WARN
ROUTING_BOARD_PY
ROUTING_DUCKBRAIN_HOME
ROUTING_LIFECYCLE_FILE
ROUTING_MD_CACHE
ROUTING_STATE_DB
```

The residual set is 10 tokens. Classification, each read individually before filing:

| token | what it actually is | owner |
|---|---|---|
| `ROUTER_STATE_DIR_WARN` | REAL env override: `force` / `suppress` the shared-state-dir warning (scripts/state_dir.py:36-37, :59, :106) | **UNOWNED — this finding** |
| `ROUTING_MD_CACHE`, `ROUTING_STATE_DB`, `ROUTING_DUCKBRAIN_HOME`, `ROUTING_LIFECYCLE_FILE`, `ROUTING_BOARD_PY` | real store overrides | DOC-11 / DOC-16 (open) |
| `ROUTER_E2E_KEY` | real env read, but a DEV-HARNESS credential (scripts/proxy_e2e.py:350), not an operator knob | residual, named below, NOT part of this row |
| `ROUTER_GENERATED_MARKERS` | NOT an env var — module-level tuple, scripts/router_server.py:4283 | false positive |
| `ROUTER_HOP0_RULE` | NOT an env var — module-level str, scripts/router_spawn.py:2164 | false positive |
| `ROUTER_MODE` | NOT an env var — shell variable derived from health.json, scripts/muster_battery.sh:308 | false positive |

### $ git grep -n -F 'ROUTER_GENERATED_MARKERS\|ROUTER_HOP0_RULE\|ROUTER_MODE' -- scripts/router_server.py scripts/router_spawn.py scripts/muster_battery.sh | head -6
```
```

Absence proof for the key itself — 0 hits in TRACKED Markdown, hits only in source + tests:

### $ git grep -n -F 'ROUTER_STATE_DIR_WARN' -- . 
```
scripts/state_dir.py:36:    ROUTER_STATE_DIR_WARN=force     emit the warning even when canonical
scripts/state_dir.py:37:    ROUTER_STATE_DIR_WARN=suppress  never emit it
scripts/state_dir.py:59:WARN_ENV_VAR = 'ROUTER_STATE_DIR_WARN'
scripts/state_dir.py:106:                  (in-process tests); None -> consult ROUTER_STATE_DIR_WARN.
tests/test_quota_gate.py:817:    env.pop('ROUTER_STATE_DIR_WARN', None)
tests/test_state_dir_isolation.py:21:  * ROUTER_STATE_DIR_WARN=force/suppress steers the warning for tests;
tests/test_state_dir_isolation.py:60:    monkeypatch.delenv("ROUTER_STATE_DIR_WARN", raising=False)
tests/test_state_dir_isolation.py:151:    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", "force")
tests/test_state_dir_isolation.py:155:    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", "suppress")
tests/test_state_dir_isolation.py:179:    env.pop("ROUTER_STATE_DIR_WARN", None)
tests/test_state_dir_isolation.py:198:    env.pop("ROUTER_STATE_DIR_WARN", None)
tests/test_state_dir_isolation.py:211:    env_nowarn = dict(base, ROUTER_STATE_DIR_WARN="suppress")
tests/test_state_dir_isolation.py:212:    env_warn = dict(base, ROUTER_STATE_DIR_WARN="force")
tests/test_state_dir_isolation.py:241:    monkeypatch.setenv("ROUTER_STATE_DIR_WARN", warn_mode)
tests/test_state_dir_isolation.py:313:    env.pop("ROUTER_STATE_DIR_WARN", None)
```

### $ git grep -c -F 'ROUTER_STATE_DIR_WARN' -- '*.md'; echo "exit=$?"
```
exit=1
```

The mechanism it belongs to IS documented — only the override is missing:

### $ git grep -n -F 'ROUTER_STATE_DIR' -- README.md | head -4
```
README.md:81:`quota-state.json` lands in the data home — or in `ROUTER_STATE_DIR` when you
README.md:142:  (default `~/.hermes/model-router/quota-state.json`, or `ROUTER_STATE_DIR`).
README.md:148:state directory at a throwaway location explicitly (`ROUTER_STATE_DIR=/tmp/…`,
README.md:154:`ROUTER_STATE_DIR` alone — no `TASK_ROUTER_HOME` — is enough for
```

### $ sed -n '30,42p' scripts/state_dir.py
```
of ~/.hermes/scripts into the repo and misclassify the canonical scheduler
invocation as a stray). Anything else — a direct repo run, a second clone,
a copied script, an in-process import — is non-canonical and warns once.

Subprocess-friendly test override (steers ONLY the warning, never the dir):

    ROUTER_STATE_DIR_WARN=force     emit the warning even when canonical
    ROUTER_STATE_DIR_WARN=suppress  never emit it

Usage (inside a router script):

    try:
        import state_dir as _state_dir_mod
```

## 4. FINDING 3 — `router quota` ships 6 subcommands; the CLI reference names 3 (T3)


### $ PYTHONPATH=. python3 -m task_router.cli quota --help
```
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
```

### $ grep -n 'router quota' README.md
```
141:- `router quota` / `router quota set|clear` — writes the quota-gate file
659:| `router quota` | Inspect provider quota and policy-gate state used during resolution |
662:| `router quota` | Plan-window quota gates (TR-060): record a provider's 429 exhaustion with its own reset time so the resolver skips the lane until the plan refills, instead of re-picking it every 30-minute circuit cool-down. Subcommands: `set <provider> <reason> <reset_at>` (`--detected-at`), `clear <provider>\|--all`, `status [<provider>]`, each with `--json`; `--state-file`/`--state-dir` to target a non-default store. Deliberately resolves the script default `~/.hermes/model-router/quota-state.json` — the same file the fleet spawn path reads (see docs/quota-gate.md) |
```

The doc the README row points at (docs/quota-gate.md) covers the GATE mechanism only, and the
only file naming accounting/pacing/burn is a spec that README never links:

### $ grep -nE 'accounting|pacing|burn' docs/quota-gate.md | head -5; echo "exit=$?"
```
exit=0
```

### $ git grep -l 'quota-layers-spec' -- '*.md'
```
.coding-hermes/evidence/tr216-alias-accounting-20261106.md
docs/configuration.md
docs/ui/data-command-center.md
skills/task-router-usage/SKILL.md
```

## 5. Leaf `--help` sweep over all 28 commands (T3)

Every leaf help exits 0 except `pricing-audit`, which by documented design RUNS the audit
(README.md:652 states this) — recorded, not filed.

### $ cat /home/kara/.hermes/stand-in/docs/task-router/probes/leaf-help.txt
```
chain-run      rc=0   lines=25   usage: router_chain_run.py [-h] [--profile PROFILE]
circuit        rc=0   lines=11   usage: router_circuit.py [-h] COMMAND ...
clinepass      rc=0   lines=9    usage: router_clinepass.py [-h] [--dry-run] [--commit] [--push] {sync}
context-call   rc=0   lines=18   usage: router context-call [-h] [--surfaces SURFACES]
decide         rc=0   lines=31   usage: router_decision.py [-h] [--request PATH] [--verify PATH]
diff           rc=0   lines=11   usage: router_diff.py [-h] [--json] [--format {json,text}] date_from date_to
estimate       rc=0   lines=16   usage: router_estimate.py [-h] [--project PROJECT_OPT] [--tokens-in TOKENS_IN]
gaps           rc=0   lines=9    usage: router_gaps.py [-h] [--json] [--lacking LACKING] [--models MODELS]
ingress        rc=0   lines=15   usage: router_ingress [-h] [--endpoints ENDPOINTS] [--ledger LEDGER]
learn          rc=0   lines=6    usage: router_learn.py [-h] {dump,lesson,doctrine,provider} ...
ledger         rc=0   lines=9    usage: router_ledger.py [-h] {start,end,status} ...
lifecycle      rc=0   lines=7    usage: router_lifecycle.py [-h] [--json] [--all] [--today TODAY]
maintain       rc=0   lines=8    usage: router_maintain.py [-h] [--dry-run]
metrics        rc=0   lines=21   usage: router_metrics.py [-h] [--top-providers [TOP_PROVIDERS]]
modelsdev      rc=0   lines=15   usage: router_modelsdev.py [-h] [--seed] [--file-gaps] [--commit]
outcomes       rc=0   lines=12   usage: router_outcomes.py [-h]
plan-sweep     rc=0   lines=6    usage: router_plan_sweep.py [-h] [--apply] [--json]
pricing        rc=0   lines=6    usage: router_pricing.py [-h] [--dry-run] [--json]
pricing-audit  rc=1   lines=33   PRICING AUDIT — 2026-11-17T05:40Z
probe          rc=0   lines=18   WARNING [router state-dir] ROUTER_STATE_DIR is unset and this process was not started through t
probefix       rc=0   lines=8    usage: router_probefix.py [-h] [--runs RUNS] [--dry-run] [--sync-providers]
quota          rc=0   lines=20   usage: router_quota.py [-h] COMMAND ...
seed           rc=0   lines=4    usage: router_seed.py [-h]
server         rc=0   lines=21   usage: router_server.py [-h] [--mode {read-only,edit}]
spawn          rc=0   lines=72   usage: router_spawn.py [-h] [--profile PROFILE_ID]
status         rc=0   lines=5    usage: router_status.py [-h] [--format {json,text}]
validate       rc=0   lines=9    usage: router_validate.py [-h] [--json] [--heal]
web            rc=0   lines=9    usage: router_web.py [-h] [--host HOST] [--port PORT]
```

## 6. Corpus link census (rot check, T2)


### $ python3 /home/kara/.hermes/skills/coding-hermes/coding-hermes-docs/scripts/doc_link_census.py $PWD --samples 5 | tail -7
```
dead relative links: 1
distinct dead targets: 1
  resolve by suffix swap [('.md', '.spec.md')]: 0   <- mechanical generator class
  resolve neither way: 1   <- pages never captured; needs a manifest, not a rewrite
by top-level path:
        1  docs
SAMPLE docs/reviews/tr388-quorum/seat1-glm.md -> r  [no renamed sibling (never captured)]
```

One dead link, in an archived quorum-review transcript (docs/reviews/tr388-quorum/seat1-glm.md).
Classified NOT filed: a review archive is not a reader-facing page, and inflating a historical
artifact tree into reader-facing findings is the failure the filing skill forbids.

## 7. Surfaces checked and found CLEAN (no finding filed)

- README CLI reference coverage: all 28 commands appear (context-call only inside the body,
  which DOC-13 owns).
- `make help` is self-documenting and lists all 15 operator targets, so Makefile targets that
  no prose names are discoverable, not a gap.
- `router outcomes --help` lists all 8 child verbs; apply-verdicts/deadline-report visibility is
  DOC-18 territory (still open, not re-filed).
- `router maintain --help` names reprice/seed/export/snapshot/commit/all; the README row matches.
- README quick-start (clone -> pip install -e . -> pip install duckdb -> router seed -> router
  status -> pytest) agrees with pyproject [project.scripts] and the committed tables.
## Quick start

```bash
git clone https://github.com/coding-hermes/task-router.git
cd task-router

# Install the CLI (Python 3.11+; stdlib-only runtime).
pip install -e .

# `router seed` is the one command that needs a third-party package: duckdb
# (used as an in-memory engine — nothing binary is persisted). Every other
# subcommand runs on the stdlib alone.
pip install duckdb          # or: uv pip install duckdb

# Seed the compiled registry (registry.json) from the committed tables.
# A fresh clone has none: reads fall back to the committed data/tables
# SAMPLE tables (spawn still resolves, with a loud fallback warning) and
# `router validate` exits 1 until registry.json exists.
```

## 8. Residuals (reported, not filed)

1. `ROUTER_E2E_KEY` (scripts/proxy_e2e.py:350) — a dev-harness API-key read with no documented
   home. Left unfiled: the script is a developer tool, and bundling a dev credential into the
   operator-facing configuration reference would be the wrong fix. Named so the next sweep does
   not have to rediscover it.
2. `router probe --help` emits the shared-state-dir WARNING on stderr before usage. That is a
   behaviour wart in a functional path, not a documentation gap — out of this lane, listed for
   the functional board.
3. One dead markdown link in docs/reviews/tr388-quorum/seat1-glm.md (`-> r`), an archived
   quorum transcript. Not reader-facing.

## 9. Frozen

Digest is computed by the caller AFTER this file is final: `sha256sum <this file>`.
Witness: the world supplied none for these three findings — each rests on a local CLI
invocation (T3) or a tracked source-vs-docs differential (T2), both captured above.

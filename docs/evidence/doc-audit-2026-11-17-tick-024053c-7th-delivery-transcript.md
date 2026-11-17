# Docs-audit probe transcript — tick task-router-docs-2026-11-17-02-40-53 (7th delivery)

Audited tip: 46e94841fde685fb113d4567037118a955869d5e  (docs(audit): file DOC-35 — README's loud-failure promise for a seed without duckdb is false)
Probe root:  /home/kara/.hermes/cache/scratch/tr-docs-7th (detached clean worktree at the published tip; the canonical
             checkout /home/kara/task-router is 116 behind / 7 ahead / 8 dirty paths
             and was never used as a probe root or a write target)
Captured:    2026-11-17T07:24:35Z

## A. README:37-39 claim — "Every other subcommand runs on the stdlib alone"

Interpreter: /home/kara/.hermes/stand-in/docs/task-router/d6venv-noduck/bin/python3 (duckdb NOT importable). The editable install resolves task_router
from /home/kara/.hermes/stand-in/docs/task-router/wt-024053-d6b, which is at the SAME commit as the audited tip (proof below) and clean:
```
$ git -C <venv source tree> rev-parse HEAD; git -C <venv source tree> status --porcelain
46e94841fde685fb113d4567037118a955869d5e
$ git diff --name-only f5e73d9e 46e94841   (what changed under the audited tree)
.coding-hermes/board/events.jsonl
.coding-hermes/board/tasks.jsonl
docs/evidence/doc-audit-2026-11-17-tick-024053c-6th-delivery-transcript.md
(no source, doc or script file changed — the battery below therefore ran the tip code)
```
```
python: 3.12.13
sys.path[0:4]: ['/home/kara/.hermes/stand-in/docs/task-router', '/home/kara/.hermes/cache/scratch/tr-docs-7th', '/home/kara/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/lib/python312.zip', '/home/kara/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/lib/python3.12']
duckdb spec: None
task_router file: /home/kara/.hermes/cache/scratch/tr-docs-7th/task_router/__init__.py
task_router ver : 0.2.0
```
```
command                                    rc    out    err  marks / tail
--version                                   2      0     58  - | router: unknown command '--version' (see 'router --help')
chain-run --help                            0    913      0  - |   --format {text,json}
circuit status --json                       0     90      0  - | }
clinepass --help                            0    268      0  - |   --push
context-call --help                         0    924      0  - |                         included).
decide --help                               0   1873      0  - |   --format {json}
diff --help                                 0    512      0  - |   --format {json,text}
estimate P1_CODING                          0  12997      0  - | }
gaps --help                                 0    298      0  - |   --top TOP
ingress endpoints                           0    375      0  - | ok        webhook-sink-local webhook              reply=webhook-separate   timeout=10s auth=none
learn --help                                0    257      0  - |   -h, --help            show this help message and exit
ledger status                               0    272      0  - |   (no traces recorded)
lifecycle --help                            0    301      0  - |   --today TODAY  frozen clock (YYYY-MM-DD) for checks
maintain --help                             0    441      0  - |   --dry-run             print what would change; write nothing
metrics --top-providers                     0    368      0  - | xkiro                    7
modelsdev --help                            0    738      0  - |   --commit              git commit + push the repo
outcomes --help                             0    766      0  - |   -h, --help            show this help message and exit
plan-sweep --help                           0    225      0  - |   --json
pricing --help                              0    196      0  - |   --json
pricing-audit --help                        1   1641      0  - | remedy: per-provider offset re-derivation from metered usage (the kimi method) for stale offsets, a source + date for undated ones, and a window-cost (or the pe
probe --help                                0    841      0  - |                         (no health-state.json / health.jsonl writes)
probefix --help                             0    411      0  - |   --sync-providers  diff config.yaml custom_providers -> probe_providers.jsonl
quota status --json                         0    190      0  - | }
seed                                        1      0      0  - | 
server --help                               0   1120      0  - |   --port PORT
spawn P1_CODING                             0  21181      0  - | }
status --json                               2      0    108  - | router_status.py: error: unrecognized arguments: --json
validate --json                             1   1139      0  - | {"valid": false, "checks": [{"name": "registry.exists", "ok": false, "detail": "missing: /home/kara/.hermes/stand-in/docs/task-router/probe7-run/home/tr/registr
web --help                                  0    333      0  - |   --data-dir DATA_DIR   override ROUTING_DATA_DIR for tables

MODULE-FAILURES (No module named):
count: 0
json: /home/kara/.hermes/stand-in/docs/task-router/probe7-run/noduck-battery.json
```

## B. README:300-315 claim — the stale watchdog's documented contract

```
$ make watchdog
STALE       http://127.0.0.1:9092  loaded 88082b62d27a/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-16T20:47:55+00:00)
UNREACHABLE http://127.0.0.1:9093  HTTP 404 from http://127.0.0.1:9093/health (no /health surface on this listener)
STALE       http://127.0.0.1:9391  loaded 1895d33f0cd2/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-17T02:54:07+00:00)
make: *** [Makefile:93: watchdog] Error 1
rc=2

$ make watchdog ARGS="--report-only --json"
{
  "results": [
    {
      "url": "http://127.0.0.1:9092",
      "verdict": "STALE",
      "detail": "loaded 88082b62d27a/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-16T20:47:55+00:00)",
      "code": {
        "loaded_commit": "88082b62d27a",
        "loaded_source_sha": "49a0d70ea81a5e44",
        "loaded_at": "2026-11-16T20:47:55+00:00",
        "repo_commit": "3d4fae675ae1",
        "live_source_sha": "49a0d70ea81a5e44",
        "stale": true,
        "error": null
      }
    },
    {
      "url": "http://127.0.0.1:9093",
      "verdict": "UNREACHABLE",
      "detail": "HTTP 404 from http://127.0.0.1:9093/health (no /health surface on this listener)",
      "code": null
    },
    {
      "url": "http://127.0.0.1:9391",
      "verdict": "STALE",
      "detail": "loaded 1895d33f0cd2/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-17T02:54:07+00:00)",
      "code": {
        "loaded_commit": "1895d33f0cd2",
        "loaded_source_sha": "49a0d70ea81a5e44",
        "loaded_at": "2026-11-17T02:54:07+00:00",
        "repo_commit": "3d4fae675ae1",
        "live_source_sha": "49a0d70ea81a5e44",
        "stale": true,
        "error": null
      }
    }
  ],
  "report_only": true
}
rc=0

$ python3 scripts/router_stale_watchdog.py
STALE       http://127.0.0.1:9092  loaded 88082b62d27a/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-16T20:47:55+00:00)
UNREACHABLE http://127.0.0.1:9093  HTTP 404 from http://127.0.0.1:9093/health (no /health surface on this listener)
STALE       http://127.0.0.1:9391  loaded 1895d33f0cd2/49a0d70ea81a5e44 vs repo 3d4fae675ae1/49a0d70ea81a5e44; restart needed (loaded_at 2026-11-17T02:54:07+00:00)
rc=1

$ python3 scripts/router_stale_watchdog.py --url http://127.0.0.1:9/
UNREACHABLE http://127.0.0.1:9/  URLError: <urlopen error [Errno 111] Connection refused>
rc=2

$ python3 scripts/router_stale_watchdog.py --url http://127.0.0.1:9/ --report-only
UNREACHABLE http://127.0.0.1:9/  URLError: <urlopen error [Errno 111] Connection refused>
rc=0

$ ROUTER_STALE_URLS=http://127.0.0.1:9/ python3 scripts/router_stale_watchdog.py
UNREACHABLE http://127.0.0.1:9/  URLError: <urlopen error [Errno 111] Connection refused>
rc=2
```

Note: the rc values printed for the two 'make watchdog' forms are MAKE's exit codes
(make reports 2 for any failed recipe); the script's own documented 0/1/2 contract is
measured directly in the four invocations below it.

## C. Inbound-link census over the reader-facing Markdown corpus (evidence tree excluded)

Command: python3 /home/kara/.hermes/stand-in/docs/task-router/probe7_inbound_md.py
```
corpus .md files: 66
docs/ci-gate.md                                      inbound= 0  ORPHAN
docs/duckbrain-namespace.md                          inbound= 0  ORPHAN
docs/evidence-run-store-decision.md                  inbound= 0  ORPHAN
docs/failure-contract.md                             inbound= 0  ORPHAN
docs/model-selection-cross-pollination.md            inbound= 0  ORPHAN
docs/muster-spec.md                                  inbound= 0  ORPHAN
docs/muster-scope.md                                 inbound= 1  docs/muster-spec.md
docs/null-debt-invariants.md                         inbound= 0  ORPHAN
docs/quality-ladder.md                               inbound= 0  ORPHAN
docs/specs/SPEC-COMPLEXITY-COVERAGE.md               inbound= 0  ORPHAN
docs/specs/SPEC-MODEL-LIFECYCLE.md                   inbound= 0  ORPHAN
docs/specs/SPEC-PROXY-DRIVERS.md                     inbound= 0  ORPHAN
docs/specs/SPEC-REDIS-READ-PLANE.md                  inbound= 0  ORPHAN
docs/specs/SPEC-STATS-ENGINE-V2.md                   inbound= 0  ORPHAN
```

Corpus = tracked *.md at the tip minus docs/evidence, docs/dogfood, docs/reviews,
docs/registry-runs, docs/releases, docs/research, docs/usage, docs/acceptance,
docs/envelopes, .coding-hermes and dated work logs (66 files). A target is ORPHAN when
no OTHER corpus file names it. muster-scope.md's only inbound link comes from
muster-spec.md, which is itself an orphan, so muster-scope is reachable only in two hops
from nothing.

## D. Re-verification of open rows (no re-filing)

```
$ router --help   (blank descriptions, DOC-28)
  BLANK chain-run
  BLANK context-call
  BLANK ingress
  BLANK lifecycle
  BLANK outcomes
  BLANK pricing-audit
blank count: 6  total commands: 28

$ router seed --help   (DOC-25: TR-383 flag invisible)
usage: router_seed.py [-h]

Seed the task-router routing registry

options:
  -h, --help  show this help message and exit
rc=0

$ grep -n ROUTER_DEGRADED_WALL_S|ROUTER_QUOTA_ORDER_GUARD docs/configuration.md   (DOC-34)
142:| `ROUTER_DEGRADED_WALL_S` | `router_server.py` | Maximum wall-clock budget for a fallback hop marked `requirements_unmet`; only shortens the normal hop budget | `120` seconds |
144:| `ROUTER_DEGRADED_WALL_S` | `router_server.py` | TR-355: wall-clock budget for a DEGRADED fallback hop (a hop whose requirements are unmet). Only ever shortens the hop budget (min with the normal budget), never lengthens it. Prevents a guaranteed-failure hop from burning the full 600s hop wall | `120` |
149:| `ROUTER_QUOTA_ORDER_GUARD` | `router_spawn.py` | TR-213 quota-ordering demote guard: a lane whose window headroom is KNOWN and below this many USD/s sorts behind otherwise-comparable lanes; `0` disables demotion. NULL headroom never demotes. See `docs/quota-layers-spec.md` §5.2 | `0.01` |
299:| `ROUTER_QUOTA_ORDER_GUARD` | `router_spawn.py` | TR-213: headroom guard for the chain ordering — a lane whose window headroom is KNOWN and below this many USD/s demotes behind otherwise-comparable lanes (same PAYG bucket, plan tier, price); unknown headroom never demotes. `0` disables the demotion (pre-TR-213 ordering) | `0.01` |

$ grep -nE "(19|30|31) operations?" docs/muster-spec.md   (DOC-33)
15:> measured 19 operations: it now publishes **30 operations — 22 reads and
95:# 2. What tools do I get? (expect 30 operations, grouped GET=22 / POST=8)
122:## 4. Operation census: 31 operations — 23 reads, 8 mutations
273:    live spec: 30 operations
274:PASS  probe 1 — muster ingests the live spec: 30 operations listed, all operationIds present
293:| 19 operations (12 reads + 7 mutations) | **30 operations (22 reads + 8 mutations)** — `/api/ui/*` lanes + `postUiRegistryEdit` (TR-150..TR-155) landed after the scope was measured; the rule (reads keyless, mutations gated) is unchanged |

$ git log --oneline 46e94841 -- README.md docs/muster-spec.md docs/configuration.md   (DOC-32/33/34 premise still stands)
7b985d91 feat(routing): the eligibility gate — a lane that cannot act leaves the chain named
95896e60 fix(seed): declared-unmeasured categories — the math gap becomes a decision, not a deadlock
6a7e57f8 feat(TR-401): effort-aware resolver — pair provider + model + reasoning effort
```

## E. Reachability checker for the widened DOC-24 criteria (control + red-proof)

Control (a page that IS linked today must produce a hit):
$ git grep -cF "docs/health-plane.md" HEAD -- README.md docs/README.md
HEAD:README.md:5

$ the thirteen measured targets (hits=0 = still unreachable)
docs/failure-contract.md hits=0
docs/ci-gate.md hits=0
docs/duckbrain-namespace.md hits=0
docs/evidence-run-store-decision.md hits=0
docs/model-selection-cross-pollination.md hits=0
docs/muster-spec.md hits=0
docs/null-debt-invariants.md hits=0
docs/quality-ladder.md hits=0
docs/specs/SPEC-COMPLEXITY-COVERAGE.md hits=0
docs/specs/SPEC-MODEL-LIFECYCLE.md hits=0
docs/specs/SPEC-PROXY-DRIVERS.md hits=0
docs/specs/SPEC-REDIS-READ-PLANE.md hits=0
docs/specs/SPEC-STATS-ENGINE-V2.md hits=0

## F. Board state before the write

```
$ boardctl -C <tip-worktree> validate   (baseline, inherited, not from this row)
errors: 455
warnings: 606
```

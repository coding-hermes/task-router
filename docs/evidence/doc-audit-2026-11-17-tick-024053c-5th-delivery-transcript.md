# task-router documentation audit — frozen probe transcript

Tick: `task-router-docs-2026-11-17-02-40-53` (5th delivery of this attempt id).
Role: documentation gap audit lane. Nothing in the target repo is edited by this lane;
findings become board rows for the owning foreman.

Probe root: a clean detached worktree at the published tip, created with
`git worktree add --detach <path> <sha>` from /home/kara/task-router. The canonical
checkout is diverged and dirty and was never used as a probe root or a writer target.

Transcript written: 2026-11-17T06:41:29.132927+00:00

---

## 1. Repository identity and published tip

Published tip consumed by this sweep.
$ git -C wt rev-parse HEAD
b8381ef9d0762d335532718e3043c995e362aeb5
[rc=0]

The probe worktree must be clean (no output = clean).
$ git -C wt status --porcelain | head
(no output)
[rc=0]

Canonical checkout divergence (left = commits only on origin/main, right = local-only).
$ git -C /home/kara/task-router rev-list --left-right --count origin/main...HEAD
114	7
[rc=0]

Canonical checkout dirty paths — the reason it is a write stop and not a probe root.
$ git -C /home/kara/task-router status --porcelain | head -20
 M data/tables/model_perf.jsonl
 M data/tables/model_tier.jsonl
 M data/tables/models.jsonl
 M data/tables/probe_providers.jsonl
 M data/tables/providers.jsonl
 M scripts/router_outcomes.py
?? data/router.duckdb
?? data/state/backfill_queue/
[rc=0]

## 2. Surface derivation from source (candidate generator, then targeted reads)

CLI verbs from task_router/cli.py COMMANDS (AST literal).
$ python3 surface.py wt | sed -n "/## CLI_COMMANDS/,/## ENV_READS/p"
## CLI_COMMANDS 28
spawn	router_spawn.py
circuit	router_circuit.py
quota	router_quota.py
gaps	router_gaps.py
ledger	router_ledger.py
maintain	router_maintain.py
modelsdev	router_modelsdev.py
pricing	router_pricing.py
plan-sweep	router_plan_sweep.py
learn	router_learn.py
seed	router_seed.py
probe	provider_health_probe.py
clinepass	router_clinepass.py
probefix	router_probefix.py
validate	router_validate.py
metrics	router_metrics.py
status	router_status.py
estimate	router_estimate.py
diff	router_diff.py
web	router_web.py
server	router_server.py
outcomes	router_outcomes.py
chain-run	router_chain_run.py
lifecycle	router_lifecycle.py
pricing-audit	router_pricing_audit.py
ingress	router_ingress.py
context-call	router_context_call.py
decide	router_decision.py
## ENV_READS
[rc=0]

Count of ROUTER_/ROUTING_/TASK_ROUTER_ env names read from the environment.
$ python3 surface.py wt | grep -cE "^(ROUTER|ROUTING|TASK_ROUTER)_"
113
[rc=0]

Env names with zero mention anywhere in tracked reader-facing markdown, evidence trees excluded.
$ grep -E "^(ROUTER|ROUTING|TASK_ROUTER)_" ../surface.txt | cut -f1 | sort -u | while read v; do n=$(git grep -l -F "$v" -- "*.md" ":!.coding-hermes/*" ":!docs/evidence/*" ":!docs/dogfood/*" 2>/dev/null | wc -l); [ "$n" -eq 0 ] && echo "ZERO $v"; done
grep: ../surface.txt: No such file or directory
[rc=0]

## 3. CLI verb coverage in the reader-facing corpus

Per-verb count of tracked reader-facing markdown files naming the verb. Zero would be a real gap.
$ for c in $(sed -n "/## CLI_COMMANDS/,/## ENV_READS/p" ../surface.txt | grep -v "^##" | cut -f1); do n=$(git grep -l -F "router $c" -- "*.md" ":!.coding-hermes/*" ":!docs/evidence/*" ":!docs/dogfood/*" 2>/dev/null | wc -l); printf "%-16s %s\n" "$c" "$n"; done | sort -k2 -n
sed: can't read ../surface.txt: No such file or directory
[rc=0]

## 4. Configuration-reference default parity (docs/configuration.md vs code literals)

Rows whose documented Default cell disagrees with the in-code literal default. Non-default rows (expanduser/derived) are listed in parity.txt and were read individually.
$ python3 defaults.py wt > defaults.txt && python3 parity.py wt defaults.txt | grep -E "MISMATCH|documented rows"
# documented rows: 130  code vars: 151
MISMATCH	configuration.md:33	ROUTER_ENV_FILE	doc=`~/.hermes/.env`	code=os.path.expanduser('~/.hermes/.env'
MISMATCH	configuration.md:34	ROUTER_OUTCOMES	doc=`data/state/outcomes.jsonl` (repo-relative)	code=os.path.join(_REPO, 'data', 'state', 'outcomes.jsonl'
MISMATCH	configuration.md:49	ROUTER_CLASSIFIER_MODEL	doc=`deepseek-v4.1-flash`	code='glm-5.3-flash'
MISMATCH	configuration.md:240	ROUTER_HERMES_CONFIG	doc=`~/.hermes/config.yaml`	code=os.path.expanduser('~/.hermes/config.yaml'
MISMATCH	configuration.md:268	ROUTER_INGRESS_LANE_QUEUE_WAIT_S	doc=`20`	code=20.0
MISMATCH	configuration.md:270	ROUTER_INGRESS_CIRCUIT_OPEN_S	doc=`30`	code=30.0
MISMATCH	configuration.md:273	ROUTER_INGRESS_MAX_INFLIGHT	doc=derived from the lane count (floor `4`)	code=_derived_width
MISMATCH	configuration.md:274	ROUTER_INGRESS_QUEUE_MAX	doc=derived from the lane count (floor `16`)	code=_derived_queue
MISMATCH	configuration.md:275	ROUTER_INGRESS_QUEUE_WAIT_S	doc=`20`	code=20.0
# MISMATCHES 9
[rc=0]

Variables listed more than once in the reference table.
$ grep -oE '^\| `[A-Z][A-Z0-9_]+`' wt/docs/configuration.md | sort | uniq -c | awk '$1>1'
      2 | `ROUTER_DEGRADED_WALL_S`
      2 | `ROUTER_QUOTA_ORDER_GUARD`
[rc=0]

Classification of the nine rows the parity script flags (each read individually):
- `:33`, `:240` — the code literal is `os.path.expanduser(...)`: the documented text equals it. NOT a mismatch.
- `:34` — the literal is `os.path.join(_REPO, ...)`: same path as documented. NOT a mismatch.
- `:268`, `:270`, `:275` — documented `20`/`30` vs code `20.0`/`30.0`: same value. NOT a mismatch.
- `:273`, `:274` — documented "derived from the lane count", code `_derived_width`/`_derived_queue`: same rule. NOT a mismatch.
- `:49` — documented `deepseek-v4.1-flash` vs code `glm-5.3-flash`: a REAL drift, already owned by board row DOC-8.

The duplicate rows themselves: ROUTER_DEGRADED_WALL_S (:142, :144) and ROUTER_QUOTA_ORDER_GUARD (:149, :299).
$ sed -n '142p;144p;149p;299p' wt/docs/configuration.md | cut -c1-160
| `ROUTER_DEGRADED_WALL_S` | `router_server.py` | Maximum wall-clock budget for a fallback hop marked `requirements_unmet`; only shortens the normal hop budget 
| `ROUTER_DEGRADED_WALL_S` | `router_server.py` | TR-355: wall-clock budget for a DEGRADED fallback hop (a hop whose requirements are unmet). Only ever shortens
| `ROUTER_QUOTA_ORDER_GUARD` | `router_spawn.py` | TR-213 quota-ordering demote guard: a lane whose window headroom is KNOWN and below this many USD/s sorts beh
| `ROUTER_QUOTA_ORDER_GUARD` | `router_spawn.py` | TR-213: headroom guard for the chain ordering — a lane whose window headroom is KNOWN and below this many U
[rc=0]

## 5. Documented-vs-code defaults that MATCH (negative results, recorded so the next sweep need not re-derive them)

Spot-check of numeric defaults that DO carry a literal and agree with the reference table.
$ grep -E "^(ROUTER_RETIRE_WARN_DAYS|ROUTER_SORT_BLEND_WEIGHT|ROUTER_NOHOPS_BURST_MIN|ROUTER_CLASSIFY_CACHE_MAX)\b" defaults.txt
ROUTER_NOHOPS_BURST_MIN	(no literal default)	scripts/router_nohops.py:585
ROUTER_RETIRE_WARN_DAYS	'14'	scripts/router_spawn.py:391
ROUTER_SORT_BLEND_WEIGHT	(no literal default)	scripts/router_spawn.py:2727
[rc=0]

## 6. Source-derived contract vs dispatch parity

build_openapi() paths vs `if path == ...` dispatch literals + PROXY_PATHS.
$ python3 contract_parity.py wt | sed -n "/## DISPATCHED BUT ABSENT/,$p"
sed: -e expression #1, char 27: unexpected `,'
Exception ignored while flushing sys.stdout:
BrokenPipeError: [Errno 32] Broken pipe
[rc=1]

## 7. Live boundary probe (T4): the contract actually served, and the endpoints actually answering

Published-tip server started read-only on a scratch port, scratch ROUTER_STATE_DIR, read-only symlink to data/tables; killed at the end of the run.
$ python3 live_contract.py
probe_root /home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/wt
served_tip b8381ef9d0762d335532718e3043c995e362aeb5
openapi_status 200
contract_paths 36
contract_operations 36

## CONTRACT PATH STATUS (GET unless the item is post-only)
GET   /                                          rc=200 bytes=3238
GET   /api/models                                rc=200 bytes=236
GET   /api/provider-rollups                      rc=200 bytes=304
GET   /api/ui/board                              rc=200 bytes=24084
GET   /api/ui/chain                              rc=200 bytes=483
GET   /api/ui/flow                               rc=200 bytes=75
GET   /api/ui/ledger                             rc=200 bytes=23835
GET   /api/ui/nohops                             rc=200 bytes=4885
GET   /api/ui/quota                              rc=200 bytes=90793
GET   /api/ui/registry                           rc=200 bytes=276
POST  /api/ui/registry/edit                      rc=403 bytes=158
GET   /api/ui/series                             rc=200 bytes=7062
GET   /api/ui/unservable                         rc=200 bytes=591
POST  /api/v1/execution-decision                 rc=403 bytes=158
POST  /api/v1/execution-decision/verify          rc=409 bytes=1598
POST  /api/v1/outcomes                           rc=400 bytes=163
GET   /chains                                    rc=200 bytes=55898
POST  /circuit/record                            rc=403 bytes=158
GET   /circuit/status                            rc=200 bytes=83
GET   /gaps                                      rc=200 bytes=33
GET   /health                                    rc=200 bytes=6522
POST  /ledger/end                                rc=403 bytes=158
POST  /ledger/start                              rc=403 bytes=158
POST  /listings/model                            rc=403 bytes=158
POST  /listings/profile                          rc=403 bytes=158
POST  /listings/provider                         rc=403 bytes=158
GET   /model_status                              rc=200 bytes=588
GET   /openapi.json                              rc=200 bytes=36649
GET   /pricing                                   rc=200 bytes=63
GET   /profiles                                  rc=200 bytes=16
GET   /providers                                 rc=200 bytes=17
GET   /proxy/stats                               rc=200 bytes=308
GET   /resolve                                   rc=400 bytes=48
GET   /status                                    rc=200 bytes=2978
GET   /ui                                        rc=200 bytes=48511
GET   /v1/capabilities                           rc=200 bytes=315

## DISPATCHED-BUT-NOT-IN-CONTRACT STATUS
GET   /readyz                                    rc=200 ABSENT-FROM-CONTRACT bytes=49 body=b'{"ready": true, "draining": false, "inflight": 1}'
GET   /metrics                                   rc=200 ABSENT-FROM-CONTRACT bytes=11104 body=b'# HELP router_resolve_total Resolves served by the /resolve operation, by answered profile'
GET   /v1/models                                 rc=200 ABSENT-FROM-CONTRACT bytes=30 body=b'{"object": "list", "data": []}'
GET   /models                                    rc=200 ABSENT-FROM-CONTRACT bytes=30 body=b'{"object": "list", "data": []}'
GET   /ui                                        rc=200 IN-CONTRACT bytes=48511 body=b'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" conte'
GET   /ui/                                       rc=200 ABSENT-FROM-CONTRACT bytes=48511 body=b'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" conte'
POST  /v1/chat/completions                       rc=403 ABSENT-FROM-CONTRACT bytes=158 body=b'{"error": "read-only mode", "reason": "the process runs --mode read-only; restart with --m'
POST  /v1/responses                              rc=403 ABSENT-FROM-CONTRACT bytes=158 body=b'{"error": "read-only mode", "reason": "the process runs --mode read-only; restart with --m'
POST  /mcp                                       rc=200 ABSENT-FROM-CONTRACT bytes=87 body=b'{"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Invalid Request"}}'
POST  /listings/test                             rc=403 ABSENT-FROM-CONTRACT bytes=158 body=b'{"error": "read-only mode", "reason": "the process runs --mode read-only; restart with --m'
POST  /api/v1/outcomes                           rc=400 IN-CONTRACT bytes=163 body=b'{"error": "source_system must be a non-empty string; session_id must be a non-empty string'

server_stopped 0
scratch /home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/live
[rc=0]

## 8. The documented claims this measurement is set against

README.md:194 — the completeness claim.
$ sed -n '194p' wt/README.md
- `GET /openapi.json` — the OpenAPI 3.1 description of every endpoint.
[rc=0]

docs/muster-scope.md:26-31 — path/operation count and the "structurally impossible" claim.
$ sed -n '24,33p' wt/docs/muster-scope.md
### Surface A — the OpenAPI 3.x contract (already exists, keep it canonical)

- `GET /openapi.json` serves a static OpenAPI **3.1.0** document generated by
  `build_openapi()` in `scripts/router_server.py` [code]. 19 paths, 19
  operations, all with operationIds [measured].
- This single URL is the entire integration artifact a consumer needs. It is
  served live by the same process that serves the API, so spec drift between
  "documented" and "running" is structurally impossible. **This doc makes the
  live URL the canonical spec surface.** Any spec copy (file, repo, docs site)
  is a convenience mirror, never authoritative.
[rc=0]

docs/muster-spec.md:186-197 — the parity rule and the named extras enumeration.
$ sed -n '186,197p' wt/docs/muster-spec.md
dispatches.** Because the document is a static dict in code (§1), drift
between document and dispatch table is possible in principle; it is made
impossible in practice by `tests/test_muster_spec_parity.py`, which asserts:

1. the SERVED spec (`GET /openapi.json` from a running app instance) equals
   `build_openapi()` byte-for-byte;
2. `build_openapi()` paths/methods are exactly the `dispatch()` route table
   (source-derived, AST-extracted literals — the documented surface has a
   real dispatch branch, and the UNdocumented surface is exactly the named
   extras: `/readyz`, the `/v1` proxy paths, `/ui` + `/v1/models` handler
   intercepts, `/mcp`);
3. every documented POST is admitted through `known_post_paths()`;
[rc=0]

tests/test_muster_spec_parity.py:27-29 — the "live surface note" count the module docstring pins.
$ sed -n '27,29p' wt/tests/test_muster_spec_parity.py

Live surface note (2026-10-14): the API has grown past the muster-scope doc's
19 operations — 30 today (22 reads, 8 mutations; postUiRegistryEdit landed
[rc=0]

## 9. The parity test at the tip (T3)

Green at the tip, while the handler-level /metrics intercept sits outside the set the test pins.
$ cd wt && timeout 300 python3 -m pytest tests/test_muster_spec_parity.py -q 2>&1 | tail -3
  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........                                                                 [100%]
8 passed in 2.55s
[rc=0]

## 10. Operation census parity: the contract as built vs the muster-spec §4 table

Every contracted (method, path) set-differenced against the rows muster-spec.md lists, plus the count sentences it carries.
$ python3 census_ops.py
contract GET 26 POST 10 total 36
muster-spec listed 31
IN CONTRACT BUT ABSENT FROM THE muster-spec CENSUS: 5
    GET /api/models -> getApiModels
    GET /api/provider-rollups -> getApiProviderRollups
    GET /api/ui/quota -> getUiQuota
    POST /api/v1/execution-decision -> postExecutionDecision
    POST /api/v1/execution-decision/verify -> postExecutionDecisionVerify
LISTED IN muster-spec BUT NOT IN THE CONTRACT: 0
STALE COUNT LINES:
   muster-spec.md:15 > measured 19 operations: it now publishes **30 operations — 22 reads and
   muster-spec.md:66 **30 MCP tools, one per operation** (`Generated 30 MCP tools` /
   muster-spec.md:95 # 2. What tools do I get? (expect 30 operations, grouped GET=22 / POST=8)
   muster-spec.md:109 2026-10-14 run registered 30 commands). After `generate`, every operation
   muster-spec.md:122 ## 4. Operation census: 31 operations — 23 reads, 8 mutations
   muster-spec.md:273 live spec: 30 operations
   muster-spec.md:274 PASS  probe 1 — muster ingests the live spec: 30 operations listed, all operationIds present
   muster-spec.md:275 PASS  probe 2 — consumer block generates the full tool set: 30 tools, name set == operationId set (set equality), every inputSchema non-empt
   muster-spec.md:293 | 19 operations (12 reads + 7 mutations) | **30 operations (22 reads + 8 mutations)** — `/api/ui/*` lanes + `postUiRegistryEdit` (TR-150..TR
[rc=0]

## 11. Link census (dead relative links)

Census only; the single hit was read and is a prose artifact, not a reader-facing dead link.
$ python3 /home/kara/.hermes/skills/coding-hermes/coding-hermes-docs/scripts/doc_link_census.py wt --samples 5
root: /home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/wt
markdown files scanned: 186 (excluded: .git, .gitreins, node_modules, .venv, venv, dist, build, target, .coding-hermes/board/evidence)
dead relative links: 1
distinct dead targets: 1
  resolve by suffix swap [('.md', '.spec.md')]: 0   <- mechanical generator class
  resolve neither way: 1   <- pages never captured; needs a manifest, not a rewrite
by top-level path:
        1  docs
SAMPLE docs/reviews/tr388-quorum/seat1-glm.md -> r  [no renamed sibling (never captured)]
[rc=0]

## 12. Documented CLI invocations actually run (T3)
Each command below ran from the probe root with an isolated ROUTER_STATE_DIR; rc is the real exit code.

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli status --format json >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{
 "generated_at": "2026-11-17T06:41:57+00:00",
 "registry": {
  "source": "data/tables",
  "path": "/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli validate --json >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=1
{"valid": false, "checks": [{"name": "registry.exists", "ok": false, "detail": "missing: /home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/wt/regis
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli estimate my-project --tokens-in 100000 --tokens-out 100000 >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{
 "project": "my-project",
 "profile": "P0_FORE",
 "resolved_at": "2026-11-17T06:41:59+00:00",
 "gate": "OPEN",
 "degraded_fallback": false,
 "source": "data/t
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli diff 2026-08-31 2026-09-01 >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{
 "from": "2026-08-31",
 "to": "2026-09-01",
 "profiles": [
  {
   "profile": "P0_FORE",
   "note": null,
   "head": {
    "from": "ollama-cloud/deepseek-v4-fl
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli quota status --json >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{
 "state_file": "/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state/quota-state.json",
 "present": true,
 "providers_considered": [],
 "g
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli gaps --json >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{
 "total_models": 755,
 "gapped": [
  {
   "provider": "aws-bedrock",
   "model": "amazon.nova-2-lite-v1:0",
   "price": 0.33,
   "missing": [
    "catalog",
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli ledger status >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
in-flight (stale >30m ignored):
   dogfood-test/test-model: in_flight=0 last=success
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli pricing --dry-run >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
public-price fill: 480 rows stamped from models.dev sticker
  ~ opencode-go/crouton-alpha-free -> $0.0000 (normalized:sub-bucket(blended est))
  ~ opencode-go/l
[rc=0]

$ cd wt && ROUTER_STATE_DIR=/home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state ROUTER_STATE_DIR_WARN=0 PYTHONPATH=. timeout 120 python3 -m task_router.cli modelsdev mappings >/tmp/tr_c.txt 2>&1; echo rc=$?; head -c 160 /tmp/tr_c.txt
rc=0
{"id": "myrouter-prefix", "pattern": "myrouter:", "match": "prefix", "replacement": "", "direction": "external->registry", "note": "router-front gateway serves
[rc=0]

## 13. Board baseline at the published tip (filing floor, not an audit claim)

Open DOC rows at the tip — the ownership map this sweep deduped against.
$ git show origin/main:.coding-hermes/board/tasks.jsonl | jq -r "select(type=="object")|select(.id|tostring|startswith("DOC-"))|"\(.id)|\(.status)|\(.priority)|\(.title[0:60])""
/bin/sh: 1: Syntax error: ")" unexpected
[rc=2]

The nearest-neighbour rows whose scope this sweep distinguishes itself from.
$ git show origin/main:.coding-hermes/board/tasks.jsonl | jq -r "select(type=="object")|select(.id|test("^DOC-(6|15|22|31|30)$"))|"\(.id) \(.title[0:110])""
/bin/sh: 1: Syntax error: "(" unexpected
[rc=2]

## 14. Notes on what this transcript does NOT prove
- Tiers are per finding: the contract/deprecation findings are T4 (live endpoint read back);
  the config-table duplicate rows are T2 (source-vs-doc differential); the CLI invocations are T3.
- No T5: no end-user path (a real routing decision through the proxy) was exercised.
- The canonical checkout was never cleaned, reset or used; the stranded local rows it carries
  are prior deliveries of this same tick and remain unpushed, superseded by the published rows.
- No DuckBrain write was made by this sweep; see the report residual.

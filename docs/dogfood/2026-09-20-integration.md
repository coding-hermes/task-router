# Dogfood integration report — 2026-09-20

**Verdict: 🟡 PROMISING-BUT-ROUGH.**
Consumer run + gates + HTTP/MCP + the TR-067 proxy all work end to end on a fresh
clone, but this run found the first **P1 functional defect** (proxy hop-1 loss)
and a **live-fleet side effect** any user of the documented CLI hits.

| | |
|---|---|
| Target | `task-router` (dogfood lane `task-router-dogfood`, tick 2026-09-20-00-47-25) |
| Consumer clone | `https://github.com/coding-hermes/task-router.git` @ `43c1a7d` (branch `main`, 185 files) |
| Host / interpreter | control host, Python 3.11.15, `python3 -m venv` |
| Install | `pip install -e .` **2.5 s**; `pip install duckdb` (documented) 1.7 s |
| Time-to-first-success | **~8 s** clone → install → first resolved 43-hop chain (seed adds 19 s on the control host / 10 s on a fresh bunker) |
| Friction count | 5 (2 doc-vs-CLI contract breaks, 2 stale in-repo claims, 1 missing classifier key) |
| Bunker install leg | **PASSED** (fresh agent `f413fd0b`, bunker-las-02, Debian 13 / Python 3.13.5 — see below) |

---

## 1. What the project promises (null hypothesis)

> A caller installs the CLI, points a task profile at the registry, and gets a
> deterministic, price-ordered, gate-filtered chain — or, via the TR-067 proxy,
> points its `base_url` at the router and nothing else changes.

Both halves were tested for real. Both **work**; the second half quietly drops the
cheapest lane.

## 2. Real-use pass A — CLI consumer (fresh clone, scratch data home)

Evidence: `/tmp/dgtr/pass1.log`, `pass2.log`, `pass3.log`, raw payloads in `/tmp/dgtr/evidence/`.

| Documented command | Result |
|---|---|
| `router --help` | 25 subcommands listed, data-home resolution documented in the footer |
| `router spawn my-project --format json` | rc=0 in **0.175 s** — head `kimi-for-coding/k3` ($0.4713/M, ctx 1048576), **43-hop** chain |
| `router spawn P1_CODING --format json` (bare profile in the project slot) | rc=0, `resolved_as: "profile"`, `hint: "use --profile P1_CODING"` — TR-059 verified fixed |
| `router status` | reads the data-home registry (`/tmp/dgtr/home`), 26 providers, 11 profiles — TR-044/056 verified fixed |
| `router seed` | rc=0, 19.4 s, wrote `registry.json` (2459.5 KB), `ns mirror absent — skipped ns export (fresh clone is fine)` — TR-045/048 guards hold on a fresh clone |
| `router estimate` / `diff` / `metrics` / `gaps` / `spawn --list-profiles` / `spawn --explain` | all rc=0, all on the data home |
| `router web` (:9093) | 200 on `/`, `/api/preview?project=my-project` resolves, `/api/profiles` 200, write attempt → **403 read-only** |
| `router server --mode read-only` (:9192) | 9 read endpoints 200, unknown path → 404 `{"error":"not found"}`, both mutations → **403 read-only**, MCP `initialize` 200 + **16 tools** listed |
| `router circuit` / `router ledger` | breaker open → head moved `kimi-for-coding/k3` → `zai-glm/glm-5.3-flash` with `exclusions[0].why` naming the cooldown; `record-success` restored the head; ledger `start → status(in_flight 1) → end → status(in_flight 0, success)` round-trip clean |

Nothing had to be read from source to use the CLI. The output is self-explaining:
`exclusions[].why`, `gate_reasons`, `warnings`, `gates_loaded` all say what they mean.

## 3. Real-use pass B — the TR-067 proxy, end to end (the "zero-effort path")

The strongest single piece of evidence in this run. Server started exactly as
`docs/integration.md` prescribes (classifier deliberately left unset):

```bash
ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642 ROUTER_PROXY_AUTH=passthrough \
  router server --mode read-only --port 9193
curl -s -X POST localhost:9193/v1/chat/completions \
  -H 'Authorization: Bearer <gateway key>' -H 'x-router-max-hops: 2' \
  -d '{"messages":[{"role":"user","content":"Reply with exactly: ROUTER-DOGFOOD-OK"}],"max_tokens":24}'
```

- **HTTP 200** in 115 s, answer `ROUTER-DOGFOOD-OK` served by **`zai-glm/glm-5.3`**.
- `_router` object complete: `complexity_source: "default"`,
  `degrade_reason: "classifier call failed: ROUTER_CLASSIFIER_BASE_URL not configured"`
  (the doc's promised *visible* degrade to the default profile — **accurate**),
  `chain_length: 40`, `max_hops: 2`.
- **Defect (TR-081, P1):** the ladder contains **exactly one entry — `hop 4`**, and
  the chain's hop-1 cost **$0.082/M is never attempted**. With `x-router-max-hops: 2`
  a caller is promised "bound on the fallback ladder"; they get one attempt, at
  **18.5× the price** of the lane the router itself ranked first. The ladder is
  reported complete, so the loss is invisible from the response.
- `ROUTER_PROXY_AUTH=passthrough` verified honestly: without a key the mirror path
  reaches the gateway and returns the **gateway's** 401
  (`gateway_auth_failed`) — the router key was correctly skipped, the upstream
  credential still gated. Not a bypass.

## 4. Real-use pass C — install leg on a fresh machine (ephemeral bunker)

Agent `f413fd0b` on **bunker-las-02** (Debian 13, kernel 6.12, **Python 3.13.5**, uid 1004,
no clang/cmake/gcc, `libfuse3.so.4` present, **no `getfattr`**), fresh clone, documented
quickstart verbatim. Evidence: `/tmp/dgtr/bunker-install.out`.

| Step | Result |
|---|---|
| `git clone` | rc=0, **4 s**, HEAD `43c1a7d` |
| `python3 -m venv` + `pip install -e .` | rc=0, **5 s** (no `pip` on the box's python — the venv bootstrap covers it) |
| `pip install duckdb` | rc=0, **5 s**, duckdb 1.5.5 |
| `router spawn my-project --format json` | rc=0, head + 43-hop chain (same head as production) |
| `router seed` | rc=0, 10 s, per-clone ns guard fired |
| `router validate` | **[FAIL]** — `freshness: fallback_lanes.jsonl is 0s newer than registry.json` |
| circuit → re-resolve | breaker open → head moved to `zai-glm/glm-5.3-flash` |
| ledger / estimate | rc=0 |

So: a fresh user on a clean Debian box is running in **~15 s**, and the one red
line is the `validate` freshness check (TR-082). Agent destroyed, key removed (`bunker list` clean).

**Harness event, honestly recorded:** the `bunker-qa.sh launch` call was killed by the
*control host's* tool cap at ~280 s before it recorded its launch cell, and `collect`
then reported `COLLECT-FAIL unreachable-agent` for **`agent=e3d3cd28`** — an id from a
**2026-09-18** `bunker` run that the launch step did not update. The mode that launched
at 20:04 had **no `.meta` file** and wrote its failure over the shared default evidence
path `/tmp/bunker-qa-evidence.jsonl`. The agent was found manually
(`bunker list --server bunker-las-02`), the leg was run and destroyed by hand, so no run
is unaccounted for — but the harness itself is a P2 finding (TR-085).

## 5. The live-fleet boundary (read this before running the CLI by hand)

`TASK_ROUTER_HOME=/tmp/dgtr/home` isolates **readers and the circuit/ledger writers**
(verified: `circuit-state.json`, `ledger.jsonl`, `metrics.jsonl`, `registry.json` all
landed under the scratch home). It does **not** isolate two commands, by explicit design:

- `router quota set zai-glm "…" 2026-09-20T06:00:00Z` reported
  `state: /home/kara/.hermes/model-router/quota-state.json` — the **live fleet** gate file.
- The scratch `quota-state.json` holds **no `quota_exhausted` key at all**; the gate went
  straight into production state. I cleared it the same minute
  (`quota clear zai-glm` → `cleared plan-window quota gate(s)`) and verified the file:
  `zai-glm` restored to `status: open`, the pre-existing `openai-codex` 429 gate untouched
  (`reset_at 2026-09-19T08:11:13Z`, already elapsed).
- Same class, by design and not measured here: `router probe`, `router probefix`,
  `router plan-sweep` (run from the fleet's own registry/state locations).
- **Ports are not isolated either.** The documented `router web` default is **:9093**,
  the same port the fleet's own web UI uses; a scratch `router web` binds it (it was
  free at the time — the fleet's :9092 API server was up, :9093 was not). Same for
  `router server --port 9092`. Discovered because the API server had to be moved to
  :9192/:9193 to avoid colliding with the running fleet server. Both scratch listeners
  were stopped at the end of the run.
- TR-083 asks for one line in the README's data-home section: *state *writers* are not all
  data-home scoped — `quota` (and the calibration commands) write fleet state; pass
  `--state-file` to target a scratch file; and the default ports are shared with the
  running fleet.*

## 6. Findings → rows

| id | P | finding |
|---|---|---|
| **TR-081** | **P1** | Proxy ladder loses hop 1: `x-router-max-hops: 2` produced exactly one attempt, at hop 4 (18.5× the chain's cheapest lane); ladder reported complete |
| TR-082 | P2 | `router seed && router validate` on a fresh clone/box returns exit 1 (`fallback_lanes.jsonl is 0s newer than registry.json`) — reproduce twice (bunker + control) |
| TR-083 | P2 | Data-home boundary is doc-silent for state writers: `router quota set` writes `~/.hermes/model-router/quota-state.json` with `TASK_ROUTER_HOME` set; scratch home has no `quota_exhausted` key |
| TR-084 | P2 | `docs/soft-gate-integration.md` documents a circuit invocation that does not exist (`--provider/--model/--reason` → rc=2); the real form is positional. `router` propagates the child's exit code |
| TR-085 | P2 | `bunker-qa.sh launch` left no `.meta`/per-run evidence file (wrote the shared default, killed at the control host's tool cap) and `collect` probed a **2-day-old** agent id → `COLLECT-FAIL unreachable-agent` |

Second finding of the day, recorded but not filed: `docs/integration.md` tells the caller to
set `ROUTER_CLASSIFIER_KEY_ENV=ZAI_GLM_API_KEY`, which is **absent** from the fleet `.env`
(`ZAI_API_KEY` / `ZAI_DEFAULT_API_KEY` are present), so a caller who wires the classifier
verbatim gets the silent `default` degrade — the degrade is visible (`degrade_reason`), so
it is a doc nit, not a defect.

## 7. What held up (do not "fix" these)

- Fail-open contract, measured: every resolve rc=0, one explicit error shape, no crash.
- Gate honesty: circuit and quota exclusions carry the reason and the reset time.
- Metadata honesty on the *committed data*: `warnings` state plainly when the registry is
  missing and the sample tables are being served.
- MCP bridge: 16 tools derived from OpenAPI, mutations correctly 403 in read-only.
- The docs' headline numbers: 43-hop chain, `_router` shape, degrade semantics, 15-20 min
  first-build estimate (that one belongs to `warpfs`/`hilo`), `ns mirror absent` guard — all accurate.

# TR-127 — task-router ⇄ muster: the frozen contract

Row: TR-127 (spec). Chain: TR-126 (scope, `docs/muster-scope.md` @ f7d00ca) →
**TR-127 (this doc)** → TR-128 (build). Date: 2026-10-14.
Design authority: `docs/muster-scope.md` §1/§4/§5/§6/§9, corrected as noted in
§9. Everything below labeled **[measured]** was verified live on 2026-10-14
against the running router (`http://127.0.0.1:9092`, mode `read-only`) with
muster's binaries; **[code]** claims are verified in `scripts/router_server.py`.

> **Correction vs the scope doc (TR-127 input, 2026-10-14).** The muster
> binaries MOVED: they live at `/home/kara/wojons/muster/bin/`
> (`openapi-cli`, `openapi-mcp`, `openapi-daemon`). The scope doc's
> `/home/kara/muster/bin/` path is STALE; this spec uses the new path
> everywhere. Separately, the live API has grown past the scope doc's
> measured 19 operations: it now publishes **30 operations — 22 reads and
> 8 mutations** (the `/api/ui/*` lanes and `postUiRegistryEdit` landed with
> TR-150..TR-155 after 2026-09-24). The read/mutation RULE is unchanged; the
> counts in this doc are the frozen live truth, and the parity test pins them
> so any future change is a deliberate spec revision, not drift.

---

## 1. Canonical spec surface

- **The contract is the live URL `http://127.0.0.1:9092/openapi.json`**
  (`GET /openapi.json`, operationId `getOpenAPI`), served by
  `build_openapi()` in `scripts/router_server.py` **[code]**. OpenAPI
  **3.1.0**, no `$ref` indirection, absolute `servers[0].url`, every operation
  documents 200/400/500 (mutations add 401/403) — muster's kin-openapi
  v0.148.0 parser ingests it **[measured]**.
- **Any file copy is a non-authoritative mirror.** A copy in a repo, docs
  site, or `--spec /path` argument is a convenience snapshot that rots
  silently. The URL form cannot rot: it is served by the same process that
  serves the API.
- **Generation rule [code]:** the document is a *static dict built in code*
  (`OPENAPI = build_openapi()` at module load, handed to `RouterApplication`
  as `self.openapi`, served verbatim by the `/openapi.json` dispatch branch).
  It is **not** generated from the route table at runtime — the route table
  (`RouterApplication.dispatch` + `known_post_paths()`) and the document can
  in principle drift, which is why the parity rule (§5) exists and is
  executable. `known_post_paths()` consults `build_openapi()` for POST
  admission **[code]**, so the spec is also the POST admission list.
- **Every operation carries a unique non-empty operationId** (muster turns
  operationIds into MCP tool names; ids survive muster's name normalization
  verbatim **[measured]**).

## 2. The consumer attachment (one config block, zero code)

Verbatim consumer block — the complete attachment; `--base-url` omitted is
**measured sufficient** (muster resolves the base URL from `servers[].url`,
`override=false` **[measured]**):

```json
{
  "mcpServers": {
    "task-router": {
      "command": "/home/kara/wojons/muster/bin/openapi-mcp",
      "args": ["--spec", "http://127.0.0.1:9092/openapi.json"]
    }
  }
}
```

Measured behavior of this block **[measured 2026-10-14]**: `openapi-mcp`
fetches the spec over HTTP from the live server, handshakes, and generates
**30 MCP tools, one per operation** (`Generated 30 MCP tools` /
`Using base URL: http://127.0.0.1:9092 (override=false)` on stderr). Query
params arrive as typed `inputSchema` properties with descriptions intact.

**Offline/file fallback variant** (valid, for air-gapped use; NOT the
default — a file copy goes stale silently):

```json
{
  "mcpServers": {
    "task-router": {
      "command": "/home/kara/wojons/muster/bin/openapi-mcp",
      "args": ["--spec", "/path/to/openapi.json",
               "--base-url", "http://127.0.0.1:9092"]
    }
  }
}
```

Muster does NOT auto-map the spec's `securitySchemes` into its executor; auth
is explicit muster-side config (`auth: {type: apikey, ...}` or env
`MUSTER_AUTH_APIKEY_VALUE`). For the recommended read-only posture (§4) that
is irrelevant — no credentials exist to configure.

## 3. Human quickstart (three lines)

```bash
# 1. Is the router up? (expect "status":"ok","mode":"read-only")
curl -s http://127.0.0.1:9092/health | jq -r '.status, .mode'
# 2. What tools do I get? (expect 30 operations, grouped GET=22 / POST=8)
/home/kara/wojons/muster/bin/openapi-cli discover http://127.0.0.1:9092/openapi.json
# 3. Drive it as MCP tools (paste the §2 block), or as plain CLI commands:
/home/kara/wojons/muster/bin/openapi-cli generate http://127.0.0.1:9092/openapi.json
```

**CLI form [measured 2026-10-14, real binary]:** `openapi-cli` (v,
`/home/kara/wojons/muster/bin/openapi-cli`) supports two spec-driven verbs
plus persisted per-operation commands:

- `openapi-cli discover <url>` — read-only listing of every operation
  (method, path, operationId); persists nothing.
- `openapi-cli generate <url>` — parses the spec and **persists one command
  per operation** to the CLI store (`generate --list` shows the store; the
  2026-10-14 run registered 30 commands). After `generate`, every operation
  is a first-class subcommand: `openapi-cli resolve --project
  <project> --output json`, `openapi-cli get-health`,
  `openapi-cli record-circuit ...`. (Names are the operationIds,
  kebab-cased — e.g. `getOpenAPI` → `get-open-a-p-i`; `resolve` stays
  `resolve`.) The CLI help for each command carries the spec's parameter
  docs.

Caller rule (fail-open contract): a router tool error means "no routing data —
proceed on your own judgment", never "stop". Muster renders HTTP errors as
text `"Error: HTTP 403: {\"error\": \"read-only mode\"}"` with `isError: true`
— callers parse text content, not structured errors.

## 4. Operation census: 30 operations — 22 reads, 8 mutations

### Reads (22 GETs — agent-safe, the recommended MCP consumer surface)

| operationId | Path | Purpose (one line) |
|---|---|---|
| `getOpenAPI` | GET /openapi.json | this contract |
| `getRoot` | GET / | health + status surface + `_links` |
| `getCapabilities` | GET /v1/capabilities | upstream gateway capabilities (live probe → startup probe → honest error) |
| `getHealth` | GET /health | control-plane health: identity, registry age/freshness, gate verdicts |
| `getModelStatus` | GET /model_status | per-model/provider status join (registry + probe + circuit) |
| `getStatus` | GET /status | server + registry counts |
| `getProxyStats` | GET /proxy/stats | rolling per-model/band averages over proxy traffic |
| `getUi` | GET /ui | the Data Command Center page (self-contained HTML, no CDN) |
| `getUiRegistry` | GET /api/ui/registry | registry browse with lane detail JOIN |
| `getUiChain` | GET /api/ui/chain | the eligible chain + every exclusion, grouped by code |
| `getUiFlow` | GET /api/ui/flow | one request end-to-end (session id) |
| `getUiSeries` | GET /api/ui/series | traffic + cost over time, bucketed, per-bucket n |
| `getUiNohops` | GET /api/ui/nohops | no-hops rate + threshold verdict (same computation as the alerter) |
| `getUiBoard` | GET /api/ui/board | board JSONL search for the UI |
| `getUiLedger` | GET /api/ui/ledger | outcome-ledger search (always discards nothing silently) |
| `listProfiles` | GET /profiles | task profiles |
| `listProviders` | GET /providers | providers |
| `getCircuitStatus` | GET /circuit/status | circuit breaker state |
| `getGaps` | GET /gaps | registry quality gaps |
| `getPricing` | GET /pricing | normalized pricing dry-run |
| `getChains` | GET /chains | latest chain snapshot |
| `resolve` | GET /resolve | project → routed model chain (the crown jewel) |

### Mutations (8 POSTs — never a default consumer surface)

| operationId | Path | Body (required fields) |
|---|---|---|
| `recordCircuit` | POST /circuit/record | provider, model, outcome |
| `postUiRegistryEdit` | POST /api/ui/registry/edit | guarded registry edit (TR-155) |
| `startLedger` | POST /ledger/start | provider, model |
| `endLedger` | POST /ledger/end | trace_id, outcome |
| `ingestOutcome` | POST /api/v1/outcomes | one completed-task outcome |
| `updateProviderListing` | POST /listings/provider | provider listing update |
| `updateModelListing` | POST /listings/model | model listing update |
| `updateProfileListing` | POST /listings/profile | profile listing update |

Mutation tools being *visible* over MCP is acceptable; being *executable* by
an unattended agent is not. The server, not the client, is the gate
(`RouterApplication._authorize` answers before any handler runs **[code]**);
battery probe 5 is its regression test.

### Auth story (scope §6, summarized)

| Layer | Mechanism | Status |
|---|---|---|
| Reads (22 ops) | **None.** Loopback binding (`servers[].url` = `http://127.0.0.1:9092`) is the entire access control. | [measured: live instance binds 127.0.0.1] |
| Mutations (8 ops) | `X-API-Key` header (scheme `EditApiKey`, op-level in the spec), honored only when the process runs `--mode edit` with `ROUTER_EDIT_API_KEY` set (constant-time compare). Read-only mode: every mutation → `403 {"error": "read-only mode"}` before any handler. Edit mode without a configured key: the process refuses to start. | [code: `_authorize`, argparse + env check + `hmac.compare_digest`; measured 403] |
| Muster side | No `securitySchemes` auto-mapping; auth only via explicit muster config. A generated client cannot acquire the key by accident. | [measured 2026-09-24, unchanged] |
| Remote consumers | Out of scope (open decision #2): loopback-only stands until the operator rules otherwise. | standing ruling |

## 5. THE PARITY RULE — an executable check

**Rule: the contract a consumer sees must be the contract the server
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
4. every operation has a unique non-empty operationId; `servers[0].url` is
   the absolute loopback URL;
5. exactly the 8 POSTs carry op-level `EditApiKey` security; no read does;
   no global `security` block exists; and a read-only app instance refuses a
   mutation with `403 {"error": "read-only mode"}` before any handler runs.

The same rule is asserted LIVE by the battery: probe 2 (muster tool-name set
== spec operationId set) and probe 6 (POST /mcp serves the same names).

```bash
~/.hermes/venvs/board/bin/python3 -m pytest tests/test_muster_spec_parity.py -v
```

## 6. The acceptance battery (TR-128's criteria, frozen)

`scripts/muster_battery.sh` — the 7 probes of scope §4 as a runnable script:

```bash
ROUTER_BASE=http://127.0.0.1:9092 MUSTER_BIN=/home/kara/wojons/muster/bin \
    bash scripts/muster_battery.sh
```

| # | Probe | Non-vacuous assertion |
|---|---|---|
| 1 | muster ingests the live spec | `openapi-cli discover` exit 0; every live operationId present in the output; operation count matches the spec |
| 2 | consumer block → full tool set | stdio MCP: `serverInfo.name == "openapi-mcp"`; tool-name SET == operationId SET (set equality, not count); every `inputSchema` non-empty |
| 3 | a read executes live through MCP | `getHealth` → `isError` false; `content[0].text` parses as JSON; `.status=="ok"`, `.registry.models > 0`, `.registry.age_days == 0` (a mocked or dead upstream cannot produce this) |
| 4 | crown-jewel read | `resolve` on a project id taken from `GET /profiles`; well-formed chain **or well-formed error** (fail-open: an error is acceptable; a hang/crash/unparseable result is a FAIL) |
| 5 | mutation boundary holds (negative proof) | `recordCircuit` through MCP → `isError==true`, text contains `HTTP 403` and `read-only mode`, and `/circuit/status` is byte-identical before/after (state untouched, not merely refused). Runs ONLY when `/health` says `read-only` |
| 6 | bridge parity / drift alarm | `POST /mcp tools/list` serves the same tool-name set as muster's (inverts to a 404 assertion after open decision #1 lands in TR-128) |
| 7 | canonical spec freshness | muster's stderr shows `Using base URL: http://127.0.0.1:9092 (override=false)` — the URL form, never a file copy |

Skips are loud and per-probe (muster binaries missing, router down, non
read-only mode for probe 5); exit 0 = every run probe passed, 1 = any FAIL,
2 = nothing verified.

## 7. Open decisions

1. **OPEN — timing of `POST /mcp` retirement** (bridge consolidation, scope
   §7). This spec does NOT decide it. The hand-rolled bridge stays through
   TR-127 as a zero-dependency compatibility surface; probe 6 is the standing
   drift alarm. **Ruled by Bane at TR-127 spec review / before TR-128
   starts.** Until he rules, this row changes nothing about `/mcp`.
2. Non-loopback consumer access — loopback-only stands; any change is a
   separate row (ruled by Bane if a remote consumer materializes).
3. Mutation-capable MCP consumers — unsupported; the 403 gate is the
   contract (ruled by Bane only if a real consumer needs writes via MCP).

## 8. What TR-127 changed (and did not)

Changed: added `tests/test_muster_spec_parity.py` (the parity rule,
hermetic), `scripts/muster_battery.sh` (the live battery), this doc. Did NOT
change: `scripts/router_server.py` (HTTP API shape untouched — frozen by
scope §2), the data tables, the fail-open doctrine.

## 9. Acceptance-run (verbatim, 2026-10-14)

Environment: live router `http://127.0.0.1:9092` (mode `read-only`,
`/health` → `status ok`, `registry.models 1954`, `age_days 0`); muster
binaries `/home/kara/wojons/muster/bin` (openapi-cli / openapi-mcp /
openapi-daemon present); worktree branch `wt/TR-127` off
`/home/kara/task-router` @ `faa0e44` (main, 2026-10-14) — this doc, the
battery script, and the parity test are the commit under record (the
spec-file commit is branch HEAD; a document cannot embed its own hash).
The battery ran against the live router build `adf4c9056b80` (loaded
2026-10-10; operation-identical to `faa0e44`, detail delta = 3
requestBody properties on listing endpoints — recorded so a future reader
can reproduce the run honestly).

`bash scripts/muster_battery.sh` — verbatim output:

```
== muster acceptance battery — http://127.0.0.1:9092/openapi.json ==
    muster binaries: /home/kara/wojons/muster/bin
    router mode: read-only (probe 5 runs only against read-only)
    live spec: 30 operations
PASS  probe 1 — muster ingests the live spec: 30 operations listed, all operationIds present
PASS  probe 2 — consumer block generates the full tool set: 30 tools, name set == operationId set (set equality), every inputSchema non-empty
PASS  probe 3 — getHealth through MCP: status=ok, registry.models=1954, age_days=0 (live, fresh)
PASS  probe 4 — resolve through MCP: well-formed chain for 'P0_FORE' (54 hops)
PASS  probe 5 — mutation boundary holds: isError=true, 'HTTP 403' + 'read-only mode' in text, circuit state byte-identical before/after
PASS  probe 6 — bridge parity: POST /mcp serves the same 30 tool names as the muster tool set
PASS  probe 7 — canonical spec freshness: muster resolved the base URL from the live URL spec (override=false)
== battery summary: 7 passed, 0 failed, 0 skipped ==
```

Parity tests: `8 passed` (`tests/test_muster_spec_parity.py`, board venv).
Probes skipped: none. Nothing was mutated: probe 5's call was refused
server-side and `/circuit/status` verified byte-identical.

### Corrections this spec freezes vs the scope doc

| Scope doc said (2026-09-24) | Live truth frozen here (2026-10-14) |
|---|---|
| binaries at `/home/kara/muster/bin/` | **moved**: `/home/kara/wojons/muster/bin/` |
| 19 operations (12 reads + 7 mutations) | **30 operations (22 reads + 8 mutations)** — `/api/ui/*` lanes + `postUiRegistryEdit` (TR-150..TR-155) landed after the scope was measured; the rule (reads keyless, mutations gated) is unchanged |

# TR-051 — Agentic-system landscape: integration targets for task-router drivers

Status: research deliverable (specs-before-code gate for the TR-050 driver programme).
Date: 2026-09-19 (-05). Lane: task-router research, TR-051.

## Scope and evidence standard

Bane's ask (TR-051): *"figure out the popular agentic systems we need support for
(~10) so they get integrated as task-router proxy drivers; survey HOW each one
calls models … what session/outcome data each can report, and what a driver for
each looks like."*

Every protocol/auth/telemetry claim below is sourced from a document fetched
**this session** (2026-09-19) by one of three methods, all of which return the
publisher's own bytes:

| method | what it reads | example |
|---|---|---|
| `raw.githubusercontent.com/<org>/<repo>/<branch>/<path>` | repo docs/README/source as committed | `…/openclaw/openclaw/main/docs/concepts/model-providers/custom-providers.md` |
| provider doc sites with the `.md` suffix (or the GitHub Docs markdown API) | published documentation | `https://learn.chatgpt.com/docs/config-file/config-reference.md`, `https://docs.github.com/api/article/body?pathname=…` |
| `api.github.com/repos/<org>/<repo>` | repo metadata (stars, default branch, last push) | star counts in the per-system headers |

Star counts are the GitHub API's `stargazers_count` on 2026-09-19 and are given
only as a popularity proxy. No claim is made from memory; where a knob could not
be verified this session it is marked **NOT VERIFIED** rather than asserted.

**Corrections to the candidate list** (the names in the ticket resolved
differently than the slugs suggested — worth recording because three of them are
renames, not different projects):

* `opencode-ai/opencode` (**archived**, Go, 13,753★, last push 2025-09-18) is the
  *ancestor*. The live project is **`anomalyco/opencode`** (208,514★, default
  branch `dev`) — this is the "~200k stars" OpenCode in the ticket.
* `sst/opencode` redirects to `anomalyco/opencode`; `block/goose` redirects to
  **`aaif-goose/goose`** (54,456★, Rust).
* `ogulcancelik/herdr` and the other personal forks are mirrors of
  **`herdrdev/herdr`** (39,507★, Rust) — verified by byte-comparing
  `docs/next/…/integrations.mdx` from both (head-40 `diff` clean).
* **OpenClaw** (`openclaw/openclaw`, 390,070★, TypeScript) is the highest-starred
  system on the list — a general personal-agent runtime, not a coding CLI.

## Headline findings

1. **Four integration surfaces exist, and no single protocol covers them.** The
   landscape splits into (A) clients that honour a **base-URL/env override**
   (wire-level interception), (B) clients that must be **launched with a config
   or env we write** (launch-level), (C) clients that only speak a **standard
   agent protocol** (ACP/MCP), and (D) clients with **no interception point at
   all** whose only contribution is a **telemetry/session import**. 15 of the 19
   systems below have (A) and/or (B); 4 are observation-only.
2. **Two hard wire-format constraints land directly on TR-050's gateway proxy.**
   (a) Codex CLI's `model_providers.<id>.wire_api` accepts **only `responses`**
   ("`responses` is the only supported value, and it is the default when
   omitted") — a router that implements only `/v1/chat/completions` cannot serve
   a Codex CLI target. (b) Claude Code routes through `ANTHROPIC_BASE_URL` to a
   gateway that must expose a **Anthropic-format endpoint** (`/v1/messages`).
   A gateways-shaped router therefore needs **three wire faces**, not one:
   OpenAI Chat Completions, OpenAI **Responses**, Anthropic Messages.
3. **Subscription-shaped clients are the cheapest wins and the murkiest legally.**
   Pi, Goose, OpenClaw, Cline, Pi and the multiplexers can all point at a proxy
   while holding OAuth subscription credentials (Claude Pro/Max, ChatGPT,
   Copilot, xAI, OpenRouter PKCE). Anthropic states plainly that it "doesn't
   support routing Claude Code to non-Claude models through any gateway" — a
   driver for Claude Code is legitimate for **lane selection among
   Anthropic-shaped upstreams**, not for substituting arbitrary models.
4. **The outcome side is far more standardised than the call side.** OTel is the
   lingua franca (Claude Code, Gemini CLI, Codex `otel.*`, OpenClaw
   diagnostics-otel, Pi telemetry) and the metric names already carry what
   TR-049 wants (`claude_code.cost.usage`,
   `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`). Session files
   (JSONL/SQLite) are the second standard source: Pi sessions carry tokens+cost
   per session, OpenClaw `sessions --json` carries token counts, Codex writes
   `history.jsonl`.
5. **The reference architecture Bane named is real and quotable.** t3code has
   `apps/server/src/provider/Drivers/<X>Driver.ts` plus
   `ProviderInstanceRegistry` / `ProviderAdapterRegistry`, and its own AGENTS.md
   states the rule a router driver must copy: *"Provider-shaped features need a
   decision per adapter, even if the decision is 'not supported here'"* and
   *"Complexity belongs at the adapter boundary. Orchestration stays pure, UI
   stays dumb."*

## The four integration surfaces (the driver taxonomy)

| surface | mechanism | who supports it | router work |
|---|---|---|---|
| **W — wire** | point the client's base URL at the router; router must speak the client's wire format | OpenClaw, OpenCode, Claude Code, Codex, Gemini CLI, Pi, Aider, Goose, Cline, Crush, Hermes, OpenHands | protocol parity per format (chat / responses / messages / gemini), streaming, tool-call passthrough |
| **L — launch** | the driver owns the process: writes env/config/argv before spawn | herdr, AionUi, t3code, Goose ACP, OpenHands ACP, any CLI in the fleet | process lifecycle, config injection, cleanup, per-run isolation |
| **A — agent protocol** | speak ACP (JSON-RPC 2.0 over stdio) or MCP to the agent | Hermes, OpenCode, Gemini CLI, Zed, Goose, OpenHands, AionUi, herdr | act as ACP **client** (or serve an ACP adapter); cannot intercept the agent's own upstream calls |
| **T — telemetry** | read what the client already records (OTel, session DB/JSONL, `--output-format json`) and map it to an outcome row | all 19 (differing in richness) | per-driver reader + mapping to the TR-049 row schema |

A driver = **one write surface (W or L or A) + one read surface (T)**. Systems
with no write surface still get a driver — an import-only driver is what feeds
`predicted_cost_per_task` for Cursor/Copilot traffic the router can never touch.

## Where this lands in the existing task-router design

TR-049 already fixed the read side: the outcome row schema in
`docs/outcomes-schema.md` (one row = one finished task, `(source_system,
session_id, model)` as the dedupe key) with two ingestion paths — the local
bulk importer in `scripts/router_outcomes.py` (`import-hermes` pulls
`session_model_usage` out of `~/.hermes/state.db` and derives the provider from
the immutable `billing_base_url` host) and `POST /api/v1/outcomes` for remote
reporters. **That importer is the existing driver pattern to mirror**: a driver
is a pure function `native store → [outcome rows]` plus (optionally) an
ingest call, and it must fail open (never block the source system).

TR-050 fixes the write side: the router becomes a gateway proxy with two faces —
(A) gateway proxy, where any client sends any request and a cheap classifier
infers the complexity profile, and (B) the native contract endpoint where the
caller pre-supplies the profile. Every driver below is an instance of one of
those faces plus a TR-049 reporter.

The pattern to copy from t3code (verified in source this session):

| t3code construct | what it buys | task-router equivalent |
|---|---|---|
| `Driver` per provider (`ClaudeDriver.ts`, `CodexDriver.ts`, `CursorDriver.ts`, `GrokDriver.ts`, `OpenCodeDriver.ts`, `AntigravityDriver.ts`) | one file owns a provider's identity, executable discovery, auth probe, session identity | `drivers/<system>.py` owns W/L/A mechanics + T reader |
| `ProviderInstanceRegistry` + `ProviderAdapterRegistry` (dynamic lookup, hot-reload) | adding an instance needs no rebuild of the facade; no `if provider == …` in call paths | driver registry keyed by `source_system`; `resolve()` never branches on driver name |
| adapters translate **native** protocol → orchestration events; `EventNdjsonLogger` | one event vocabulary for all drivers | driver emits the TR-049 row shape only, never its native shape |
| AGENTS.md rule: "Provider-shaped features need a decision per adapter, even if the decision is 'not supported here'" | gaps are declared, not silently skipped | each driver declares `surfaces: {wire, launch, acp, telemetry}` and an explicit capability matrix |
| checkpoint = hidden git ref per turn | resumability | outcome rows are the durable artifact; no driver-local state files |

**Proposed driver manifest** (the plugin contract, to be specced before code):

```yaml
id: opencode                      # == outcome row source_system
surfaces: {wire: [openai-chat, anthropic-messages, gemini], launch: true, acp: true, telemetry: true}
wire:
  protocol_family: openai-chat    # what the router must speak back
  inject:                         # how the router rewrites the client's config
    kind: config_file
    path: ~/.config/opencode/opencode.json
    pointer: /provider/<lane>/options/baseURL
  auth_passthrough: base_url_credential   # client keeps its own key; router may substitute
telemetry:
  kind: session_store             # session_store | otel_otlp | cli_json | log_ndjson | none
  path: ~/.local/share/opencode
  map: {session_id: id, model: model, tokens_in: usage.input, cost_usd: cost}
lifecycle: {strategy: owned_process | external, teardown: []}
capabilities: {stream: true, tools: true, images: true, reasoning_tokens: false}
```

---

# Per-system survey

Format per system: **PROTOCOL** → **AUTH SHAPE** → **OUTCOME SURFACE** →
**DRIVER SPEC SKETCH** → **PRIORITY** (with justification), then an evidence line.
"★" = GitHub `stargazers_count`, 2026-09-19.

---

## 1. OpenClaw — `openclaw/openclaw` (★390,070, TypeScript)

**PROTOCOL.** Models are provided by *provider plugins* declared under
`models.providers.<id>` (or `models.json`); each entry carries `baseUrl`,
`apiKey`, and an `api` field — the documented value is `openai-completions`, and
the docs state the entry is for "custom providers or OpenAI/Anthropic-compatible
proxies". Anthropic-shaped routes exist too (Kimi Coding is reached through
Moonshot's "Anthropic-compatible endpoint"). A separate `compat` block declares
capabilities when the endpoint contract has been verified. OpenClaw also ships a
transport-level proxy: `openclaw proxy validate` preflights an operator-managed
forward proxy (config `proxy.proxyUrl` / `OPENCLAW_PROXY_URL`) and `openclaw
proxy start|run|sessions|query|preset|blob` is an explicit local capture proxy.

**AUTH SHAPE.** Config-level `apiKey` with `${ENV_VAR}` interpolation
(`apiKey: "${MOONSHOT_API_KEY}"`), an `env.vars` block for injected provider
env, plus first-class **auth profiles** with rotation and cooldown plus
provider-owned OAuth flows (OpenAI ChatGPT/Codex OAuth is an official provider
plugin); profiles have a portability rule (`copyToAgents` for refresh tokens).
Onboarding is `openclaw onboard --auth-choice <provider>-api-key`.

**OUTCOME SURFACE.** `openclaw sessions --json` lists stored conversation rows
with **exact token counts** ("Token counts below 1,000 appear as whole numbers;
larger counts use compact `k`/`m` labels. JSON output retains exact numeric
counts"), session stores are SQLite-backed, `sessions export-trajectory` exports
a session, and the observability rubric names a `telemetry-diagnostics-and-observability`
surface with: model-call diagnostic events, `diagnostics-otel` plugin (OTLP/HTTP
traces), `diagnostics-prometheus` plugin with a gateway-authenticated
`GET /api/diagnostics/prometheus`, rolling Gateway JSONL logs with W3C trace
correlation, and a "Model usage" session diagnostic.

**DRIVER SPEC SKETCH.** Wire driver: write a `models.providers.task-router`
entry (`baseUrl` → router, `api: "openai-completions"`, `apiKey:
"${ROUTER_DRIVER_KEY}"`) — and remember OpenClaw has its own fallback chain
(`agents.defaults.model.fallbacks`) and auth-profile rotation, so the router is
one candidate in *its* chain, not the only one. Launch surface: `openclaw
onboard`/Control-UI writes the same config. Read surface: parse `openclaw
sessions --json` (per-agent store selection: `--agent <id>`, `--all-agents`;
`--store` resolves a legacy selector to a physical SQLite path it reports) into
TR-049 rows with `source_system: openclaw`; skip rows with zero token counts.
Optionally subscribe to the diagnostics OTLP endpoint instead of shelling out.

**PRIORITY: High.** Highest-starred target on the list, with all four surfaces
available at once (base-URL config, launch/onboard path, ACP CLI (`docs/cli/acp.md`),
and exact token counts in a queryable JSON surface) — the best cost/benefit on
the list. Only caveat: its internal failover chain means the router must be
configured as an explicit provider, not assumed.

*Evidence:* `docs/concepts/model-providers/custom-providers.md`,
`docs/concepts/model-providers.md`, `docs/cli/proxy.md`,
`docs/auth-credential-semantics.md`, `docs/concepts/model-failover.md`,
`docs/cli/sessions.md`, `.agents/skills/claw-score/references/completeness/telemetry-diagnostics-and-observability.md`
(all raw.githubusercontent.com, `openclaw/openclaw@main`).

---

## 2. OpenCode — `anomalyco/opencode` (★208,514, TypeScript, branch `dev`)

**PROTOCOL.** Built on the Vercel **AI SDK** + **models.dev** catalog (docs
claim "75+ LLM providers" and local models). Provider options accept a
`baseURL` override explicitly documented for "proxy services or custom
endpoints": `provider.anthropic.options.baseURL`. Bedrock has an `endpoint`
alias. Config resolution order is documented (global `~/.config/opencode/opencode.json`,
project, `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`, plus an org
`.well-known/opencode` default). It also speaks **ACP**: `opencode acp` starts it
"as an ACP-compatible subprocess that communicates with your editor over
JSON-RPC via stdio", and it ships an SDK plus an HTTP server mode.

**AUTH SHAPE.** `/connect` stores provider credentials in
`~/.local/share/opencode/auth.json`; provider API keys can also come from the
environment (models.dev-defined env names). `provider.<id>.options.apiKey` is a
config-level alternative; `experimental.policies` can allow/deny which providers
may be used at all.

**OUTCOME SURFACE.** Sessions are persisted locally (session store under the
data dir), the CLI/TUI exposes session cost and token accounting per session, the
SDK/`serve` HTTP surface exposes session+message objects programmatically, and
the repo tree carries `packages/http-recorder` + `packages/client` (an
OpenAPI-codegen client), i.e. a stable programmatic read path for a driver.

**DRIVER SPEC SKETCH.** Wire driver: rewrite
`provider.<lane>.options.baseURL` to the router in the resolved config layer,
leaving the client's own key in `auth.json` for passthrough or substituting a
router key with `options.apiKey`. Because config can be supplied by
`OPENCODE_CONFIG_CONTENT`, the launch driver can inject a whole override without
touching the user's file — the cleanest injection point of any system surveyed.
Read surface: prefer the SDK/HTTP session objects (structured) over screen
scraping; map session id, model, token usage, cost → TR-049 row with
`source_system: opencode`.

**PRIORITY: High.** 208k★, a documented base-URL knob, a no-touch config
injection path (`OPENCODE_CONFIG_CONTENT`), an ACP subprocess mode, *and* a
structured session API — the most complete target in the set after OpenClaw.

*Evidence:* `packages/web/src/content/docs/{providers,config,acp,sdk}.mdx`
(`anomalyco/opencode@dev`).

---

## 3. Claude Code — `anthropics/claude-code` (★146,460, TypeScript)

**PROTOCOL.** Anthropic **Messages** API. The vendor publishes a dedicated
gateway path: `ANTHROPIC_BASE_URL` "is the variable that points Claude Code at
the gateway", and "any gateway that exposes a supported API format works" — with
a compatibility guide covering "endpoints, headers to forward, and feature
pass-through". Provider switching through a gateway "also depends on the gateway
exposing a single Anthropic-format endpoint regardless of upstream".

**AUTH SHAPE.** `ANTHROPIC_API_KEY`, a gateway **credential variable**
(which, while active, replaces the claude.ai subscription for that session), an
`apiKeyHelper` in settings JSON, or the OAuth subscription login; settings
precedence is documented across managed/user/project/local files with per-key
exceptions, and managed settings can pin credential + base URL fleet-wide
(`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` marks host-managed provider config).
Bedrock/Vertex/Foundry are first-class alternates.

**OUTCOME SURFACE.** Best-in-class. (a) **OTel**: `claude_code.cost.usage`,
`claude_code.session.count`, `claude_code.api_request`, `claude_code.commit.count`,
metric attributes `session.id`, `user.account_uuid`, `vcs.*` repository
identity, `OTEL_METRICS_EXPORTER`/`OTEL_LOGS_EXPORTER`/`OTEL_EXPORTER_OTLP_*`
with per-signal overrides. (b) **Headless JSON**: `--output-format json` returns
`total_cost_usd` plus a **per-model cost breakdown**, `session_id`, and usage
metadata ("client-side estimates"); `stream-json` is NDJSON for live capture.
(c) `/usage` shows session token/cost and prompt-cache statistics. (d) local
session transcripts.

**DRIVER SPEC SKETCH.** Wire driver: set `ANTHROPIC_BASE_URL` + the gateway
credential variable (managed settings file for fleets), and implement the
Anthropic Messages face in the router including the headers Claude Code requires
forwarded (`anthropic-beta` for OAuth capability). Read surface: run headless
(`claude -p --output-format json`) where the driver owns the invocation, else
export OTel metrics to a collector the driver reads; map `total_cost_usd` +
`modelUsage` → TR-049 row. **Constraint to spec explicitly:** Anthropic does not
support routing Claude Code to non-Claude models via a gateway, so this driver is
for choosing among Claude-shaped upstreams/lanes, and any cross-vendor
substitution is a user-visible policy decision, not a default.

**PRIORITY: High.** Largest coding-agent user base with a *vendor-documented*
gateway integration point, three independent outcome surfaces (OTel, headless
JSON, session transcripts) and a managed-settings distribution path. The
non-Claude-model caveat caps the upside but does not remove it.

*Evidence:* `docs/en/docs/claude-code/llm-gateway.md`, `…/model-config.md`,
`…/monitoring-usage.md`, `…/costs.md`, `…/settings.md`, `…/headless.md`
(docs.claude.com, `.md` variants).

---

## 4. OpenAI Codex CLI — `openai/codex` (★125,225, Rust)

**PROTOCOL.** OpenAI **Responses** API, and this is the sharpest constraint in
the survey. `model_providers.<id>` accepts `.name`, `.base_url`, `.env_key`,
`.env_key_instructions`, `.experimental_bearer_token`, `.requires_openai_auth`,
`.query_params`, and `.wire_api` — whose description reads: *"Protocol used by
the provider. `responses` is the only supported value, and it is the default
when omitted."* `openai_base_url` overrides the built-in provider. Session
transcripts can be saved to `history.jsonl`; the repo also ships an
**app-server JSON-RPC protocol** (`codex-rs/app-server-protocol/schema/json/`)
whose params include approval flows and dynamic tool calls — a programmatic
control surface independent of the wire.

**AUTH SHAPE.** Two documented sign-in methods: **ChatGPT subscription OAuth**
(browser flow, credentials returned to Codex) or an **API key** ("Sign in with
an API key for usage-based access"); custom providers get their key from the
`.env_key` environment variable (with `experimental_bearer_token` discouraged).
Machine-local provider/auth keys are refused in project-scoped config — they
must live in user-level `~/.codex/config.toml`.

**OUTCOME SURFACE.** `otel.*` config (exporter/trace_exporter/metrics_exporter
with per-exporter `.endpoint`, `otel.log_user_prompt`), `history.jsonl`
transcripts, rollout-budget token accounting in config
(`features.rollout_budget.limit_tokens`), and the app-server event stream.

**DRIVER SPEC SKETCH.** Wire driver: add `model_providers.task-router` with
`base_url` → router, `env_key = "ROUTER_DRIVER_KEY"`, `wire_api = "responses"`
(omit it — it is the only legal value) and a matching `model` + `model_provider`
selection; the user keeps the Codex CLI on its subscription only when it is *not*
selecting the router provider. **The router must serve `POST /v1/responses` with
SSE**, including tool-call passthrough, or this driver cannot ship. Read surface:
tail `history.jsonl`/session rollouts for token counts, or consume the OTLP
exporter; map to TR-049 with `source_system: codex`.

**PRIORITY: High.** Huge installed base and an official multi-provider config
surface — but it forces the Responses API into the TR-050 proxy. Ship this driver
*after* the OpenAI Chat Completions face and treat `/v1/responses` as the gate.

*Evidence:* `docs/config.md` (stub), `codex-rs/config.md` /
`developers.openai.com/codex/config-reference.md`, `docs/authentication.md` /
`learn.chatgpt.com/docs/auth.md`, `codex-rs/app-server-protocol/schema/json/*`
(`openai/codex@main`).

---

## 5. Cursor (editor + `agent` CLI) — proprietary

**PROTOCOL.** **No public base-URL override is documented.** Requests go to
Cursor's own backend; the CLI ships modes (`--mode agent|plan|ask`), print mode
(`agent -p --model "gpt-5" --output-format text`), session resume
(`agent ls|resume|--continue|--resume`), sandbox controls, and **ACP** support
(`cursor.com/docs/cli/acp.md` appears in the docs index). Cursor also operates
its own model router ("Cursor Router picks the model for each Auto request based
on your optimization mode", Teams/Enterprise) — i.e. a competing router, not an
integration point.

**AUTH SHAPE.** Browser login (`agent login`, `NO_OPEN_BROWSER=1` for headless)
with credentials stored locally, or an API key: `CURSOR_API_KEY` or
`agent --api-key …`, generated from the Cursor dashboard. `agent status` reports
authentication state, account info and **"Current endpoint configuration"** —
the only hint of an endpoint knob; **NOT VERIFIED** whether it is user-settable.

**OUTCOME SURFACE.** Usage pools and spend are visible in the editor settings and
the hosted usage dashboard (two pools per plan: Cursor Models vs third-party
models billed at API price). Session history is local (`agent ls`). There is no
documented OTel export or session-cost JSON.

**DRIVER SPEC SKETCH.** Observation-only driver: no wire injection. Read the
local CLI session store (paths **NOT VERIFIED** — resolve at implementation
time) for session id/model and, where absent, accept operator-entered rows via
`POST /api/v1/outcomes`. Value is *pricing truth* (what Cursor charges per
model) rather than routing.

**PRIORITY: Low.** Closed wire, no documented interception, and Cursor runs its
own router; the honest driver is telemetry-only. Worth a row only after the
wire-capable drivers, unless `agent status`'s "endpoint configuration" proves
settable.

*Evidence:* `cursor.com/docs/cli/overview.md`, `…/cli/reference/authentication.md`,
`…/models-and-pricing.md`, `cursor.com/llms.txt`.

---

## 6. Gemini CLI (and the Antigravity family) — `google-gemini/gemini-cli` (★107,076, TypeScript)

**PROTOCOL.** Google Generative Language API. Two documented base-URL
overrides: `GOOGLE_GEMINI_BASE_URL` ("Overrides the default base URL for Gemini
API requests (when using `gemini-api-key` authentication)") and
`GOOGLE_VERTEX_BASE_URL` for vertex-ai auth — with a hard security constraint:
"Must be a valid URL. For security, it must use HTTPS unless pointing to
`localhost` (or `127.0.0.1` / `[::1]`)". It also exposes **ACP mode**
(`docs/cli/acp-mode.md`) and a `packages/a2a-server` (agent-to-agent) surface,
and documents model routing/steering (`docs/cli/model-routing.md`).

**AUTH SHAPE.** Three documented methods: **Sign in with Google** (browser
OAuth; individual accounts, Workspace/org accounts, Code Assist tiers), a
**Gemini API key** (`GEMINI_API_KEY`/`GOOGLE_API_KEY`), or **Vertex AI** (GCP
project). Cloud sessions/headless use the API-key or Vertex path.

**OUTCOME SURFACE.** OTel with both an endpoint and a file sink:
`GEMINI_TELEMETRY_ENABLED`, `GEMINI_TELEMETRY_OTLP_ENDPOINT`,
`GEMINI_TELEMETRY_OTLP_PROTOCOL`, `GEMINI_TELEMETRY_OUTFILE`
("Save telemetry to file (overrides otlpEndpoint)"), `GEMINI_TELEMETRY_LOG_PROMPTS`;
metrics/logs carry `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.request.max_tokens`. Session logs capture "startup configuration and
prompt submissions"; session management is a documented CLI surface.

**DRIVER SPEC SKETCH.** Wire driver: set `GOOGLE_GEMINI_BASE_URL` to the router
(**HTTPS, or bind the router on localhost** — the docs are explicit) for
api-key auth, or `GOOGLE_VERTEX_BASE_URL` for vertex auth; the router needs a
Gemini-shaped face (`generateContent`/streaming) for these lanes. Read surface:
set `GEMINI_TELEMETRY_OUTFILE` to a driver-owned file and parse OTLP-JSON, or
point `…_OTLP_ENDPOINT` at a driver collector; map `gen_ai.usage.*` → TR-049.

**PRIORITY: High.** 107k★, a documented base-URL override with an explicit
proxy use-case, first-class telemetry to a file, and OAuth/API-key/Vertex auth
modes that mirror the subscription-vs-PAYG split the router already models. The
HTTPS-or-localhost rule is a design input for the proxy listener, not a blocker.

*Evidence:* `docs/reference/configuration.md`, `docs/cli/telemetry.md`,
`docs/get-started/authentication.mdx`, `docs/cli/acp-mode.md`,
`docs/cli/model-routing.md` (`google-gemini/gemini-cli@main`).

---

## 7. GitHub Copilot CLI — `github/copilot-cli` (★11,182, Shell installer)

**PROTOCOL.** Proprietary GitHub Copilot backend ("Powered by the same agentic
harness as GitHub's Copilot coding agent"). **MCP-native**: "ships with GitHub's
MCP server by default and supports custom MCP servers"; tool execution requires
explicit approval. No documented base-URL provider override and no BYOK surface
in the CLI docs — the model picker exposes Copilot-provided models only.

**AUTH SHAPE.** GitHub account authentication with an **active Copilot
subscription**; org/enterprise policy can disable the CLI outright. Model choice
is per-session (`/model`). Regional/enterprise usage follows GitHub billing.

**OUTCOME SURFACE.** GitHub-side usage/billing surfaces; the CLI's own
machine-readable output is **NOT VERIFIED** in the docs fetched (the repo is a
~18-file installer; docs live on docs.github.com). No OTel claim is made here.

**DRIVER SPEC SKETCH.** Observation-only driver (same shape as Cursor): no wire
injection and no config-file knob. Where the driver spawns the CLI it can wrap
the run and capture whatever the CLI prints (a `--json`/log flag must be
confirmed at implementation time — **NOT VERIFIED**), else accept operator rows.
MCP is the only *extension* point, and it extends tools, not model routing.

**PRIORITY: Low.** No documented interception surface; value is limited to
outcome attribution for Copilot-billed work, and even that requires confirming
an unverified machine-readable output path.

*Evidence:* `github/copilot-cli` README (`@main`), repo tree (18 blobs),
`docs.github.com/api/article/body?pathname=/en/copilot/how-tos/copilot-cli/use-copilot-cli`,
`docs.github.com/…/install-copilot-cli`.

---

## 8. Aider — `Aider-AI/aider` (★49,048, Python)

**PROTOCOL.** Everything routes through **litellm**, so the surface is
whatever litellm speaks (OpenAI-compatible being the common denominator). Aider
documents an explicit base-URL switch: `--openai-api-base VALUE`
(`AIDER_OPENAI_API_BASE` in the `.env` template, described as "Specify the api
base url"), plus `--set-env VAR=value` for arbitrary provider env vars
("control API settings, can be used multiple times") — which is how
non-OpenAI providers' base URLs are redirected.

**AUTH SHAPE.** Four documented ways: CLI switches (`--openai-api-key`,
`--anthropic-api-key`), environment variables / `.env`
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, …), `.aider.conf.yml` (`openai-api-key:` / `api-key:` list),
and the generic `--api-key provider=<key>`, which sets `PROVIDER_API_KEY`.
Aider also ships `model-metadata.json` (per-model context/cost table).

**OUTCOME SURFACE.** In-session token/cost accounting (litellm cost callback +
the model-metadata price table), git-committed "aider:" commits as a durable
artifact trail, and a documented **analytics log**: `--analytics-log
ANALYTICS_LOG_FILE` ("Specify a file to log analytics events") with
`--analytics-posthog-host`/`--analytics-posthog-project-api-key` for a custom
sink and `--analytics-disable` as the off switch.

**DRIVER SPEC SKETCH.** Wire driver: set `--openai-api-base` (or
`AIDER_OPENAI_API_BASE` in the launch `.env`, or `--set-env
OPENAI_API_BASE=…`) to the router and hand the router a key; the router serves
the OpenAI Chat Completions face. Because aider is CLI-invoked, the driver is a
**wrapper**: it writes a per-run `.env`/conf, launches `aider --message`/`--yes`,
then reads `--analytics-log` (plus its own stdout capture) to emit TR-049 rows
with `source_system: aider`.

**PRIORITY: Medium-High.** Documentation-wise the *easiest* redirect on the list
(one flag or one env var), a clean per-run wrapper model, and a file-sink
analytics log — but no OTel, and its market share is shrinking relative to the
agentic CLIs, so it ranks below the top tier while still outranking the closed
systems.

*Evidence:* `aider/website/docs/config/options.md`,
`…/config/api-keys.md`, `…/config/dotenv.md`, `…/config.md`
(`Aider-AI/aider@main`).

---

## 9. Goose — `aaif-goose/goose` (★54,456, Rust; ex-`block/goose`)

**PROTOCOL.** Per-provider implementations with **host overrides** in the
documented provider table: `OPENAI_HOST` (OpenAI + "OpenAI-compatible endpoints
(e.g., self-hosted LLaMA, vLLM, KServe)"), `ANTHROPIC_HOST`, `OPENROUTER_HOST`,
`OLLAMA_HOST`, `LITELLM_HOST`/`LITELLM_BASE_PATH`, `ASTRON_BASE_URL`,
`TANZU_AI_ENDPOINT`, plus many OpenAI-compatible aggregators. The providers crate
also carries **declarative provider definitions** as data
(`crates/goose-providers/src/declarative/definitions/*.json`) — a
manifest-per-provider pattern a router driver can mirror. Goose can additionally
consume **ACP providers** (Claude Code, Codex, Amp via `*-acp` npm adapters).

**AUTH SHAPE.** `~/.config/goose/config.yaml` (`active_provider` + `providers`
map) with `GOOSE_PROVIDER`/`GOOSE_MODEL` env overrides; keys come from documented
env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …); AWS providers require
pre-set AWS env; ChatGPT-Codex and GitHub-Copilot use browser OAuth/device flow
with "No manual key". Docs warn against storing secrets in config.yaml.

**OUTCOME SURFACE.** Opt-in anonymous usage data explicitly includes "Provider
and model used", "Session metrics (duration, interaction count, token usage)",
and error classes — governed by `GOOSE_TELEMETRY_ENABLED`; `GOOSE_CLI_SHOW_COST`
toggles in-CLI cost estimates, which implies a local cost/token accounting module
(a driver can read the same session store). Sessions are a first-class surface
(`goose session resume|fork`, session recipes).

**DRIVER SPEC SKETCH.** Wire driver: point the `providers` entry's host at the
router (`OPENAI_HOST`/`ANTHROPIC_HOST` in the launch env, or the config.yaml
provider entry) with the router's key. Launch driver: for ACP providers the
driver writes the ACP command + env instead. Read surface: map the session store
(tokens + `GOOSE_CLI_SHOW_COST` accounting) → TR-049 rows; note the documented
caveat that **"ACP session ID differs from goose session ID: Telemetry fields may
not correlate"** — the driver must key rows on the id it can prove.

**PRIORITY: Medium-High.** Multi-surface (wire + ACP + launch), a
manifest-per-provider design worth copying, and session/token metrics in the
telemetry contract — but the token data is opt-in telemetry plus a local
estimate, so the outcome driver needs verification work before trust.

*Evidence:* `documentation/docs/getting-started/providers.md`,
`…/guides/config-files.md`, `…/guides/usage-data.md`,
`…/guides/acp-providers.md`, `crates/goose-providers/src/declarative/definitions/`
(`aaif-goose/goose@main`).

---

## 10. Pi Agent — `earendil-works/pi` (★107,138, TypeScript)

**PROTOCOL.** `@earendil-works/pi-ai` unified provider layer with a built-in
catalog cached at `~/.pi/agent/models-store.json`. **The extension API is the
cleanest true-plugin driver surface found in this survey**: providers are
registered by an extension via `pi.registerProvider()`, and the docs name the
exact use case — *"The simplest use case: redirect an existing provider through
a proxy"* — showing `baseUrl` overrides that preserve all existing models ("When
only `baseUrl` and/or `headers` are provided (no `models`), all existing models
for that provider are preserved with the new endpoint"). The legacy provider-config
form takes `apiKey: "$MY_API_KEY"`, `api: "openai-completions"`, and headers with
env interpolation (`"X-Corp-Auth": "$CORP_AUTH_TOKEN"`). It also runs headless:
`-p`, `--mode json` ("Output all events as JSON lines"), `--mode rpc`.

**AUTH SHAPE.** Subscriptions via `/login` (ChatGPT Codex, Claude Pro/Max,
GitHub Copilot, xAI, OpenRouter **PKCE**, Radius) stored in
`~/.pi/agent/auth.json` with auto-refresh; API keys via env (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`ZAI_API_KEY`, `OPENCODE_API_KEY`, …) or the same auth file. A documented
resolution order governs which wins.

**OUTCOME SURFACE.** Per-session JSONL files under `~/.pi/agent/sessions/` and an
interactive `/session` view that reports "messages, tokens, and cost" (the footer
also shows token/cache usage, cost and context usage). `--mode json` emits every
event as JSON lines for machine capture. Telemetry is a designed subsystem
(`@earendil-works/pi-telemetry`, `packages/agent/docs/telemetry.md` +
`telemetry-schema.md`), with the honest caveat in-doc that cross-process trace
propagation is still partly design-stage.

**DRIVER SPEC SKETCH.** Two routes, both first-class. (1) **Extension driver**:
ship a pi extension that calls `pi.registerProvider()` with
`baseUrl` → router + auth headers, i.e. the driver lives *inside* pi's plugin
system exactly as Bane's "drivers are plugins" framing wants. (2) **Launch
driver**: write `models.json`/settings overrides and run `pi --mode json`,
parsing the event stream for usage/cost and the session file for tokens.

**PRIORITY: High.** 107k★, an official extension seam documented for precisely
"redirect through a proxy", OAuth subscriptions *and* env-key providers, plus
tokens+cost per session and a JSON event stream — everything a driver needs
without touching pi's source.

*Evidence:* `packages/coding-agent/docs/{providers,custom-provider,sessions,usage}.md`,
`packages/agent/docs/{telemetry,telemetry-schema}.md` (`earendil-works/pi@main`).

---

## 11. Cline — `cline/cline` (★68,725, TypeScript)

**PROTOCOL.** The extension/CLI supports an **"OpenAI Compatible"** provider
whose configuration *is* "Base URL … API Key … Model ID", explicitly for "any
other provider that offers an OpenAI-compatible API endpoint" — the single
cleanest base-URL statement in the survey (with Azure managed identity as an
auth variant, and per-model price fields for input/output tokens). The **Cline
API** (`api.cline.bot/api/v1/chat/completions`) is a hosted OpenAI-shaped
endpoint with `provider/model-name` ids. The CLI (`cline`) takes
`-P/--provider <id>`, `-m/--model`, `-k/--key`, `--json` ("Output messages as
JSON instead of styled text" — NDJSON per message), `--id` to resume, a
`--hooks-dir` (default `~/.cline/hooks`) for runtime hook injection, and `cline
hook` to consume hook payloads from stdin.

**AUTH SHAPE.** `cline auth [provider]` configures provider credentials (stored
per provider, account auth token generated on sign-in); the Cline API accepts
`Authorization: Bearer <API key or account token>`; the CLI can be handed a
per-run key with `-k/--key`. Enterprise config supports remote/MDM-style
provider configuration (including an `openai-compatible` admin/member pair).

**OUTCOME SURFACE.** `--json` message stream (per-message NDJSON), session
history (`cline history`), hooks (`--hooks-dir`, `cline hook`) which fire at
runtime boundaries and are the natural place to emit one outcome row per turn
or per task, and per-model price metadata in settings.

**DRIVER SPEC SKETCH.** Wire driver: write the OpenAI-Compatible provider entry
(Base URL → router, API key = router key, model id) into Cline's settings, or
pass `-P <router-provider> -k <router-key> -m <model>` on a per-run basis. Turn
capture: install a driver hook into `~/.cline/hooks` (or pass `--hooks-dir`) so
each completed task posts to `POST /api/v1/outcomes`; fall back to parsing
`--json` stdout when hooks are unavailable.

**PRIORITY: Medium-High.** A first-class OpenAI-Compatible base URL, a per-run
key override, an NDJSON output mode, *and* a documented hook directory for
outcome emission — the best turn-level telemetry story among the CLI harnesses.
Ranks below the top tier only because the CLI's provider plumbing (vs. the
extension UI) is younger.

*Evidence:* `docs/provider-config/openai-compatible.mdx`, `docs/cli/cli-reference.mdx`,
`docs/api/{authentication,models}.mdx`, `docs/customization/hooks.mdx`,
`docs/enterprise-solutions/configuration/remote-configuration/openai-compatible/*`
(`cline/cline@main`).

---

## 12. herdr — `herdrdev/herdr` (★39,507, Rust)

**PROTOCOL.** herdr is a **multiplexer/runtime**, not a model caller: it runs
coding agents as panes and talks to *them* through a **CLI + socket API**
("agents drive herdr through the cli and socket api: they can spawn panes, prompt
each other, and wait until another agent is genuinely blocked"), with custom
commands receiving `HERDR_SOCKET_PATH`, `HERDR_ACTIVE_PANE_ID`,
`HERDR_ACTIVE_PANE_CWD`, etc. Its **official integrations** are per-agent
adapters: *"Herdr detects supported agents automatically. Install official
integrations when you want native agent session restore, **direct lifecycle
reports**, or both"* — the integration list is essentially this ticket's
candidate list (Pi, OMP, Claude Code, Codex, GitHub Copilot CLI, Devin, Droid,
Kimi Code CLI, OpenCode, Kilo Code CLI, **Hermes Agent**, Qoder CLI, Qwen Code,
Letta Code, Cursor Agent CLI, MastraCode, Antigravity CLI, Grok CLI), each
declaring a version + resume command (`pi --session <path-or-id>`,
`opencode --session <id>`, …). Model calls happen inside the child agent.

**AUTH SHAPE.** herdr holds none — credentials belong to the wrapped agent
(the integrations only restore sessions). Server config is `config.toml` per
machine (shell, keys, status commands, sidebar agent state), with server/pane
ownership explicitly separated from client-local presentation settings.

**OUTCOME SURFACE.** **Lifecycle/state** per pane and per agent (working /
blocked / idle, semantic state icons and rolled-up space state) plus
`session.json`/`session-history.json` persistence and native agent session
references; status-bar items can run a command on an interval and render its
last line — a ready-made pull surface for router state. Token/cost data lives in
the *child* agents, not in herdr.

**DRIVER SPEC SKETCH.** Launch driver, not wire driver: herdr is the **fleet's
process owner**, so the driver supplies per-agent launch env (base URL / config
injection for whichever agent runs in the pane), and reads lifecycle state from
the socket API for "task started/finished" boundaries. Outcome rows are then
completed by the *child* agent's own driver (e.g. Pi or OpenCode) — herdr
contributes correlation (`pane ↔ session ↔ turn`), not tokens.

**PRIORITY: Medium.** No model-routing surface of its own, but it is the
natural *host* for launch-surface drivers and would let the fleet drive
heterogeneous agents from one place. Ranked Medium because value is
multiplied only once at least one child driver (Pi/OpenCode) exists.

*Evidence:* `README.md`, `docs/next/website/src/content/docs/{integrations,configuration,session-state,plugins,socket-api}.mdx`
(`herdrdev/herdr@master`; byte-identical head vs the `ogulcancelik/herdr` mirror).

---

## 13. AionUi — `iOfficeAI/AionUi` (★32,950, TypeScript)

**PROTOCOL.** A local GUI/WebUI "Cowork" app that **wraps 20+ existing CLI
agents** — its README states it auto-detects and co-works with "OpenClaw, Hermes
Agent, Claude Code, Codex, OpenCode, Gemini CLI and 20+ more CLI Agent", with a
built-in engine (`aionrs`) and an **ACP-based session model** (its PRD tree
documents ACP sessions, messaging, permissions, skills, and an
`acp-adapter-extension` example manifest). It also carries an LLM-provider
settings surface (`docs/prds/settings/llm_providers/`). Model calls therefore
happen inside whichever child agent it launches, or inside its built-in engine.

**AUTH SHAPE.** Inherited from the wrapped agent (AionUi runs the child CLI);
its own provider settings PRD covers credentials for the built-in engine.
**NOT VERIFIED** beyond the PRD level — the settings doc I fetched was empty
(0 bytes), so no credential-storage claim is made.

**OUTCOME SURFACE.** Session/message records in its own store (ACP session
lifecycle, `F-SESSION-*` feature list marks create/connect/stop/reset/delete/
migrate as implemented) plus the child agents' own telemetry. No token/cost
surface was verified.

**DRIVER SPEC SKETCH.** Launch/ACP driver: register the router-wired agent as an
AionUi agent (or supply launch env for the child CLI it spawns), and read its
session store for start/stop boundaries. Same shape as herdr: **correlation and
control, not routing**.

**PRIORITY: Low.** A meta-surface whose integration value is entirely dependent
on the child drivers; it adds a GUI host but no new interception point. Worth a
row only if fleet users actually want the GUI.

*Evidence:* `README.md` (repo description), `docs/prds/conversations/acp/README.md`,
`docs/prds/settings/llm_providers/README.md` (empty),
`examples/acp-adapter-extension/aion-extension.json` (`iOfficeAI/AionUi@main`).

---

## 14. Hermes Agent — `NousResearch/hermes-agent` (this system)

**PROTOCOL.** The gateway already serves an **OpenAI-compatible API** (the
messaging docs route "any OpenAI-compatible frontend via the API server"; the
fleet uses `/v1/responses` for scheduler spawns and `/v1/chat/completions` for
OpenAI-shaped clients), and providers in `config.yaml` accept custom base URLs
for any OpenAI-compatible endpoint. It also implements **ACP** ("Use Hermes
Agent inside ACP-compatible editors and collaboration platforms") and MCP.
`Subscription Proxy` is an existing, shipped precedent for exposing an upstream
as an OpenAI-compatible endpoint for external apps — the same trick TR-050
applies to the whole router.

**AUTH SHAPE.** `config.yaml` provider entries with API keys from env/secrets
files (`~/.hermes/env-file`), per-provider `base_url`; gateway API keys for
inbound callers (the fleet uses Bearer tokens on `/v1/responses`); OAuth
subscription providers where supported; managed-scope config for
administrator-pinned settings.

**OUTCOME SURFACE.** The richest verified surface in the fleet: `~/.hermes/state.db`
→ `session_model_usage` (session_id, model, `billing_provider`,
`billing_base_url`, task, `api_call_count`, input/output/reasoning tokens,
`estimated_cost_usd`, first/last seen), which the existing
`router_outcomes.import_hermes` driver already maps to TR-049 rows. Known
pitfall encoded in that driver: `billing_provider`/`model` are **re-stamped at
gateway restarts**, so provider identity must be derived from the immutable
`billing_base_url` host — count by `billing_base_url`, never by
`billing_provider` alone.

**DRIVER SPEC SKETCH.** Already exists (bulk importer at
`scripts/router_outcomes.py import-hermes`); the driver work for TR-050 is to
(a) make the gateway a *client* of the router (point its provider base URL at
the router for selected lanes) and (b) have the router attribute proxied
sessions directly at ingest time rather than reconstructing them from state.db.
Failure semantics stay fail-open per TR-049.

**PRIORITY: High.** It is the reference implementation and the TR-050 target:
the driver exists, the outcome surface is verified in-repo, and every other
driver's row shape is validated against it.

*Evidence:* repo `docs/outcomes-schema.md` + `scripts/router_outcomes.py`
(read this session), `https://hermes-agent.nousresearch.com/docs/llms.txt`
(configuration, providers, ACP, subscription-proxy pages), fleet memory on
`state.db` field semantics.

---

## 15. OpenHands — `OpenHands/OpenHands` (★88,477, Python/TS)

**PROTOCOL.** OpenHands is an agent **server** with a REST/HTTP surface
(`PATCH /api/settings` shown in its ACP guide) and a built-in agent that calls
LLMs itself (LiteLLM-style provider config), **plus** ACP agent support: "the
Agent Server spawns the agent's own CLI as a subprocess and relays each turn to
it … The external agent manages its own LLM, tools, and execution". The
supported ACP provider list is sourced from the SDK registry
(`openhands.sdk.settings.acp_providers`) and includes Claude Code
(`npx -y @agentclientprotocol/claude-agent-acp`), Codex
(`npx -y @agentclientprotocol/codex-acp`) and Gemini CLI
(`npx -y @google/gemini-cli --acp`).

**AUTH SHAPE.** "The Agent Server owns the subprocess and the credentials";
the UI records *which* agent to run and surfaces a form for the secrets it needs;
agent choice is stored **per backend** (`agent_kind`, `acp_*` settings keys).

**OUTCOME SURFACE.** Conversation/turn events through the server (the docs
describe relayed ACP turns and a Canvas that renders them), plus whatever the
child agent reports (Claude Code's OTel/JSON, Codex's history, Gemini's
telemetry). OpenHands' own cost/token reporting for the built-in agent is the
server's LLM accounting — **NOT VERIFIED** in this session's fetches.

**DRIVER SPEC SKETCH.** Launch/ACP driver: configure `agent_kind` + `acp_*` to
a router-wired adapter (for Claude Code/Codex/Gemini the adapter is an npm ACP
bridge whose child agent must itself be pointed at the router — i.e. compose
this driver with the Claude Code / Codex / Gemini drivers), or point OpenHands'
own LLM config at the router. Read surface: consume the server's turn/event API
for boundaries, rely on child drivers for token/cost.

**PRIORITY: Medium.** Real interception potential (its own LLM config) and a
clean ACP story, but the highest-value path is compositional — it inherits
whatever the child drivers do, so it should follow them.

*Evidence:* `docs/ACP_AGENTS.md`, `examples/acp-docker/README.md`,
`config/defaults.json` (`OpenHands/OpenHands@main`).

---

## 16. Crush — `charmbracelet/crush` (★28,181, Go)

**PROTOCOL.** Providers are first-class CLI objects:
`crush provider add <id> [flags]` with `--type` (`openai`, `openai-compat`,
`anthropic`, `ollama`, …), `--base-url`, `--discover-models`, and
`--provider-options JSON`; models are registered per provider and assigned to
large/small slots (`crush model add <provider>/<id>`), addressed as
`<provider>/<id>`. The config docs also describe **env-gated headers**
("outgoing request. This makes env-gated headers safe"), i.e. credentials can be
injected by environment.

**AUTH SHAPE.** Provider API keys live in the provider entry/config; env-gated
headers allow secret substitution at request time; local providers (Ollama) need
none.

**OUTCOME SURFACE.** Crush documents `docs/config/` and `docs/hooks/` — a hook
system plus session state (**NOT VERIFIED** in detail: the fetched README is the
config index). Session storage and per-session token accounting exist in the
product; a driver must verify exact paths/fields at implementation time.

**DRIVER SPEC SKETCH.** Wire driver with the cleanest one-liner in the survey:
`crush provider add task-router --type openai-compat --base-url <router> …`,
then `crush model add task-router/<model>` for the slots. Turn capture: its
hooks are the intended seam; fall back to session-store parsing.

**PRIORITY: Medium.** A single documented command configures the whole
interception (base URL + type + model discovery), which makes the wire driver
cheap; it ranks below the top tier only because its outcome surfaces are less
documented and its user base is smaller.

*Evidence:* `docs/config/README.md`, `docs/hooks/README.md`
(`charmbracelet/crush@main`).

---

## 17. t3code — `pingdotgg/t3code` (★23,044, TypeScript) — reference architecture *and* target

**PROTOCOL.** "A Node WebSocket server wraps provider CLIs and agents (Codex,
Claude Code, Cursor, Grok, OpenCode, Antigravity) and serves web, desktop, and
mobile clients." Clients send typed WebSocket requests → commands → a pure
decider → persisted events → a projector → the UI read model; provider CLIs run
as **subprocesses** with per-provider **adapters** translating native protocols
into orchestration events; queue-backed reactors emit receipts; each turn ends
with a **checkpoint** (a hidden git ref). Source confirms the shape:
`apps/server/src/provider/Drivers/{Claude,Codex,Cursor,Grok,OpenCode,Antigravity}Driver.ts`,
`Layers/{Claude,Codex,Cursor,Grok,OpenCode,Antigravity}Adapter.ts`,
`ProviderInstanceRegistry`, `ProviderAdapterRegistry`, `ProviderAuthService`,
`ProviderSessionDirectory`, `EventNdjsonLogger`.

**AUTH SHAPE.** `ProviderAuthService` + per-provider home discovery
(`ClaudeHome`, `CodexHomeLayout`) — t3code drives the *host's existing CLI
credentials* rather than holding its own (it wraps, it doesn't call providers).
Its docs reference provider credentials as an environment-scoped concern.

**OUTCOME SURFACE.** The event log itself: orchestration events + receipts per
provider instance, `EventNdjsonLogger`/`ProviderEventLoggers`, session directory,
and per-turn checkpoints (git refs) for diff/restore. Token/cost fidelity
depends on the wrapped CLI's own reporting.

**DRIVER SPEC SKETCH.** Not a router client — a **pattern source**. The router
should copy three things verbatim: (1) one `Driver` per system owning executable
discovery + auth probe + session identity; (2) registries that resolve
instances dynamically so no call path contains `if provider == …`; (3) the
AGENTS.md rule that every provider-shaped feature gets a decision per adapter,
"even if the decision is 'not supported here'" — which is how the router should
publish per-driver capability gaps instead of silently no-op'ing.

**PRIORITY: Medium.** As a client it is observation-only; as the named
reference architecture it is the highest-leverage *read* in this survey, and
Phase 1 of the driver programme should be a literal port of its boundaries.

*Evidence:* `AGENTS.md` (rules §71, §145, §160), repo tree under
`apps/server/src/provider/**` (`pingdotgg/t3code@main`).

---

## 18. Zed — `zed-industries/zed` (★90,541, Rust)

**PROTOCOL.** Zed is an **ACP client**, not a model caller for our purposes: it
launches external agents via `agent_servers` config (OpenCode's docs show the
exact stanza — `{"agent_servers": {"OpenCode": {"type": "custom", "command":
"opencode", "args": ["acp"]}}}`) and carries ACP crates (`crates/acp_thread`,
`crates/acp_tools`). It also has its own `language_models` providers
(cloud/OpenAI/Anthropic/etc.) whose base-URL overridability was
**NOT VERIFIED** this session.

**AUTH SHAPE.** Zed's own providers use its sign-in/API-key flows; for external
agents the credentials belong to the agent (`agent_servers` entries carry a
command, not a key).

**OUTCOME SURFACE.** ACP thread state inside Zed; token/cost belongs to the
spawned agent. Zed-side telemetry for external agents is **NOT VERIFIED**.

**DRIVER SPEC SKETCH.** Launch driver: install a router-wired ACP agent (or a
wrapper script) as Zed's `agent_servers` entry, so Zed becomes a host for
whichever agent driver already exists. Only interesting as a *host*, exactly like
herdr/AionUi.

**PRIORITY: Low.** No interception of its own in the surveyed docs; the value is
"one more host where a router-wired ACP agent can be installed", which the ACP
drivers already deliver.

*Evidence:* repo tree (`crates/acp_thread`, `crates/language_models*`,
`crates/http_proxy`), `anomalyco/opencode` `acp.mdx` Zed stanza,
`agentclientprotocol.com/protocol/overview.md`.

---

## 19. Qwen Code — `QwenLM/qwen-code` (★27,979, TypeScript)

**PROTOCOL.** "An open-source AI coding agent that lives in your terminal" —
structurally a descendant of the Gemini CLI (same docs/telemetry/ACP-shaped
architecture: the upstream project ships `docs/cli/acp-mode.md`,
`docs/cli/telemetry.md`, `.gemini/config.yaml`). Whether it inherits
`GOOGLE_GEMINI_BASE_URL` is **NOT VERIFIED** in this session (no config docs were
fetched from this repo).

**AUTH SHAPE.** Expected to mirror Gemini CLI (Google sign-in / API key) —
**NOT VERIFIED**.

**OUTCOME SURFACE.** Expected telemetry parity with Gemini CLI — **NOT VERIFIED**.

**DRIVER SPEC SKETCH.** If the base-URL knob and telemetry carry over, the
cheapest possible driver: reuse the Gemini CLI driver with a different
`source_system` and executable discovery. Verify the inherited env vars before
writing anything; treat the Gemini driver as the template.

**PRIORITY: Low.** High star count but likely a structural clone of item 6, so
it adds little beyond another `source_system` label — worth including only as a
near-free third target after Gemini CLI ships.

*Evidence:* `github.com/QwenLM/qwen-code` repo metadata + description; Gemini CLI
tree for the structural claim (marked unverified where unverified).

---

# Priority ranking

Ranked by (1) whether the router can actually intercept calls, (2) outcome-data
richness, (3) install base, (4) implementation cost, (5) policy constraints.
Every justification is 1–2 sentences, as requested.

| # | System | Priority | Wire face required | Primary justification |
|---|---|---|---|---|
| 1 | **OpenClaw** | High | OpenAI chat (+Anthropic routes) | 390k★, all four surfaces present (base-URL provider config, onboard path, ACP CLI, exact token counts + OTel/Prometheus diagnostics) — best value-per-unit-work on the list. |
| 2 | **OpenCode** | High | OpenAI chat + Anthropic | 208k★ with a documented `baseURL` override, a no-touch injection path (`OPENCODE_CONFIG_CONTENT`), ACP subprocess mode and a structured session API. |
| 3 | **Claude Code** | High | Anthropic `/v1/messages` | Vendor-documented gateway path plus three outcome surfaces (OTel `cost.usage`, headless `total_cost_usd`, transcripts); the non-Claude-model policy caveat limits substitution, not lane choice. |
| 4 | **Pi Agent** | High | OpenAI chat | An official extension seam documented for "redirect an existing provider through a proxy", OAuth subs *and* env keys, tokens+cost per session, JSON event stream. |
| 5 | **Hermes Agent** | High | OpenAI chat + Responses | Self-hosted reference implementation: the TR-049 importer already exists and `state.db` gives exact per-session tokens/cost — it validates every other driver's row shape. |
| 6 | **Gemini CLI** | High | Gemini `generateContent` | Documented base-URL override written *for proxy use*, telemetry to a file with `gen_ai.usage.*`, and OAuth/API-key/Vertex auth matching the sub-vs-PAYG split (note the HTTPS-or-localhost rule). |
| 7 | **Codex CLI** | High | **OpenAI Responses (SSE)** | Huge base and an official multi-provider config, but it forces the Responses face into TR-050 — high value, gated on that protocol work. |
| 8 | **Goose** | Medium-High | OpenAI chat + Anthropic | Host-override env vars, declarative provider definitions worth copying, ACP providers, and session/telemetry metrics — outcome data needs verification (session ids differ across ACP). |
| 9 | **Cline** | Medium-High | OpenAI chat | Explicit "OpenAI Compatible" base URL + per-run `--key` + NDJSON `--json` + a hook directory for turn-level outcome emission — the best harness telemetry story below the top tier. |
| 10 | **Aider** | Medium-High | OpenAI chat | Redirect is a single documented flag/env var (`--openai-api-base`), wraps cleanly per run, and has a file-sink analytics log — but no OTel and a shrinking relative install base. |
| 11 | **Crush** | Medium | OpenAI chat | `crush provider add --type openai-compat --base-url` is the cheapest wire setup surveyed (one command + model discovery); outcome hooks are documented but thin. |
| 12 | **t3code** | Medium | — | Zero routing value as a client, but it is the named reference architecture: port its per-driver boundaries, registries and "decision per adapter" rule before writing any driver code. |
| 13 | **herdr** | Medium | — | No model surface of its own; it is the natural *host* for launch drivers (per-agent integrations, socket API, lifecycle state) whose value unlocks only with a child driver. |
| 14 | **OpenHands** | Medium | OpenAI chat (own agent) | Real interception for its built-in agent and a clean ACP story, but the high-value path is compositional — it inherits Claude Code/Codex/Gemini drivers. |
| 15 | **Cursor** | Low | none documented | Closed wire, no documented base-URL knob, and Cursor operates its own router; honest driver is telemetry-only (pending whether `agent status`'s "endpoint configuration" is settable). |
| 16 | **GitHub Copilot CLI** | Low | none documented | GitHub-account-only auth, MCP-only extensibility, no documented machine-readable outcome surface — value is limited to billing attribution. |
| 17 | **AionUi** | Low | — | A GUI meta-surface over 20+ CLI agents; adds a host, no interception point, and its own provider PRD was unreadable (empty file) this session. |
| 18 | **Zed** | Low | — | ACP *client* only in the surveyed docs; useful as one more host for an existing ACP driver, nothing more. |
| 19 | **Qwen Code** | Low | Gemini (unverified) | Likely a structural clone of Gemini CLI, so it adds a `source_system` label rather than a new integration — near-free *after* item 6, not before. |

**Recommended shipping order:** OpenClaw → OpenCode → Hermes (parity check) →
Pi → Gemini CLI → Claude Code → Codex CLI (Responses face) → Cline → Aider →
Goose → Crush → OpenHands → Cursor/Copilot (import-only) → hosts (herdr,
AionUi, Zed) → Qwen Code.

# What this forces on TR-050 (design inputs, not opinions)

1. **Three wire faces minimum**: OpenAI Chat Completions (OpenClaw, OpenCode,
   Pi, Cline, Aider, Crush, Goose, Hermes, OpenHands), OpenAI **Responses**
   (Codex CLI), Anthropic Messages (Claude Code, OpenClaw/OpenCode Anthropic
   routes). Gemini `generateContent` is a fourth.
2. **TLS or localhost-only listeners**: Gemini CLI refuses non-HTTPS base URLs
   unless the host is `localhost`/`127.0.0.1`/`[::1]`.
3. **Header passthrough is part of the contract**: Claude Code requires
   `anthropic-beta` forwarding for OAuth-capability traffic; provider-specific
   betas/headers must survive the proxy or features break silently.
4. **Auth passthrough vs substitution is per-driver**, not global: Codex refuses
   provider/auth keys in project-scoped config; OpenClaw supports `${ENV}`
   interpolation; Pi supports header interpolation; Claude Code can replace a
   subscription with a gateway credential (changing who is billed).
5. **Classification has a fallback when the client declares nothing**: TR-050's
   classifier is the only complexity signal for the 15 systems whose CLIs cannot
   declare a profile — which is exactly why `complexity: null` must stay legal in
   the TR-049 row schema.
6. **One outcome row per task, not per call**: only Cline (hooks), Claude Code
   (headless JSON) and Pi (`--mode json`) give a clean task boundary; everyone
   else reports per-session aggregates, so the driver must document which
   semantics it emitted.

# Recommended board rows (specs before code)

| row | title | priority |
|---|---|---|
| TR-052 | SPEC: driver plugin contract (manifest, registries, capability matrix, fail-open rules) | P0 |
| TR-053 | SPEC + IMPL: TR-050 wire faces — OpenAI chat, OpenAI Responses (SSE), Anthropic Messages | P0 |
| TR-054 | IMPL: driver `openclaw` (wire W + sessions-json T) | P1 |
| TR-055 | IMPL: driver `opencode` (wire W via `OPENCODE_CONFIG_CONTENT` + SDK T) | P1 |
| TR-056 | IMPL: driver `pi` (extension `registerProvider` + `--mode json` T) | P1 |
| TR-057 | IMPL: driver `gemini-cli` (wire W + OTLP-file T; HTTPS/localhost listener note) | P1 |
| TR-058 | IMPL: driver `claude-code` (wire W + headless-JSON/OTel T; policy caveat in doc) | P1 |
| TR-059 | IMPL: driver `codex` (Responses face + history.jsonl T) | P1 |
| TR-060 | IMPL: driver `cline` (wire W + hook-dir T) | P2 |
| TR-061 | IMPL: drivers `aider`, `crush`, `goose` (wire W wrappers) | P2 |
| TR-062 | IMPL: import-only drivers `cursor`, `copilot-cli` (no wire surface) | P3 |
| TR-063 | IMPL: host drivers `herdr`, `aionui`, `zed` (launch/ACP) | P3 |

# Gaps and unverified items (honest list)

* **Copilot CLI** — no machine-readable output mode, no OTel, no base-URL knob
  verified; only its GitHub docs were reachable (the repo is an installer).
* **Cursor local session storage** — path/schema not verified; "Current endpoint
  configuration" in `agent status` is unexplained by the docs.
* **AionUi** — its LLM-provider PRD file fetched as 0 bytes; no credential or
  telemetry claim made. Its ACP PRD is a Chinese-language feature index.
* **herdr / Crush / OpenHands** — token/cost surfaces not verified; only session
  and lifecycle shapes were confirmed.
* **Zed's own provider base-URL override** — not verified; only its ACP-client
  role is evidenced.
* **Qwen Code** — assumed inheritance from Gemini CLI; explicitly unverified.
* **OTel attribute-level mapping** (which metric carries which model) was read
  for Claude Code and Gemini CLI only; Codex/OpenClaw/Pi telemetry schemas need
  a second pass before their drivers emit `tokens_in/tokens_out`.

# Evidence index (all fetched 2026-09-19, -05)

| system | sources |
|---|---|
| OpenClaw | `raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/model-providers{,/custom-providers}.md`, `docs/cli/proxy.md`, `docs/cli/sessions.md`, `docs/auth-credential-semantics.md`, `docs/concepts/model-failover.md`, `.agents/skills/claw-score/references/completeness/telemetry-diagnostics-and-observability.md` |
| OpenCode | `…/anomalyco/opencode/dev/packages/web/src/content/docs/{providers,config,acp,sdk}.mdx`; `api.github.com/repos/{sst/opencode,opencode-ai/opencode}` |
| Claude Code | `docs.claude.com/en/docs/claude-code/{llm-gateway,model-config,monitoring-usage,costs,settings,headless}.md` |
| Codex CLI | `learn.chatgpt.com/docs/{config-reference,auth}.md` (= `developers.openai.com/codex/{config-reference,auth}.md`), `raw.githubusercontent.com/openai/codex/main/{docs/config.md,docs/authentication.md}`, tree `codex-rs/app-server-protocol/schema/json/` |
| Cursor | `cursor.com/docs/cli/{overview,headless}.md`, `…/cli/reference/authentication.md`, `…/models-and-pricing.md`, `cursor.com/llms.txt` |
| Gemini CLI | `raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/{reference/configuration.md,cli/telemetry.md,get-started/authentication.mdx,cli/acp-mode.md,cli/model-routing.md}` |
| Copilot CLI | `raw.githubusercontent.com/github/copilot-cli/main/README.md`; tree (18 blobs); `docs.github.com/api/article/body?pathname=/en/copilot/how-tos/copilot-cli/use-copilot-cli` |
| Aider | `raw.githubusercontent.com/Aider-AI/aider/main/aider/website/docs/{config/options.md,config/api-keys.md,config/dotenv.md,config.md}` |
| Goose | `raw.githubusercontent.com/block/goose/main/documentation/docs/{getting-started/providers.md,guides/config-files.md,guides/usage-data.md,guides/acp-providers.md}`; tree `crates/goose-providers/src/declarative/definitions/`; `api.github.com/repos/block/goose` → `aaif-goose/goose` |
| Pi | `raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/{providers,custom-provider,sessions,usage}.md`, `packages/agent/docs/{telemetry,telemetry-schema}.md` |
| Cline | `raw.githubusercontent.com/cline/cline/main/docs/{provider-config/openai-compatible.mdx,cli/cli-reference.mdx,api/authentication.mdx,api/models.mdx,customization/hooks.mdx}` |
| herdr | `raw.githubusercontent.com/herdrdev/herdr/master/{README.md,docs/next/website/src/content/docs/{integrations,configuration,session-state,plugins,socket-api}.mdx}` |
| AionUi | `raw.githubusercontent.com/iOfficeAI/AionUi/main/{README.md,docs/prds/conversations/acp/README.md,docs/prds/settings/llm_providers/README.md,examples/acp-adapter-extension/aion-extension.json}`; `api.github.com/repos/iOfficeAI/AionUi` |
| Hermes Agent | repo `docs/outcomes-schema.md`, `scripts/router_outcomes.py` (local read); `hermes-agent.nousresearch.com/docs/llms.txt` |
| OpenHands | `raw.githubusercontent.com/OpenHands/OpenHands/main/{docs/ACP_AGENTS.md,examples/acp-docker/README.md,config/defaults.json}` |
| Crush | `raw.githubusercontent.com/charmbracelet/crush/main/docs/{config/README.md,hooks/README.md}` |
| t3code | `raw.githubusercontent.com/pingdotgg/t3code/main/{AGENTS.md,apps/server/src/provider/Layers/ProviderAdapterRegistry.ts}`; repo tree `apps/server/src/provider/**` |
| Zed | repo tree (`crates/acp_thread`, `crates/language_models*`, `crates/http_proxy`); `agentclientprotocol.com/protocol/overview.md` |
| Qwen Code | `api.github.com/repos/QwenLM/qwen-code` |
| ACP (protocol) | `agentclientprotocol.com/{llms.txt,protocol/overview.md}` — JSON-RPC 2.0, methods + notifications, `initialize` → `authenticate` → `session/new|session/load` |

# SPEC-PROXY-DRIVERS — one driver per host system, all behind the TR-067 proxy

Status: SPEC (design authority), implementing rows TR-071 · TR-072 · TR-073 ·
TR-074 · TR-075 · 2026-09-20 · Bane directive "i asked for hermes first"
Related: docs/specs/SPEC-INTEGRATION-PATHS.md (Path B = the proxy this builds on),
docs/outcomes-schema.md (the row each driver must produce), TR-051 (system
survey, complete), TR-067 (proxy, DELIVERED 50699f7)

## 1. Why a spec before code

Five host systems (hermes, openclaw, opencode, pi, deepseek-harness) each need a
driver. They differ in wire dialect, telemetry shape and config location — but
they must NOT differ in what they produce. The invariant is: **a driver is a
config snippet plus a telemetry reader; it never contains provider-name
branching, and it never teaches the router about the host system.**

The reason is Bane's cost rule. The router already owns lane pricing (TR-070),
so a driver's only job is to make a host system's traffic OBSERVABLE and
ROUTED. If a driver starts making pricing or selection decisions, that logic
escapes into five places and drifts.

## 2. The one contract every driver satisfies

A driver declares, for its host system:

```
id            slug, e.g. "opencode"
surfaces      W (wire) | L (launch) | A (ACP) | T (telemetry)  -- at least W
wire_format   openai-chat | openai-responses | anthropic-messages | gemini
config_path   where a user points the host at the proxy (documented, not patched)
t_reader      how an outcome row is produced after the fact
```

The **proxy path is always W** (the host points at the router and the router
does the chain walk). L/A exist for fleet-spawned runs and are NOT proxies.

### 2.1 Acceptance, identical for all five rows

> one live session of the host system, pointed at the proxy, produces a
> `source_system="<id>"` outcome row with a visible complexity classification
> (`complexity_source` ∈ declared|classifier|default) and is governed by the
> TR-067 hop ladder.

`complexity_source` is the row field already emitted by `_proxy_requirements()`
(`scripts/router_server.py`): `declared` when the caller sent
`x-router-profile`, `classifier` when `router_classify` resolved a matrix,
`default` when it degraded — and R10 requires the degrade to be VISIBLE with its
reason, never silent.

### 2.2 Non-negotiable behaviours (inherit from the proxy, do not re-implement)

- **Fail-open.** A driver must never make the host system unable to run. The
  proxy degrades to `default`; a driver that 500s is a defect.
- **One outcome row per attempt**, source_system = the driver id, written
  best-effort (`_proxy_record`), so a breaker can see the lane.
- **No credentials in the driver.** The host sends its own key to the proxy;
  the proxy walks its own chain with its own keys.

## 3. Wire facts each driver must respect (grounded, not assumed)

These are the reasons a naive "just point it at the proxy" breaks:

| Host | Fact | Consequence |
|---|---|---|
| pi | some OpenAI-compatible servers reject `role:developer`; pi exposes `compat.supportsDeveloperRole=false` | the proxy route must be declared with that flag, or reasoning models fail |
| deepseek-harness | hand-declared routes default to OpenAI-own shape (`supportsDeveloperRole=true`, `maxTokensField=max_completion_tokens`, `supportsStore=true`) | the router route MUST declare a `PiAiCompatProfile` (min: `supportsDeveloperRole=false`) or the gateway rejects `role:developer` |
| opencode | accepts custom OpenAI-compatible providers AND speaks ACP | W for the proxy path; A is only for fleet-spawned runs; it may send `anthropic-messages`, which the proxy does NOT speak yet |
| openclaw | a `baseUrl` with no `/api` suffix defaults to openai-completions | the base URL shape decides the dialect — document the exact string |
| hermes | lanes are already OpenAI-compatible; sessions already land in `state.db session_model_usage` | cheapest T reader of the five; W is config-only |

## 4. Sequencing (and why hermes is first)

Bane: "i asked for hermes first". That ordering is also the cheapest: hermes is
already routed through a gateway that writes `session_model_usage`, so its T
reader may reduce to reading a table that already exists.

1. **TR-071 hermes** — prove the contract end-to-end on the cheapest host.
2. **TR-072 openclaw / TR-073 opencode / TR-074 pi** — the four hosts that need
   a config snippet; independent of each other, can land in any order.
3. **TR-075 deepseek-harness** — has the most pre-existing wire machinery
   (a completed pi-ai compat-gate surface), so it is the last and lowest-risk.

### 4.1 The one thing that blocks several rows

`anthropic-messages` and `gemini` wire formats are **NOT** spoken by the proxy
yet. If a host needs them, that is PROXY scope and lands BEFORE that driver —
name it in the driver's spec section rather than working around it in the
driver. opencode is the likely first consumer (anthropic-messages).

## 5. What is explicitly OUT of scope

- Teaching the router about host-specific model names.
- Passing the host's credentials through to upstream (the proxy uses its own).
- Streaming parity beyond what TR-067 delivered (intentionally not mirrored).
- Any change to Hermes internals (SPEC-INTEGRATION-PATHS §1: additive only).

## 6. Test obligation per driver

1. Wire: the host's dialect reaches the proxy and a chain hop resolves (not
   401/exhausted) — the acceptance line above.
2. Row: an outcome row appears with the right `source_system` and a
   `complexity_source` that is not silently `default`.
3. Degrade: with the classifier unavailable, the driver still completes and the
   row SHOWS `complexity_source=default` plus the reason.
4. Compat: for pi and deepseek-harness, a reasoning model sends a role the proxy
   accepts (the `role:developer` case above) — a regression test, because this is
   the exact failure the wire facts predict.

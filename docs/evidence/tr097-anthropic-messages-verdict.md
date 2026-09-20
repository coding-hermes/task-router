# TR-097 STEP 1 VERDICT — anthropic-messages is NOT required for opencode

**Date:** 2026-09-20 · **Host verified:** opencode 1.18.29 (`~/.opencode/bin/opencode`)
**Method:** ran the REAL opencode binary against the REAL proxy
(`scripts/router_server.py`) with the proxy's upstream aimed at a recording mock.
Binary strings were treated as a hypothesis; this run is the evidence.
Harness: `/tmp/hilo-bench/tr097_live.py` (end state: `VERDICT: NOT REQUIRED —
opencode speaks openai-chat`).

## Verdict

**TR-097 closes as NOT REQUIRED** for TR-073. opencode does not need the proxy to
speak `anthropic-messages`.

## Evidence (measured, not inferred)

opencode emitted **119 requests, 100% to `/v1/chat/completions`** — zero to any
anthropic path. The first request body:

```
top-level keys : ['max_tokens', 'messages', 'model', 'stream', 'stream_options']
has messages                : True
has TOP-LEVEL system        : False   <- anthropic shape would have this
has stop_sequences          : False   <- anthropic shape would have this
max_tokens: True | max_completion_tokens: False
message roles               : ['system', 'user', 'user']
first content form          : str     <- anthropic shape uses content BLOCKS
```

Every one of the four anthropic-messages markers TR-097 lists as different is
absent. The request is openai-chat in both path and body shape — including the
`system`-as-a-message convention, which is the marker that would have forced a
translation layer.

## Why the config works with no dialect declaration

opencode's provider config (`ProviderConfig.api`) is an open string, so the
dialect is chosen by the bundled package. The binary shows the fallback for a
hand-declared provider:

```
api: { id: ..., url: provider?.api ?? ..., npm: SN(...) ?? provider?.npm ?? '$?.npm ?? "@ai-sdk/openai-compatible" }
```

`@ai-sdk/openai-compatible` is bundled (22 occurrences of that literal; 20
`@ai-sdk/*` provider packages). `anthropic-messages` appears only **2** times in
the entire binary — it is a supported option, not the default. The driver must
still declare `npm: '@ai-sdk/openai-compatible'` and `api: '<proxy>/v1'`
explicitly, because omit-vs-omit is not the safe default to rely on.

## Separate mechanism confirmed for TR-073 (not a new requirement)

The TR-097 run's rows landed as `source_system="router-proxy"`, NOT `opencode`.
Cause: TR-071 attributes a proxied attempt to a driver only when the caller sends
`x-router-caller`, and `opencode` is **not registered** in the driver registry
(verified: `drivers.get_driver('opencode') -> None`), so the header degrades to
`router-proxy` by design.

This is ordinary TR-073 work, not an added prerequisite. Verified that opencode
CAN send it — pointed straight at a mock (no proxy in between, since the proxy
strips the header before forwarding), opencode's `options.headers` arrived:

```
x-router-caller        opencode
x-router-profile       P1_CODING
x-session-affinity     ses_f42c...
x-session-id           ses_f42c...
user-agent             opencode/1.18.29 ai-sdk/provider-utils/4.0.23 runtime/bun/...
```

So TR-073's acceptance line (`source_system="opencode"`) is reachable once the
driver is registered. Its driver spec must record that the header rides in
`provider.router.options.headers`.

## Two operational traps found while doing this (worth keeping)

1. **The live router server owns 127.0.0.1:9092.** A test that starts its own
   proxy on 9092 silently loses: `Popen` succeeds, the bind fails, and the test
   drives the PRODUCTION server instead. The symptom was a `Forbidden` that no
   env change could fix — a 403 means the path was not in `PROXY_PATHS`, i.e. it
   was not OUR server at all (the live one runs read-only; ours ran edit mode).
   Use a throwaway port.
2. **opencode ignores a project config unless `OPENCODE_CONFIG` points at it.**
   The first run bootstrapped from the global config dir and resolved
   `router/tr-auto` against the models.dev catalog
   (`ProviderModelNotFoundError: ... Did you mean: ai-router, unorouter,
   fastrouter?`). Setting `OPENCODE_CONFIG` to the project file fixed model
   resolution immediately.

## Not built (deliberately)

`gemini` — no row needs it; TR-097 says do not build it speculatively. Still true:
TR-072 openclaw (openai-completions default), TR-073 opencode (verified here), and
the delivered TR-074 pi / TR-075 deepseek-harness all speak openai-chat.

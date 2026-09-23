# TR-120 Part A — WORKING recipe (verified live 2026-09-23, proxy :9397)

A real `hermes chat` turn reached the proxy, got served, and landed ONE
attributed, metered outcome row.

## Root cause of the two earlier failures (do NOT repeat)

The `--provider router` flag DID resolve — but to the BUILT-IN Ramp Router
plugin (`plugins/model-providers/router/__init__.py:246` registers
`name="router"`, base_url `https://api.router.com/v1`). A `custom_providers`
entry of the same name is SHADOWED by a canonical built-in
(`hermes_cli/runtime_provider_custom.py:93` `_shadowed_by_builtin`). The 401
from api.router.com then tripped the configured `fallback_providers` chain
(9router lanes) — the proxy never saw a POST. agent.log line 14617 proves the
wrong base_url: `provider=router base_url=https://api.router.com/v1
model=router/auto`.

Second blocker: the proxy forwarded the client's `stream: true` verbatim, the
upstream answered SSE, `json.loads` failed, and the mirror served a 200 with no
`choices` — the OpenAI client retried on "empty stream". Fixed on the router
side (see commit `dafb349`): strip `stream`, treat unservable 2xx as hop
failure, synthesize SSE for streaming clients.

## WORKING recipe

1. `custom_providers` entry — the name must NOT collide with a built-in/alias
   (`router`, `ramp` are TAKEN). Use `task-router`:

```yaml
custom_providers:
  - name: task-router
    api_key_env: API_SERVER_KEY          # the gateway/proxy key
    base_url: http://127.0.0.1:9397/v1   # the proxy, /v1 included
    models:
      router/auto: {}                    # namespaced; the proxy rewrites it
    extra_headers:
      x-router-caller: hermes            # -> source_system 'hermes'
      x-router-session: hermes-router-test-1   # -> parent_session_id
```

2. Proxy env (TR-102 recipe): `ROUTER_PROXY_AUTH=passthrough`,
   `ROUTER_PROXY_UPSTREAM=http://127.0.0.1:8642`, classifier env, keys exported
   (`set -a; source ~/.hermes/.env; set +a`), then
   `scripts/router_server.py --mode read-only --port 9397`.

3. Real turn (one-shot flags ARE honored when the provider name resolves to the
   custom entry):

```bash
hermes chat -q "Reply with exactly: ROUTER-CLIENT-OK" \
  --provider task-router -m glm-5.3-flash --max-turns 3 -Q
```

Verified live: proxy log `POST /v1/chat/completions HTTP/1.1 200`; outcome row

```json
{"source_system": "hermes", "session_id": "hermes:hermes-router-test-1",
 "parent_session_id": "hermes-router-test-1", "provider": "openai-codex",
 "model": "gpt-5.6-sol", "tokens_in": 61092, "tokens_out": 9,
 "cost_usd": 0.2445, "steps": 1, "success": true}
```

## FAILED paths (recorded so nobody repeats them)

- `--provider router -m glm-5.3-flash` → resolved to the Ramp Router BUILT-IN
  (name shadowing), 401 at api.router.com, fallback chain activated, proxy saw
  NO POST. Fix: name the entry `task-router` (or `custom:<name>`).
- `-m router/auto` alone → same hijack; the models.dev/overlay provider map
  resolves `router` before `custom_providers` is ever consulted
  (`hermes_cli/providers.py:467` `resolve_provider_full` — built-in rung comes
  before the custom rung).
- `--provider <custom-name> -m <other-provider-model>` (the original attempt):
  `hermes_cli/oneshot.py:298` `_resolve_model_and_provider` auto-detects the
  provider FROM the model string (models.dev) when the model is explicit, and
  the model wins over the provider flag — the `--provider` value is dropped.
  Use a model the custom entry serves, or no `-m` at all.

## Honest limitation

`extra_headers` are static config (no `${ENV}` expansion in header values), so
`x-router-session` is per-DEPLOYMENT, not per-session. Per-session attribution
needs either one provider entry per lane or a Hermes-side hook; the proxy
already accepts the header per-request, so a caller that can set request
headers (curl, drivers) gets true per-session rows today.
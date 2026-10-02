# TR-236 live evidence — 2026-10-02, control box (karaHermes-mde-7840hs)

Every line below is a real command output from this run. Ledger rows are quoted
from `/tmp/tr236-live/ingress-ledger.jsonl` (runtime state, not in the repo).

## 0. The gap this closes, measured

```
$ grep -rn "crier" scripts/router_server.py        # before this change
(no matches)
$ ss -ltn | grep -E ':9391|:9092|:8642'
LISTEN  127.0.0.1:9391     # classified proxy
LISTEN  127.0.0.1:9092     # read API
LISTEN  0.0.0.0:9119 …     # (gateway is on 8642 via its unit)
```

Both doors were HTTP; nothing read a bus message.

## 1. The endpoint registry loads clean

```
$ router_ingress.py endpoints
ok        hermes-gateway-local hermes-gateway       reply=sse-last-message   timeout=900s auth=env-file:~/.hermes/.env:API_SERVER_KEY
ok        router-proxy-local openai-compatible    reply=json:choices.0.message.content timeout=900s auth=env-file:~/.hermes/.env:API_SERVER_KEY
ok        webhook-sink-local webhook              reply=webhook-separate   timeout=10s auth=none
exit=0
```

## 2. The ingress identity is a real bus participant

```
$ tr236_bus.py register
register_if_missing -> True
identity: task-router | public: 3a3afcb25678e194f9b51c3b42868683544d789f32cfd772771e2d8ddb12da24
registry count: 18
registry has task-router: True
ids: asce,deepseek-charts…,orchestrator,speclang,task-router,temple-runner,totalstack,uhlp
```

The key lives at `~/crier-fleet/keys/task-router.key` (PKCS#8 PEM, mode 0600),
written by `crier keygen` — the private half never left the box and never
appeared in a log.

## 3. Bus → ingress → Hermes gateway `/v1/responses` SSE → bus

Delivered to the `task-router` inbox (guard verdict `allow`), drained by the
poller:

```
$ ... router_ingress.py poll --once --limit 5
inbound f868f543a04bc366913a5d0d -> hermes-gateway-local [ok] 3.219s
```

The reply, read back from the target inbox:

```
$ tr236_bus.py recv orchestrator
inbox orchestrator: 1 message(s)
--- message id=2aec635c9f899b1971d84755 sender=task-router
{"in_reply_to": "f868f543a04bc366913a5d0d", "endpoint": "hermes-gateway-local",
 "protocol": "hermes-gateway", "transform": "hermes-gateway/v1-responses+sse",
 "ok": true, "outcome": "ok", "reason": "extracted from output",
 "reply": "TR236-INGRESS-OK", "seconds": 3.219, "tokens_in": 51239, "tokens_out": 49,
 "session": "tr236-e2e-1"}
stats: {"queue_depth": 0, "leased_count": 0, "oldest_age_ms": 0}
```

`in_reply_to` matches, the answer is the model's, and the inbound message is
acked — the inbox is back to 0 queued / 0 leased.

## 4. The model choice is the router's — and the ledger says which model RAN

Message carrying `"profile": "P1_CODING"`:

```
inbound 880ab2e74f7f1f121dd6007f -> hermes-gateway-local [ok] 2.487s
reply: "TR236-PROFILE-OK"
ledger: model_chosen = qwen/qwen3.7-flash:free   model_served = qwen/qwen3.7-flash:free
```

The pair came from `router_spawn.py` (the same resolver the scheduler and the
proxy use), was injected into the request, and the endpoint echoed it back — so
"we asked for it" and "it ran" are the same, stated fact.

Through the classified proxy (the card's "poller → local proxy" path):

```
inbound 4dcf41d572d3dc165d15fc7c -> router-proxy-local [ok] 4.492s
ledger: model_chosen = qwen/qwen3.7-flash:free
        model_served = meta/muse-spark-1.3-contributor:free
```

Configured versus served, both recorded: that is the difference between a ledger
that can diagnose a misroute and one that agrees with itself.

## 5. A non-Hermes shape, exercised (webhook → live sink)

```
$ router_ingress.py forward --message-file webhook-msg.json
inbound file-webhook-1 -> endpoint=webhook-sink-local outcome=accepted ok=True in 0.008s
reply_to=orchestrator ok=True reason=reply rule 'webhook-separate' (the answer arrives as its own bus message)
```

The sink (a plain `http.server` on 127.0.0.1:9411) received the transformed
payload:

```
{"path": "/hook", "body": "{\"in_reply_to\": \"file-webhook-1\", \"sender\": \"orchestrator\",
 \"session\": \"tr236-wh-1\", \"prompt\": \"POST this to the non-Hermes sink\", \"reply_to\": \"orchestrator\"}"}
```

No inline answer exists, and the reply says so instead of inventing an empty one.

## 6. The push door: authenticated, or refused by name

`serve --token-file … --require-sig` (ed25519 X-Agent-ID/Ts/Sig, the bus's own
scheme, verified with the bus client's own primitive):

```
1) no token, no sig   -> (401, {'ok': False, 'outcome': 'refused',
                                'reason': 'unauthorized: bad or missing bearer token'})
2) token, no sig      -> (401, {'reason': 'unauthorized: signed ingress requires X-Agent-ID/X-Agent-Ts/X-Agent-Sig'})
3) token + bad sig    -> (401, {'reason': "unauthorized: signature does not verify for 'task-router'"})
4) token + valid sig  -> 200 {"ok": true, "outcome": "ok", "in_reply_to": "http-1790973025325",
                              "endpoint": "hermes-gateway-local", "seconds": 1.859,
                              "reply_to": "orchestrator", "bus_delivery": "ok:orchestrator"}
```

and the reply did land on the bus:

```
--- message id=c13dc4eee174a7c8778c1399 sender=task-router
{"in_reply_to": "http-1790973025325", "reply": "TR236-PUSH-OK", "ok": true, …}
```

Every refusal above wrote its own ledger row (§7, rows 6–8).

## 7. The ledger — nine rows, all outcomes visible

```jsonl
{"cost_usd": null, "endpoint_requested": "hermes-gateway-local", "endpoint_served": "hermes-gateway-local", "http_status": 200, "idempotency_key": "tr236-1790972768", "inbound_id": "f868f543a04bc366913a5d0d", "ingress": "task-router", "model_chosen": null, "model_served": null, "null_reasons": {"cost_usd": "not reported by endpoint", "model_chosen": "no profile or project given (endpoint declared model=none?)", "model_served": "not reported by endpoint"}, "ok": true, "outcome": "ok", "protocol": "hermes-gateway", "reason": "extracted from output", "seconds": 3.219, "sender": "orchestrator", "session": "tr236-e2e-1", "tokens_in": 51239, "tokens_out": 49, "transform": "hermes-gateway/v1-responses+sse", "ts": "2026-10-02T15:26:14-0500"}
{"cost_usd": null, "endpoint_requested": "hermes-gateway-local", "endpoint_served": "hermes-gateway-local", "http_status": 200, "idempotency_key": "tr236-1790972804", "inbound_id": "880ab2e74f7f1f121dd6007f", "ingress": "task-router", "model_chosen": "qwen/qwen3.7-flash:free", "model_served": "qwen/qwen3.7-flash:free", "null_reasons": {"cost_usd": "not reported by endpoint"}, "ok": true, "outcome": "ok", "protocol": "hermes-gateway", "reason": "extracted from output", "seconds": 2.487, "sender": "orchestrator", "session": "tr236-e2e-2", "tokens_in": 51236, "tokens_out": 8, "transform": "hermes-gateway/v1-responses+sse", "ts": "2026-10-02T15:26:50-0500"}
{"cost_usd": null, "endpoint_requested": "router-proxy-local", "endpoint_served": null, "idempotency_key": "tr236-1790972804", "inbound_id": "b57a7ff6bcd55f383f5707b0", "ingress": "task-router", "model_chosen": null, "model_served": null, "null_reasons": {"cost_usd": "no successful hop", "model_served": "no successful hop", "tokens_in": "no successful hop", "tokens_out": "no successful hop"}, "ok": true, "outcome": "duplicate", "protocol": null, "reason": "idempotency_key already forwarded", "seconds": 0.0, "sender": "orchestrator", "session": "tr236-e2e-3", "tokens_in": null, "tokens_out": null, "transform": null, "ts": "2026-10-02T15:26:53-0500"}
{"cost_usd": null, "endpoint_requested": "router-proxy-local", "endpoint_served": "router-proxy-local", "http_status": 200, "idempotency_key": "tr236-1790972833-7026", "inbound_id": "4dcf41d572d3dc165d15fc7c", "ingress": "task-router", "model_chosen": "qwen/qwen3.7-flash:free", "model_served": "meta/muse-spark-1.3-contributor:free", "null_reasons": {"cost_usd": "not reported by endpoint"}, "ok": true, "outcome": "ok", "protocol": "openai-compatible", "reason": "extracted from json:choices.0.message.content", "seconds": 4.492, "sender": "orchestrator", "session": "tr236-e2e-3", "tokens_in": 51236, "tokens_out": 100, "transform": "openai-compatible/v1-chat-completions+json", "ts": "2026-10-02T15:27:20-0500"}
{"cost_usd": null, "endpoint_requested": "webhook-sink-local", "endpoint_served": "webhook-sink-local", "http_status": 202, "idempotency_key": null, "inbound_id": "file-webhook-1", "ingress": "task-router", "model_chosen": null, "model_served": null, "null_reasons": {"cost_usd": "no successful hop", "model_chosen": "endpoint declared model=none", "model_served": "no successful hop", "tokens_in": "no successful hop", "tokens_out": "no successful hop"}, "ok": true, "outcome": "accepted", "protocol": "webhook", "reason": "reply rule 'webhook-separate' (the answer arrives as its own bus message)", "seconds": 0.008, "sender": "orchestrator", "session": "tr236-wh-1", "tokens_in": null, "tokens_out": null, "transform": "webhook/raw-payload", "ts": "2026-10-02T15:27:46-0500"}
{"endpoint_requested": null, "endpoint_served": null, "inbound_id": null, "ingress": "task-router", "null_reasons": {"model_chosen": "refused before routing"}, "ok": false, "outcome": "refused", "protocol": null, "reason": "unauthorized: bad or missing bearer token", "seconds": 0.0, "transform": null, "ts": "2026-10-02T15:30:25-0500"}
{"endpoint_requested": null, "endpoint_served": null, "inbound_id": null, "ingress": "task-router", "null_reasons": {"model_chosen": "refused before routing"}, "ok": false, "outcome": "refused", "protocol": null, "reason": "unauthorized: signed ingress requires X-Agent-ID/X-Agent-Ts/X-Agent-Sig", "seconds": 0.0, "transform": null, "ts": "2026-10-02T15:30:25-0500"}
{"endpoint_requested": null, "endpoint_served": null, "inbound_id": null, "ingress": "task-router", "null_reasons": {"model_chosen": "refused before routing"}, "ok": false, "outcome": "refused", "protocol": null, "reason": "unauthorized: signature does not verify for 'task-router'", "seconds": 0.0, "transform": null, "ts": "2026-10-02T15:30:25-0500"}
{"cost_usd": null, "endpoint_requested": "hermes-gateway-local", "endpoint_served": "hermes-gateway-local", "http_status": 200, "idempotency_key": null, "inbound_id": "http-1790973025325", "ingress": "task-router", "model_chosen": null, "model_served": null, "null_reasons": {"cost_usd": "not reported by endpoint", "model_chosen": "message named no profile/project: the router is not asked to guess a pair (nothing is injected)", "model_served": "not reported by endpoint"}, "ok": true, "outcome": "ok", "protocol": "hermes-gateway", "reason": "extracted from output", "seconds": 1.859, "sender": "task-router", "session": "tr236-push-1", "tokens_in": 51254, "tokens_out": 7, "transform": "hermes-gateway/v1-responses+sse", "ts": "2026-10-02T15:30:25-0500"}
```

(Row 1 was written before the resolver's no-profile reason string was reworded;
rows 6–8 are the three refusals from §6 — a refusal is visible in the ledger,
which is the criterion.)

## 8. A finding the live run produced

A bus payload whose JSON carries `"prompt":` is refused **by the bus's own
guard**:

```
crier_client.CrierError: HTTP 403 (/agents/task-router/inbox): GUARD_BLOCKED
```

crier's `control_keys` prematch (`"(system|instructions|prompt|tools|schema)"\s*:`)
classifies it as a structured-object injection attempt. The same payload with
`"task"` as the prompt key is accepted (guard `decision: allow`). This is why the
fleet's dispatcher reads `task` first; it is recorded here because an ingress
that only ever tried `"prompt"` would look broken on the live bus while passing
every local test.

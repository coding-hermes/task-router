# TR-236 verification — 2026-10-11

This is a verification-only close. The implementation is already reachable from the published branch; no worker or new implementation was dispatched.

## Implementation witness

- `git show --stat --oneline c4687d6` shows the landed TR-236 ingress implementation: `scripts/router_ingress.py`, `data/endpoints.jsonl`, `tests/test_ingress.py`, `docs/tr236-ingress.md`, and the CLI/docs wiring.
- `git merge-base --is-ancestor c4687d6 HEAD` returned exit 0.
- Follow-up ingress hardening commits `5cbd6b5`, `f253900`, and `9102f68` are also in the current history.

## Acceptance verification

- `~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_ingress.py` returned `47 passed in 23.10s`.
- `~/.hermes/venvs/board/bin/python3 scripts/router_ingress.py endpoints` returned exit 0 and validated three shipped declarations: `hermes-gateway-local`, `router-proxy-local`, and `webhook-sink-local`.
- `docs/tr236-ingress.md` declares the endpoint data contract (`protocol`, `address`, `auth`, `reply`, `timeout_s`), the Hermes SSE and OpenAI-compatible transforms, the webhook non-Hermes shape, named refusal reasons, the ledger fields, per-lane admission/circuit bounds, and idempotency.
- `docs/evidence/tr236-ingress-live-2026-10-02.md` records the external live witness: Crier bus → Hermes `/v1/responses` SSE → correlated bus reply; a live webhook sink as the non-Hermes shape; authenticated/signed push handling; and ledger rows for success and refusal cases.

## Conclusion

TR-236's implementation and acceptance evidence are already present at current HEAD. This tick reconciles the stale pending board row; it does not re-judge the already-landed implementation or create duplicate code.

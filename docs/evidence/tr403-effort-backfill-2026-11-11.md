# TR-403 effort backfill — 2026-11-11

Branch: `tr-effort-backfill-1` (based on `origin/main` at `0937493b`).

## Traffic-ranked live lanes

Re-derived from `data/state/outcomes.jsonl` by exact `(provider, model)` count; malformed outcome lines (2) were excluded. Live means `archive`, `disabled`, and `valid_to` are all false/null in `data/tables/models.jsonl`.

| rank | provider/model | outcomes |
|---:|---|---:|
| 1 | deepseek/deepseek-v4-pro | 38,571 |
| 2 | opencode-go/mimo-v2.5 | 11,530 |
| 3 | xkiro/openai/gpt-6-luna | 7,391 |
| 4 | deepseek/deepseek-flash | 3,103 |
| 5 | fireworks-ai/accounts/fireworks/models/deepseek-v4p1-flash-1017 | 3,003 |
| 6 | deepseek-foreman/deepseek-flash | 2,082 |
| 7 | ollama-cloud/kimi-k2.7-code | 1,548 |
| 8 | ollama-cloud/glm-5.3-flash | 926 |
| 9 | ollama-cloud/kimi-k3 | 814 |
| 10 | xkiro/openai/gpt-6.1-terra | 773 |
| 11 | commandcode/gpt-6.1-sol | 766 |
| 12 | deepseek-duckbrain-sync/deepseek-flash | 586 |
| 13 | fireworks-ai/accounts/fireworks/models/glm-5p3-flash | 454 |
| 14 | xkiro/cohere/command-r7b-12-2024 | 407 |
| 15 | xkiro/openai/gpt-6.1-sol | 398 |
| 16 | opencode-go/glm-5.3-flash | 337 |
| 17 | stepfun/step-3.7-flash | 228 |
| 18 | xkiro/nvidia/nemotron-3-nano-omni | 214 |
| 19 | commandcode/tencent/hy4-preview | 205 |
| 20 | stepfun/step-5-preview | 200 |

## Capability / disposition

- `xkiro/openai/gpt-6-luna`: catalog explicitly accepts `none, low, medium, high, xhigh, max`; normalized to `off, low, medium, high, x-high, max`. Measured at all six non-default efforts.
- `xkiro/openai/gpt-6.1-terra`, `xkiro/openai/gpt-6.1-sol`: catalog explicitly accepts all six non-default efforts; skipped to preserve the hard cap after the first measured lane.
- `xkiro/nvidia/nemotron-3-nano-omni`: catalog accepts `off,on`; `off` is the only closed-vocabulary non-default effort. Measured at `off`; `on` is the provider's default, not a closed-vocabulary non-default level.
- `xkiro/cohere/command-r7b-12-2024`: catalog says `reasoning=false`; non-default reasoning efforts unsupported.
- `opencode-go/mimo-v2.5`, `deepseek/deepseek-v4-pro`, `deepseek/deepseek-flash`, `deepseek-foreman/deepseek-flash`, `deepseek-duckbrain-sync/deepseek-flash`, `fireworks-ai/*`, `ollama-cloud/*`, `commandcode/*`, and `stepfun/*`: provider catalog/rules did not expose a supported-effort contract for these exact lanes in this session. They were not probed; this is recorded as `unsupported/undetermined: provider contract absent`, rather than guessed support.

## Measured cells

The probe runner was extended to accept the closed effort vocabulary and send the provider wire aliases (`off -> none`, `x-high -> xhigh`). Each run used the existing v5 + agentic batteries (10 categories).

- `xkiro/nvidia/nemotron-3-nano-omni`, `off`: 10 rows added (agent_tick, delegation, schema, long_doc, long_horizon, test, tool_use, code_gen, reasoning, debug).
- `xkiro/openai/gpt-6-luna`, each of `off`, `low`, `medium`, `high`, `x-high`, `max`: 10 rows per effort, 60 rows total.
- Total rows added: **70**; all carry an explicit non-default `reasoning_effort`.

## Spend control

Cap: **$3.00**. Probes were run cheapest-first among catalog-confirmed candidates: xkiro's `gpt-6-luna` and Nemotron lanes. The probe harness does not return per-request billing, and no pre-run usage snapshot was captured, so exact actual spend is **not observable from this run**. The API's post-run usage endpoint was reachable, but is an aggregate account window. With the runner's 3,000-token maximum and the catalog prices, the measured request set has a conservative upper bound below $0.10, well below the $3 cap; exact actual is intentionally not fabricated.

## Skips

- Missing provider key path in this session: deepseek, deepseek-foreman, ollama-cloud, fireworks-ai. No probes attempted.
- No exact model match in the live provider catalogue: opencode-go/mimo-v2.5. No probe attempted.
- No effort contract in provider catalogue/rules: commandcode, stepfun, deepseek variants, fireworks variants, ollama variants. No effort probe attempted.
- Explicit non-reasoning lane: xkiro/cohere/command-r7b-12-2024. Non-default efforts unsupported.
- Cap-preserving skip after measured lanes: xkiro/openai/gpt-6.1-terra and xkiro/openai/gpt-6.1-sol. Their efforts are supported, but probing them would consume the reserved cap without a pre-run billing snapshot.

## Verification

- `python3 scripts/model_data_commit_gate.py --scan-tree`: **PASS**, 278 usability defects; no new defect was introduced by the scan.
- `python3 -m pytest -q tests/test_tr398_effort_axis.py`: **1 failed, 8 passed**. The single failure is the pre-existing pinned expectation that every committed benchmark row is `default`; it conflicts with TR-403's required non-default rows. The effort validation tests themselves passed.
- `git diff --check`: **PASS**.

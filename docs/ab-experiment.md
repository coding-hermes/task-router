# TR-295 — the A/B harness: rank lanes by measured $ per PASSED task

Owner's target, 2026-10-03: cost per token cannot rank models because it
assumes every model spends the same tokens and succeeds equally often. The
ranking this harness produces is:

```
cost_per_passed_task = (sum of attempt costs on the lane, from real metered
                        tokens x that lane's verified price)
                     / (independently passed tasks)
```

with `cost_per_attempt` and `success_rate` always named separately, so a
cheap-but-failing lane cannot look best. Terminal-Bench/DeepSWE set the prior
only; pass/fail comes from the task's own acceptance criteria, never from the
model claiming success.

## Files

- `scripts/router_ab.py` — the harness (offline-first; see below)
- `tests/test_router_ab.py` — the offline behavior suite (21 cells, all offline)

## The frozen task set

One run executes the SAME 4 lane-tasks on every provider/model pair
(`frozen_tasks()` in the script): `ab-fix-null-reason`, `ab-regex-allowlist`,
`ab-tz-window-fix`, `ab-band-pooling-note`. Each carries an explicit
acceptance command (`grep`/`python3` over the attempt's artifact) and the raw
category levels that drive the band key.

## Modes

- **Default (dry-run)**: a deterministic fixture executor, no model/provider
  API call, no production ledger write. Output goes only to the `--out` /
  `--report` paths you name.
- **`--execute`**: the real run. Refuses unless given >= 4 explicit
  `--lane provider/model` pairs AND `--executor-script` — a meter-reading
  wrapper whose served-model identity and tokens come from state.db
  `session_model_usage` (billing_provider + billing_base_url + model), never
  from the requested flag. **Not run by the worker; it waits for foreman
  review** (it consumes quota/billing).

## The requested-vs-served mismatch rule (the xkiro trap)

Measured on this fleet: a request for `openai/gpt-6-luna` @ xkiro silently
served `z-ai/glm-5.3-flash`. The harness therefore records BOTH the requested
pair and the actual served pair (plus `billing_base_url`) per attempt, and:

- flags the mismatch (`requested_served_mismatch: true`, `exclusion_reason`),
- EXCLUDES the sample from the requested lane's bucket (no pass, no cost),
- never counts a GLM run as Luna.

## Missing is never 0 / the sample floor

- No verified price for a lane, or a missing token meter, leaves `cost_usd`
  NULL with a `cost_basis` reason string; the lane reports unmeasured, never
  ranked at $0.
- Below the sample floor (default 3 usable attempts, `--sample-floor` to
  change) a lane reports unmeasured with the reason — same rule as the
  resolve path: unknown is not cheap.
- Zero independently passed tasks ⇒ unmeasured (`zero independently passed
  tasks`), regardless of cost.

## The GLM cost anomaly

The board re-derivation put `z-ai/glm-5.3-flash` at $1,344.97/task against
sibling `glm-5.3-flash` at $0.0013/task — a ~10^6 contradiction that is either
a unit bug or a pricing defect. The harness hard-excludes that lane unless a
`--price-source` file supplies the entry WITH a verified `source` and `basis`
naming where the price came from; the exclusion is recorded with the evidence,
never silent.

## Output / TR-299 ingestion

Attempt rows are appended through `router_outcomes.append_rows` (dedupe key
`source_system`/`session_id`/`model`, so re-running the same run id is
idempotent) and carry everything TR-299 needs per row: task id, attempt id,
requested/served pair, billing base URL, `complexity_levels`,
`complexity_sig`, versioned `complexity_band` (TR-289's `band_key()`,
imported — not reimplemented), `band_version`, tokens in / cache-read / out,
API calls, wall time, `price_source`, `cost_usd`, `cost_basis`, acceptance
command/result, `passed`, mismatch flag/exclusion reason.

**This runner does NOT update the live rolling averages.** That requires the
TR-299/TR-289 integration; wiring rows into the averages is out of scope here.

Production paths are REFUSED as outputs: anything under
`~/task-router/data/state/` is rejected.

## CLI

```
python3 scripts/router_ab.py \
  --out /tmp/ab/rows.jsonl --report /tmp/ab/report.json \
  --price-source /tmp/ab/prices.jsonl \
  --lane fixture/model-a --lane fixture/model-b \
  --lane fixture/model-c --lane xkiro/openai/gpt-6-luna \
  --run-id ab-20261003

# real run (after foreman review; consumes quota):
python3 scripts/router_ab.py --execute \
  --executor-script /path/to/meter_reading_wrapper.py \
  --price-source /path/to/verified_prices.jsonl \
  --out ... --report ... \
  --lane deepseek/deepseek-v4-pro --lane ... (>= 4)
```

## What was and was not run

The worker ran ONLY the dry-run fixture mode and the offline test suite. No
real A/B experiment has executed, no quota was consumed, and no live rolling
average was updated. The measured run needs foreman-approved lanes, verified
price sources and a meter-reading executor script.

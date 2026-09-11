# Pitfall record 2026-09-11 — stale probe defaults, mirror-provider rule gaps, and a sub billing gate

Three findings from the daily data-quality run. All three are data-layer classes;
no code changes were needed (and none should be made without re-reading this).

## 1. Probe `default_model` is a stale-id trap after upstream renames

**Symptom:** `neuralwatt` and `crof` reported provider-level DOWN (HTTP 404) on
`deepseek-flash` for days while 10/11 sibling lanes answered 200 in the same
hourly snapshot. The auto 404 scanner flagged them "UNEXPLAINED — likely
transient/auth" every run.

**Root cause:** DeepSeek renamed `deepseek-flash` → `deepseek-v4-flash`
(fleet-wide rename, TR-038 fallout). `probe_providers.jsonl` still carried the
dead short-id as each provider's `default_model`, so the probe's own headline
entry 404'd forever. Same-hour evidence: `deepseek-v4-flash` → 200,
`deepseek-flash` → 404, both providers, several consecutive runs.

**Diagnosis trick:** provider status DOWN with a 404 on ONE model while
`model_stats` shows N-1/N OK in the same snapshot = the DOWN entry is the
probe's own default, not a lane failure.

**Fix (data):** updated both `default_model` values + appended probe_gaps
resolution rows. Never "fix" by editing probe output.

## 2. Mirror-account providers need their own `provider_rules` rows

**Symptom:** the probe 404 scanner re-flagged all 11 opencode-go-2 lanes as
UNEXPLAINED every run since 09-07, despite the session-header contract being
documented in `provider_rules` — under `opencode-go` (lane 1).

**Root cause:** the scanner's premise-false detection matches provider_rules
rows by EXACT provider name. Mirror-account providers (`opencode-go-2`,
`-dogfood`/`-qa` siblings generally) inherit the CONTRACT but not the RULE ROW.

**Fix (data):** appended `opencode-go-2/session-header`. Doctrine: any
provider_rules row written for a lane-1 provider must be evaluated for its
mirrors at write time.

**Related (same provider, already documented):** `deepseek-v4-flash` and
`muse-spark-1.3-contributor` answer 403 on opencode-go-2 = meta-model/
product-surface-only (mirror of the 09-09 lane-1 exclusion) — not new gaps.

## 3. ollama-cloud HTTP 402 fleet-wide = billing gate, not a probe artifact

**Timeline:** first 402 in health.jsonl history at 2026-09-10T23:00:02Z (line
227); all 10 lanes DOWN 402 on every hourly run through 09-11 10:00Z (~35
lines). Zero OK lanes before, zero after.

**Reading:** the grandfathered Max $100 plan went payment-required overnight.
No balance endpoint exists (credits source: none), so the fleet cannot
self-verify plan state — human must check ollama.com billing (card / plan
status / top-up). Lanes keep their bucket-plan estimate prices; the health
gate blocks admission while DOWN.

**Chain impact (measured 09-11):** contained — heads are kimi-for-coding/
k3-256k (muster/uhlp/hermes-dagger/duckbrain-sync) and neuralwatt/glm-5.2-
short-fast (coding-hermes-scheduler). duckbrain-sync + hermes-dagger lost
ollama fallback depth only.

**Rule:** a uniform HTTP 402 across every lane of a subscription provider,
starting at one instant, is a billing event — escalate to the human, don't
queue it as lane work.

# Health coverage: the providers the gate cannot see

`~/.hermes/model-router/health-state.json` is a **gate input**. `router_spawn.py`
reads `providers.<p>.status` per hop and treats an **absent** provider as
un-gated — `hs.get(prov, {})` in the hop loop, and the same shape in the
fallback filter. Fail-open is deliberate (a missing probe must never block the
fleet), but it has a sharp edge: a provider that is missing from the probe's
enumeration is *silently* un-gated. That is the same defect class as a gate that
can never fire.

## The incident that named the class (2026-11-16 → 2026-11-21)

`neuralwatt` returned HTTP 402 `Subscription overage spending cap reached` on
every chat call. `health-state.json` had **no neuralwatt entry at all**, and
`data/tables/probe_providers.jsonl` had no row for it either, so the hourly
probe never pinged it and the resolve-time gate had nothing to filter on.
38 lanes stayed in chains (83 hops across 9 of 11 profiles) from the
cancellation until the lanes were disabled by hand (`72f339f9`).

## What the check counts

`scripts/health_coverage_check.py` applies three filters in order, so the report
names exposure rather than noise:

1. **Active lane** — a `models.jsonl` row that is not `disabled`, not `archive`,
   not retired (`valid_to` in the past), whose `providers.jsonl` row is not
   `archive`. Same eligibility the resolver uses.
2. **Routable** — the provider passes the TR-203 policy gate: a
   `quota-state.json` `providers` row with `status == "open"`. A provider absent
   from `quota-state.json` is already excluded by the router
   (`policy-gate-missing-row`), and a `gated` row is skipped, so neither is
   exposure.
3. **Uncovered** — no `providers.<p>` entry in `health-state.json`. **This is
   the gap.**

Each gap is classified so the fix is obvious: `probe-row` (the probe has
credentials and never covered it), `alias` (another probe row shares the same
`base_url`), `registry-resolvable` (`providers.jsonl` carries `api_base_url` +
`api_key_env`), or `unprobeable` (no bearer-key HTTP surface — e.g. SigV4).

Exit codes: `0` every routable provider is covered, `1` at least one gap, `2`
the check could not look (no models table, no health-state). A check that cannot
look never reports PASS.

```bash
python3 scripts/health_coverage_check.py          # human report
python3 scripts/health_coverage_check.py --json   # machine report
```

## Measured 2026-11-21

```
🩺 Health coverage — 22 routable providers of 24 active · 29 health entries
🕳️  ACTIVE BUT UNPROBED (1)
    muse-code  2 lane(s)  [alias] same base_url as probed provider(s) meta-model
⚪ not routable, so not a gap (2): aws-bedrock (policy gate gated), grok-build (policy gate gated)
⚪ no active lane, so not a gap (6): crof, gw-deepseek, myrouter:zai-glm, neuralwatt, sambanova, xkiro-2
```

`muse-code` is the one live exposure: `providers.jsonl` gives it
`https://api.meta.ai/v1`, the same endpoint the probed `meta-model` row uses, so
the fix is one data row with a verified default model — not code.

## Decision: the probe does NOT auto-enumerate from the registry

`probe_providers.jsonl` stays the enumeration source. It carries facts the
registry does not:

- the **default model that actually answers** (a dead default makes the hourly
  probe stamp the whole provider DOWN — the 2026-10-18 law — which gates live
  lanes on a harness artifact);
- per-provider **contract headers** (opencode-go answers HTTP 400
  `MissingSessionID` without `x-opencode-session`);
- the **wire-id form** (`wire_model_id`, TR-233).

Synthesizing a probe row from `providers.jsonl` alone would ping a guessed model
id and could gate healthy lanes. Instead the gap is **asserted every run**:
`provider_health_probe.py` prints the `ACTIVE BUT UNPROBED` section in its
hourly report and carries the list in `health-state.json` under
`unprobed_routable` (additive key; consumers that do not know it ignore it).
Closing a gap is then a deliberate one-line data row with a verified default
model.

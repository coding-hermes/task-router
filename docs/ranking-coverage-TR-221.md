# Ranking coverage — TR-221 backfill waves (2026-10-11)

Measured before/after census for the TR-221 ranking backfill. Every number in
this file is tool output, not an estimate: the census comes from
`router_tier_probe.py` (`category_census()` over the committed tables), the
unsatisfiable ratio from `router_tier_coverage.py` over the same frozen
2079-row `rated-nohops` ledger sample, and the reason-coverage count from the
harness's own stamp bookkeeping.

## What ran

| wave | providers | lanes | battery calls | result rows added | dead-id gaps filed |
|---|---|---|---|---|---|
| pilot | groq, deepseek | 4 | 4 batteries (v5, agentic, extended2, extended3) | 12 | 0 |
| A | commandcode, commandcode-2, xkiro, xkiro-2 | 57 measured (+10 dead) | same | 64 | 10 |
| B | commandcode, commandcode-2 (fill via `--models`) | 86 | same | 146 | 18 |

One source key per measured category throughout (the 67d553c rank-fidelity
rule): every ingested row carries exactly one of `T1-TOOL, T2-CODE, T3-REASON,
T5-DEBUG, AGENT-TICK, DELEGATION, SCHEMA, LONG-DOC, LONG-HORIZON, TEST,
GUARD-V2, REVIEW-V2, MECHANICAL-V3, MULTILINGUAL-V3, SPEC-DOCS-V3, MOCK-V3` —
the keys derive from `router_probe_ingest.BATTERIES`, so the seed's
BENCH_OVERLAY wires each score to exactly its own category.

Cost per model (documented, per AC): 12 chat calls per lane, max_tokens=3000
each ⇒ worst case ~36K output tokens/lane ≈ $0.11 at a $3/M sticker; measured
lanes emit ~10x less (deterministic short answers), so typical $0.01–0.04/lane;
`:free` lanes $0; dead ids cost one ~0.1s 404 each. Provider-level worst-case
estimates print with every run (`--dry-run` is free).

## Coverage census — active lanes tiered per category

| category | before | after | delta |
|---|---|---|---|
| agent_tick | 186 | 189 | +3 |
| delegation | 186 | 189 | +3 |
| schema | 187 | 190 | +3 |
| long_doc | 161 | 164 | +3 |
| long_horizon | 140 | 143 | +3 |
| test | 239 | 242 | +3 |
| **guard** | **112** | **134** | **+22** |
| **review** | **98** | **119** | **+21** |
| **mechanical** | **27** | **61** | **+34** |
| multilingual | 103 | 126 | +23 |
| spec_docs | 66 | 95 | +29 |
| mock | 110 | 129 | +19 |
| **TOTAL** | **1615** | **1781** | **+166** |

The board row's AC — mechanical ≥ 60 lanes tiered (from 18 at row-writing
time; 27 at wave start on the current ledger) — is met: **mechanical = 61**.
guard/agent_tick raised by named amounts: guard +22, review +21; agent_tick
+3 (its long-standing coverage comes from the 09-27 muse battery; only 3
lanes in this wave scope lacked it).

`terminal` is NOT in the live-probe wave set by design: a text-only
chat/completions probe cannot measure a terminal session. Terminal evidence
arrives via Terminal-Bench research rows in `benchmarks.jsonl`
(BENCH_OVERLAY `Terminal-Bench → terminal`); fabricating a chat-probe tier
for it would be the exact over-ranking the 09-27 floor-test exclusion exists
to prevent. Terminal stands at 45 active lanes tiered (tier ≥3: 11).

## Unsatisfiable ratio — honest ledger

| metric | before waves | after waves |
|---|---|---|
| rated-nohops sample | 2079 rows | same 2079 rows (frozen) |
| ELIGIBLE-unsatisfiable | 766 = 36.8% | 765 = 36.8% |
| REGISTRY-unsatisfiable | 764 = 36.7% | 763 = 36.7% |

The ratio did NOT move materially, and the attribution is measured, not
hand-waved: **728 of the 765 unsatisfiable rows miss exactly one category —
`terminal` at level 3** — and 732 of 765 would clear if `terminal` were
dropped from their conjunction. Today's waves measured the four categories
the row names, but the unsatisfiable population is concentrated on the one
category no live probe can reach. Closing it needs either (a) Terminal-Bench
research rows for the 26 active lanes sitting at terminal tier 1–2, or (b) an
owner decision to relax the terminal ask (the tool's `--relax terminal:2`
simulation quantifies that path on the same frozen sample). Neither is a
worker decision; both are now precisely scoped.

## Reason coverage — no unexplained NULLs on live rows

Every lane in the wave scope (active + priced + missing ≥1 wave category)
now carries either measured tiers or an explicit reason row in
`data/null_reasons.jsonl` (`field: model_tier`, the TR-197 sidecar):

| state | lanes |
|---|---|
| measured this wave (benchmarks rows dated 2026-10-11) | 31 models / 222 rows |
| reason stamped: `unreachable-provider` | 0 |
| reason stamped: `no-sample-yet` | 552 |
| **wave-scope lanes without either** | **0** |

Openrouter (332 lanes), aws-bedrock-class providers (99 no-credentials), and
the transport-skip providers (51) are inside the 552 `no-sample-yet` stamps
with their exclusion class named in the harness output (`--dry-run` prints
the full exclusion census: dead-id 18, no-credentials 99,
unprobeable-provider 51, probe-excluded 1, lifecycle states 1134). The
remaining waves to actually MEASURE those lanes are the same one command:
`router_tier_probe.py --commit --providers openrouter ...` (worst-case $76,
typical ~$8 — priced lanes; the 16 `:free` lanes are $0).

## How to reproduce

```
# before census (free)
~/.hermes/venvs/board/bin/python3 scripts/router_tier_probe.py --dry-run \
    --save-census /tmp/census-before.json
# a wave
~/.hermes/venvs/board/bin/python3 scripts/router_tier_probe.py --commit \
    --providers commandcode,xkiro --stamp-reasons
# rebuild the derived tables (the only path tiers change through)
~/.hermes/venvs/board/bin/python3 scripts/router_seed.py
# after census
~/.hermes/venvs/board/bin/python3 scripts/router_tier_probe.py --dry-run \
    --baseline-census /tmp/census-before.json
# unsatisfiable ratio
~/.hermes/venvs/board/bin/python3 scripts/router_tier_coverage.py
```

Side effects landed with this file: the probe runner's per-battery isolation
fix (one task's 400 no longer poisons the lane's other batteries — measured
live on groq/gpt-oss-20b, which answers T1-TOOL with tool-call JSON and 400s),
the harness (`scripts/router_tier_probe.py`), its tests
(`tests/test_tier_probe.py`), 222 benchmark rows, 28 typed `probe_gaps`
(dead ids), and 552 `model_tier` null-reason stamps.

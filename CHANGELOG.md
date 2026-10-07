# Changelog — task-router

All notable changes to task-router are documented here. The project follows
Conventional Commits; commit subjects are the source of truth (this file is
curated from the git log, not generated).

## [Unreleased]

### Added

- **Verified outcome feedback leg (TR-299)**: `scripts/verified_outcomes.py`
  projects every completed task outcome into
  `data/state/outcomes-verified.jsonl` under the lane that ACTUALLY served it
  (derived from the immutable `billing_base_url` host, never the re-stamped
  `billing_provider` label), with the exact token/call/wall-time meter and a
  pass/fail taken from the board row's independent acceptance evidence — not
  the worker's report. Side-purpose usage rows (title generation, approval,
  background review) never bill to the task lane. The write is an atomic
  projection: re-running a session is byte-identical (idempotent). The
  rolling averages gain `cost_per_passed_task_<N>h` (NULL with a reason at
  zero passes — cheap failures cannot read as cheap per passed task),
  `n_passed`, and an unranked-with-reason gate
  (`router_outcomes.ranking_verdict`) below the verified-sample floor;
  `outcomes_averages.py --extra-input` folds the verified store in, with
  verified rows superseding same-key billed-lane rows. Schema:
  `docs/outcomes-schema.md` §"Verified outcomes".

### Changed

- **Ingress admission and failure handling are now PER LANE** (SCHED-GAP-1713).
  The bus ingress (`router ingress`) no longer bounds forwards with one shared
  pool: every addressed endpoint gets its own in-flight budget, queue and circuit
  breaker. A lane that is hung, saturated or tripping can no longer refuse, slow
  or flood a peer; a lane that keeps failing is refused fast and loudly
  (`endpoint-circuit-open`) without firing at the target, and `GET /health`
  reports each lane's state. The legacy shared pool lives on as the explicit
  `Admission` class; the global cap is now an opt-in backstop
  (`ROUTER_INGRESS_GLOBAL_MAX_INFLIGHT`, default off). Contract:
  `docs/tr236-ingress.md`; design record incl. the measured finding that an agent
  container has no gateway to forward to: `docs/router-in-bunker-shortcut.md`.

## [0.2.0] — 2026-09-24

Minor bump from 0.1.0: 121 features, 85 fixes and 21 docs commits since the
project's first release-ready state, with no breaking CLI contract change —
the `router` entry point, its subcommands, and every documented flag keep
their 0.1.0 semantics.

### Added

- Complexity scoring on the spawn path: `router_spawn.py` now scores the
  TASK text, not just its profile — the derived signed matrix becomes the
  requirement list (TR-124, Bane design 2026-09-23), with fail-open
  degradation to the profile chain when the classifier is unavailable.
- Per-provider last-mile upstream routing on the proxy path (TR-118).
- OpenAI `user` field is read as a session fallback (TR-122).
- Classifier startup self-check + `timeout_s` pass-through (TR-119).
- Diversity two-knob caps + per-model concurrency skip (TR-007): caps on
  consecutive/total slots per provider with explicit exclusion reasons.
- Plan-window quota-exhaustion gates with auto-clearing `reset_at` (TR-060).
- Pricing cache end-to-end, plus the Sep-23 new-model classifications.
- GPT-6 Luna/Astra model set + OpenRouter pricing refresh and preset.
- Rate-limit resilience: a provider rate limit must not take the router
  down; `/v1/models` continues to serve (TR-121-adjacent hardening).
- Board merge hardening: JSONL board merge driver made order-independent
  and idempotent; renumbered board ids derived from content so every clone
  converges (2 commits).

### Fixed

- `router_validate.py` registry default now follows the seed/spawn repo
  convention, ending false INVALID verdicts on a healthy checkout (TR-106).
- A catalog refresh preserves a live row's `plan_tier`; only NET-NEW rows
  take the preset tier.
- Provider health probe endpoints/models calibrated to config ground truth
  (stepfun/minimax/synthetic/opencode-go corrected; explicit unsupported
  marking) (TR-001).
- Session ledger: requests without a declared session each stand alone;
  outcome dedup can no longer swallow a row.
- Spawn degrade-path regression: the fail-open test no longer depends on
  ambient machine state (fixture registry + hermetic state dir, same
  pattern as tests/test_diversity.py) — CI on a fresh checkout is green.

### Data

- Routing data slices: 09-24 probe evidence, 4-model research refresh,
  id-form-window contract; 09-23 price fills; provisional per-category
  capability for stealth/space-bunny-alpha.

### Infrastructure

- gitreins evaluator `max_input_tokens` 0.1M→0.5M (TR-001 judge hit the cap
  at 109k/100k) and `max_time` 20m→45m.
- CI gained `workflow_dispatch` so the release tag commit is CI-verified.

## [0.1.0] — 2026-09-21

Initial release-ready state: deterministic task router for the coding-hermes
fleet — capability profiles, eligibility filtering, price-ranked chains,
provider gates (quota / health / circuit), the `router` CLI entry point, and
the JSONL workboard (RELEASE-001 readiness sweep, 2026-09-21).

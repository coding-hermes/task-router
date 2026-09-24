# Changelog — task-router

All notable changes to task-router are documented here. The project follows
Conventional Commits; commit subjects are the source of truth (this file is
curated from the git log, not generated).

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

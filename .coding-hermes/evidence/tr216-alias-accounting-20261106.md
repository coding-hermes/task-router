TR-216 (phase 3) — alias inheritance on the REAL ledger + per-account accounting
Measured 2026-11-06, tree = main @ 5c79257 + this change. Owner dispatch:
"Implement alias inheritance and per-account accounting for TR-216."

WHY THIS PHASE EXISTED
  TR-216 shipped in 91f7c62 (L0 account keys + alias rows) and d3eb520 (seat
  fold on the real ledger). The ALIAS half was still unenforceable on the real
  path: `router_quota_accounting.account()` read `alias_of` ONLY from the limits
  config, and data/quota_limits.json carries no such map (`providers` is empty,
  no top-level `alias_of`), so the single source of truth the row names —
  data/tables/provider_quota.jsonl — never reached the view.
  Impact if an alias id had rows: its spend would report as its OWN budget while
  the parent's window stayed under-counted (over-stated remaining), i.e. the
  same window drawn down twice. Measured today 0 ledger rows under either alias
  id, so nothing was visibly wrong yet — the wiring was absent, not the data.

WHAT CHANGED (scripts/)
  router_quota_accounting.py
    * load_alias_map(): {alias: parent} from the committed L0 `alias_of` rows,
      read at query time (no second store). Uniform, non-empty, non-self only.
    * canonical_provider(): ONE resolver for seat folds AND alias hops, applied
      per row — chains resolve identically in any declaration order; a cycle
      folds nothing (never an arbitrary winner, never dropped spend).
    * account(): alias rows land in the PARENT block, `aliases` names what was
      absorbed (mirrors `seats`); a limits-config `alias_of` still overrides the
      table; an alias/seat-keyed config entry describes the parent's window
      instead of becoming a second budget or a limit-less one.
    * account() is now PURE: rows are copied before labeling (setdefault on the
      caller's dicts made the next query answer differently — a drifting view).
    * account_provider(): the no-limit reason names the ACCOUNT on split blocks.
    * _load_quota_table_rows(): tolerant per line — one torn tail used to blank
      the whole table, silently disabling every alias/account label.
  router_quota_panel.py   folds alias rows before grouping (derived window +
                          burn-surplus view cannot understate spend).
  router_spawn.py         TR-213 headroom: folds alias rows into the parent and
                          resolves an alias id to the parent key (fail-open
                          untouched; live guard is inert — 0 configured limits).

VERIFICATION — RED PROOF (new tests run against HEAD 5c79257, scratch worktree)
  13 failed / 61 passed with the fix's tests on the base tree; 0 failed with the
  fix. Failing on base: ac1_aliases_never_double_count_their_parent,
  ac6_l0_alias_map_declares_the_gateway_aliases,
  ac6_alias_rows_fold_into_the_parent_not_a_second_budget,
  ac6_alias_spend_is_counted_exactly_once,
  ac6_alias_chain_resolves_the_same_in_any_declaration_order,
  ac6_alias_cycle_folds_nothing_and_drops_nothing,
  ac6_the_view_is_pure_and_never_mutates_the_callers_rows,
  ac6_a_limits_config_alias_overrides_the_table_edge,
  ac6_an_alias_keyed_config_still_describes_the_parent_window,
  ac6_a_torn_tail_does_not_blank_the_alias_map,
  ac6_cli_resolves_an_alias_to_its_parent_block,
  test_quota_panel::test_alias_spend_counts_toward_the_parent_window,
  test_tr213_quota_ordering::test_alias_spend_counts_toward_the_parents_headroom

VERIFICATION — TESTS (board venv, ~/.hermes/venvs/board/bin/python3)
  tests/test_quota_alias_multiaccount.py            38 passed
  AC1 alias rows resolve + never double-count (parent block, `aliases` named,
      no alias budget) — data-level and view-level
  AC2 account keys per provider in the data; table still validates clean
  AC3 config-declared per-account split (xkiro, opencode-go) + legacy rows
  AC4 commandcode rollover declared in data (no-rollover / top-up never-expire)
  AC5 one entry per (provider, account) on the REAL table shape, for EVERY
      family the L0 declares (xkiro, opencode-go, commandcode), CLI breadth for
      all three, `seats`/`aggregate_display` unknown-with-reason
  AC6 L0-derived alias inheritance end to end (fold, exactly-once ledger
      coverage, chain order-independence, cycle safety, purity, config
      override, alias-keyed config, torn tail, CLI alias resolution)

VERIFICATION — LIVE (this box, real ledger data/state/outcomes.jsonl)
  $ router_quota.py accounting --provider xkiro --json
    entries=2 :: xkiro-1 spent=3.2123922 n=183 agg=unknown |
                xkiro-2 spent=0.0 n=0 seats=["xkiro-2"] agg=unknown
  $ ... --provider opencode-go --json
    entries=2 :: opencode-go-1 spent=0.01234771 n=2 | opencode-go-2 seats=["opencode-go-2"]
  $ ... --provider commandcode --json
    entries=2 :: seat-1 n=0 | seat-2 seats=["commandcode-2"]
    (never one summed row; every split block carries aggregate_display unknown)
  $ ... --provider gw-deepseek --json
    entries=1 provider=deepseek account=default      <-- alias resolved to parent
  $ ... --provider xkiro   (human view)
    xkiro/xkiro-1  rolling_5h spent=$3.2661 ...
    xkiro/xkiro-2 (+xkiro-2) rolling_5h spent=$0.0000 ...
  $ router_spawn.py task-router --format json        exit 0, chain intact (fail-open)
  $ router_quota_panel.py                            exit 0, 92 windows, 1 burn candidate

VERIFICATION — FULL SUITE (regression law)
  ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/
  run A: 2 failed, 2918 passed, 3 skipped in 679.17s
  run A attribution (both NOT this change):
    1) tests/test_validate.py::test_bare_run_at_repo_root_exits0_on_healthy_checkout
       -> "category_levels.jsonl is 433s newer than registry.json". A concurrent
       sibling tick rewrote data/tables/{category_levels,model_tier}.jsonl with
       BYTE-IDENTICAL content (git diff empty), moving only their mtime; the
       freshness check is mtime-based. Restored the invariant with
       `touch registry.json` (gitignored build artifact, content already
       current): the test then PASSES.
    2) tests/test_xkiro_catalog_freshness.py::test_non_archived_xkiro_models_are_in_live_catalog
       -> the 9 minimax lanes already filed as XK-404-001-R3 (HEAD commit
       5c79257: "9 xkiro minimax lanes absent from the live catalog").
       PRE-EXISTING fleet-data red, untouched by this row.
  run B (final tree, after the registry mtime restore):
      1 failed, 2919 passed, 3 skipped, 2 warnings in 679.44s (0:11:19)
      The single red is the pre-existing XK-404-001-R3 xkiro catalog drift
      (9 minimax lanes absent from the live catalog); tests/test_validate.py
      passes on this tree. No new failure was introduced by this change.

HONEST LIMITS
  - data/quota_limits.json has 0 configured providers, so TR-213's headroom term
    is INERT on this box; the spawn-side fold is proven by test, not by live
    traffic.
  - Live ledger currently has 0 rows under gw-deepseek / myrouter:zai-glm, so the
    live alias fold is proven by the CLI resolution probe + tests, not by
    observing real alias spend.
  - data/tables/provider_quota.jsonl was NOT edited by this change (no data fix
    was needed; the L0 already declares both aliases).

ch:trace row=TR-216 spec=docs/quota-layers-spec.md#2-l0--declaration-data-only \
  test=tests/test_quota_alias_multiaccount.py evidence=.coding-hermes/evidence/tr216-alias-accounting-20261106.md

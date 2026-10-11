# TR-237 verification — 2026-10-11

Worker tick 2026-10-11 (dispatched by the foreman; worker model glm-5.3-flash @
zai-glm). This is a verification-only close for the worker side: the implementation
that satisfies every acceptance criterion is already reachable from `main` in
commit `4c3be2e` (2026-10-07, "test(proxy): pin ledger-row attribution for declared
vs no-profile calls"), which landed both the regression suite and the proxy docs
this row asks for. No new implementation was needed; this tick proves the criteria
against current HEAD instead of re-implementing working code.

## Implementation witness

- `git merge-base --is-ancestor 4c3be2e main` → exit 0 (in main's history).
- `tests/test_proxy_declared_profile.py` (4 tests) pins the ledger-row contract:
  - declared arm — a Hermes-shaped body + `x-router-profile: P1_CODING` appends a
    REAL row (store not stubbed) with `complexity_source='declared'`,
    `profile_id == P1_CODING`, `degrade_reason: null`, and the classifier module
    poisoned so "never runs" is structural, not asserted-from-silence;
  - no-profile arms — `complexity_source='default'` (rating failed) /
    `'classifier-empty'` (rated, presses no category), `profile_id: null` (the
    cheap floor, never P0_FORE), `degrade_reason` naming the cause in one field
    (`unrated -> fail-cheap floor requirements (TR-139) | <classifier cause>`),
    floor matrix `-5` on every registry category in `required_categories`;
  - `/api/ui/ledger` returns + filters those fields per row (the UI flow
    drill-down in `scripts/router_ui_page.py` renders "rating source",
    classifier evidence, and degrade_reason from the same row).

## Acceptance verification (all commands run at HEAD this tick)

1. **Declared profile → recorded, not classifier-empty.**
   `python3 -m pytest -q tests/test_proxy_declared_profile.py` → `4 passed in
   1.08s`. `test_declared_profile_lands_on_the_ledger_row` proves the row records
   the caller-declared profile and the matching `profile_id`, with the poison
   classifier proving the classifier never ran.
2. **Attribution to the caller's profile, not the floor.** Same test asserts
   `profile_id == 'P1_CODING'` and `!= 'P0_FORE'`; RED-proof on record in the
   implementation commit message (mutating `_proxy_record`'s profile_id stamp
   fails exactly that test).
3. **Absent profile stays clearly recorded as floor/default with reason.**
   `test_no_profile_row_names_the_floor_and_the_reason` (rating failed →
   `default`, floor set, named degrade_reason) and
   `test_empty_matrix_row_is_the_other_named_no_profile_class` (rated-empty →
   `classifier-empty`, same floor, own reason). The UI face is pinned by
   `test_ui_ledger_shows_the_attribution_fields`.
4. **Both arms covered by tests.** The same 4-test file is the regression
   coverage; re-run this tick: `4 passed`.
5. **Docs: the OpenAI `model` field does not pin a lane; how a caller requests
   a specific hop.** `docs/integration.md` (proxy section, landed in the same
   commit) states: "The `model` field in the request body does NOT pin a lane" —
   it is forwarded verbatim as a hint, never a routing command — and explains
   the steering surface: `x-router-profile` declares complexity (attributed
   `complexity_source: "declared"` + `profile_id`, scorer skipped); a specific
   lane is requested by declaring a profile only that lane clears, or by sending
   the requirement set directly; `x-router-sort` / `x-router-window-h` /
   `x-router-max-hops` reorder/bound the chain; with no profile the prompt is
   rated and, absent a rating, the row names the no-profile class + cause.

## Test evidence this tick

- `python3 -m pytest -q tests/test_proxy_declared_profile.py` → 4 passed.
- `python3 -m pytest -q tests/test_doc_parity.py tests/test_proxy_classify.py`
  → 25 passed.
- `python3 -m pytest -q tests/test_proxy_driver_hermes.py
  tests/test_jev_scoring.py tests/test_envelope_step_count.py
  tests/test_degraded_path_contract.py` → 44 passed.
- Full suite: `python3 -m pytest -q tests/` (log `/tmp/tr237-full-suite.log`) —
  result recorded in the worker report / foreman close note.

No production service touched, no restart, no push. The historical board evidence
rows (`classifier-empty` / `default` with `profile_id=P0_FORE`) predate TR-139 +
4c3be2e; current HEAD is the corrected behavior.

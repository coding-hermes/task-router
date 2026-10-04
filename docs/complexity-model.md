# The complexity model — design authority

Status: DRAFT for owner review. This is the spec the owner asked for on 2026-09-19 ("we need to make sure we
have built all the complexity supports for each model… we want the model selector to first select which
models match based on complexity") and which, as of 2026-10-03, does not exist as a document. The model is
currently specified only implicitly, across `docs/integration.md` (18 KB) and `AGENTS.md` (2.8 KB).

Owner's requirement, restated over five weeks:
- 2026-09-19 — build the complexity supports; select models by matching complexity.
- 2026-09-23 — "not always just going to cheapest model just because it is in that list"; the goal was that
  routing "actually look[s] at complexity directly" instead of a fixed list.
- 2026-09-28 — the router uses an LLM or JEV, by setting, to fill the classifiers from the prompt, so the
  chain is dynamic and adjusting; "no more fixed lists like we been doing wrong."
- 2026-10-03 — callers pass **raw complexities** per task, not an assigned profile; the input prompt is what
  decides; the router can generate this in most cases; the board and the prompts align to it.

---

## 1. Vocabulary

- **Category** — a capability dimension with signed levels `-5..+5` (e.g. `code_gen`, `debug`, `tool_use`,
  `guard`, `agent_tick`, `creative`, `mechanical`). The registry is keyed by these; the registry is the
  authority for the list, not this document.
- **Raw levels** — a `{category: level}` map describing what a task *needs*. This is data, and it is the
  thing the owner wants callers to pass.
- **Requirement matrix** — the validated raw-level map after range/normalisation checks.
- **Profile** (`profile_id`, e.g. `P0_FORE`, `P1_CODING`) — a **named, pre-assigned set** of levels. A
  convenience and a policy handle. **It is not the selector.**
- **Band** — the canonical key derived from raw levels, used to key the rolling averages. Must be
  derivable from a task at resolve time.
- **Scorer source** — where the levels came from: `declared-raw`, `classifier`, `jev`, `default-floor`.

## 2. Where complexity comes from (precedence, highest first)

1. **Raw levels supplied by the caller** — chosen level map, `complexity_source = declared-raw`.
2. **Prompt classification** — the router reads the input prompt; `classifier` or `jev` per setting
   (`COMPLEXITY_SCORERS = auto | classifier | jev`), `complexity_source = classifier | jev`.
3. **Floor** — only when 1 and 2 both fail, `complexity_source = default-floor`.

Rules that bind this section:

- **R2.1** `profile_id` never overrides 1 or 2. A declared profile may only apply as policy: allow/deny,
  floor, ceiling.
- **R2.2** A missing input must never silently become the cheapest lane. The floor is permissive today
  (21 categories at `-5`), and a permissive floor resolves to the cheapest eligible lane. A request that
  reaches the floor carries `degrade_reason` naming the failure, and the row records it.
- **R2.3** Classification failure is a bug to fix, not a state to tolerate: `classifier-empty` accounted for
  221 of all-time proxy rows. Parse failures are recorded with the raw prompt length and the parse error so
  the failure is diagnosable rather than merely safe.
- **R2.4** Classification runs on every request, including a plain Hermes provider call. A caller carrying no
  task metadata is exactly the case that must be classified, not defaulted.

## 3. The band key

**Correction (measured 2026-10-03, after this section was first drafted).** The key and the join both
already exist: `complexity_sig` is the sha1 of the canonical `{category: min_level}` map (dict-order
independent, level-sensitive), and the resolve path already computes it for the task and matches it against
each lane's stored rows — `want_sigs` carries both the readable form and the sig, and `measured_basis()`
reports the match. What is missing is not the function but its **input and its coverage**:

- **145 averaged groups carry no sig at all**, because the traffic behind them was never rated (see R2.3 —
  the classifier returned no JSON on 502 of 747 unrated rows). No sig means no band, so those lanes can only
  ever be compared unbanded.
- **39 groups carry a NAME key** (`profile:P0_FORE`) rather than a level key — the fallback taken when the
  profile could not be resolved to levels (the registry was unavailable). That is a silent change of key
  space: a name-keyed average can never match a level-keyed task, and nothing said so. It must be NULL with
  a reason, or explicitly labelled as its own space.
- The join only ran when a measured sort was requested; since 2026-10-03 that is the default.

- **R3.1** The band key is a canonical, documented function of the requirement matrix, stable across runs and
  machines, and the same function is used on the write side (averages) and the read side (task resolve).
- **R3.2** The band key is human-readable enough to be shown in the UI (`why this lane` must be able to name
  the band).
- **R3.3** Changing the derivation is a versioned event: a new band-key version namespaces new averages, and
  old rows remain readable under their own version.

## 4. Ranking (how the model is chosen)

- **R4.1** Rank by **measured cost per task** where the measurement clears a floor and a coverage bar
  (existing: `measured_basis()`, floor 3 samples, coverage bar per TR-183). Price ranks only where
  measurement is thin.
- **R4.2** **Success rate participates.** A lane with excellent measured cost and a poor measured success
  rate for the band does not take the head position for that band.
- **R4.3** Every hop carries its **basis**: measured (with `n_samples`, window, success rate) or
  `fell_back_to_price` with the reason. Unmeasured is never reported as measured-cheap.
- **R4.4** Quota- and health-gated lanes are filtered at resolve time as today; the measured ordering is
  applied to what survives the gates.
- **R4.5** Staleness: an average whose window holds no recent samples is treated as unmeasured, and the
  resolve states which windows were used.

## 5. Recording (what every request must log)

- **R5.1** the raw requirement matrix, and `complexity_source` with the reason when it is not level 1/2;
- **R5.2** the band key;
- **R5.3** the chain as ordered (with `chain_length`, `max_hops`, `hops_attempted`);
- **R5.4** the hop actually served by (`served_by_hop`) and the **model that actually ran**, plus provider;
- **R5.5** tokens (in/out/reasoning), cost with its basis (`price_basis`), and wall time;
- **R5.6** skipped hops and exclusions with the reason each was skipped.
- **R5.7** A field that cannot be measured is `null` with a stated reason. Never a fake zero; a plan-covered
  lane may report its offset price but never "free".

## 6. The board's side of the contract

- **R6.1** A row can carry raw levels per category, not a scalar. Today `boardctl create` takes
  `--complexity N` and live rows contain `"complexity": "moderate"` in a numeric field.
- **R6.2** The scalar, if kept, is derived for ordering only; it is not the routing input.
- **R6.3** Prompts (foreman, worker, satellites) instruct: pass raw levels per task; do not pass an assigned
  profile.
- **R6.4** The routing decision for a row is replayable: given the row, the requirement matrix in force, and
  the averages at that time, the same chain is derivable.

Built state (TR-292, 2026-10-03): a row carries the raw map in `required_categories`
({category: level}, int −5..+5 — the resolver's existing channel; nothing new invented). The
write side is `scripts/board_row_levels.py` (normalize / validate); the scalar `complexity` is an
int 0..5 for ordering only and is type-enforced so the string drift ("moderate" x13) cannot return.
`router spawn --profile-from-board` reads the row's raw levels as `complexity_source=declared-raw`
— no classifier call — and a row so resolved follows the same chain as the identical levels on
`--profile-req` (replayability, pinned in tests/test_row_raw_levels.py). The shipped prompt text
(skills/task-router-usage) instructs: pass the raw levels of THIS task; never an assigned profile.

## 7. Non-goals

- Not a cost-only router: the owner's requirement is capability matching first, cost within it.
- Not a provider-selection rewrite: gates (quota, health, circuit) keep their current semantics.
- Not a replacement for the classifier's prompt contract; this spec governs the levels it returns.

## 8. Compliance

A change complies with this spec when a task, resolved on live traffic, can be shown to carry: a
non-floor complexity source; its raw levels; its band key; a chain whose head is justified by measured
cost/task and success rate where measurement exists; and the model that actually ran. Verified by an outcome
probe on the live ingress, not by reading this document.

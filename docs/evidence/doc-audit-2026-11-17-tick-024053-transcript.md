# task-router documentation audit — frozen probe transcript

tick: task-router-docs-2026-11-17-02-40-53 (re-fire of the 02:40:53 delivery)
target: task-router
audit revision: origin/main a2b44fd7d637abe618b89735845121e89d4122ff
probe root: clean detached worktree at that revision (no foreign dirty paths)
produced by: the task-router-docs lane, 2026-11-17
note: this file is a command transcript, not a report. No trace markers are
authored here; the rows cite this file.

## 0. Revision and cleanliness

    $ git -C <probe-root> rev-parse HEAD
    a2b44fd7d637abe618b89735845121e89d4122ff
    $ git -C <probe-root> status --porcelain
    (empty)

The canonical checkout /home/kara/task-router is NOT at this revision (it was
103 commits behind origin/main and carried 7 unpushed commits plus 8 dirty
paths), so the audit and the board write were performed in the clean
published-tip worktree and never in the canonical checkout.

## 1. Finding A — /health block inventory vs docs/health-plane.md

Probe (T4): started the published-tip service read-only on a scratch port against
a scratch data dir with a read-only symlink to the published tables, then read
GET /health back from the live endpoint.

    $ cd <probe-root> && python3 health_probe_land.py
    served_tip_commit=a2b44fd7d637
    top_level_key_count=26
    top_level_keys=admission,cache_generation,chains_snapshot,circuit,classify_cache,code,commit,context_fidelity,coverage,data_cache,eligibility,gate,mode,probe,registry,registry_age,registry_state,runtime,service,serving,source_sha,stale,status,surface_mode,ts,unmeasured_categories
    artifact=<scratch>/health-probe-land/health.json

The daemon was started only for this probe and terminated by the probe script; it
is not a durable endpoint.

Documented inventory — docs/health-plane.md, the jsonc block in
"## 2. What `/health` guarantees" (lines 45-60), documents 14 top-level keys:

    $ cd <probe-root> && grep -nE '^\s{2}"[a-z_]+":' docs/health-plane.md | awk -F: '$1>=40 && $1<=60'
    46:  "status": "ok",
    47:  "service": "task-router/1.0",
    48:  "commit": "df592c4c2d09",        // running commit SHA ("" -> "unknown")
    49:  "mode": "read-only",
    50:  "ts": "2026-09-23T21:24:03+00:00",
    51:  "registry":      { /* DATA freshness: newest valid_from date + age_days */ },
    52:  "registry_age":  { /* FILE freshness: mtime of registry.json */ },
    53:  "gate":          { /* the `router validate` verdict, in-process */ },
    54:  "probe":         { /* hourly provider_health_probe state */ },
    55:  "circuit":       { /* circuit-breaker / policy-gate pairs */ },
    56:  "chains_snapshot": { /* newest data/state/chains/<date>.md */ },
    57:  "admission":     { /* TR-172: proxy admission counters + the ceiling */ },
    58:  "serving":       { /* TR-274: which serving path is active (see below) */ },
    59:  "coverage":      { /* SCHED-GAP-16942: how much of the chain is MEASURED */ }

Published minus documented (12 keys): cache_generation, classify_cache,
context_fidelity, data_cache, eligibility, registry_state, runtime, source_sha,
stale, surface_mode, unmeasured_categories, code.

Occurrence census in that same file (grep -c, whole file, 278 lines):

    eligibility 0 | classify_cache 0 | data_cache 0 | surface_mode 0
    cache_generation 0 | source_sha 0 | registry_state 0 | context_fidelity 0
    unmeasured_categories 0
    stale 17 (all inside the `registry_age`/`gate` narrative, never as the
    top-level block) | runtime 1 (line 248, `scripts/sync_runtime.sh` — not a block)

Contradiction inside the corpus: docs/configuration.md:193 states the
`ROUTER_ELIGIBILITY` gate's audit block is published — "/health plus
`scripts/router_usability_gate.py` publish the same census" — while
docs/health-plane.md, the file whose stated job is "What /health guarantees",
never names `eligibility`. scripts/router_server.py:927-937 is where the block is
attached.

## 2. Finding B — derived tables with no documented refresh path

Row counts at the audit revision:

    $ wc -l data/tables/benchmarks.jsonl data/tables/cost_effort.jsonl
       8286 data/tables/benchmarks.jsonl
       1035 data/tables/cost_effort.jsonl

Generators (source anchors):

    scripts/router_market_ingest.py:26   DEFAULT_OUTPUT = "data/tables/benchmarks.jsonl"
    scripts/router_market_ingest.py:251-255
        --models  --output  --queue-dir  --input  --date
    scripts/cost_effort.py:259           def main(argv=None):
    scripts/cost_effort.py:261-263       --check  --report  --dry-run

Absence census at the same revision (name-as-substring over the whole
reader-facing corpus, not an anchored pattern):

    $ grep -rn 'market_ingest' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' .
    (no output)
    $ grep -rn 'cost_effort\.py' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' .
    (no output)
    $ grep -nE 'benchmark|cost_effort|ingest' Makefile
    (no output)

`benchmarks.jsonl` is mentioned only as a data artifact or in run transcripts:
docs/catalog-freshness-2026-09-25.md:44, docs/decisions/TR-1000976681059248-seed-publish-atomicity.md:72,
docs/specs/SPEC-COMPLEXITY-COVERAGE.md:39 and :54, docs/ranking-coverage-TR-221.md:57,
docs/research/TR-100-weekly-lane3-closeout-2026-09-21.md:73,
docs/research/TR-037-weekly-lane3-closeout-2026-09-11.md:11 and :27.
None of them states how to rebuild the file.

`cost_effort.jsonl` is described as an artifact at docs/tier-cost-cube.md:15
(`| data/tables/cost_effort.jsonl | (provider, model, reasoning_effort) | ... |`)
and its producer is called only "the cost generator" at docs/tier-cost-cube.md:66.
The consumer side is scripts/router_spawn.py:741 `_effort_cost_index` /
:4592 `tables.get('cost_effort')`.

Why the staleness matters: docs/configuration.md:193 defines the eligibility gate
as admitting a lane "only if it carries a `normalized_price` AND at least one
measured capability row (`model_perf.perf` or `benchmarks.score` ...)", so a
stale benchmarks.jsonl silently shrinks the resolvable chain, and the /health
payload's `eligibility` block is what reports it.

Freshness state observed (informational, not a defect — honest-null is the
TR-400 design):

    $ python3 scripts/cost_effort.py --check
    rows=1027 costed=0 null=1027 comparable_pairs=0
    monotonicity: OK

(1027 vs the 1035 file lines is the generator's own accounting of parsed rows;
the gap is not part of this finding.)

## 3. Dedupe — published board owner search

    $ git show origin/main:.coding-hermes/board/tasks.jsonl | jq -r '... test("health-plane|health_probe|/health payload|health contract|benchmarks\\.jsonl|cost_effort|market_ingest|refresh path|derived tables?";"i")'
    TR-109  complete P3  provider_health_probe.py missing from sync_runtime.sh install list
    TR-164  complete P1  health probe: --only rewrites the whole provider map
    TR-223  complete P1  Ranking gaps with no measurement path
    TR-226  blocked  P2  clinepass lanes are unmeasurable
    TR-233  complete P2  clinepass lanes are unmeasurable
    TR-256  complete P1  RUNTIME DEPLOY DRIFT: sync_runtime.sh copy-list drift
    TR-342  complete P2  /tmp litter: every completed seed run drops benchmarks.jsonl.bak
    XK-404-001-R2 complete P2 recurrence of xkiro archival
    TR-357  complete P1  A price-0 lane sits at hop 0 of every foreman chain
    TR-376 / TR-378 complete P0 fleet-monitoring scrape job

No open row owns either mechanism (the nearest are closed: TR-109 the prober
install list, TR-342 the .bak litter). The published board's highest DOC id is
DOC-19.

## 4. ID minting

    $ python3 ~/.hermes/scripts/board_next_id.py <canonical>/tasks.jsonl DOC --count 2
    DOC-22
    DOC-23
    $ python3 ~/.hermes/scripts/board_next_id.py <published-tip>/tasks.jsonl DOC --count 2
    DOC-20
    DOC-21

The published-tip minter would reuse DOC-20/DOC-21 because those ids exist only in
the stale canonical checkout's unpushed commits (75c11adc, 3d4fae67 — same tick,
never pushed). DOC-22/DOC-23 are free on BOTH boards, so the filing below cannot
collide with those stranded local rows if the canonical checkout is ever rebased
and pushed. Verified free before the write:

    $ grep -c '"id": "DOC-22"\|"id":"DOC-22"' <published-tip>/tasks.jsonl
    0
    $ grep -c '"id": "DOC-23"\|"id":"DOC-23"' <published-tip>/tasks.jsonl
    0

## 5. Board write and validation

Board writes were executed with `boardctl -C <clean published-tip worktree>`; the
canonical checkout was never mutated. Validation is compared against a pristine
extraction of the same commit so a pre-existing lint population is not attributed
to this filing (recorded in the close-out below).

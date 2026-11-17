# task-router documentation audit — RE-FIRE re-verification transcript

tick: task-router-docs-2026-11-17-02-40-53 (second delivery; first delivery at 22:15 local filed the findings)
produced by: the task-router-docs lane, 2026-11-17/18 (local 23:34-00:05)
audit revision: origin/main c497fb4e590f86fc575fa16afe590cad8d8b9afc (tips 9e630627 -> c497fb4e both probed; see §8)
probe root: clean worktree /home/kara/.hermes/stand-in/docs/task-router/wt-land (detached at c497fb4e)
result: filed_rows: 0, amended_rows: 0 — this is a reconcile-and-report re-fire, not a new filing.
note: this file is a command transcript. It authors no trace markers; the owned rows
(DOC-22, DOC-23 on origin/main) cite their own evidence artifact.

## 0. Why this delivery files nothing

The 02:40:53 attempt delivered twice against two checkouts:

  a) canonical checkout /home/kara/task-router — 7 unpushed commits, 106 commits BEHIND
     origin/main, dirty paths (data/tables/*.jsonl, scripts/router_outcomes.py, untracked
     duckdb + probe logs). Its commits 75c11adc + 3d4fae67 carry DOC-20/DOC-21 at
     priority P4 — an off-vocabulary value this repo's push gate refuses (see §5).
  b) clean published-tip worktree — filed DOC-22/DOC-23 (identical findings, mapped to
     P3 + "P4 —" title per scripts/board_priority_normalize.py) and pushed:
     ca89c31d (rows) + 9e630627 (markers), both ancestors of the current tip.

So the findings are already owned on the published board. Nothing was re-minted and no
row was amended.

    $ git -C wt-land status --porcelain
    (empty before this transcript was staged)
    $ git -C /home/kara/task-router rev-list --left-right --count origin/main...HEAD
    106	7
    $ git -C /home/kara/task-router log --oneline origin/main..HEAD
    3d4fae67 board(task-router): add mandatory ch:trace markers to DOC-20 and DOC-21
    75c11adc docs(task-router): file DOC-20/DOC-21 (health contract inventory, table refresh paths) + re-triage DOC-5..DOC-9 to P4
    1895d33f ch(board): annotate TR-OF-20261101-01 — outcome-floor ladder now fully empty, 215 gateway_5xx/24h hitting foreman traffic
    88082b62 board: file TR-CMPLX-001 + refresh INT-CI-19 recurrence evidence
    d4bf4469 board: acceptance criteria for TR-1000976681059267
    87351d49 registry: retire pre-2026-generation lanes (Bane directive 2026-11-15)
    dc06cfb8 registry: pause cohere/command-r7b-12-2024 on xkiro (Bane directive 2026-11-16)

## 1. Owned rows read back from the PUBLISHED board (not the working tree)

    $ git -C /home/kara/task-router show origin/main:.coding-hermes/board/tasks.jsonl \
        | jq -c 'select(type=="object") | select((.id//"")=="DOC-22" or (.id//"")=="DOC-23")'
    DOC-22 pending P3 "P4 — docs/health-plane.md §2 documents 14 of the 26 blocks the published /health returns ..."
      created_at 2026-11-17T04:17:40Z  updated_at 2026-11-17T04:25:55Z
    DOC-23 pending P3 "P4 — No documented refresh path for data/tables/benchmarks.jsonl ... cost_effort.jsonl ..."
      created_at 2026-11-17T04:17:40Z  updated_at 2026-11-17T04:25:55Z

Marker fields resolved against the published tip:

    $ git -C /home/kara/task-router merge-base --is-ancestor ca89c31dfff54663802b138388f32b2d211f6a02 origin/main && echo ancestor
    ancestor
    $ git cat-file -e origin/main:docs/evidence/doc-audit-2026-11-17-tick-024053-transcript.md && echo present
    present
    $ git show origin/main:docs/evidence/doc-audit-2026-11-17-tick-024053-transcript.md | wc -lc
        174    8746
    $ git show origin/main:docs/health-plane.md | grep -n '^#\{1,4\} '
    37:## 2. What `/health` guarantees          -> anchor #2-what-health-guarantees (DOC-22 spec=)
    $ git show origin/main:docs/registry-maintenance.md | grep -n '^#\{1,4\} '
    7:## 1. The maintenance loop               -> anchor #1-the-maintenance-loop (DOC-23 spec=)

## 2. DOC-22 premise re-verified at the published tip (T4: live endpoint, not source text)

    $ cd /home/kara/.hermes/stand-in/docs/task-router && python3 health_probe_refire.py
    served_payload_commit='c497fb4e590f'
    top_level_key_count=26
    top_level_keys=admission,cache_generation,chains_snapshot,circuit,classify_cache,code,commit,context_fidelity,coverage,data_cache,eligibility,gate,mode,probe,registry,registry_age,registry_state,runtime,service,serving,source_sha,stale,status,surface_mode,ts,unmeasured_categories
    artifact=/home/kara/.hermes/stand-in/docs/task-router/health-probe-refire/health.json

    (the probe script starts scripts/router_server.py --mode read-only on 127.0.0.1:9311
     against a scratch data dir with a read-only symlink to wt-land/data/tables, reads
     /health, then terminates the process — the daemon does not outlive this tick. The same
     probe returned the identical 26-key set at 9e630627 before the tip advanced.)

Documented inventory, wt-land at c497fb4e:

    $ grep -cE '^\s{2}"[a-z_]+":' docs/health-plane.md
    14
    $ wc -l docs/health-plane.md
    278 docs/health-plane.md

Occurrence census in that file (grep -c, whole file):

    eligibility 0 | classify_cache 0 | data_cache 0 | surface_mode 0
    cache_generation 0 | source_sha 0 | registry_state 0 | context_fidelity 0
    unmeasured_categories 0

Contradiction inside the corpus — still true at c497fb4e:

    $ sed -n '193p' docs/configuration.md
    | `ROUTER_ELIGIBILITY` | `router_spawn.py` | ... `/health` plus `scripts/router_usability_gate.py`
      publish the same census ...
    $ grep -n 'eligibility' scripts/router_server.py | head -3
    927:                        _census = _rs.eligibility_census(_tables)
    928:                        payload['eligibility'] = dict(
    937:                        payload['eligibility'] = {

Published minus documented = 12 keys: cache_generation, classify_cache, code,
context_fidelity, data_cache, eligibility, registry_state, runtime, source_sha, stale,
surface_mode, unmeasured_categories. DOC-22's premise holds exactly.

## 3. DOC-23 premise re-verified at the published tip

    $ wc -l data/tables/benchmarks.jsonl data/tables/cost_effort.jsonl
       8286 data/tables/benchmarks.jsonl
       1035 data/tables/cost_effort.jsonl
    $ grep -n 'DEFAULT_OUTPUT' scripts/router_market_ingest.py | head -1
    26:DEFAULT_OUTPUT = "data/tables/benchmarks.jsonl"
    $ grep -n 'add_argument' scripts/router_market_ingest.py
    251:--models  252:--output  253:--queue-dir  254:--input  255:--date
    $ grep -n 'add_argument' scripts/cost_effort.py
    261:--check  262:--report  263:--dry-run

Absence census (name-as-substring over the reader-facing corpus):

    $ grep -rn 'market_ingest' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' . | grep -v '^\./docs/evidence/'
    (no output)   -> 0 reader-facing hits
    $ grep -rn 'cost_effort\.py' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' . | grep -v '^\./docs/evidence/'
    (no output)   -> 0 reader-facing hits
    $ grep -rn 'market_ingest\|cost_effort\.py' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' .
    (7 hits, every one inside docs/evidence/doc-audit-2026-11-17-tick-024053-transcript.md)
    $ grep -nE 'benchmark|cost_effort|ingest' Makefile
    (no output)

PRECISION NOTE on DOC-23's title ("both generator names appear in zero docs"): the only
hits are inside the audit's OWN evidence transcript, which is a command log under
docs/evidence/, not an operator-facing refresh path. The finding stands, but a worker
re-running the exact grep at the tip sees 7 transcript hits rather than "no output".
Recorded as a residual below, not re-filed.

    $ sed -n '15p;66p' docs/tier-cost-cube.md
    15: | `data/tables/cost_effort.jsonl` | (provider, model, reasoning_effort) | est_cost_per_task, basis, n_samples, null_reason | **lane** |
    66: - `no cost_effort cell for this lane at this effort` — the cost generator never enumerated this ...

## 4. No reader-facing drift between the audit revision and the current tip

    $ git diff --name-only a2b44fd7d637 9e630627
    .coding-hermes/board/events.jsonl
    .coding-hermes/board/tasks.jsonl
    docs/evidence/doc-audit-2026-11-17-tick-024053-transcript.md
    $ git diff --name-only a2b44fd7d637 c497fb4e
    .coding-hermes/board/events.jsonl
    .coding-hermes/board/tasks.jsonl
    docs/evidence/doc-audit-2026-11-17-tick-024053-transcript.md
    scripts/router_seed.py

No reader-facing doc moved since the audit revision, and the one source file that did
(router_seed.py, c497fb4e) is not on either finding's surface (neither /health's block
assembly nor the two table generators).

    $ python3 link_census.py .
    relative links checked: 29
    BROKEN (1):
      docs/reviews/tr388-quorum/seat1-glm.md:59 -> r  [name]

(the single broken link is inside a quorum review transcript, not a reader path —
unchanged from the first delivery's note; below worker-task bar.)

## 5. Board state at the published tip (baseline; this tick wrote no row)

    $ boardctl -C wt-land validate
    RESULT: FAIL (455 error(s), 594 warning(s))
    $ grep -c '^\[error\]' validate-refire-024053.txt
    455
    $ grep -c 'duplicate' validate-refire-024053.txt
    0
    $ grep -n 'DOC-22\|DOC-23' validate-refire-024053.txt
    572:[warn] tasks.jsonl line 544 (task DOC-22): title carries "P4" but priority is "P3" — the priority field wins
    573:[warn] tasks.jsonl line 545 (task DOC-23): title carries "P4" but priority is "P3" — the priority field wins

Both are WARNINGS and they are the intended TR-348 mapped state (severity on the title,
canonical P3 in the field, provenance in foreman_note). Zero of the 455 pre-existing
errors name DOC-22 or DOC-23. The board blob is byte-identical between 9e630627 and
c497fb4e (`git rev-parse <tip>:.coding-hermes/board/tasks.jsonl` -> 867b9269e1b9 both),
so this baseline holds at both tips. The 455 errors pre-date this tick and were not
triaged here.

Priority vocabulary (proves why the stranded local copies cannot be published):

    $ grep -n 'CLOSED at P0-P3\|off-vocabulary' scripts/board_commit_boundary_gate.py
    27:`priority` field verbatim; the board vocabulary is CLOSED at P0-P3 ...
    170: f"closed at {PRIORITY_VOCABULARY}. Map docs-audit severities before filing ..."
    $ grep -n 'SEVERITY_MAP' scripts/board_priority_normalize.py
    68:SEVERITY_MAP = {"P4": "P3", "P5": "P3"}

DOC-20/DOC-21 in the canonical checkout carry priority "P4" in the field, i.e. exactly
what board_commit_boundary_gate.py refuses at push time.

## 6. Re-triage check (doc-severity policy)

Doc rows open on origin/main: DOC-5..DOC-12, DOC-13..DOC-16, DOC-18, DOC-19, DOC-22,
DOC-23 — all already P3-in-field with a "P4 —"/"P5 —" title, which IS the mapped state for
this repo. DOC-19 is P3 by policy and stays there: its finding is that a documented path
(router chain-run outside a checkout) is broken, which the ruling forbids demoting.
The only other doc-flavoured open rows below P3 are TR-1000242751831738 (load gate
mis-set — functional) and TR-1000976681059263 ([P2] README quickstart seed exits 1 — a
broken documented path, again functional). No re-triage action was correct this tick.

## 7. Residuals (reported, not filed)

R1. Canonical checkout /home/kara/task-router is diverged: 106 behind / 7 ahead, with 8
    dirty paths. Its unpushed DOC-20/DOC-21 duplicate the published DOC-22/DOC-23 and sit
    at off-vocabulary P4. Reconciling that checkout (rebase/harvest, dropping DOC-20/21)
    belongs to its owner; this lane did not touch it.
R2. DOC-23's title wording "zero docs" is imprecise now that the audit transcript itself
    lives under docs/evidence/ (§3 precision note). Cosmetic; the row's marker is
    byte-frozen and was not amended.
R3. 455 pre-existing boardctl validate errors at the published tip; unattributed, out of
    scope here, quoted so the next reader can recompute the baseline.
R4. No CI run; documentation findings only, and this delivery edited no target file.

## 8. Tip advance during this tick

origin/main advanced from 9e630627 to c497fb4e mid-tick (one commit, scripts/router_seed.py
only). The /health probe was re-run against c497fb4e and returned the same 26-key set;
every doc/source anchor above was re-read at c497fb4e. No measurement in this transcript
mixes tips.

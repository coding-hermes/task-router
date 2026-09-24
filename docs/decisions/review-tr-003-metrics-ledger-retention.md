# REVIEW-TR-003 — Metrics ledger retention policy (`data/metrics.jsonl`)

Status: DECIDED (bounded window + size ceiling stated; compaction tooling NOT yet implemented — see §6) · 2026-09-23 · Investigator: worker tick, read-only probes
Board row: REVIEW-TR-003 ("Repo tracks guard logs (47 dirty entries) — third project with this class — plus a 1.3 GB metrics ledger")
Related: TR-021 (metrics append + query CLI), TR-089/TR-113 (untrack `.gitreins/logs`, compressed archive + run index), TR-105 (guard logs rotational yet tracked)

## 1. Context

The fleet review flagged `data/metrics.jsonl` at 1.3 GB written in a single day, with
no visible rotation or compaction policy. The file is correctly gitignored — it is
runtime state, not repo content — so this is NOT a repository-hygiene defect like the
tracked guard logs. The question the review actually raises is whether the ledger is
allowed to grow without bound, and if not, what the bound is.

Answer: unbounded growth is NOT intended. The ledger is operational telemetry used to
answer recency questions ("which providers/models/pairs served hops in the last day or
week"), not a history of record. The policy below states a retention window and a size
ceiling, and names the compaction mechanism.

## 2. Live measurements (2026-09-23, read-only probe of the live file)

Probe: one streaming pass over `/home/kara/task-router/data/metrics.jsonl`
(no load into memory; row counts and byte totals accumulated per line).

| Item | Value |
|---|---|
| Size | 1,479,145,986 bytes (1.48 GB) |
| Rows | 2,970,299 |
| Unparsable rows | 0 |
| Average row | 498 bytes |
| First row `ts` | 2026-09-01T02:12:37+00:00 |
| Last row `ts` | 2026-09-23T21:24:29+00:00 (days covered: 23) |
| Average | 129,143 rows/day · 64.3 MB/day |
| Peak day | 2026-09-19 — 506,295 rows / 257,663,224 bytes |
| Growth rate | 1 GB every ~15.5 days; ~23.5 GB/year at the measured rate |
| Host disk headroom | 229 GB available on `/` (87% used) |

Row composition by outcome:

| Outcome | Rows | Share | Bytes | Share |
|---|---|---|---|---|
| excluded | 1,785,262 | 60.1% | 922,598,369 | 62.4% |
| resolved | 1,175,945 | 39.6% | 553,782,783 | 37.4% |
| error | 9,093 | 0.3% | 3,626,484 | 0.2% |

Two facts drive the policy:

- **Exclusion telemetry is the bulk.** `quota GATED: blocked` alone is 1,238,745 rows
  (69% of all exclusions). The ledger records every chain hop it *did not* take, which
  is exactly what makes it grow ~2.5x faster than resolved hops.
- **`config_snapshot` is near-constant.** 535 distinct snapshot values cover all
  2,970,299 rows; the most-repeated one accounts for 315,155 rows (10.6%, 255 bytes
  each). Snapshot bytes are ~11% of the file at most — a normalization win worth
  naming, but not the lever that fixes growth.

Reader cost: `router_metrics.py::_filter_rows` streams the ENTIRE file for every query
and applies `--since` in a Python filter — there is no index and no default window, so
`router metrics --top-pairs` with no `--since` today reads 1.48 GB / 2.97 M rows. The
documented and test-covered windows are 24h, 7d and 30d (README `## Metrics`;
`tests/test_metrics.py::test_metrics_since_window`). Nothing in the repo, the fleet
crons, or `router_maintain.py` currently prunes, rotates, compacts or rolls up this
file — the absence flagged by the review is real.

## 3. Decision

**Retention window: 30 days.** Rows older than 30 days are removed by compaction.
30 days is chosen deliberately wider than every window the tooling documents or tests
(24h / 7d / 30d), so no supported query loses data.

**Size ceiling: 2 GiB.** Compaction also triggers whenever the live file exceeds 2 GiB,
whichever comes first. At the measured 64.3 MB/day a 30-day window lands near 1.9 GB,
so the ceiling normally coincides with the window — it exists to catch rate spikes
(2026-09-19 ran at ~4x the daily average) rather than to be the routine trigger.

**Compaction is a window prune, not a numbered file rotation.** The single-file append
contract is load-bearing: `router_spawn.py::_append_metrics` opens the path in append
mode (`TASK_ROUTER_HOME/metrics.jsonl`, else `<repo>/data/metrics.jsonl`) and
`router_metrics.py` reads exactly that path. Renaming the live file to
`metrics.<n>.jsonl` would silently split the writer from the reader — the same
divergence class TR-056 already had to fix for the CLI's env exports. So the mechanism
is: stream the live file, write the rows inside the window to a temp file in the same
directory, fsync, atomically `rename()` over the live path. The writer keeps appending
to one path throughout; it never learns compaction happened.

**Compaction rules (binding on any implementation of this policy):**

1. **Dry run first.** Compaction prints rows and bytes that would be removed and
   writes nothing, unless explicitly told to apply. Mutating the ledger is the one
   irreversible action in this area.
2. **Fail-open, like `router_spawn.py`.** Any error leaves the ledger untouched and
   exits 0. Metrics must never block the scheduler.
3. **Retain on doubt.** A row whose `ts` is missing or unparsable is RETAINED, never
   dropped — a parse failure is not evidence of age.
4. **Never touch the writer's path.** Atomic rename only; no delete-then-recreate
   window where the live path is absent.
5. **Report the outcome.** Every compaction prints rows before/after and bytes
   before/after, so the next audit can verify it ran from the ledger's own record
   rather than from a cron status (the CHATGAP-001 law: verify pipelines by
   destination coverage).

**Aggregation is deliberately out of scope.** Because readers are O(file),
30-day-bounded queries stay fast enough, but multi-month trend questions cannot be
answered from a windowed file. The intended answer is a daily rollup (one row per
day × provider × model × outcome) written by the same maintenance cadence, so history
survives compaction in aggregate. That is a new table and a new row, not part of this
policy statement; it is named here so the window is not mistaken for data loss.

## 4. Cadence and ownership

- Trigger: monthly, or immediately when the 2 GiB ceiling is crossed.
- Home: a `compact` step on the existing `router_maintain.py` cadence (`all` =
  reprice → seed → export → snapshot → commit), so it rides the loop that already owns
  this repo's state maintenance and gets its `--dry-run` convention for free. Adding a
  new `router metrics --prune --retain 30d` verb on the same code path is acceptable
  and arguably better for operators; either way the mechanism is the window prune
  above, with rules 1–5.
- `data/metrics.jsonl` stays gitignored. This policy adds no tracked file; the repo
  owns the *rule*, the operator owns the *file*.

## 5. Verification

- Measurements above are reproducible with a streaming pass over the live file; they
  are not estimates. Row/byte totals, the outcome split and the distinct-snapshot
  count came from the same pass.
- `tests/test_repo_hygiene.py` pins the ignore rules this row's first two acceptance
  criteria rest on (`.gitreins/logs/` by class, plus the `tasks.yaml.lock` residual),
  and asserts the live metrics path is ignored while `.gitreins/config.yaml` — which
  IS tracked by design — is not.
- The guard-log half of this row is already closed: TR-089/TR-113 untracked
  `.gitreins/logs/*` on 2026-09-22 (commit `919c544`). Upstream GitReins additionally
  bounds that directory itself (`GUARD_LOG_KEEP = 20` newest files,
  `GUARD_LOG_MAX_BYTES = 2 MiB` per log, pruned on every run) and ships
  `.gitreins/logs/` in its own managed ignore block, so the class rule here matches
  the tool's own contract rather than inventing a local one.

## 6. What this change does NOT do

No compaction tooling is added by this row, and none exists today — the review asked
for a *stated* policy, and a policy that ships with its own untested implementation is
a bigger change than the defect warrants. Concretely, the enforcement described in §4
is a specification, not running code. The follow-up work, in priority order:

1. **`router_metrics.py --prune --retain 30d [--dry-run]`** implementing §3's rules
   1–5. Acceptance: a dry run against the live ledger reports the rows/bytes it would
   remove and writes nothing; a real run on a COPY leaves the writer's path intact
   (the writer appends before and after without divergence); a row with a missing or
   unparsable `ts` survives the prune.
2. **Daily rollup** (day × provider × model × outcome) so compaction does not close
   the multi-month question — this is what makes the 30-day window safe to enforce.
3. A cadence trigger (monthly, or on the 2 GiB ceiling) wired to the existing
   `router_maintain.py` loop rather than a new cron.

Until (1) lands, the ledger keeps growing; the window and ceiling are the stated
intent and the reviewer's answer to "is unbounded growth intended?" — it is not.

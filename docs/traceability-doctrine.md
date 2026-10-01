# coding-hermes Traceability Doctrine

**Status:** draft contract · ported from Axiom `specs/21-Traceability-Doctrine.md` v1.2 (2026-10-01)
**Why we need it:** an x-ray of task-router found 222 rows marked complete with 8 carrying evidence and 0
carrying an evidence run, while the project's declared deliverable had zero tags at origin. The missing layer
is not another gate — it is a **link graph** between intent, work, code, evidence and observation, thin enough
to survive a busy tick and mechanical enough for an independent auditor to check.

## Core principle

Everything links to everything else in a navigable graph. The graph MUST be:

- **Grep-friendly** — every marker is one line, findable with `grep -rn "ch:trace"`.
- **Stable across refactors** — markers reference logical ids (board row, spec path, test path), never line numbers.
- **Incremental** — authors fill what they know now; `key=` with an empty value is an honest placeholder.
- **Verifiable by an independent auditor** — and the auditor must not be the author of the claim it checks.

## Canonical marker

```
ch:trace row=<ID> spec=<path#anchor> wave=<manifest#step> test=<path::test> doc=<path> prompt=<path> evidence=<path> witness=<locator> verdict=<id> commit=<sha> memory=<duckbrain-key>
```

| field | meaning | rule |
|---|---|---|
| `row` | board row id (`TR-249`, `INT-CI-20260930-05`, `RELEASE-004`) | **REQUIRED.** A marker without a row is a lint failure. |
| `spec` | contract path + optional `#anchor` | required when a spec exists for the behaviour |
| `wave` | wave manifest + step (`task-router-2026-09-30-01-14-11#task-2`) | when the work came from a wave |
| `test` | `tests/test_x.py::test_name` | required for behaviour changes |
| `doc` | docs/runbook/report path | when documentation exists |
| `prompt` | the lane prompt this work rides | when a prompt was changed |
| `evidence` | **path to an artifact we produced** (report, film, log) | never another row, never prose |
| `witness` | **locator on a surface we cannot write** (`tag:v0.2.0@origin`, `http:200+sha256:...`, `ledger:router-proxy/rows>0`, `gh:release/v0.2.0`) | preferred over `evidence` when the claim is about the world |
| `verdict` | gitreins verdict id | must be bound to `commit` |
| `commit` | short sha | must be an ancestor-or-equal of `origin/main` |
| `memory` | DuckBrain key (`/project/<name>/<key>`) | cited records only, never the sole citation |

### Hard rules (the mechanical ones)

1. **`row=` is mandatory.** Trace without a work unit is a comment, not trace.
2. **NO MARKER MAY BE CLOSED BY REFERENCE.** `evidence=` must resolve to an artifact or a witness. A marker
   that points at another row, verdict, report or marker is REFUSED — that is box-in-a-box and it is how 222
   rows came to be complete with nothing behind them.
3. **A claim that cannot name a witness says so:** `witness=none:no-external-surface-exists`. A stated reason is
   acceptable; silence is not.
4. **`memory=` is never load-bearing.** It may accompany at least one repo-local reference (`spec`/`test`/`doc`/
   `evidence`/`witness`), never replace it. Unverified memory records MUST NOT appear.
5. **`verdict=` must be bound to `commit=`,** and `commit` must be reachable from `origin/main`. A verdict that
   judged a pre-merge tree, or an absent verdict, is not evidence of anything.
6. **The auditor is not the author.** Whoever writes the claim never satisfies the check. For fleet work the
   referee is the **film** — the independent surface scan (see `reports/xray/`), and an external surface
   outranks any artifact we authored.

## Placement (by layer)

- **Intent** — a request, spec or PRD section gets the marker that will later link down to work.
- **Work** — the board row's `detail`/`evidence` and its events.
- **Code** — the module/class/function header comment that implements the behaviour.
- **Test** — the test that proves it, with `spec=` and `row=`.
- **Git** — a trace footer in the commit body and a `## Trace` section in the PR/merge note.

## Commit footer

```
ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_doc_parity.py::test_env_keys_documented evidence=reports/xray/task-router-xray.html
```

Every coding-hermes commit carries one footer per row it closes. Multi-row work gets multi-line footers.

## The unified context surface (with Hilo)

The trace graph is queried, not read. One call returns four surfaces at once, each fragment labelled with its
origin and timestamp:

| surface | source | what it answers |
|---|---|---|
| code structure | **Hilo / WarpFS** (`hilo graph`, `.vfs/graph/edges.jsonl`, 26 languages) | what exists, what depends on what, impact of a change |
| memory | **DuckBrain** (namespaces, `mem://` style keys) | what we already decided/learned, cited only |
| contracts | **specs/docs** (`docs/`, `specs/`, `reports/`) | what we promised |
| state | **boards + waves** (`tasks.jsonl`, manifests, gitreins verdicts) | what we claim and what was judged |

Design constraints, from Hilo's own doctrine: **metadata, not injection** (never rewrite code to index it),
**JSONL for edges**, **DuckDB for queries** (rebuildable, never the source of truth), **inventory as truth**.

## Verification

- `grep -rn "ch:trace"` finds every marker.
- A validator refuses: missing `row`; reference-closures; `memory=` as sole citation; `verdict` without a bound
  `commit`; `commit` not reachable from `origin/main`.
- The film (`reports/xray/<project>-xray.html`) is the referee: a marker's `witness=` must be visible in the next
  film. If it is not, the marker is stale, whatever the row says.

## Relationship to the rows we already filed

- **TR-249** completion gate — this doctrine supplies what the gate should actually require.
- **TR-251** the mechanical (evidence run, replay, probes, commit-boundary gate, `verified_complete_share`).
- **TR-252** the witness scale (external observation, non-author, re-observation, no closure by reference).
- This document is the **link format** those three describe but do not define.

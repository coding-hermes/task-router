# Evidence-Run Store Decision (TR-251 criterion 6)

**Status:** DECIDED · 2026-10-16
**Row:** TR-251 (mechanism 2 depends on this; nothing in it is built before this is settled)

## The question

`boardctl update --evidence-run-id RUN` accepts a run id but no store was ever
specified — the only code references to `evidence_run_id` on live boards were
unrelated audit scripts. Where does RUN resolve? Without an answer, a bound
run id is a success marker: the same text-as-state sin TR-251 exists to end.

## The decision

**The evidence-run store is the GitReins verdict history, already on disk at
`.gitreins/history/<date>/<run-id>/verdict.json`, plus the two sanctioned
synthetic forms below.** Nothing new is built for storage; the decision makes
the EXISTING store authoritative.

A run id resolves when one of these holds:

1. **GitReins verdict** — `.gitreins/history/<date>/<run-id>/verdict.json`
   exists (relative to the repo that owns the board). The verdict file
   carries `task_id`, `commit`, `passed`, `evaluated_at`; it is gitignored
   local history, so the replay contract (mechanism 2) treats a verdict as
   resolving the run and binds the row's `commit_hash` as the durable pointer.
2. **CI run** — `RUN` matches a GitHub Actions run id (9-11 digits) that
   `gh run view <id> --repo <owner>/<repo>` reports as completed. Resolves
   out-of-band; no local artifact needed.
3. **Stated-reason null** — `none:<reason>` (doctrine hard rule 3). Accepted
   by the write gate as evidence the author DECLARED the absence; it is never
   replayable, so it can never satisfy mechanism 2's replay for a row whose
   criteria are checkable.

Anything else (bare uuids, timestamps, free prose) does NOT resolve. The
census (`boardctl doctor`, mechanism 5) counts a row verified when it carries
ANY run id — the id is the pointer; resolution is replay's job, not the
census's.

## Why the verdict history

- It already exists on every gitreins-managed repo in the fleet, per-run and
  per-date; a new runs table would duplicate it and drift.
- Its shape is exactly what replay needs: the criteria the judge checked, the
  commit it judged, the pass/fail verdict.
- It is append-only, like the board. No mutation surface to argue about.

## What each mechanism consumes

- **Mechanism 1 (required flag):** unchanged — the flag binds the id.
- **Mechanism 2 (CI replay):** resolves the run through the forms above; a
  stored digest alone never passes (the digest is not one of the three
  resolving forms).
- **Mechanism 5 (doctor census):** counts ids; does not resolve them.

## Follow-up left open deliberately

Backfilling the 222 historic complete rows is NOT part of this decision. The
write gate (mechanism 1) stops new debt; the census (mechanism 5) reports the
old debt; converting old rows happens row-by-row as they are re-touched, or
never. Mass-backfill would manufacture evidence, which is the failure mode
under repair.

<!-- ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_evidence_replay.py evidence=docs/evidence-run-store-decision.md -->

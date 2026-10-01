# Fleet Traceability Doctrine

**Status:** draft contract · v1 (2026-10-01)
**Scope:** every artifact this fleet produces — board rows, code, tests, commits, reports, films.

**Why it exists.** An independent scan of one project found 222 rows marked complete, 8 carrying any evidence
field and 0 carrying an evidence run, while that project's declared deliverable had zero tags at its origin. Two
of the complete rows described their own failure in plain text ("no-go", "release surface still absent"). We
file non-completion as a completed task. What is missing is not another gate — it is a **link graph** thin enough
to survive a busy tick and mechanical enough for a second party to check.

> Wording and design here are original to this repository. It takes the *idea* of a trace link graph from an
> internal doctrine; none of that doctrine's text is reproduced. Private product, host and project names are
> deliberately absent from this public file.

## The idea

Every artifact should be walkable back to the intent that asked for it, in one grep. A marker that costs an
author ten seconds is worth more than a process that costs a reviewer ten minutes.

A usable link graph has four properties:

- **one line per marker** — anything findable with a single recursive grep;
- **logical ids, never positions** — a marker that references a line number breaks on the next refactor;
- **partial is honest** — an author fills what is known; an empty value is a placeholder, silence is not;
- **checked by someone who did not write it** — self-certification is not verification.

## The marker

```
ch:trace row=<ID> spec=<path#anchor> wave=<manifest#step> test=<path::test> doc=<path> prompt=<path> \
         evidence=<path> witness=<locator> verdict=<id> commit=<sha> memory=<key>
```

| field | meaning | rule |
|---|---|---|
| `row` | the work unit (`TR-249`, `RELEASE-004`, `INT-CI-*`) | **required.** No row, no trace. |
| `spec` | contract path, optional `#anchor` | required when a contract covers the behaviour |
| `wave` | dispatch manifest + step id | when the work arrived in a wave |
| `test` | `tests/test_x.py::test_name` | required for behaviour changes |
| `doc` | documentation or runbook path | when documentation exists |
| `prompt` | the lane prompt this rides | when a prompt changed |
| `evidence` | path to an artifact **we produced** (report, film, log) | never prose, never another row |
| `witness` | locator on a surface **we cannot write** | preferred when the claim is about the world |
| `verdict` | evaluation verdict id | must be bound to `commit` |
| `commit` | short sha | must be reachable from the published branch tip |
| `memory` | long-term memory key | cited records only; never load-bearing |

`witness` is the field this fleet added, and it is the one that matters most. Evidence we author is a claim about
our own work; a witness is a reading taken somewhere else. Accepted witness forms:

`tag:<version>@origin` · `release:<version>` · `http:<code>+sha256:<digest>` · `path@origin:<path>` ·
`ledger:<predicate>` · `provider:<usage-page>` · `live:<endpoint resolves with >=1 hop>`

## Hard rules

1. **`row` is mandatory.** Trace without a work unit is a comment.
2. **Nothing closes by reference.** `evidence` resolves to an artifact or a witness. A marker pointing at another
   row, verdict, report or marker is refused. Nesting claims is how a board reaches 72% complete while the
   deliverable it names does not exist anywhere outside the machine.
3. **No witness means saying so** — `witness=none:<reason>`. A stated reason is acceptable; an omission is not.
4. **The memory field never carries a claim on its own.** It may accompany a repo-local reference; it may not
   replace one, and only records that resolve may be cited.
5. **A verdict counts only when it is bound to the commit it judged**, and that commit is reachable from the
   published tip. A verdict produced against an unmerged tree is not evidence of the shipped code.
6. **The checker is never the author.** Whoever writes the claim does not satisfy it. The referee is a scan taken
   out of band against surfaces we do not control; an external reading outranks any artifact we wrote.

## Where markers go

- **Contract** — the spec section that defines the behaviour links down to the work that implements it.
- **Work** — the row body and its events carry the same marker as the code.
- **Code** — the module, class or function header comment that implements the behaviour.
- **Test** — the test that proves it, carrying both `row` and `spec`.
- **Commit** — one footer line per row closed, repeated for multi-row work.
- **Change note** — a `## Trace` section in the merge or pull-request description.

## Commit footer

```
ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_doc_parity.py::test_x evidence=reports/xray/
```

## One call, four surfaces

A lane should not reason from whatever happens to be in its prompt. One query returns four surfaces at once, and
every fragment is labelled with where it came from and when:

| surface | answers |
|---|---|
| code structure | what exists, what depends on what, the blast radius of a change |
| long-term memory | what we already decided or learned, cited only |
| contracts | what we promised, in specs and reports |
| state | what we claim (boards, manifests) and what was judged (verdicts) |

Indexing code must never modify it: build the structure alongside the source, not inside it. Edges belong in
append-only lines; the query engine is rebuildable cache and never the record.

**Disagreement is the point.** When one surface contradicts another — the board says complete, the outside says
untagged — the tool reports the contradiction instead of merging it away. That single line has caught more real
problems than any dashboard.


<!-- ch:trace row=TR-253 spec=docs/traceability-doctrine.md wave=task-router-2026-10-01#trace doc=~/.hermes/skills/coding-hermes-traceability/SKILL.md evidence=docs/traceability-doctrine.md witness=none:prompt-loading-not-yet-verified -->

## Verification

- One recursive grep finds every marker.
- A validator refuses: a marker without `row`; a reference-closure; a memory field as sole citation; a verdict
  without a bound commit; a commit not reachable from the published tip.
- A marker's `witness` must appear in the most recent out-of-band scan. If it does not, the claim is stale
  regardless of what the row says.

## Related work in this repo

Completion gate (TR-249), the mechanical (TR-251), the witness scale (TR-252), default prompt loading (TR-253),
and the four-surface context call (TR-254). This document supplies the link format those rows assume but do not
define.

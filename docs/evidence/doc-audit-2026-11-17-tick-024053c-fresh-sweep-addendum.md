# Addendum — audit-boundary and measurement corrections for tick 2026-11-17-02-40-53

Addendum to `docs/evidence/doc-audit-2026-11-17-tick-024053c-fresh-sweep-transcript.md`
(sha256 `06024edcc58ed99790849f65a1723acb8b0b50acdc788165ff035b97bc25ad93`, frozen before filing,
left byte-unchanged).

The transcript is immutable. This addendum records what changed AFTER it was frozen, so a later
reader can reconciliate the artifact with the published tree instead of finding a contradiction.

## 1. The tip advanced during the tick

Probes ran at `origin/main` = `c024739583b2819028c1661c8344ac4440199930`. The first push was
rejected as non-fast-forward because `origin/main` had advanced to
`642006cbedaa53e3c9755e03b9cb917e11e06482` (one commit: `test(maintain): the CI-only seed fallback
must not write the checkout it tests`, touching only `tests/test_maintain_repair.py`).

None of the three findings' surfaces ( `task_router/cli.py`, `README.md`, `docs/configuration.md`,
`docs/quota-gate.md`, `scripts/state_dir.py` ) was touched by that commit, so the row commit was
rebased onto `642006cb` and the decisive probes were re-run at the new tip:

```
$ git log -1 --format=%H 642006cb
642006cbedaa53e3c9755e03b9cb917e11e06482
$ PYTHONPATH=. python3 -m task_router.cli --help | awk '/^  [a-z]/{n++} END{print "listed=" n}'
listed=28
$ PYTHONPATH=. python3 -m task_router.cli --help | awk '/^  [a-z]/ && NF==1 {printf "%s ", $1}'
chain-run context-call ingress lifecycle outcomes pricing-audit
$ PYTHONPATH=. python3 -m task_router.cli quota --help | grep -cE '^    (accounting|pacing|burn)'
3
```

Identical results at both tips. The transcript's measurements therefore hold at the published
revision; no row needed re-measurement.

## 2. The evidence artifact becomes part of the corpus it measures

This is a correction of the *measurement shape*, not of the finding.

The transcript says, correctly for the moment it was frozen, that
`git grep -c -F 'ROUTER_STATE_DIR_WARN' -- '*.md'` returns zero hits: the transcript was UNTRACKED
while the probes ran, so `git grep` (tracked blobs only) could not see it. Publishing the transcript
under `docs/evidence/` makes it a tracked `*.md` file, and it names the variable:

```
$ git grep -c -F 'ROUTER_STATE_DIR_WARN' -- '*.md'
docs/evidence/doc-audit-2026-11-17-tick-024053c-fresh-sweep-transcript.md:22
```

So after publication the bare command returns ONE hit and proves nothing — the hit is this audit's
own artifact, not documentation of the key. The correct census excludes the evidence tree:

```
$ git grep -n -F 'ROUTER_STATE_DIR_WARN' -- '*.md' ':!docs/evidence/*'
(no hits, exit 1)
```

## 3. Consequence for DOC-29

DOC-29's acceptance criterion was rewritten in place (before publication) to require the
`:!docs/evidence/*` exclusion and to say why, with one sentence naming this artifact so a verifier
who runs the naive form understands the hit they found. The stored `foreman_note` marker was NOT
touched and still appears exactly once on the row; `--reasoning` is not writable through
`boardctl update`, so the reasoning's "0 hits in TRACKED Markdown" sentence stands as a true
statement about its own measurement instant, superseded by this addendum.

## 4. Other residuals

- `ROUTER_E2E_KEY` (`scripts/proxy_e2e.py:350`) remains undocumented and unfiled — a dev-harness
  credential, not an operator knob. Named so the next sweep need not rediscover it.
- `router probe --help` writes the shared-state-dir WARNING to stderr before the usage block. That
  is a functional wart on a help surface, not a documentation gap; it belongs on the functional
  board, not here.
- One dead relative link, `docs/reviews/tr388-quorum/seat1-glm.md -> r`, inside an archived quorum
  transcript. Not reader-facing; not filed.

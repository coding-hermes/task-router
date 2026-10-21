# Envelopes — THE SCALE (board row TR-252)

An envelope is a project's statement of done that TERMINATES in an external
observation. Every verification mechanism that points at another artifact
opens a box and finds another box; the envelope is where the boxes stop:
each item names a surface we do NOT write, and only an observation of that
surface — recorded out-of-band, by a non-author, after the claim, re-observed
within the window — satisfies it.

**The rule that terminates the recursion: NO ITEM MAY EVER BE CLOSED BY
REFERENCE.** A closure that points at another item, row, verdict or report is
REFUSED by `scripts/board_complete_gate.py` — the refusal names `witness`.

## File format

`<project>.jsonl` — JSONL, two kinds of lines:

1. Header (line 1): `{"kind": "envelope", "project": ..., "version": ...,
   "author": ..., "created_at": ...}`
2. Item lines (one JSON object each):

```json
{"id": "release-0.2.0",
 "statement": "v0.2.0 is published as a GitHub Release at origin.",
 "owner": "Bane",
 "witness": {"kind": "github_release",
             "locator": "coding-hermes/task-router#v0.2.0",
             "expect": "a release object for v0.2.0 exists at origin"},
 "rows": ["RELEASE-004", "RELEASE-005", "RELEASE-006"],
 "window_days": 30}
```

- `id` — stable item id (referenced by rows via
  `witness: "envelope:<project>#<item>"`)
- `statement` — the statement of done, in observable terms
- `owner` — who answers for the item (a human; envelopes are authored
  OUTSIDE the work loop — by Bane or a spec, never by a tick)
- `witness.kind` — one of: `github_tag`, `github_release`, `http_get`
  (content digest), `path_at_origin`, `live_ingress_resolves`,
  `provider_usage_page`, `ledger_predicate`
- `witness.locator` — the exact external surface to observe
- `witness.expect` — the expected observation (value or `sha256:<hex>`)
- `rows` — board rows bound to this item (optional); a bound row cannot
  close while the item is not WITNESSED
- `window_days` — the re-observation window (staleness)

## Versioning and append-only

Envelopes are append-only. Adding items is free. **Removing or changing an
item requires an explicit sign-off recorded in the new header** (`author`)
plus a version increase — shrinking the scope to claim completion must show
up as a diff requiring sign-off. `board_complete_gate.py envelope <p>
--shrink-from <old-file>` enforces this.

## Observations (out-of-band only)

Observations live in `<project>.observations.jsonl`, appended by an
OUT-OF-BAND observer (a human, a cron, CI — never the tick loop, never the
row's author):

```json
{"item_id": "release-0.2.0", "observer": "external-cron",
 "locator": "coding-hermes/task-router#v0.2.0",
 "observed": "0 releases", "observed_at": "2026-10-21T09:00:00Z"}
```

The gate checks at close time: observer identity present, observer != row
author, `observed_at` strictly later than the row's claim, locator names the
witness surface, and the observation is re-observed within `window_days`
(else STALE — an item that was true and stopped being true stops counting).

## The number

```
~/.hermes/venvs/board/bin/python3 scripts/board_complete_gate.py coverage task-router
```

prints `coverage <project> v<version>: witnessed W/N (stale S)` —
witnessed_items / envelope_items — plus per-item WITNESSED / STALE /
NOT WITNESSED with the reason. `--live` re-observes externally probeable
witnesses right now (`git ls-remote --tags origin`, `gh release list`,
http digest, `git cat-file origin/main:<path>`); unobservable = refused,
never assumed. `provider_usage_page`, `ledger_predicate` and
`live_ingress_resolves` are deliberately NOT probeable from the work loop:
they require the out-of-band observer by design.

## Commands

```bash
# validate + display an envelope
... board_complete_gate.py envelope task-router

# THE NUMBER, printed with the envelope version
... board_complete_gate.py coverage task-router --live

# record an out-of-band observation (never from the tick loop)
... board_complete_gate.py record task-router --item release-0.2.0 \
    --observer external-cron \
    --locator 'coding-hermes/task-router#v0.2.0' \
    --observed '1 release: v0.2.0' --at 2026-10-21T09:00:00Z

# gate one row's completion (exit 2 = refused, names `witness`)
... board_complete_gate.py check --task RELEASE-006 --prev-status pending --dry-run

# complete-but-unproven totals (now includes unwitnessed)
... board_complete_gate.py backlog --dry-run

# envelope shrink sign-off
... board_complete_gate.py envelope task-router --shrink-from old/task-router.jsonl
```

## Demonstration case (2026-10-21)

`git ls-remote --tags origin` returns **0 tags** and
`gh release list --repo coding-hermes/task-router` returns **0 releases**,
so item `release-0.2.0` reads **NOT WITNESSED** and the board's release rows
may not close — including the RELEASE-005 ("v0.2.0 NO-GO") / RELEASE-006
("release surface still absent") shape, which the gate refuses outright: a
complete row whose own text states the non-completion is box-in-a-box.

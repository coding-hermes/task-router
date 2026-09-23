"""REVIEW-TR-002 — the board's priority column carries ONE vocabulary.

Review #17 (fleet review programme, 2026-09-22) measured this board at 118 rows
with two priority schemes: `P0`-`P3` on 90 rows and bare digits (`1`/`2`/`3`) on
28. The row filed for it (REVIEW-TR-002) asked for the 28 rows to be migrated and
for the validator to flag a stray scheme.

BOTH halves were already true when the row was picked up, so this file is not a
fix — it is the PIN that keeps the property from silently regrowing. Measured
2026-09-23, before any change to the tree:

  * board census: P0 x10, P1 x48, P2 x54, P3 x23 — zero off-vocabulary values
    (`b0e504c`, "close items 86-90 — priority vocabulary", migrated the 28).
  * `boardctl validate` flags an off-vocabulary priority on disk (BT-048,
    `coding-hermes-boardctl` 53a79cc) in the same report shape as a bad status.

What the migration actually used is the IDENTITY mapping `1->P1, 2->P2, 3->P3`
(also what `board.NormalizePriority` implements), NOT the `1->P2, 2->P1, 3->P0`
guess written into the row's own description. The mapping is verifiable because
each migrated row kept its old value in `notes` ("TR-088 priority migration: was
N") — `test_migration_notes_agree_with_the_priority_that_landed` re-derives the
mapping from the board itself instead of trusting either docstring.
"""
import collections
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(REPO, ".coding-hermes", "board", "tasks.jsonl")

#: The closed priority vocabulary every fleet board uses. Kept as a literal here
#: rather than imported: the Go implementation lives in a sibling repo
#: (coding-hermes-boardctl), and a test that could not run without it would be
#: skipped exactly when the drift it guards against is introduced.
VOCABULARY = ("P0", "P1", "P2", "P3")

#: "TR-088 priority migration: was 3" / "... was 2; guard_result was '...'".
#: The note is free text, so anchor on the phrase and the digit token — a `\S+`
#: capture swallows the trailing `;` and reports a false mismatch.
_MIGRATION_NOTE = re.compile(r"priority migration:\s*was\s+([0-9]+)")


def read_rows(path):
    """Parse a JSONL board, skipping (never crashing on) unparseable lines.

    Returns a list of `(line_number, row)` for every parsed dict carrying an id.
    """
    rows = []
    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "id" in row:
                rows.append((lineno, row))
    return rows


def priority_census(rows):
    """Return `(histogram, off_vocabulary)` over parsed rows.

    `off_vocabulary` maps each non-canonical stored value to the ids carrying it.
    An ABSENT priority is counted in the histogram under `<absent>` but is NOT
    reported as off-vocabulary: it is a missing-field shape, not a second
    spelling, and the write path has never required the key on legacy rows.
    """
    histogram = collections.Counter()
    off = collections.defaultdict(list)
    for _, row in rows:
        value = row.get("priority")
        if value is None or value == "":
            histogram["<absent>"] += 1
            continue
        histogram[str(value)] += 1
        if str(value) not in VOCABULARY:
            off[str(value)].append(row.get("id"))
    return histogram, dict(off)


# ───────────────────────────── the live board ──────────────────────────────

def test_board_exists_and_parses():
    """A pin over zero rows would pass vacuously (DF-BOARDCTL-9's class)."""
    rows = read_rows(BOARD)
    assert rows, f"no parseable task rows in {BOARD}"
    # The board measured 118 rows at review time and 135 at pin time; assert a
    # floor, not the exact count, so normal board growth does not fail the pin.
    assert len(rows) >= 118, f"only {len(rows)} rows parsed — expected the whole board"


def test_board_carries_only_the_canonical_priority_vocabulary():
    """AC1: zero numeric (or otherwise off-vocabulary) priorities on the board.

    The fence is the RAW stored value: `boardctl` normalizes bare digits and case
    variants on WRITE, but a hand-edited or legacy row keeps whatever it was given,
    so a tolerant comparison here would reproduce the exact blindness the row
    reported ("mixed values are invisible to ranking code" — `"3"` and `"P2"` sort
    as unrelated strings).
    """
    rows = read_rows(BOARD)
    histogram, off = priority_census(rows)
    assert off == {}, (
        "board carries off-vocabulary priority values "
        f"(expected only {VOCABULARY}); histogram={dict(histogram)}; offending={off}"
    )


def test_the_pin_bites_when_a_numeric_priority_is_reintroduced():
    """RED control: the assertion above must FAIL on a board that carries `3`.

    Same code path, injected fault — a test that has only ever run green cannot
    be shown to be able to fail.
    """
    rows = [(1, {"id": "TR-900", "priority": "P2"}),
            (2, {"id": "TR-901", "priority": "3"}),
            (3, {"id": "TR-902", "priority": "P1"})]
    histogram, off = priority_census(rows)
    assert off == {"3": ["TR-901"]}, off
    assert histogram["P2"] == 1 and histogram["3"] == 1


def test_absent_priority_is_not_reported_as_a_second_scheme():
    """A missing key is a missing field, not an off-vocabulary spelling.

    Keeping these two classes apart is what makes the census actionable: the
    off-vocabulary list is the set of rows someone must re-spell.
    """
    rows = [(1, {"id": "TR-900", "priority": "P2"}),
            (2, {"id": "TR-901"})]
    histogram, off = priority_census(rows)
    assert off == {}
    assert histogram["<absent>"] == 1


# ────────────────────────── the migration it rests on ──────────────────────

def test_migration_notes_agree_with_the_priority_that_landed():
    """Re-derive the migration mapping from the board; do not trust the docs.

    Each migrated row kept its old value in `notes`, so the applied mapping is
    checkable after the fact. The board's own description of the row guessed
    `1->P2, 2->P1, 3->P0`; the tool implements and the board received the
    identity mapping. Asserting note-vs-value agreement catches BOTH a row
    migrated under the wrong scheme and a note left stale by a later re-prioritise.
    """
    rows = read_rows(BOARD)
    checked, mismatches, unparseable = 0, [], []
    for lineno, row in rows:
        note = str(row.get("notes") or "")
        if "priority migration" not in note:
            continue
        match = _MIGRATION_NOTE.search(note)
        if not match:
            unparseable.append((lineno, row.get("id"), note[:80]))
            continue
        checked += 1
        if row.get("priority") != "P" + match.group(1):
            mismatches.append(
                (lineno, row.get("id"), match.group(1), row.get("priority")))

    assert checked >= 28, (
        f"expected the 28 migrated rows to still carry their note, found {checked}")
    assert unparseable == [], (
        f"migration note present but no 'was N' token: {unparseable}")
    assert mismatches == [], (
        "migration note disagrees with the stored priority "
        f"(lineno, id, was, now): {mismatches}")


def test_every_off_vocabulary_row_would_have_been_recorded():
    """The 28 migrated rows all carry a note — no silent re-spelling.

    A row whose priority was changed without a note is indistinguishable from a
    row that was always canonical, which is how a second vocabulary survives a
    "we already normalised it" claim. The counts must match.
    """
    rows = read_rows(BOARD)
    noted = [r for _, r in rows if "priority migration" in str(r.get("notes") or "")]
    assert len(noted) == 28, (
        f"{len(noted)} rows carry a migration note; the review measured 28 to migrate")

"""TR-348 — the docs-audit writer can no longer emit an off-vocabulary priority.

Origin: main's CI went red because eight DOC rows (DOC-5..7, DOC-13..17)
carried `priority: "P4"` — the docs-audit lane's documentation severity,
copied verbatim into a field whose vocabulary is CLOSED at P0-P3. The writer
lives outside this repo (the filing skill's doctrine), so the contract is
enforced at the two points this repo owns:

  * scripts/board_priority_normalize.py — the canonical form a writer can
    adopt: P4/P5 -> P3, original label preserved (title untouched + a notes
    provenance marker), nothing else re-spelled, idempotent, line-wise.
  * the TR-251 commit-boundary gate — a diff that INTRODUCES an
    off-vocabulary priority is REFUSED (pre-push + CI both run it), so the
    TR-112 class (a second vocabulary regrowing silently) cannot recur.

Pinned shapes below (every case is a measured shape from the incident):
  * the EXACT filed DOC-15 row shape (P4 field, "P4 —" title) -> canonical
    P3, title byte-identical, notes marker, rest of the row untouched;
  * the exact gate diff that shipped in c45ee4f (old P4 -> new P3) passes —
    a normalization must be landable;
  * the exact regression (pending row, P4 field, no history) is REFUSED;
  * P5, bare digits, prose, corrupt fragments: refused equally;
  * history re-appends and absence are never flagged;
  * the reducer runs on BOTH boards' real files and finds nothing.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
BOARD = os.path.join(REPO, ".coding-hermes", "board", "tasks.jsonl")
sys.path.insert(0, SCRIPTS)

import board_commit_boundary_gate as gate  # noqa: E402
import board_priority_census as bpc  # noqa: E402
import board_priority_normalize as norm  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures: the shapes the incident actually produced.                         #
# --------------------------------------------------------------------------- #

def _doc_row(task_id="DOC-15", **overrides):
    """The filed DOC-15 shape: P4 in the FIELD, "P4 —" in the title."""
    row = {
        "id": task_id,
        "title": "P4 — Document API readiness, model-browser, rollup, outcome, listing, and serving-mode routes",
        "status": "pending",
        "priority": "P4",
    }
    row.update(overrides)
    return row


def _lines(*rows):
    return [json.dumps(r) for r in rows]


# --------------------------------------------------------------------------- #
# The writer contract: the adapter maps docs severities canonically.          #
# --------------------------------------------------------------------------- #

def test_docs_severity_map_is_the_lowest_canonical_tier():
    """P4 AND P5 both land on P3 — the LOWEST canonical tier (P0-P3), so a
    doc row is visible but can never outrank functional work."""
    assert norm.SEVERITY_MAP == {"P4": "P3", "P5": "P3"}
    for severity in norm.SEVERITY_MAP:
        assert norm.canonical_priority(severity) == "P3"


def test_adapter_maps_a_fresh_docs_audit_row_injection():
    """THE AC3 fixture: a fresh docs-audit row carrying P4/P5 in the field
    canonicalizes to P3 while the original label survives legibly."""
    fresh = _doc_row("DOC-15")
    out, changed = norm.canonicalize_row(fresh)
    assert changed
    assert out["priority"] == "P3"
    # The original severity survives in TWO places: the untouched title...
    assert out["title"] == fresh["title"]
    assert out["title"].startswith("P4 — ")
    # ...and a machine-checkable notes marker naming both values.
    assert "P4" in str(out["notes"])
    assert norm.CANON_NOTE_MARKER in str(out["notes"])


def test_adapter_touches_only_priority_and_notes():
    """The canonicalization must not silently edit anything else on the row."""
    row = _doc_row("DOC-6", acceptance_criteria=["a"], complexity=1)
    out, changed = norm.canonicalize_row(row)
    assert changed
    delta = {k for k in set(row) | set(out) if row.get(k) != out.get(k)}
    assert delta == {"priority", "notes"}, delta


def test_adapter_is_idempotent():
    once, first = norm.canonicalize_row(_doc_row("DOC-5"))
    twice, second = norm.canonicalize_row(once)
    assert first and not second
    assert twice == once


def test_adapter_never_guesses_unmapped_values():
    """Bare digits, prose, corrupt fragments: NOT the adapter's business —
    those are refused at the boundary, never silently re-spelled."""
    for value in ("3", "high", "Critical", 'P0","source":"x', "PP3", "P6"):
        out, changed = norm.canonicalize_row({"id": "X", "priority": value})
        assert not changed, value
        assert out["priority"] == value


def test_adapter_leaves_canonical_and_absent_alone():
    for value in ("P0", "P1", "P2", "P3", None, ""):
        out, changed = norm.canonicalize_row({"id": "X", "priority": value})
        assert not changed, value
        assert out["priority"] == value


def test_adapter_dry_run_and_apply_on_a_tmp_board(tmp_path):
    """End-to-end CLI contract on a throwaway board: dry run reports and
    changes nothing; --apply rewrites line-wise; a second --apply is a
    no-op guard; the result re-verifies against the census classifier."""
    board = tmp_path / "tasks.jsonl"
    rows = [_doc_row("DOC-A"), {"id": "TR-1", "priority": "P2"},
            {"id": "TR-2"}]  # absent priority stays absent
    board.write_text("".join(json.dumps(r) + "\n" for r in rows))

    dry = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_priority_normalize.py"),
         "--board", str(board)],
        capture_output=True, text=True)
    assert dry.returncode == 3
    assert "DOC-A" in dry.stdout

    applied = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_priority_normalize.py"),
         "--board", str(board), "--apply"],
        capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr

    out_rows = [json.loads(ln) for ln in board.read_text().splitlines() if ln.strip()]
    assert len(out_rows) == 3, "line count must survive the rewrite"
    assert out_rows[0]["priority"] == "P3"
    assert out_rows[0]["title"].startswith("P4 — ")
    assert out_rows[1] == rows[1], "untouched row must keep its exact values"
    assert out_rows[2] == rows[2], "absent priority must stay absent"

    # Whole-board verify: only canonical values or absent ones remain.
    parsed = norm.read_rows(str(board))
    assert all(bpc.classify(r.get("priority")) in ("canonical", "absent")
               for _, r in parsed)

    again = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_priority_normalize.py"),
         "--board", str(board), "--apply"],
        capture_output=True, text=True)
    assert again.returncode == 2, "a second --apply must refuse as a no-op"


def test_apply_preserves_untouched_lines_byte_for_byte(tmp_path):
    """The line-wise law: a row NOT under normalization keeps its exact
    bytes, key order included — the board diff must carry only the target
    rows (never a wholesale reserialization)."""
    board = tmp_path / "tasks.jsonl"
    untouched = '{"id": "Z-1", "priority": "P1", "zkey": "v"}\n'
    board.write_text(untouched + json.dumps(_doc_row("DOC-B")) + "\n")
    applied = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_priority_normalize.py"),
         "--board", str(board), "--apply"],
        capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    assert board.read_text().splitlines(keepends=True)[0] == untouched


# --------------------------------------------------------------------------- #
# The boundary: the gate refuses a diff that introduces off-vocabulary.       #
# --------------------------------------------------------------------------- #

def test_gate_refuses_the_exact_recurring_shape():
    """The incident's diff: a docs-audit row lands PENDING with priority P4
    and no history. This is the emission that must never pass again."""
    old: list[str] = []
    new = _lines(_doc_row("DOC-15"))
    introduced = gate.priorities_introduced(old, new)
    assert [i["id"] for i in introduced] == ["DOC-15"]
    refusals = gate.check_introduced_priorities(introduced)
    assert refusals and "DOC-15" in refusals[0] and "P4" in refusals[0]


def test_gate_refusal_names_the_mapping():
    refusals = gate.check_introduced_priorities(
        [{"id": "DOC-9", "priority": "P4", "why": "off-vocabulary"}])
    assert "board_priority_normalize" in refusals[0]
    assert "P4/P5 -> P3" in refusals[0]


def test_gate_refuses_every_off_vocabulary_class_equally():
    """P5, bare digits, prose and a corrupt fragment are the same defect
    class at the boundary: a value the closed vocabulary does not define."""
    for value, why in (("P5", "off-vocabulary"), ("3", "off-vocabulary"),
                       ("High", "off-vocabulary"),
                       ('P0","src":"x', "corrupt-fragment")):
        introduced = gate.priorities_introduced(
            [], _lines({"id": "X", "priority": value}))
        assert len(introduced) == 1 and introduced[0]["why"] == why, value


def test_gate_passes_the_shipped_normalization_diff():
    """THE landability fixture: exactly the c45ee4f shape (old P4 -> new P3)
    must PASS, or the already-landed AC1/AC2 work could not have been done
    and a future re-normalization would be blocked."""
    old = _lines({"id": "DOC-5", "title": "P4 — t", "status": "pending",
                  "priority": "P4"})
    new = _lines({"id": "DOC-5", "title": "P4 — t", "status": "pending",
                  "priority": "P3"})
    assert gate.priorities_introduced(old, new) == []


def test_gate_passes_history_reappend_of_an_off_vocabulary_row():
    """A row whose off-vocabulary value was ALREADY carried on the old side
    is history (a re-append/rewrite), not a new emission — the gate must
    not re-judge it, or every read of a legacy board would wedge commits."""
    row = {"id": "OLD-1", "priority": "P4"}
    assert gate.priorities_introduced(_lines(row), _lines(row)) == []


def test_gate_ignores_absent_and_canonical_values():
    new = _lines({"id": "A", "priority": "P1"},
                 {"id": "B"})
    assert gate.priorities_introduced([], new) == []


def test_gate_cli_end_to_end_refuses(tmp_path):
    """File-mode CLI: the pre-push/CI invocation shape refuses the bad diff
    and passes the clean one, and the refusal names the row."""
    old_f, new_f = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    old_f.write_text("")
    new_f.write_text(json.dumps(_doc_row("DOC-15")) + "\n")
    bad = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_commit_boundary_gate.py"),
         "--old-file", str(old_f), "--new-file", str(new_f)],
        capture_output=True, text=True)
    assert bad.returncode == 1
    assert "DOC-15" in bad.stderr

    old_f.write_text(json.dumps({"id": "DOC-15", "priority": "P4"}) + "\n")
    new_f.write_text(json.dumps({"id": "DOC-15", "priority": "P3"}) + "\n")
    good = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "board_commit_boundary_gate.py"),
         "--old-file", str(old_f), "--new-file", str(new_f)],
        capture_output=True, text=True)
    assert good.returncode == 0, good.stderr


# --------------------------------------------------------------------------- #
# Both boards, live: the guard must find nothing on what actually shipped.    #
# --------------------------------------------------------------------------- #

def test_main_board_is_canonical_and_normalize_is_a_noop():
    rows = norm.read_rows(BOARD)
    assert rows, "no rows parsed from the main board"
    assert all(bpc.classify(r.get("priority")) in ("canonical", "absent")
               for _, r in rows)


def test_worktree_board_is_canonical_and_normalize_is_a_noop():
    """THIS worktree's board — the one this row's diff touches — carries
    only canonical values, so the new gate arm does not wedge the landing
    of its own fix."""
    wt_board = os.path.join(REPO, ".coding-hermes", "board", "tasks.jsonl")
    rows = norm.read_rows(wt_board)
    assert rows
    assert all(bpc.classify(r.get("priority")) in ("canonical", "absent")
               for _, r in rows)


def test_adapter_and_gate_agree_on_the_vocabulary_source():
    """Single-sourced vocabulary: the adapter, the gate and the census must
    never carry three spellings of {P0..P3}."""
    assert norm.CANONICAL_VOCABULARY is bpc.VOCABULARY
    assert gate.PRIORITY_VOCABULARY is bpc.VOCABULARY


# --------------------------------------------------------------------------- #
# The 8 shipped rows: AC2 legibility preserved (regression pin).              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("task_id", ["DOC-5", "DOC-6", "DOC-7", "DOC-13",
                                     "DOC-14", "DOC-15", "DOC-16", "DOC-17"])
def test_the_eight_normalized_rows_still_carry_p4_legibly(task_id):
    """AC2 preservation: canonical P3 in the field, the original "P4 —"
    wording still legible in the title."""
    rows = {rid: row for _, rid, row in
            [(_ln, r["id"], r) for _ln, r in norm.read_rows(BOARD)]}
    row = rows[task_id]
    assert row["priority"] == "P3", task_id
    assert str(row.get("title", "")).startswith("P4 — "), task_id


def test_board_row_ids_never_duplicated_by_the_adapter_fixtures():
    """Guard against test fixtures leaking into the real board."""
    ids = [rid for _, rid in norm.read_rows(BOARD)]
    assert "DOC-A" not in ids and "DOC-B" not in ids

"""TR-148 in the prober — a verified probe_fixes row must win over the prefix guess.

clinepass ':free' lanes answer ONLY at the GET /models vendor-org id
('nvidia/nemotron-3.5-lightning:free'); the bare and 'cline-pass/<bare>:free'
forms answer 400/404. probe_fixes.jsonl has carried a verified row per lane
since 2026-09-09, and router_wire_ids.py is the documented data-first
transform — but router_probe_run.py kept its OWN copy of the rule that only
knew the provider prefix. Every clinepass free lane therefore probed a bogus
id and filed [not_served] 404: 7 of the 8 lanes on the evidence card were
unmeasurable while the fix sat in a table nobody read.

Hermetic: ROUTING_DATA_DIR points at a tmp table dir; no network, no
credentials. The census test asserts the COMMITTED table still carries the
vendor-org fix for the lanes the card names.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import router_probe_run as rpr  # noqa: E402
import router_wire_ids as rwi  # noqa: E402

#: (registry id, the id the provider actually serves) — verified against the
#: live GET /api/v1/models catalog (452 ids, 2026-11-15).
CLINEPASS_FREE = (
    ("dots-3-note-preview:free", "dots-studio/dots-3-note-preview:free"),
    ("flint-v1.0:free", "recursive/flint-v1.0:free"),
    ("nemotron-3-nano-omni-30b-a3b-reasoning:free",
     "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
    ("nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("nemotron-3.5-lightning:free", "nvidia/nemotron-3.5-lightning:free"),
    ("nemotron-3.5-super-vl:free", "nvidia/nemotron-3.5-super-vl:free"),
    ("north-mini-code:free", "cohere/north-mini-code:free"),
)


@pytest.fixture
def table_dir(monkeypatch, tmp_path):
    """A tmp probe_fixes table; the module caches are reset around it."""
    d = tmp_path / "tables"
    d.mkdir()
    monkeypatch.setenv("ROUTING_DATA_DIR", str(d))
    rwi.reset_cache()
    yield d
    rwi.reset_cache()


def _write_fixes(d, rows):
    with open(d / "probe_fixes.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_verified_fix_wins_over_prefix(table_dir):
    _write_fixes(table_dir, [{"provider": "clinepass", "model": "north-mini-code:free",
                              "fix_to": "cohere/north-mini-code:free"}])
    assert rpr.wire_model_id("clinepass", "north-mini-code:free") == \
        "cohere/north-mini-code:free"


def test_last_row_wins(table_dir):
    """Append-only ledger: the newest verdict for a lane is the live truth."""
    _write_fixes(table_dir, [
        {"provider": "clinepass", "model": "glm-5.3", "fix_to": "z-ai/glm-5.3"},
        {"provider": "clinepass", "model": "glm-5.3", "fix_to": "cline-pass/glm-5.3"},
    ])
    assert rpr.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"


def test_prefix_default_survives_without_a_fix(table_dir):
    """No row for the lane -> the provider default still applies (TR-233)."""
    _write_fixes(table_dir, [])
    assert rpr.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"


def test_other_providers_stay_verbatim(table_dir):
    _write_fixes(table_dir, [])
    assert rpr.wire_model_id("groq", "gpt-oss-120b") == "gpt-oss-120b"


def test_slash_ids_pass_through(table_dir):
    """Already wire-shaped ids are never re-prefixed (idempotent)."""
    _write_fixes(table_dir, [])
    assert rpr.wire_model_id("clinepass", "nvidia/nemotron-3.5-lightning:free") == \
        "nvidia/nemotron-3.5-lightning:free"


def test_missing_table_never_raises(table_dir):
    """A broken/absent table must not take the prober down."""
    assert rpr.wire_model_id("clinepass", "glm-5.3") == "cline-pass/glm-5.3"


def test_committed_table_carries_the_free_lane_fixes():
    """Census over the COMMITTED data: the card's lanes keep their vendor-org fix."""
    rwi.reset_cache()
    path = os.path.join(REPO, "data", "tables", "probe_fixes.jsonl")
    fixes = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("provider") == "clinepass" and r.get("fix_to"):
                fixes[r["model"]] = r["fix_to"]  # file order: last row wins
    missing = [(m, want) for m, want in CLINEPASS_FREE if fixes.get(m) != want]
    assert not missing, f"probe_fixes lost the vendor-org fix for: {missing}"

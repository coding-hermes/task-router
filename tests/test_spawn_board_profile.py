"""TR-124 — board rows can declare their own capability profile.

Bane's verified gap (2026-09-24): the tier gate exists and works, but 0/137
board rows carry a `profile` field, so the spawn path always resolved a FIXED
profile (P1_CODING/P1_WORKER, code_gen>=-2 — a lane with NO tier data scores
-1 and still clears three of four bars). The board could not express the
complexity it wants.

The wiring (profile levels themselves are an OWNER decision, deliberately
untouched):
  - a board task row may carry `"profile": "<id-or-tag>"`;
  - `router_spawn.py --profile-from-board <task-id> [--board path]` reads the
    row and resolves through THAT profile's actual bars;
  - no field / no row / unknown id / unreadable board all degrade VISIBLY to
    the caller's normal profile path — fail-open, scheduler never blocked.

Pins below cover the reader (profile_ref_for), the resolve() tier difference
(a strict declared bar really drops lanes a lenient default keeps), and the
CLI wiring end-to-end via subprocess (matched override of the project default,
backward compat without the field, missing-board fail-open, matched
declaration skipping complexity scoring).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_spawn as rs  # noqa: E402


# ---------------------------------------------------------------- fixtures ----

def _write_board(tmp_path, rows, name="tasks.jsonl"):
    d = tmp_path / ".coding-hermes" / "board"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def _tier_tables():
    """Two profiles, two lanes: 'strong' clears a code_gen>=1 bar, 'weak'
    only clears the lenient code_gen>=-2 bar. Same registry otherwise."""
    return {
        "models": [
            {"provider": "prov", "model": "weak", "normalized_price": 0.1},
            {"provider": "prov", "model": "strong", "normalized_price": 1.0},
        ],
        "model_tier": [
            {"model": "weak", "category": "code_gen", "tier": -2},
            {"model": "strong", "category": "code_gen", "tier": 3},
        ],
        "task_profiles": [
            {"id": "P_LEN", "title": "lenient default"},
            {"id": "P_BAR", "title": "declared bar"},
        ],
        "task_profile_requirements": [
            {"task_id": "P_LEN", "category": "code_gen", "level": -2},
            {"task_id": "P_BAR", "category": "code_gen", "level": 1},
        ],
        "projects": [{"id": "proj", "profile": "P_LEN"}],
        "providers": [], "fallback_lanes": [], "plan_terms": [],
    }


@pytest.fixture
def tier_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (_tier_tables(), "test", False, None))
    # house pattern (tests/test_lifecycle_states.py): hermetic gate state —
    # the one provider OPEN, health/circuit files absent (gates open,
    # fail-open). An EMPTY state dir would gate every lane on missing quota.
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(
        '{"updated": "test", "providers": {"prov": {"status": "open"}}}')
    monkeypatch.setattr(rs, "MR", str(state))
    return state


# ------------------------------------------------- profile_ref_for (reader) ----

def test_declared_profile_id_is_returned(tmp_path):
    p = _write_board(tmp_path, [{"id": "T-1", "title": "x", "profile": "P1_CODING"}])
    ref, meta = rs.profile_ref_for(task_id="T-1", board=p)
    assert ref == "P1_CODING"
    assert meta["matched"] is True and meta["declared"] == "P1_CODING"
    assert meta["profile_id"] == "P1_CODING" and meta["source_path"] == p


def test_tag_declaration_resolves_to_the_version_row(tmp_path):
    """Tags resolve like --profile (TR-020): the row names the tag, resolve()
    gets the id of the tagged version row."""
    tables = _tier_tables()
    tables["task_profiles"].append(
        {"id": "P_BAR_v2", "title": "retag", "tag": "P_BAR", "version": 2})
    monkey_tables = tables
    orig = rs._load_registry

    def fake_load():
        return monkey_tables
    rs._load_registry = fake_load  # no monkeypatch: reader runs outside pytest ctx too
    try:
        p = _write_board(tmp_path, [{"id": "T-1", "title": "x", "profile": "P_BAR"}])
        ref, meta = rs.profile_ref_for(task_id="T-1", board=p)
    finally:
        rs._load_registry = orig
    assert ref == "P_BAR_v2" and meta["matched"] is True


def test_row_without_profile_field_says_so(tmp_path):
    p = _write_board(tmp_path, [{"id": "T-1", "title": "x"}])
    ref, meta = rs.profile_ref_for(task_id="T-1", board=p)
    assert ref is None and meta["matched"] is False
    assert any("no profile field" in w for w in meta["problems"])


def test_missing_row_says_so_and_fails_open(tmp_path):
    p = _write_board(tmp_path, [{"id": "T-1", "title": "x", "profile": "P1_CODING"}])
    ref, meta = rs.profile_ref_for(task_id="T-404", board=p)
    assert ref is None and meta["matched"] is False
    assert meta["requested"] == "T-404"
    assert any("not found" in w for w in meta["problems"])


def test_unknown_profile_id_degrades_with_near_miss(tmp_path, capsys):
    p = _write_board(tmp_path, [{"id": "T-1", "title": "x", "profile": "p1_coding"}])
    ref, meta = rs.profile_ref_for(task_id="T-1", board=p)
    assert ref is None and meta["matched"] is False
    assert meta["declared"] == "p1_coding"
    assert any("not in registry" in w and "P1_CODING" in w
               for w in meta["problems"])


def test_unreadable_board_fails_open(tmp_path):
    ref, meta = rs.profile_ref_for(task_id="T-1",
                                   board=str(tmp_path / "nope" / "tasks.jsonl"))
    assert ref is None and meta["matched"] is False


# ------------------------------- resolve(): the declared bars really apply ----

def test_matched_declaration_runs_the_declared_bars(tier_registry):
    """The AC: a row declaring a STRICT profile drops lanes the lenient default
    keeps — the bars on the chain are the row's, not a hardcoded default."""
    ref_meta = {"requested": "T-1", "matched": True, "declared": "P_BAR",
                "profile_id": "P_BAR", "source_path": "/tmp/x"}
    doc = rs.resolve(project="proj", ref_meta=ref_meta, use_health=False)
    assert doc["profile"] == "P_BAR", doc.get("error")
    models = {h["model"] for h in doc["chain"]}
    assert "strong" in models
    assert "weak" not in models, "code_gen -2 must fail the declared >=1 bar"
    assert doc["board_profile"] == ref_meta


def test_unmatched_declaration_falls_back_to_project_bars(tier_registry):
    """Backward compat: no match -> the project row's own profile, untouched."""
    ref_meta = {"requested": "T-404", "matched": False,
                "problems": ["task T-404 not found in 1 board path(s)"]}
    doc = rs.resolve(project="proj", ref_meta=ref_meta, use_health=False)
    assert doc["profile"] == "P_LEN"
    models = {h["model"] for h in doc["chain"]}
    assert "weak" in models, "the lenient default must keep routing the cheap lane"
    assert doc["board_profile"] == ref_meta


def test_resolve_without_the_channel_has_null_provenance(tier_registry):
    doc = rs.resolve(project="proj", use_health=False)
    assert doc["board_profile"] is None, "additive key, null when unused"


# ----------------------------------------------------- main() CLI wiring ----

def _cli_env(tmp_path):
    """Hermetic state + every real provider whitelisted OPEN (the TR-059 house
    pattern — _open_state_env): the assertions need a REAL chain, and absent
    gate state is fail-closed by design."""
    import shutil
    data = tmp_path / "data"
    shutil.copytree(os.path.join(REPO, "data", "tables"), data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    env = {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state)}
    with open(os.path.join(data, "providers.jsonl")) as f:
        providers = [json.loads(l)["id"] for l in f if l.strip()]
    with open(os.path.join(state, "quota-state.json"), "w") as f:
        json.dump({"updated": "test",
                   "providers": {p: {"status": "open"} for p in providers}}, f)
    return env


def _run_main(args, env):
    import subprocess
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts",
                                                        "router_spawn.py"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_cli_resolves_the_row_profile_over_the_project_default(tmp_path):
    """The fleet call shape: `router spawn <project> --profile-from-board <tid>`
    — the row's declaration outranks the project row's default profile."""
    board = _write_board(tmp_path, [{"id": "T-9", "title": "x",
                                     "profile": "P1_WORKER"}])
    p = _run_main(["9router", "--profile-from-board", "T-9",
                   "--board", board, "--format", "json"], _cli_env(tmp_path))
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert "error" not in doc, doc.get("error")
    assert doc["profile"] == "P1_WORKER"
    assert doc["board_profile"]["matched"] is True
    assert doc["board_profile"]["declared"] == "P1_WORKER"


def test_cli_without_the_field_keeps_the_caller_profile(tmp_path):
    """Backward compat: a row without `profile` resolves exactly as before."""
    board = _write_board(tmp_path, [{"id": "T-9", "title": "x"}])
    p = _run_main(["P1_CODING", "--profile-from-board", "T-9",
                   "--board", board, "--format", "json"], _cli_env(tmp_path))
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert "error" not in doc, doc.get("error")
    assert doc["profile"] == "P1_CODING"
    assert doc["board_profile"]["matched"] is False
    assert doc["chain"], "fail-open: a chain still resolves"


def test_cli_missing_board_is_fail_open(tmp_path):
    p = _run_main(["P1_CODING", "--profile-from-board", "T-9",
                   "--board", str(tmp_path / "absent" / "tasks.jsonl"),
                   "--format", "json"], _cli_env(tmp_path))
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert "error" not in doc, doc.get("error")
    assert doc["profile"] == "P1_CODING"
    assert doc["board_profile"]["matched"] is False
    assert doc["chain"], "the scheduler must never be blocked by the board lookup"


def test_cli_matched_declaration_skips_complexity_scoring(tmp_path):
    """A declared profile IS the complexity contract — scoring is skipped
    entirely (mirrors the proxy's x-router-profile precedence), so no scorer
    call happens even when --from-task text is available."""
    board = _write_board(tmp_path, [{"id": "T-9", "title": "deadlock under race",
                                     "profile": "P1_CODING"}])
    p = _run_main(["--profile-from-board", "T-9", "--from-task", "T-9",
                   "--board", board, "--format", "json"], _cli_env(tmp_path))
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert "error" not in doc, doc.get("error")
    assert doc["profile"] == "P1_CODING"
    assert doc["board_profile"]["matched"] is True
    assert "complexity" not in doc, "declared profile must skip the scorer"

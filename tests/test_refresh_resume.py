"""TR-086: refresh resume helper tests."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import router_refresh_resume as rrr  # noqa: E402


def test_resume_plan_is_fail_open_and_wellshaped():
    plan = rrr.build_plan()
    assert "error" not in plan or plan.get("error") is None
    assert plan["head"]["commit"]
    assert plan["last_good_registry_commit"]["commit"]
    assert "idempotent_steps" in plan["steps"]
    assert "partial_state" in plan["steps"]
    # registry_dirty entries must all be registry tables
    for d in plan["resume"]["registry_dirty"]:
        assert d["path"] in rrr.REGISTRY_TABLES


def test_resume_cli_exits_zero_with_json():
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "router_refresh_resume.py")],
        capture_output=True, text=True)
    assert proc.returncode == 0  # fail-open contract
    plan = json.loads(proc.stdout)
    assert plan["head"]["commit"]


def test_idempotent_steps_declared():
    names = [n for n, _ in rrr.PIPELINE_STEPS]
    assert names == ["phase0_reconfigure", "phase1_data_quality",
                     "phase2_seed", "phase3_export", "phase4_tests"]


# ─── TR-108: the plan reports registry freshness + a concrete self-heal ─────

def _tmp_repo(tmp_path, stale=False, missing_registry=False):
    """A minimal REPO-shaped tree for the freshness predicates: data/tables
    with one table + a registry.json (mtime-controlled). No seed involved —
    registry_staleness() only reads mtimes/files."""
    data = tmp_path / "data" / "tables"
    data.mkdir(parents=True)
    (data / "probe_gaps.jsonl").write_text('{"k": "v"}\n')
    reg = tmp_path / "registry.json"
    reg.write_text("{}")
    now = time.time()
    if missing_registry:
        reg.unlink()
    elif stale:
        os.utime(reg, (now - 3600, now - 3600))  # registry 1h older
    else:
        os.utime(reg, (now + 10, now + 10))  # registry newer -> fresh
    return tmp_path


def test_plan_reports_freshness_and_heal_command(monkeypatch, tmp_path):
    monkeypatch.setattr(rrr, "REPO", _tmp_repo(tmp_path))
    plan = rrr.build_plan()
    assert plan["registry_freshness"]["ok"] is True
    assert plan["self_heal"]["needed"] is False
    assert plan["self_heal"]["command"] == (
        "ROUTER_VALIDATE_HEAL=1 python3 scripts/router_validate.py --heal --json")


def test_plan_flags_stale_registry_as_heal_needed(monkeypatch, tmp_path):
    """The 2026-09-22 shape: tables newer than the registry beyond the slack
    (no content match) -> needed=True, same verdict `router validate` enforces."""
    monkeypatch.setattr(rrr, "REPO", _tmp_repo(tmp_path, stale=True))
    plan = rrr.build_plan()
    assert plan["registry_freshness"]["ok"] is False
    assert plan["self_heal"]["needed"] is True


def test_plan_flags_missing_registry_as_heal_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(rrr, "REPO", _tmp_repo(tmp_path, missing_registry=True))
    plan = rrr.build_plan()
    assert plan["registry_freshness"]["ok"] is False
    assert "missing" in plan["registry_freshness"]["detail"]
    assert plan["self_heal"]["needed"] is True


def test_plan_without_data_dir_is_fail_open(monkeypatch, tmp_path):
    """No data/tables at all -> the freshness predicates can't run; the plan
    carries None + needed=False instead of guessing."""
    monkeypatch.setattr(rrr, "REPO", tmp_path)
    assert rrr.registry_staleness() is None
    assert rrr.registry_freshness_blocks_tests() is False
    plan = rrr.build_plan()
    assert plan["registry_freshness"] is None
    assert plan["self_heal"]["needed"] is False


def test_rescue_executes_heal_when_stale(monkeypatch, tmp_path):
    """--commit on the interrupted-cron shape runs the self-heal seed BEFORE
    the follow-up test phase (plan carries the executed result)."""
    monkeypatch.setattr(rrr, "REPO", _tmp_repo(tmp_path, stale=True))
    plan = rrr.build_plan(commit_partial=True)
    executed = plan["self_heal"]["executed"]
    assert executed["ok"] is False  # the tmp repo has no real seed inputs —
    # the point is that the rescue RAN the heal and reported it fail-open
    assert "seed" in executed["detail"]


def test_rescue_skips_heal_when_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(rrr, "REPO", _tmp_repo(tmp_path))
    plan = rrr.build_plan(commit_partial=True)
    assert "executed" not in plan["self_heal"]
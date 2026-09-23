"""TR-086: refresh resume helper tests."""
import json
import subprocess
import sys
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
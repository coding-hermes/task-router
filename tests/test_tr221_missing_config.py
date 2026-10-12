"""TR-221: ranking probe remains usable without fleet-only config."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
PY = sys.executable


def test_probe_run_load_providers_missing_config_is_empty(tmp_path, monkeypatch):
    sys.path.insert(0, SCRIPTS)
    import router_probe_run as probe_run

    monkeypatch.setattr(probe_run, "CONFIG", str(tmp_path / "missing.yaml"))
    assert probe_run.load_providers() == {}


def test_tier_probe_dry_run_missing_config_classifies_no_credentials(tmp_path):
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "models.jsonl").write_text(
        '{"provider":"fake-provider","model":"fake-model",'
        '"normalized_price":1.0,"archive":false,"valid_to":null,'
        '"disabled":null,"public_out_per_m":1.0}\n',
        encoding="utf-8",
    )
    for name in ("model_tier", "probe_gaps", "probe_excludes"):
        (tables / f"{name}.jsonl").write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "ROUTING_DATA_DIR": str(tables),
        "ROUTER_HERMES_CONFIG": str(tmp_path / "missing.yaml"),
    })
    result = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"), "--dry-run"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "no-credentials" in result.stdout
    assert "DRY RUN" in result.stdout

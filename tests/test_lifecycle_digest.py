"""TR-069 digest tests — the `router lifecycle` command (human view of the
same lifecycle data the resolver uses). Pins: CLI contract, export map,
report content under a frozen clock, fail-open on unreadable registry."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

import router_lifecycle as rl  # noqa: E402
import router_spawn as rs  # noqa: E402
from task_router import cli  # noqa: E402


ROWS = [
    {"provider": "p", "model": "plain", "normalized_price": 1.0},
    {"provider": "ollama-cloud", "model": "deepseek-v4-flash:0731",
     "normalized_price": 0.1, "valid_to": "2026-09-25",
     "replaced_by": "ollama-cloud/deepseek-v4.1-flash",
     "lifecycle_source": "provider-announcement:test"},
    {"provider": "p", "model": "ghost", "available_from": "2026-10-01"},
    {"provider": "p", "model": "gone", "valid_to": "2026-09-01"},
]


def test_digest_report_groups_by_state_frozen_clock():
    today = "2026-09-19"
    report = rl.build_report(ROWS, today=today, show_retired=True)
    assert "live 1 | coming_soon 1 | retiring 1 | retired 1" in report
    assert "deepseek-v4-flash:0731" in report
    assert "2026-09-25" in report and "in 6d" in report
    assert "-> ollama-cloud/deepseek-v4.1-flash" in report
    assert "ghost" in report and "2026-10-01" in report  # coming soon section
    assert "gone" in report and "2026-09-01" in report   # retired section


def test_digest_uses_the_shared_state_rule():
    """The digest must classify with lifecycle_state (the ONE rule), not its
    own date logic — pin by shifting the clock and watching states move."""
    today = "2026-10-02"  # ghost's available_from has passed -> live; 0731 retired
    report = rl.build_report(ROWS, today=today, show_retired=False)
    assert "retiring 0" in report
    assert "coming_soon 0" in report
    assert "live 2" in report


def test_digest_registered_in_cli_contract():
    assert "lifecycle" in cli.COMMANDS
    assert cli.COMMANDS["lifecycle"] == "router_lifecycle.py"


def test_digest_exports_registry_view():
    """Same contract as spawn/chain-run: the digest must read the registry the
    resolver reads (TR-056 defect class: divergent fleet views)."""
    exports = cli._home_env_exports()["lifecycle"]
    assert exports["ROUTING_REGISTRY"] == cli._home_env_exports()["spawn"]["ROUTING_REGISTRY"]


def test_digest_nothing_readable_is_empty_not_crash(tmp_path, capsys):
    """No registry anywhere -> empty rows (fail-open display, never a crash)."""
    rows = rl._load_rows_from([str(tmp_path / "a.json"), str(tmp_path / "b.json")])
    assert rows == []


def test_digest_reads_tables_models_shape(tmp_path, capsys, monkeypatch):
    reg = {"version": 1, "tables": {"models": ROWS}}
    monkeypatch.setenv("ROUTING_REGISTRY", str(tmp_path / "reg.json"))
    (tmp_path / "reg.json").write_text(json.dumps(reg))
    rc = rl.main(["--today", "2026-09-19"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "retiring 1" in out and "coming_soon 1" in out

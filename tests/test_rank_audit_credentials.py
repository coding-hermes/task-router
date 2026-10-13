"""TR-223 — the audit names credential-blocked unranked lanes instead of
flagging them as work.

Acceptance (the credential half of the row): "either a working key, or the
rows carry an explicit reason (credential-expired) so the audit stops
flagging them as unranked-with-no-explanation."

The registry is synthetic (zero network, zero credential reads): two openrouter
lanes + one healthy provider lane, all active and priced, none with tier rows.
The audit must (a) count all three as unranked, (b) name openrouter's 2 as
credential_blocked_unranked with the reason credential-expired, (c) EXCLUDE
openrouter from next-wave probe targets (work nobody can do), and (d) keep the
healthy provider lane as an actionable target.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
SCRIPT = os.path.join(SCRIPTS, "router_rank_audit.py")

sys.path.insert(0, SCRIPTS)
import router_rank_audit as rra  # noqa: E402


def _tables(tmp_path):
    d = tmp_path / "tables"
    d.mkdir()
    models = [
        # two openrouter lanes: healthy-looking, priced, no tiers — but the key
        # is expired, so no probe can ever measure them
        {"provider": "openrouter", "model": "vendor/a", "normalized_price": 1.0,
         "valid_to": None, "archive": False, "disabled": None},
        {"provider": "openrouter", "model": "vendor/b", "normalized_price": 1.0,
         "valid_to": None, "archive": False, "disabled": None},
        # a lane that IS measurable: must remain an actionable target
        {"provider": "healthy", "model": "m1", "normalized_price": 1.0,
         "valid_to": None, "archive": False, "disabled": None},
    ]
    minimal = {"provider": "p", "model": "x", "valid_to": None}
    (d / "models.jsonl").write_text(
        "\n".join(json.dumps(m) for m in models) + "\n", encoding="utf-8")
    for name in ("model_tier", "category_levels", "model_perf"):
        (d / f"{name}.jsonl").write_text("", encoding="utf-8")
    (d / "category_levels.jsonl").write_text(
        json.dumps({"category": "code_gen", "level": 1, "min_perf": 0.5}) + "\n",
        encoding="utf-8")
    (d / "probe_gaps.jsonl").write_text("", encoding="utf-8")
    del minimal
    return d


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    d = _tables(tmp_path)
    monkeypatch.setenv("ROUTING_DATA_DIR", str(d))
    monkeypatch.setattr(rra, "DATA_DIR", str(d))
    return d


def _run(hermetic, *flags):
    env = dict(os.environ, ROUTING_DATA_DIR=str(hermetic))
    return subprocess.run(
        [sys.executable, SCRIPT, *flags], env=env, capture_output=True, text=True)


def test_credential_block_reason_names_openrouter_only():
    assert rra.credential_block_reason("openrouter") == "credential-expired"
    assert rra.credential_block_reason("healthy") is None
    assert rra.credential_block_reason("unknown-provider") is None


def test_json_names_credential_blocked_slice(hermetic):
    r = _run(hermetic, "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    # all three lanes are still counted unranked — nothing is hidden
    assert doc["unranked"] == 3
    # but the credential slice is NAMED with its reason, not silently mixed in
    assert doc["credential_blocked_unranked"] == {"openrouter": 2}


def test_next_wave_excludes_credential_blocked_providers(hermetic):
    r = _run(hermetic, "--next-wave")
    assert r.returncode == 0
    providers = set()
    models = []
    for line in r.stdout.splitlines():
        if line.startswith("providers="):
            providers = set(line.split("=", 1)[1].split(","))
        elif line.startswith("models="):
            models = line.split("=", 1)[1].split(",")
    assert "openrouter" not in providers      # work nobody can do is not listed
    assert "healthy" in providers             # the measurable lane remains
    assert models == ["m1"]


def test_text_report_carries_the_credential_line(hermetic):
    r = _run(hermetic)
    assert r.returncode == 0
    assert "CREDENTIAL-BLOCKED" in r.stdout
    assert "credential-expired" in r.stdout
    # the actionable sample is NOT drowned in 2 unmeasurable openrouter rows
    assert "vendor/a" not in r.stdout

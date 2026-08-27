"""Smoke suite for the task-router runtime tools.

The scripts are pure-JSON clients (registry.json + state JSON files) — no
duckdb required. Tests run against the repo's live registry.json (the default
path the scheduler's SCHEDULER_ROUTER_CMD uses), which is exactly what the
resolver reads in production.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")


def run(*args, timeout=60):
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, timeout=timeout
    )


def test_list_profiles_lists_seeded_profiles():
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "--list-profiles")
    assert p.returncode == 0
    assert "P0_FORE" in p.stdout


def test_resolve_json_schema():
    p = run(
        os.path.join(SCRIPTS, "router_spawn.py"),
        "coding-hermes-scheduler",
        "--format",
        "json",
    )
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "profile" in data
    assert "gate" in data
    assert "chain" in data


def test_unknown_project_fail_open():
    p = run(
        os.path.join(SCRIPTS, "router_spawn.py"),
        "definitely-not-a-real-project-xyz",
        "--format",
        "json",
    )
    assert p.returncode == 0  # fail-open: NEVER blocks the scheduler
    data = json.loads(p.stdout)
    assert "error" in data


def test_adhoc_profile_returns_json():
    p = run(
        os.path.join(SCRIPTS, "router_spawn.py"),
        "--profile-req",
        "reasoning=5 debug=3 vision=-2",
        "--format",
        "json",
    )
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "gate" in data


def test_circuit_status_text():
    p = run(os.path.join(SCRIPTS, "router_circuit.py"), "status")
    assert p.returncode == 0
    assert "no open" in p.stdout or "failures=" in p.stdout

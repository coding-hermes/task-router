"""TR-339: verify the CI wiring of TR251_CI_MODE and reference-gate.

Closes the Tier-2 judge gap: the boundary gate logic itself is tested in
test_tr251_boundary_and_replay.py, but that suite never checks that the CI
workflow *wires* the environment variables and outcome_probes subcommands
that the dogfood findings surfaced.

This file reads the live .github/workflows/ci.yml and asserts the presence
of the three TR-339 sub-fixes that the Tier-2 verdict flagged as unmetered.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YAML = os.path.join(REPO, ".github", "workflows", "ci.yml")


@pytest.fixture()
def ci_content():
    """Return the full text of ci.yml."""
    with open(CI_YAML) as f:
        return f.read()


def test_ci_wires_TR251_CI_MODE_for_boundary_gate(ci_content):
    """339-03: the boundary gate step must have env: TR251_CI_MODE: '1'."""
    # Match the step name for the boundary gate
    assert "TR-251 boundary gate" in ci_content
    # The step must carry the env block
    assert 'TR251_CI_MODE' in ci_content
    assert '"TR251_CI_MODE"' in ci_content or 'TR251_CI_MODE:' in ci_content
    # Verify the env is on the boundary gate step, not just present anywhere
    # Split into steps and check the boundary gate step has the env
    steps = ci_content.split("- name:")
    for step in steps:
        if "TR-251 boundary gate" in step:
            assert "TR251_CI_MODE" in step, (
                "TR251_CI_MODE must be set on the 'TR-251 boundary gate' step")
            break
    else:
        pytest.fail("Could not find 'TR-251 boundary gate' step in ci.yml")


def test_ci_wires_TR251_CI_MODE_for_evidence_replay(ci_content):
    """339-03: the evidence replay step must ALSO have env: TR251_CI_MODE: '1'."""
    steps = ci_content.split("- name:")
    for step in steps:
        if "TR-251 evidence replay" in step:
            assert "TR251_CI_MODE" in step, (
                "TR251_CI_MODE must be set on the 'TR-251 evidence replay' step")
            break
    else:
        pytest.fail("Could not find 'TR-251 evidence replay' step in ci.yml")


def test_ci_wires_reference_gate_step(ci_content):
    """339-05: outcome_probes.py reference-gate must be wired into CI."""
    assert "reference-gate" in ci_content, (
        "CI must invoke `python3 scripts/outcome_probes.py reference-gate`")
    # Verify it's actually called as a command, not just mentioned in a comment
    # Look for the run block that invokes it
    run_blocks = re.findall(r'- name:.*?\n\s+run: \|\n((?:\s+.+\n?)*)', ci_content)
    reference_gate_called = False
    for block in run_blocks:
        if "reference-gate" in block and "outcome_probes.py" in block:
            reference_gate_called = True
            break
    assert reference_gate_called, (
        "reference-gate must be invoked as a command in a CI run block, not just a comment")


def test_ci_wires_check_all_step(ci_content):
    """339-05: outcome_probes.py check-all must be wired into CI."""
    assert "check-all" in ci_content, (
        "CI must invoke `python3 scripts/outcome_probes.py check-all`")
    run_blocks = re.findall(r'- name:.*?\n\s+run: \|\n((?:\s+.+\n?)*)', ci_content)
    check_all_called = False
    for block in run_blocks:
        if "check-all" in block and "outcome_probes.py" in block:
            check_all_called = True
            break
    assert check_all_called, (
        "check-all must be invoked as a command in a CI run block, not just a comment")

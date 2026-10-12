#!/usr/bin/env python3
"""TR-300: end-to-end goal gate — pytest wrapper.

Wraps scripts/tr300_e2e_goal_gate.py (the self-contained harness) as a
pytest test. The harness itself runs the full proof against the LIVE proxy
(:9391) and the LIVE outcome store; this wrapper executes it as a subprocess
(process isolation on purpose: the harness exports ROUTER_STATE_DIR /
ROUTING_AVERAGES_FILE at import time and must never share a process with
another test module that imports router_spawn), then asserts on the
harness's own exit status and its JSON summary artifact.

Marked SKIPPED by default: the proof needs the live proxy up and makes one
real model call. Run it explicitly:

  TR300_RUN_LIVE=1 pytest tests/test_tr300_e2e_goal_gate.py -v
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
HARNESS = os.path.join(REPO, 'scripts', 'tr300_e2e_goal_gate.py')

pytestmark = [
    pytest.mark.skipif(
        os.environ.get('TR300_RUN_LIVE', '') not in ('1', 'true', 'yes'),
        reason='live end-to-end proof: run with TR300_RUN_LIVE=1 '
               'against a healthy proxy on :9391'),
]


def test_tr300_full_loop_proves_all_acs(tmp_path):
    """Run the harness end-to-end; every AC assertion lives in the harness
    (clear per-AC messages), the wrapper verifies the verdict artifact."""
    proc = subprocess.run(
        [sys.executable, HARNESS, '--out', str(tmp_path)],
        cwd=REPO, capture_output=True, text=True, timeout=3600)
    assert proc.returncode == 0, (
        'TR-300 harness failed —\nstdout:\n' + proc.stdout[-4000:]
        + '\nstderr:\n' + proc.stderr[-4000:])
    summary_path = os.path.join(str(tmp_path), 'tr300-summary.json')
    assert os.path.exists(summary_path), \
        f'harness wrote no summary artifact at {summary_path}'
    with open(summary_path) as f:
        summary = json.load(f)
    # the harness itself claims — and every phase artifact backs — all six
    assert summary.get('all_acs_proven') is True, json.dumps(summary)[:400]
    assert set(summary.get('acs') or []) == \
        {'AC1', 'AC2', 'AC3', 'AC4', 'AC5', 'AC6'}, json.dumps(summary)[:400]
    assert summary.get('live_proxy_used') is True, json.dumps(summary)[:400]

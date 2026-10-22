#!/usr/bin/env python3
"""TR-258 addendum runbook — how to re-verify the delivered feature.

1. Start a scratch server (never on the live :9092):
     cd ~/task-router && ~/.hermes/venvs/board/bin/python3 scripts/router_server.py \
         --port 9194 --mode read-only &
2. Shape:
     curl -s http://127.0.0.1:9194/api/models | jq '{counts, lanes: (.lanes|length)}'
     curl -s http://127.0.0.1:9194/api/provider-rollups | jq '.totals'
   -> totals.reconciles must be true (sum(provider tokens) == sum(lane tokens)).
3. Tests:
     ~/.hermes/venvs/board/bin/python3 -m pytest tests/test_models_browser.py -q   # 10 pass
4. Kill the scratch server when done.
Known-red quality pins (PRE-EXISTING at HEAD b931dbb, none produced by TR-258):
  * tests/test_tr251_boundary_and_replay.py::test_digest_alone_never_passes
    (commit 1252ead changed load_command's contract to return None for a
    missing command.json — the TR-339-02 fallback — without updating the test)
  * 4 board-priority vocabulary tests (commit b931dbb added INT-CI-1 rows with
    priority:"high", an off-vocabulary value; also removed one migration note)
  * main had 3 consecutive red CI runs before TR-258 (filed as INT-CI-1 rows).
"""
from __future__ import annotations
print(__doc__)

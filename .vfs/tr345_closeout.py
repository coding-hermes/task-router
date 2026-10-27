"""TR-345 board closeout — repo's gated writer + events append (foreman-direct).

Runs the completion transition through scripts/board_complete_gate.py's
append_completion (THE gated writer, TR-249) and appends one task_completed
event to events.jsonl in the board's own shape.
"""
import json
import os
import sys
import time

REPO = '/home/kara/task-router'
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import board_complete_gate as bcg  # noqa: E402

BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')
EVENTS = os.path.join(REPO, '.coding-hermes', 'board', 'events.jsonl')

CI_RUN_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
COMMIT = 'a039048fb156513d37468b373628c54f9d0642b7'
VERDICT_DIR = '.gitreins/history/2026-10-23/4824ef27'

result = (
    '2026-10-23 (tick task-router-foreman-2026-10-23-16-14-00): fresh-install '
    'split-brain fixed in a039048 (6 files, 526 insertions). scripts/state_dir.py '
    'gains one bootstrap implementation (bootstrap_quota_state) that writes the '
    'all-OPEN starter quota-state.json into the state dir the READER resolves '
    '(never overwrites an existing file, atomic replace, fail-open, provider ids '
    'seeded from registry UNION providers.jsonl so the registry-only '
    'deepseek-duckbrain-sync is covered); scripts/router_spawn.py bootstraps an '
    'explicitly isolated ROUTER_STATE_DIR before the resolve (_maybe_bootstrap_'
    'isolated_state; the shared production dir is never bootstrapped) and the '
    'payload gains the policy-plane identity (quota.path/present/bootstrap/note) '
    'plus the distinct exclusion code policy-gate-no-state-file; '
    'task_router/cli.py targets _bootstrap_state_dir at the state dir the '
    'dispatched spawn reads (honors operator-set ROUTER_STATE_DIR, shared '
    'implementation + inline wheel fallback). Foreman re-ran the row ACs on the '
    'committed tree: AC-A isolated run gate=OPEN chain=133 '
    'quota.present=true bootstrap=true 30 providers OPEN; AC-B 0 '
    'policy-gate-missing-row exclusions (was 137 at b80c096, gate=NO-OPEN-HOP, '
    'chain=0); AC-C default-env run gate=OPEN chain=83; AC-D '
    'tests/test_quota_gate.py 39 passed. Full smoke suite on the committed '
    'tree: 2544 passed / 3 skipped / 0 failed in 1980s. Regression tests: '
    'test_isolated_state_dir_bootstraps_its_own_policy_plane, '
    'test_absent_policy_plane_is_self_explaining_not_a_row_complaint, '
    'test_cli_bootstraps_the_state_dir_it_exports, '
    'test_shared_state_dir_is_never_bootstrapped (tests/test_quota_gate.py). '
    'GitReins: tier1 guards PASS, tier2 PASS — verdict .gitreins/history/'
    '2026-10-23/4824ef27 (passed=true, graded commit a039048). Worker: '
    'glm-5.3-flash@zai-glm dispatched 16:23Z wrote the implementation '
    '(session 20261023_112317_9655e2) but exited before committing — salvage '
    'path: foreman reviewed the diff, ran the gates and committed.'
)

witness = (
    f'commit {COMMIT} pushed origin/main (0 unpushed, ls-remote matches); '
    f'verdict {VERDICT_DIR} passed=true graded commit {COMMIT}; '
    f'CI {CI_RUN_ID} (a039048, success); live isolated spawn '
    'gate=OPEN chain=133 quota.present=true bootstrap=true (mktemp dir self-'
    'bootstrapped quota-state.json, 30 providers OPEN)'
)

ch_trace = (
    'ch:trace row=TR-345 spec=README.md:81,148 wave=none:serial-tick '
    'test=tests/test_quota_gate.py::test_isolated_state_dir_bootstraps_its_own_policy_plane '
    'doc=README.md:148,docs/registry-maintenance.md prompt=none:worker-brief-external '
    'evidence=commit witness=tests/test_quota_gate.py '
    f'verdict={VERDICT_DIR}'
)

worker_summary = (
    'ch:trace row=TR-345 spec=README.md:81,148 wave=none:serial-tick '
    'test=tests/test_quota_gate.py::test_isolated_state_dir_bootstraps_its_own_policy_plane '
    'doc=README.md:148,docs/registry-maintenance.md prompt=none:worker-brief-external '
    'evidence=commit witness=tests/test_quota_gate.py '
    f'verdict={VERDICT_DIR}\n'
    'STATUS: complete\n'
    'TIER: T3 (invoked and observed)\n'
    f'EVIDENCE: commit={COMMIT} ci-run={CI_RUN_ID} '
    f'verdict={VERDICT_DIR}'
)

sets = {
    'status': 'complete',
    'result': result,
    'witness': witness,
    'evidence_run_id': CI_RUN_ID,
    'ch:trace': ch_trace,
    'worker_summary': worker_summary,
}

DRY = len(sys.argv) > 2 and sys.argv[2] == 'dry'
EVENT_ONLY = len(sys.argv) > 2 and sys.argv[2] == 'event-only'
if EVENT_ONLY:
    print('event-only mode: skipping the completion append')
else:
    verdict = bcg.append_completion(BOARD, 'TR-345', sets, dry_run=DRY)
    print('APPEND verdict:', verdict.get('verdict'), 'written:', verdict.get('written'))
if DRY:
    print('DRY RUN — nothing written')
    sys.exit(0)

# events.jsonl: one task_completed event in the board's own shape
rows = []
with open(EVENTS, encoding='utf-8') as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
next_id = max([r.get('id') for r in rows if isinstance(r.get('id'), int)] or [0]) + 1
ev = {
    'id': next_id,
    'event_type': 'task_completed',
    'task_id': 'TR-345',
    'actor': 'foreman',
    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'detail': json.dumps({'status': 'complete'}),
    'tick_number': None,
}
with open(EVENTS, 'a', encoding='utf-8') as fh:
    fh.write(json.dumps(ev, separators=(', ', ': ')) + '\n')
print('event appended id=', next_id)

# readback: last task row + last event
last_task = [json.loads(l) for l in open(BOARD, encoding='utf-8') if l.strip()][-1]
print('readback task:', last_task.get('id'), last_task.get('status'),
      'gate:', (last_task.get('completion_gate') or {}).get('verdict'),
      'evidence_run_id:', last_task.get('evidence_run_id'))
last_ev = [json.loads(l) for l in open(EVENTS, encoding='utf-8') if l.strip()][-1]
print('readback event:', last_ev.get('id'), last_ev.get('event_type'), last_ev.get('task_id'))

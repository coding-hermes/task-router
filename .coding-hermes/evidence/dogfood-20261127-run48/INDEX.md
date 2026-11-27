# Dogfood run 48 evidence index

Tick: task-router-dogfood-2026-11-27-20-50-20
Tested commit: 91888bc89ce3fd7e8d4fef4e3989c95c37aade1d

Primary scratch root: /home/kara/.hermes/cache/scratch/task-router-dogfood-20261127-run

Files copied here:
- postseed-spawn.json — seeded isolated CLI resolve
- estimate.json and estimate-text.txt — JSON and human cost surfaces
- chain.json — dry-run chain execution
- validate2.json — post-seed validation
- bunker-qa-evidence.jsonl — detached install-leg collection at close

Install leg: bunker-qa.sh launch completed on bunker-las-03, agent 9e2371cf. Collection remained RUNNING at 10 evidence rows; fresh-install was OK, later test/UI/chaos/teardown cells are unverified. This is partial evidence, not a pass.

Transformations: command outputs copied byte-for-byte from the scratch run; no product source was changed.

Refire verification 2026-11-27 17:08 -05: the preserved live collector was polled again. The refreshed evidence has 17 rows: target-binding OK, launch OK, fresh-install OK, 10 toolchain-bootstrap OK, lint/docker_build/compose_config N/A, and tests FAIL after 1,866.86 s with rc=1 at tests/test_tr168_model_uniqueness.py::test_only_the_five_twin_lines_were_removed_and_survivors_edited; the pulled detail says git show 1fcd9aa failed rc=128 in the synced tree. The battery remains RUNNING and agent 9e2371cf remains intact. This is a harness/checkout-history failure already owned by QA-TASKROUTER-7, not a new product row. The collector output was copied to refire-20261127-2050-bunker-qa-evidence.jsonl; the prior 10-row artifact remains unchanged.

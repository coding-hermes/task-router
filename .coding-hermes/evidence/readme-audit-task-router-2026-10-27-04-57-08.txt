README health sweep: task-router

Commands and exact observations

1. git log -1 --format='%ad %H %s' --date=iso -- README.md
2026-10-26 17:19:51 -0500 1659efd281d27d710299c40203bcb76648c35e15 fix(decide): mint no signing material on request and bound verification to one indexed month (H1-F4)

2. git diff 1659efd..HEAD --name-only
.coding-hermes/board/board.jsonl
.coding-hermes/board/events.jsonl
.coding-hermes/board/tasks.jsonl
.coding-hermes/evidence/release-readiness-task-router-2026-10-27-04-36-21.json
.coding-hermes/waves/task-router-foreman-2026-10-27-04-36-21.json
docs/configuration.md
docs/decisions/SUP-DEPS-20261018-dependency-review.md
docs/evidence/tr348-docs-audit-writer-contract-2026-10-26.md
docs/priority-vocabulary-review-tr-002.md

docs/specs/SPEC-EXECUTION-DECISION.md
scripts/board_commit_boundary_gate.py
scripts/board_priority_normalize.py
scripts/router_decision.py
tests/test_execution_decision.py
tests/test_muster_spec_parity.py
tests/test_tr348_writer_priority_contract.py

3. curl -sS -L --max-time 20 https://api.github.com/repos/coding-hermes/task-router/releases/latest
{"message":"Not Found","documentation_url":"https://docs.github.com/rest/releases/releases#get-the-latest-release","status":"404"}

4. Stranger install probe
Commands: git clone --depth 1 file:///home/kara/task-router /var/tmp/task-router-stranger; python3 -m venv /var/tmp/task-router-stranger-venv; pip install -e .; pip install duckdb; router seed; router status; router validate; router spawn my-project --format json; router spawn P1_CODING --format json; python3 -m pytest -q tests/
Observed: install rc=0; duckdb install rc=0; seed rc=0 and wrote /var/tmp/task-router-stranger/registry.json; status rc=0 with registry.source=registry.json and fallback_used=false; validate rc=0 after seed; spawn commands rc=0; documented test command rc=1 with /var/tmp/task-router-stranger-venv/bin/python3: No module named pytest.

5. Clean stranger make path
Commands: make seed; make validate from /var/tmp/task-router-stranger after removing registry.json.
Observed: make seed rc=0; make validate rc=0 with valid=true, registry.schema valid, freshness valid, 11 profiles and 69 requirements.

6. CLI reference comparison
Command: /var/tmp/task-router-stranger-venv/bin/router --help
Observed command list includes context-call. README.md:618-653 repeats chain-run (622,638), lifecycle (629,639), outcomes (630,640), pricing-audit (635,641), quota (642,645), and has no context-call row.

7. Links and structure
All 17 external/in-repository README links resolved to existing files. README headings include Quick start and run/test/install/deploy-related material, but no dedicated limits/known-gaps section. No badges/images were present.

README was not edited. No target README or docs were modified by this lane.

ch:trace row=README-1 spec=docs/traceability-doctrine.md#hard-rules wave=task-router-readme-2026-10-27-04-57-08#sweep test=none:documentation-command-execution doc=README.md:34-70 prompt=~/.hermes/skills/coding-hermes/coding-hermes-readme/SKILL.md evidence=/var/tmp/task-router-stranger.txt witness=none:README-install-path-is-repository-authored verdict=none:not-a-judged-code-change commit=none:pending-worker-fix memory=none:not-written
ch:trace row=README-2 spec=docs/traceability-doctrine.md#hard-rules wave=task-router-readme-2026-10-27-04-57-08#sweep test=none:documentation-command-execution doc=README.md:613-653 prompt=~/.hermes/skills/coding-hermes/coding-hermes-readme/SKILL.md evidence=/var/tmp/task-router-readme-structure.txt witness=none:README-structure-is-repository-authored verdict=none:not-a-judged-code-change commit=none:pending-worker-fix memory=none:not-written
ch:trace row=README-3 spec=docs/traceability-doctrine.md#hard-rules wave=task-router-readme-2026-10-27-04-57-08#sweep test=none:documentation-freshness-check doc=README.md:1-747 prompt=~/.hermes/skills/coding-hermes/coding-hermes-readme/SKILL.md evidence=.coding-hermes/evidence/readme-audit-task-router-2026-10-27-04-57-08.txt witness=http:404+sha256:unavailable-github-release-endpoint verdict=none:not-a-judged-code-change commit=none:pending-worker-fix memory=none:not-written

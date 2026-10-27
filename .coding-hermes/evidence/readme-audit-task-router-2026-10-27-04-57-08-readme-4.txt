README health sweep supplemental finding
Tick: task-router-readme-2026-10-27-04-57-08

Command: grep -n -E 'ROUTER_CLASSIFIER_MODEL|deepseek-v4.1-flash|glm-5.3-flash' README.md docs/configuration.md scripts/router_classify.py
Exact output:
README.md:329:export ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash
README.md:376:`state.db` `session_model_usage` showed **zero** `deepseek-v4.1-flash`
docs/configuration.md:47:| `ROUTER_CLASSIFIER_MODEL` | `router_classify.py`, `router_server.py` | Model ID to use for classification requests | `deepseek-v4.1-flash` |
scripts/router_classify.py:286:              'model': os.environ.get('ROUTER_CLASSIFIER_MODEL', 'glm-5.3-flash'),
scripts/router_classify.py:566:    out = {'prompt_version': version, 'model': os.environ.get('ROUTER_CLASSIFIER_MODEL'),

Finding: README.md:329 tells an operator to export deepseek-v4.1-flash, while the classifier implementation default is glm-5.3-flash at scripts/router_classify.py:286. The documented deployment recipe therefore does not describe the current default and can select a rejected/legacy model identifier. Existing docs/configuration.md row DOC-8 covers the configuration reference, but this README deployment block is a separate reader-facing command defect.

Fix: update the README deployment recipe and its nearby explanatory text to the currently supported classifier model identifier, and state whether the value is an example override or the implementation default. Re-run the exact grep and the deployment command in a fresh checkout after the worker change.

ch:trace row=README-4 spec=docs/traceability-doctrine.md#hard-rules wave=task-router-readme-2026-10-27-04-57-08#accuracy test=none:documentation-command-execution doc=README.md:324-331 prompt=~/.hermes/skills/coding-hermes/coding-hermes-readme/SKILL.md evidence=/home/kara/task-router/.coding-hermes/evidence/readme-audit-task-router-2026-10-27-04-57-08-readme-4.txt witness=none:repository-authored-command-and-source-comparison verdict=none:not-a-judged-code-change commit=none:pending-worker-fix memory=none:not-written

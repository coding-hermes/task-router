# The CI gate — why red-on-main cannot be ignored

## The failure this exists to prevent

On 2026-09-27 `main` had **three consecutive red CI runs** while work kept landing on
top of them. The repo was not blind to it — it files its own CI rows (`INT-CI-20260927-01`,
`INT-CI-20260927-02`) — but nothing *blocked* anything. The reviewed commit `1dc5d6a` was
itself red.

The reason the local guard did not catch it: `scripts/gitreins-guard-tests.sh` runs the
suite **with this box's live state present** (`~/.hermes/model-router/*`, `registry.json`).
CI runs the same suite with **no state at all**. So the suite was green here and red there,
for the same commit. A guard that cannot see the environment CI sees is not a gate for CI.

## What the gate is

Two layers, deliberately small:

1. **`scripts/ci_gate_check.py`** — asks GitHub whether the branch tip's newest completed
   run is `success`. Exit 0 = green (or state unknowable), exit 1 = RED.
2. **`.githooks/pre-push`** — refuses a push that *updates* `main` while that check fails.
   Feature branches and worktrees push freely; the gate is about not stacking onto red.

Installed per clone with `scripts/install-hooks.sh` (sets `core.hooksPath`).

## The decisions inside it, stated so they are not read as oversights

| Decision | Why |
|---|---|
| `gh` missing / unauthenticated / offline ⇒ **skip, not fail** | A network blip or a CI runner without credentials must never wedge the fleet. The skip is announced on stderr. |
| `--require-green` turns that skip into a failure | For callers that would rather stop than proceed blind. |
| Only pushes that update the gated branch are checked | The point is not to slow down work; it is to stop stacking commits onto a branch already failing CI. |
| The escape hatch is an env var (`ALLOW_RED_PUSH=1`) that **prints a warning** | No gate in this repo may be overridden silently. |
| Runs on every fleet tick's guard | A foreman that lands work on a red main is the exact behaviour being fixed, so the gate belongs where the foremen already look. |

## Using it by hand

```bash
python3 scripts/ci_gate_check.py                  # informational; exits 1 when red
python3 scripts/ci_gate_check.py --require-green  # treat "unknowable" as failure too
gh run view <id> --repo coding-hermes/task-router --log-failed   # what actually failed
```

## What this gate does NOT do

- It does not run the suite; it reports CI's verdict on it.
- It does not block a direct write by a human who is not using git (there is none here).
- It does not make a red run green. Fixing the failure is separate work — the gate only
  guarantees the failure stops being *ignorable*.
- It does not replace branch protection. Requiring a status check on `main` through GitHub
  would forcibly route every fleet push through a pull request — a real process change for
  ~20 commits/day, so it is an operator decision, not something to flip unilaterally.

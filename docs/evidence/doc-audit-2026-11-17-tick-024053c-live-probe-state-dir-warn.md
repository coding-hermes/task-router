# Live probe — ROUTER_STATE_DIR_WARN actually steers the warning

Supplementary evidence for DOC-29 (filed by tick 2026-11-17-02-40-53).
Companion to `doc-audit-2026-11-17-tick-024053c-fresh-sweep-transcript.md` (sha256
`06024edcc58ed99790849f65a1723acb8b0b50acdc788165ff035b97bc25ad93`), which is frozen and
unmodified.

## Why this file exists

The transcript proves the DOCUMENTATION gap (a corpus differential: the key appears in
`scripts/state_dir.py` and in tests, and in zero Markdown outside this lane's own evidence
tree). That is a T2 measurement. It does not prove the variable does anything — a documented
default that is dead code would be a different, weaker finding.

This probe closes that: the router CLI was invoked three times with the variable set to each
of its documented values, and the warning's presence was read back from stderr.

## Probe

Probe root: clean worktree of the published tip. The command is `router probe --help`
because it is the cheapest real dispatch that reaches the state-dir resolver without writing
state (the warning fires on the resolver path, before any argparse output).

```
$ PYTHONPATH=. python3 -m task_router.cli probe --help 2>&1 >/dev/null | head -2
WARNING [router state-dir] ROUTER_STATE_DIR is unset and this process was not started through
the canonical live install (/home/kara/.hermes/scripts): using the SHARED production state dir
/home/kara/.hermes/model-router — set ROUTER_STATE_DIR=<dir> to isolate.

$ ROUTER_STATE_DIR_WARN=suppress PYTHONPATH=. python3 -m task_router.cli probe --help 2>&1 >/dev/null | head -2
(no output — the warning is suppressed)

$ ROUTER_STATE_DIR_WARN=force PYTHONPATH=. python3 -m task_router.cli probe --help 2>&1 >/dev/null | head -2
WARNING [router state-dir] ROUTER_STATE_DIR is unset and this process was not started through
the canonical live install (/home/kara/.hermes/scripts): using the SHARED production state dir
/home/kara/.hermes/model-router — set ROUTER_STATE_DIR=<dir> to isolate.
```

## What this establishes

1. `suppress` silences the warning on a real non-canonical invocation — the value the module
   docstring promises, observed rather than asserted.
2. `force` emits it — so the variable genuinely overrides the canonical-install detection
   rather than only being read on the warning path's fallback.
3. The warning names `ROUTER_STATE_DIR` as THE escape hatch and does not mention
   `ROUTER_STATE_DIR_WARN`: the operator reading that stderr line is given no way to silence
   it for a deliberate second checkout, which is exactly the gap DOC-29 asks a worker to
   document.

## Scope limit

This probe proves the variable is live. It does NOT prove the warning is desirable on a
`--help` invocation — that it leaks into help output at all is a functional-policy question,
recorded as a residual and deliberately not filed in this lane.

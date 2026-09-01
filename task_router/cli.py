"""task_router.cli — installable `router` CLI (TR-016).

One subcommand per script tool in scripts/. Dispatch runs the underlying
script via runpy.run_path(..., run_name='__main__') with sys.argv rewritten
to ['router_<name>.py', *args], so each script's own argparse (prog, usage,
subcommands, --help passthrough) stays the single source of truth.

Before dispatch, env overrides derived from the task-router data home
(task_router.paths) are exported for the script env hooks that exist — and
ONLY those hooks (never set a variable no script reads), and never clobber an
explicit user override (os.environ.setdefault semantics).

Full hook map (grep 'os.environ.get' across scripts/, TR-016 audit):

  script                      env hooks exported from data home
  --------------------------  -----------------------------------------------
  router_spawn.py             ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTER_STATE_DIR (dir that holds
                              circuit-state.json, health-state.json,
                              ledger.jsonl)
  router_circuit.py           ROUTER_STATE_DIR
  router_ledger.py            LEDGER_FILE
  router_maintain.py          ROUTING_REGISTRY, ROUTING_DATA_DIR
  router_seed.py              ROUTING_REGISTRY, ROUTING_DATA_DIR
  router_gaps.py              ROUTING_DATA_DIR
  router_pricing.py           ROUTING_DATA_DIR
  router_modelsdev.py         ROUTING_DATA_DIR (MODELSDEV_CACHE left as-is:
                              models.dev catalog cache, not router data)
  router_clinepass.py         ROUTING_DATA_DIR
  router_probefix.py          none (hardcodes ~/.hermes/model-router +
                              ~/task-router; documented gap — file off-limits)
  router_plan_sweep.py        none (hardcodes <repo>/data/tables; documented
                              gap — file off-limits)
  router_learn.py             none (DuckBrain CLI in ~/duckbrain; independent
                              of data home by design)
  provider_health_probe.py    none (hardcodes ~/task-router +
                              ~/.hermes/model-router; documented gap —
                              file off-limits)
  router_metrics.py           not exposed as a `router` subcommand (library
                              used by router_spawn; no standalone CLI)

Fail-open doctrine applies at the CLI boundary too: dispatch errors are
printed and turn into SystemExit(0) for fail-open tools (spawn, probefix,
plan-sweep); all other scripts keep their native exit codes.
"""

import os
import runpy
import sys

from task_router import paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
SCRIPTS_DIR = os.path.join(REPO, "scripts")

# Subcommand name -> script basename. `validate` is reserved for a future
# contract validator; it has no script yet, so it is documented but not
# dispatched (router validate -> usage error, exit 2).
COMMANDS = {
    "spawn":      "router_spawn.py",
    "circuit":    "router_circuit.py",
    "gaps":       "router_gaps.py",
    "ledger":     "router_ledger.py",
    "maintain":   "router_maintain.py",
    "modelsdev":  "router_modelsdev.py",
    "pricing":    "router_pricing.py",
    "plan-sweep": "router_plan_sweep.py",
    "learn":      "router_learn.py",
    "seed":       "router_seed.py",
    "probe":      "provider_health_probe.py",
    "clinepass":  "router_clinepass.py",
    "probefix":   "router_probefix.py",
    "validate":   "router_validate.py",
    "metrics":    "router_metrics.py",
    "status":     "router_status.py",
    "estimate":   "router_estimate.py",
    "diff":       "router_diff.py",
    "web":        "router_web.py",
    "server":     "router_server.py",
}
RESERVED = ()

# Scripts whose failure must NEVER block a caller (scheduler doctrine):
# they print their own error payload and exit 0 (or we coerce them to 0).
FAIL_OPEN = {"spawn", "probefix", "plan-sweep"}

# Per-command env exports derived from the data home. Lists contain ONLY
# hooks that actually exist in the target script (see module docstring map).
# Values are computed lazily at dispatch time so a monkeypatched env (tests)
# or a TASK_ROUTER_HOME set inside the wrapper is honored.
def _home_env_exports():
    """Env-var map derived from the current data home (called per dispatch)."""
    home = paths.resolve_data_home(create=True)
    state_dir = os.path.dirname(paths.circuit_state_path())  # == home
    _bootstrap_state_dir(state_dir)
    return {
        "spawn": {
            "ROUTING_REGISTRY": paths.registry_path(),
            "ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables"),
            "ROUTER_STATE_DIR": state_dir,
        },
        "circuit": {
            "ROUTER_STATE_DIR": state_dir,
        },
        "ledger": {
            "LEDGER_FILE": paths.ledger_path(),
        },
        "maintain": {
            "ROUTING_REGISTRY": paths.registry_path(),
            "ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables"),
        },
        "seed": {
            "ROUTING_REGISTRY": paths.registry_path(),
            "ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables"),
        },
        "gaps":   {"ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables")},
        "pricing": {"ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables")},
        "modelsdev": {"ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables")},
        "clinepass": {"ROUTING_DATA_DIR": os.path.join(REPO, "data", "tables")},
        # probefix / plan-sweep / learn / probe: no data-home hooks in those
        # scripts (documented gaps) — nothing to export.
        "probefix": {},
        "plan-sweep": {},
        "learn": {},
        "probe": {},
    }


def _apply_env_exports(cmd, exports):
    """setdefault every mapping in exports[cmd] into os.environ."""
    for var, value in exports.get(cmd, {}).items():
        os.environ.setdefault(var, value)


def _bootstrap_state_dir(state_dir):
    """First-run bootstrap (dogfood 2026-09-01): create a starter
    quota-state.json in a EMPTY state dir with every provider from the repo's
    data table explicitly OPEN.

    Spawn's fail-closed semantics are untouched: absent file = everything
    gated (deliberate fleet safety). The CLI instead makes first-run honest —
    the operator gets a visible, editable file declaring the default policy
    rather than a silent zero-chain surprise. Never overwrites an existing
    file; any error is non-fatal (the underlying script still runs).
    """
    qpath = os.path.join(state_dir, 'quota-state.json')
    if os.path.exists(qpath):
        return
    try:
        import json as _json
        provs = {}
        table = os.path.join(REPO, 'data', 'tables', 'providers.jsonl')
        with open(table, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = _json.loads(line)
                pid = row.get('id')
                if pid:
                    provs[pid] = {'status': 'open'}
        doc = {'updated': 'bootstrap', 'providers': provs,
               'note': 'first-run bootstrap: all providers OPEN; edit to gate'}
        with open(qpath, 'w', encoding='utf-8') as f:
            _json.dump(doc, f, indent=1)
    except Exception as e:  # noqa: BLE001 — bootstrap is best-effort
        print(f"router: state bootstrap skipped: {e}", file=sys.stderr)


def dispatch(cmd, argv):
    """Run scripts/<script> as __main__ with argv rewritten. Never returns."""
    script = os.path.join(SCRIPTS_DIR, COMMANDS[cmd])
    sys.argv = [os.path.basename(script), *argv]
    runpy.run_path(script, run_name="__main__")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args, extra = parser.parse_known_args(argv)

    if args.command is None:
        parser.print_help()
        return 2
    if args.command in RESERVED:
        print(f"router: '{args.command}' is reserved (no implementation yet)",
              file=sys.stderr)
        return 2

    # Unknown extra args are passed through verbatim — the underlying
    # script's argparse owns full validation (help passthrough included).
    cmd_argv = (args.cmd_args or []) + extra
    exports = _home_env_exports()
    # Exports are PER-DISPATCH: snapshot the vars we will touch and restore
    # them in `finally`, so one long-lived process can dispatch many
    # subcommands without earlier exports leaking into later ones.
    touched = exports.get(args.command, {})
    saved = {k: os.environ.get(k) for k in touched}
    _apply_env_exports(args.command, exports)
    try:
        dispatch(args.command, cmd_argv)
        rc = 0
    except SystemExit as e:
        # Scripts raise SystemExit for argparse usage errors (--help -> 0).
        code = e.code if isinstance(e.code, int) else 0 if e.code is None else 1
        if args.command in FAIL_OPEN and code not in (0,):
            print(f"router: {args.command} exited {code} — fail-open "
                  f"(coerced to 0)", file=sys.stderr)
            rc = 0
        else:
            rc = code
    except Exception as e:  # noqa: BLE001 — never crash the wrapper
        print(f"router: dispatch failed: {e}", file=sys.stderr)
        rc = 0 if args.command in FAIL_OPEN else 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc


def _build_parser():
    parser = _make_parser()
    return parser


def _make_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="router",
        description="task-router CLI — deterministic model routing for the "
                    "coding-hermes fleet. Each subcommand runs the matching "
                    "scripts/router_<name>.py tool with data-home env "
                    "overrides applied.",
        epilog="Data home: $TASK_ROUTER_HOME > $XDG_DATA_HOME/task-router > "
               "~/.local/share/task-router (see task_router.paths).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    help_texts = {
        "spawn":      "resolve a task/profile to a model chain (fail-open JSON/text)",
        "circuit":    "circuit-breaker state for (provider, model) pairs",
        "gaps":       "registry coverage gap report",
        "ledger":     "spawn-ledger start/end/status (TR-007)",
        "maintain":   "registry repair/export/reprice maintenance",
        "modelsdev":  "models.dev catalog sync + price refresh",
        "pricing":    "price table diagnostics",
        "plan-sweep": "disable plan-outside flat-subscription lanes",
        "learn":      "DuckBrain-backed learning loop (dump/lesson/doctrine)",
        "seed":       "rebuild registry.json from data/tables/*.jsonl",
        "probe":      "provider health probe (manual calibration run)",
        "clinepass":  "Cline Pass plan lane diagnostics",
        "probefix":   "resolve 404/400 model ids from probe logs",
        "validate":   "registry/schema/state/profile integrity check (--json, exit 1 on issues)",
        "metrics":    "usage metrics: top providers/models/pairs, per-profile, since-window",
        "status":     "one-command overview: registry, gates, circuit, gaps (json|text)",
        "estimate":   "cost estimate for a project's chain at given token volumes",
        "diff":       "chain snapshot diff between two dates (head moves, price deltas)",
        "web":        "local web UI: settings editor + live resolve preview (:9093)",
        "server":     "OpenAPI API server + MCP bridge (read-only | edit with API key)",
    }
    for name in sorted(COMMANDS):
        # add_help=False: `router <cmd> --help` must pass through to the
        # underlying script's argparse (AC5), not be swallowed by a stub.
        sp = sub.add_parser(name, help=help_texts.get(name, ""),
                            add_help=False)
        sp.add_argument("cmd_args", nargs=argparse.REMAINDER,
                        help=argparse.SUPPRESS)
    for name in RESERVED:
        sub.add_parser(name, help=help_texts.get(name, ""))
    return parser


if __name__ == "__main__":
    sys.exit(main())

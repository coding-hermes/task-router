"""task_router.cli — installable `router` CLI (TR-016).

One subcommand per script tool in scripts/. Dispatch runs the underlying
script via runpy.run_path(..., run_name='__main__') with sys.argv rewritten
to ['router_<name>.py', *args], so each script's own argparse (prog, usage,
subcommands, --help passthrough) stays the single source of truth.

Before dispatch, env overrides derived from the task-router data home
(task_router.paths) are exported for the script env hooks that exist — and
ONLY those hooks (never set a variable no script reads), and never clobber an
explicit user override (os.environ.setdefault semantics).

Full hook map (grep 'os.environ.get' across scripts/, TR-016 audit, extended
by the TR-056 audit to EVERY subcommand in COMMANDS — a command missing from
this map gets no exports and silently falls back to its own repo-relative or
hardcoded default, so two `router` subcommands can disagree about which
registry/state dir is live from the same invocation):

  script                      env hooks exported from data home
  --------------------------  -----------------------------------------------
  router_spawn.py             ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTER_STATE_DIR (dir that holds
                              circuit-state.json, health-state.json,
                              ledger.jsonl)
  router_circuit.py           ROUTER_STATE_DIR
  router_ledger.py            LEDGER_FILE
  router_maintain.py          ROUTING_REGISTRY, ROUTING_DATA_DIR
  router_seed.py              ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTING_NS (data-home ns dir — keeps a
                              data-home seed out of the fleet duckbrain
                              mirror; TR-045)
  router_gaps.py              ROUTING_DATA_DIR
  router_pricing.py           ROUTING_DATA_DIR
  router_modelsdev.py         ROUTING_DATA_DIR (MODELSDEV_CACHE left as-is:
                              models.dev catalog cache, not router data)
  router_clinepass.py         ROUTING_DATA_DIR
  router_status.py            ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTER_STATE_DIR (TR-056). LEDGER_FILE is
                              intentionally not exported: status resolves the
                              ledger as LEDGER_FILE or ROUTER_STATE_DIR/
                              ledger.jsonl — the same file `router ledger`
                              names explicitly.
  router_validate.py          ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTER_STATE_DIR (TR-056 — the brief listed only
                              registry+tables; the script reads
                              ROUTER_STATE_DIR for its state-file checks too)
  router_server.py            ROUTING_REGISTRY, ROUTING_DATA_DIR,
                              ROUTING_DOCS_DIR, ROUTER_STATE_DIR (TR-056)
  router_estimate.py          no hook of its own, but the data home must still
                              reach it: resolve_chain() subprocesses
                              router_spawn.py with the inherited env AND
                              estimate() imports router_spawn to load the
                              providers table in-process, so both read these
                              three vars (TR-030/TR-056)
  router_web.py               ROUTING_DATA_DIR only (TR-056). ROUTING_REGISTRY
                              is deliberately NOT exported: resolve_preview()
                              builds its child env itself and pins
                              ROUTING_REGISTRY to <repo>/registry.json, so an
                              export here could never be read.
  router_diff.py              ROUTING_DOCS_DIR only (TR-056) — it reads the
                              chains-<date>.md snapshot dir and no registry/
                              tables/state hook at all.
  router_metrics.py           nothing exported (TR-056): it reads
                              TASK_ROUTER_HOME directly, the SAME var (and
                              <repo>/data/metrics.jsonl default) that
                              router_spawn.py's metric writer uses, so reader
                              and writer already agree. Exporting
                              TASK_ROUTER_HOME here would point the reader at
                              <home>/metrics.jsonl while the spawn dispatch
                              kept appending to the repo file.
  router_probefix.py          nothing exported (TR-056 audit correction: this
                              script DOES read ROUTING_DATA_DIR and
                              ROUTER_STATE_DIR — the earlier "hardcodes only"
                              note was wrong). Its defaults are the FLEET
                              locations (~/task-router/data/tables +
                              ~/.hermes/model-router), which is where the
                              scheduler-side direct invocations keep state;
                              pointing the CLI at a different dir is a
                              separate, behaviour-changing decision.
  provider_health_probe.py    nothing exported, same reason (reads
                              ROUTING_REGISTRY / ROUTING_DATA_DIR /
                              ROUTER_STATE_DIR with fleet defaults; a
                              calibration run must keep writing the health
                              state the fleet reads).
  router_plan_sweep.py        none (hardcodes <repo>/data/tables; documented
                              gap — file off-limits)
  router_learn.py             none (DuckBrain CLI in ~/duckbrain; independent
                              of data home by design)

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
def _data_dir():
    """<repo>/data/tables — the committed table dir every script defaults to."""
    return os.path.join(REPO, "data", "tables")


def _docs_dir():
    """<repo>/docs — chains-<date>.md chain snapshots (git-tracked)."""
    return os.path.join(REPO, "docs")


def _home_env_exports():
    """Env-var map derived from the current data home (called per dispatch)."""
    home = paths.resolve_data_home(create=True)
    state_dir = os.path.dirname(paths.circuit_state_path())  # == home
    _bootstrap_state_dir(state_dir)
    registry = paths.registry_path()
    data_dir = _data_dir()
    docs_dir = _docs_dir()
    return {
        "spawn": {
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            "ROUTER_STATE_DIR": state_dir,
        },
        "circuit": {
            "ROUTER_STATE_DIR": state_dir,
        },
        "ledger": {
            "LEDGER_FILE": paths.ledger_path(),
        },
        "maintain": {
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
        },
        "seed": {
            # ROUTING_NS guard (TR-045): router_seed.py falls back to the live
            # DuckBrain mirror (/home/kara/duckbrain/namespaces/routing) when
            # ROUTING_NS is unset, so a data-home `router seed` would export
            # INTO the fleet mirror. Deriving it under the data home keeps
            # scratch/data-home seeds self-contained; an operator who exports
            # ROUTING_NS explicitly still wins (setdefault semantics).
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            "ROUTING_NS": os.path.join(home, "ns", "routing"),
        },
        "gaps":   {"ROUTING_DATA_DIR": data_dir},
        "pricing": {"ROUTING_DATA_DIR": data_dir},
        "modelsdev": {"ROUTING_DATA_DIR": data_dir},
        "clinepass": {"ROUTING_DATA_DIR": data_dir},
        # --- TR-056: the overview / contract / server commands read the same
        # hooks spawn does. Before this they got NO exports, so their
        # module-level REGISTRY/state constants kept the script defaults
        # (<repo>/registry.json, ~/.hermes/model-router) while `router spawn`
        # resolved from the data home — `router status` and `router spawn`
        # reported two different registries from the same invocation.
        "status": {
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            "ROUTER_STATE_DIR": state_dir,
            # LEDGER_FILE deliberately absent: router_status.py resolves
            # LEDGER_FILE or ROUTER_STATE_DIR/ledger.jsonl — the same file
            # `router ledger` exports, reached here through state_dir.
        },
        "validate": {
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            # The script reads ROUTER_STATE_DIR for its circuit/health/quota/
            # ledger state-file checks (router_validate.py STATE_DIR).
            "ROUTER_STATE_DIR": state_dir,
        },
        "estimate": {
            # router_estimate.py has no hook of its own but needs all three:
            # resolve_chain() runs router_spawn.py as a subprocess with the
            # inherited env, and estimate() imports router_spawn to load the
            # providers table in-process — both resolve these vars at read
            # time. Without the export, `router estimate` priced the REPO
            # registry while `router spawn` resolved the data home.
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            "ROUTER_STATE_DIR": state_dir,
        },
        "diff": {
            # Only hook router_diff.py has: the snapshot dir it reads
            # chains-<date>.md from. It reads no registry/tables/state hook,
            # so none is exported (TR-056 audit).
            "ROUTING_DOCS_DIR": docs_dir,
        },
        "server": {
            "ROUTING_REGISTRY": registry,
            "ROUTING_DATA_DIR": data_dir,
            "ROUTING_DOCS_DIR": docs_dir,
            "ROUTER_STATE_DIR": state_dir,
        },
        "web": {
            # router_web.py reads only ROUTING_DATA_DIR (the tables the UI
            # shows/edits). ROUTING_REGISTRY is deliberately NOT exported:
            # resolve_preview() builds its child env itself and pins
            # ROUTING_REGISTRY to <repo>/registry.json, so an export here
            # could never be read.
            "ROUTING_DATA_DIR": data_dir,
        },
        "metrics": {
            # No export: router_metrics.py reads TASK_ROUTER_HOME directly
            # (not the ROUTING_* hooks) and router_spawn.py's metric WRITER
            # uses the same var with the same <repo>/data/metrics.jsonl
            # default, so reader and writer already agree. Exporting
            # TASK_ROUTER_HOME for the metrics dispatch would move the READER
            # to <home>/metrics.jsonl while every spawn dispatch kept
            # appending to the repo file — a new divergence, not a fix.
        },
        "probefix": {},
        "plan-sweep": {},
        "learn": {},
        # probefix / probe / plan-sweep / learn: no export by design. probefix
        # and provider_health_probe.py DO read ROUTING_*/ROUTER_STATE_DIR
        # (TR-056 audit corrected the old "hardcodes only" note in the module
        # docstring) but their defaults are the FLEET locations
        # (~/task-router/data/tables + ~/.hermes/model-router) that the
        # scheduler-side direct invocations read and write; pointing the CLI
        # dispatch at the data home would silently redirect a calibration
        # run's health-state.json away from the file the fleet consumes.
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
    # No argparse subparsers for the passthrough: argparse.REMAINDER drops the
    # value of any option that STARTS a subcommand invocation ('router spawn
    # --profile-req reasoning=5 ...' lost 'reasoning=5' on every interpreter,
    # 2026-09-01 dogfood find). Dispatch argv[1:] verbatim instead; the
    # underlying script's argparse owns full validation (help passthrough).
    if not argv:
        _print_help()
        return 2
    cmd = argv[0]
    if cmd in ("-h", "--help"):
        _print_help()
        # argparse convention: --help raises SystemExit(0). The 2026-09-01
        # passthrough rewrite (dispatch argv verbatim) returned 0 without
        # raising, breaking test_top_level_help_lists_every_subcommand and any
        # caller using the argparse idiom (pytest.raises(SystemExit)).
        raise SystemExit(0)
    if cmd not in COMMANDS:
        print(f"router: unknown command {cmd!r} (see 'router --help')",
              file=sys.stderr)
        return 2
    cmd_argv = argv[1:]
    exports = _home_env_exports()
    # Exports are PER-DISPATCH: snapshot the vars we will touch and restore
    # them in `finally`, so one long-lived process can dispatch many
    # subcommands without earlier exports leaking into later ones.
    touched = exports.get(cmd, {})
    saved = {k: os.environ.get(k) for k in touched}
    _apply_env_exports(cmd, exports)
    try:
        dispatch(cmd, cmd_argv)
        rc = 0
    except SystemExit as e:
        # Scripts raise SystemExit for argparse usage errors (--help -> 0).
        code = e.code if isinstance(e.code, int) else 0 if e.code is None else 1
        if cmd in FAIL_OPEN and code not in (0,):
            print(f"router: {cmd} exited {code} — fail-open "
                  f"(coerced to 0)", file=sys.stderr)
            rc = 0
        else:
            rc = code
    except Exception as e:  # noqa: BLE001 — never crash the wrapper
        print(f"router: dispatch failed: {e}", file=sys.stderr)
        rc = 0 if cmd in FAIL_OPEN else 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc


def _print_help():
    """Manual help (no argparse subparsers since 2026-09-01 — see main())."""
    print("usage: router [-h] COMMAND [args...]")
    print()
    print("task-router CLI — deterministic model routing for the coding-hermes fleet.")
    print("Each subcommand runs the matching scripts/router_<name>.py tool with")
    print("data-home env overrides applied. `router <cmd> --help` passes through to")
    print("the underlying tool's own argparse.")
    print()
    print("COMMAND")
    texts = {
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
        pad = " " * (12 - len(name))
        print(f"  {name}{pad}{texts.get(name, '')}")
    print()
    print("Data home: $TASK_ROUTER_HOME > $XDG_DATA_HOME/task-router > "
          "~/.local/share/task-router (see task_router.paths).")


if __name__ == "__main__":
    sys.exit(main())

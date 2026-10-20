"""TR-202 — every live-installed runtime path constant must survive a symlink exec.

The live installs at ~/.hermes/scripts/ are SYMLINKS into the checkout for most
runtime tools (scripts/sync_runtime.sh topology 1). Python sets __file__ to the
LINK path when a script is exec'd through a symlink, so any
os.path.abspath(__file__)-derived repo root resolves to ~/.hermes — where
data/tables, registry.json and the board do not exist (measured 2026-09-29:
six modules' DATA_DIR pointed at the nonexistent ~/.hermes/data/tables).
realpath(__file__) (and Path(__file__).resolve()) resolve through the link and
land in the real tree; provider_health_probe.py was fixed this way first
(commit 4558290's neighbourhood, TR-CI lineage).

Fourteen of the fifteen probed modules exec the real repo files through a
scratch symlink (never the live ~/.hermes/scripts tree — the scheduler and
crons own it). The exception is router_seed.py: its whole module body IS the
seed, so since INT-CI-5 its constants come from a static probe that never
runs the seed mainline (declaration-only AST slice exec'd under a no-write
guard — _module_constants_static below). On CI the runpy probe executed the
full duckdb seed just to read a handful of path strings and hung past the
300s per-test cap six red main runs straight (stack: seed:1386
apply_overlay -> :1339 con.execute, reaped by pytest-timeout). Both probes
assert that every repo-derived path constant the module itself computed
lands INSIDE the real checkout. A mechanism self-test proves both
techniques discriminate: the same probe run against a deliberately-abspath
module flags it, the realpath module passes.
"""
import ast
import builtins
import glob
import io
import os
import re
import runpy
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

SYNC_RUNTIME = os.path.join(SCRIPTS, 'sync_runtime.sh')

# TR-301 (second pass): sentinels the never-writes pin below asserts against.
# data/tables is committed content (present in every clone/worktree);
# registry.json is gitignored machine state whose PRESENCE is ambient.
REAL_DATA = os.path.join(REPO, 'data', 'tables')
REGISTRY_SENTINEL = os.path.join(REPO, 'registry.json')

# Modules FIXED under TR-202 (abspath -> realpath), exec'd through a symlink.
# 2026-10-07 re-pass: cost_backfill / dummy_scheduler / proxy_smoke swapped
# hardcoded /home/kara data-state paths for realpath repo-relative defaults
# (they are NOT in sync_runtime.sh's live-install lists, but they ride the
# same symlink-safe idiom — see tests/test_tr202_abspath_portability.py).
FIXED = [
    'router_clinepass.py',
    'router_gaps.py',
    'router_modelsdev.py',
    'router_plan_sweep.py',
    'router_pricing.py',
    'router_probefix.py',
    'cost_backfill.py',
    'dummy_scheduler.py',
    'proxy_smoke.py',
]

# Already-correct neighbours kept as regression controls: realpath / resolve
# derived constants must keep landing in the real tree through a symlink.
CONTROLS = [
    'router_seed.py',
    'router_spawn.py',
    'router_diff.py',
    'router_validate.py',
    'provider_health_probe.py',
    # TR-209: the L2 accounting view is live-installed (symlink) and resolves
    # its limit-config default from the repo tree — keep it symlink-safe.
    'router_quota_accounting.py',
]

# INT-CI-5: modules whose symlink-contract probe must NOT execute the module
# body. router_seed.py's module body IS the seed (import-time duckdb work:
# executemany over ~1,959 model rows + apply_overlay over ~3,944 model_perf
# rows); the runpy probe executed all of it just to read a handful of path
# strings and hung past CI's 300s per-test cap — six consecutive red main
# runs (stack: seed:1386 apply_overlay -> :1339 con.execute). This row routes
# through _module_constants_static instead; the other 14 modules keep their
# runpy fixtures (the fidelity reference — they execute in well under a
# second).
NO_EXEC = {'router_seed.py'}

# INT-CI-5: the static probe keeps ONLY declaration statements. No Try, If,
# For, While, With, Expr, AugAssign — measured trap: a prototype that also
# kept ast.Try (to reach FunctionDefs nested in try:) EXECUTED the seed
# mainline, which sits inside module-level try: blocks (seed:20/36/428/456/
# 673): it ran the TR-232 benchmarks-undamp pass — writing
# data/tables/benchmarks.jsonl (same bytes, git status stayed clean, but it
# is a real write) — and died with duckdb.CatalogException: Table with name
# model_perf does not exist. The allowlist below is load-bearing.
_DECLARATION_STMTS = (
    ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)


def _is_inside(real_root, candidate):
    """True when candidate IS real_root or lives under it."""
    return candidate == real_root or candidate.startswith(real_root + os.sep)

# Path constants that are repo-derived BY DESIGN in this codebase. If any of
# them resolves to a path outside the real checkout, the module derived it from
# the symlink path — the TR-202 bug class. (State/env paths like
# ROUTER_STATE_DIR's ~/.hermes/model-router are deliberately NOT here.)
REPO_DERIVED = {
    '_REPO', 'REPO', '_HERE', '_SCRIPTS_DIR', 'SCRIPTS',
    'DATA_DIR', 'REGISTRY', 'PROMPT_DIR', 'BANDS_PATH', 'MODELS', 'PLANS',
    'HEAL_SCRIPT', '_DEFAULT_OUTCOMES', '_DEFAULT_AVERAGES',
}
# The subset that must also EXIST on disk in the real tree (default env, no
# overrides): they are the tables/files every consumer reads. REGISTRY is
# deliberately NOT here: registry.json is gitignored machine state (TR-175),
# seeded by CI/fleet ops and absent in fresh clones and worktrees — and its
# default path is <repo>/registry.json BY DESIGN, which the inside-REPO check
# above already pins. Asserting its existence here made this parametrised
# test ambient-checkout-dependent: on any unseeded checkout, the
# cost_backfill.py case (which exports REGISTRY without DATA_DIR) failed
# with "does not exist on disk" BEFORE the router_seed.py control case —
# whose import-time seed body writes registry.json — could create it,
# silently healing every later run (TR-301 order-dependence).
MUST_EXIST = {
    'DATA_DIR', 'PROMPT_DIR', 'BANDS_PATH', 'MODELS', 'PLANS',
    '_HERE', '_SCRIPTS_DIR', 'HEAL_SCRIPT',
}


def _module_constants_through_symlink(module_file, link_dir, repo=None):
    """Exec repo script via a symlink in link_dir; return its path constants.

    `repo` (default: this checkout) is the tree the ROUTING_* env pins point
    at; the TR-301 regressions pass a scratch repo so the probe never touches
    this checkout's machine state.

    INT-CI-5: for modules in NO_EXEC (router_seed.py) this helper is NOT
    used — the seed's whole module body is import-time duckdb work, and
    executing it to read a few path strings hung the CI job (six red main
    runs). Those modules route through _module_constants_static, which
    compiles a declaration-only AST slice instead of running the file. The
    env-pinning contract below still applies to every runpy-routed module.
    """
    repo = repo or REPO
    link = os.path.join(link_dir, os.path.basename(module_file))
    os.makedirs(link_dir, exist_ok=True)
    os.symlink(module_file, link)
    with pytest.MonkeyPatch.context() as mp:
        # Hermetic: the modules honour these env overrides; pin them so a test
        # run never reads (or writes) operator state. TR-301 second pass: the
        # pins are repo-relative BY DESIGN, so a module that WRITES its
        # default paths at import time (router_seed.py's whole seed body is
        # module-level) still lands those writes inside the real checkout —
        # measured live: a full-suite run created <repo>/registry.json and
        # re-synced data/tables/ from the router_seed.py control case, the
        # exact "silently healing every later run" mechanism behind the
        # original order-dependence. ROUTING_NS closes the remaining escape:
        # with it unpinned the seed's ns-mirror export resolved to
        # ~/.local/share/task-router/scratch/ns/routing (outside the repo)
        # and wrote six tables there. All three now pin into the probe's
        # repo argument (scratch for the TR-301 regressions, REPO for the
        # parametrised suite), keeping every default-path write inside the
        # tree the case is already pinned to. ROUTING_NS pins under link_dir
        # (the probe's per-case tmp): a nonexistent ns dir makes the seed's
        # conditional export branch skip entirely (isdir(NS) false, and
        # _NS_REDIRECTED cannot fire while ROUTING_NS is set), so the ns
        # mirror export writes NOTHING — measured live, an unpinned ns
        # resolved to ~/.local/share/task-router/scratch/ns/routing (outside
        # the repo) and the seed wrote six tables there.
        mp.setenv('ROUTING_DATA_DIR', os.path.join(repo, 'data', 'tables'))
        mp.setenv('ROUTING_REGISTRY', os.path.join(repo, 'registry.json'))
        mp.setenv('ROUTING_NS', os.path.join(link_dir, 'ns-mirror'))
        mp.delenv('ROUTER_STATE_DIR', raising=False)
        buf = open(os.devnull, 'w')
        saved, sys.stdout = sys.stdout, buf
        try:
            mod = runpy.run_path(link, run_name='_tr202_probe_not_main')
        finally:
            sys.stdout = saved
            buf.close()
    return {k: v for k, v in mod.items()
            if k in REPO_DERIVED and isinstance(v, str) and os.path.isabs(v)}


class _NoWriteDenied(RuntimeError):
    """Raised by _no_write_guard's patched callables when the static-probe
    slice attempts ANY filesystem write. It is always swallowed by the
    probe's exec wrapper — the abort IS the desired behaviour — and exists
    so a write attempt can never masquerade as a NameError."""


class _no_write_guard:
    """During the body, every filesystem WRITE entry point reachable from
    builtins/io/os/shutil raises _NoWriteDenied instead of touching disk;
    read paths are untouched.

    Measured necessity (INT-CI-5): the declaration-only slice still EXECUTES
    module-level Assign RHSes, and the seed's RHSes call module-level
    functions (seed:420 `_undamp_n = undamp_probe_benchmarks()`). A bare
    slice exec ran the TR-232 benchmarks-undamp pass, which rewrote
    data/tables/benchmarks.jsonl inside the sandbox (same bytes) and dropped
    a /tmp/benchmarks.jsonl.bak-<ts> backup OUTSIDE every sandbox plane.
    With the guard, that write attempt raises and the slice aborts before
    anything lands anywhere — the constants assigned before the first
    guarded RHS (seed:34/35/48) are already in the probe's namespace dict.

    Guarded surfaces: open/io.open in any write-ish mode ('w','a','x','+');
    os.rename/replace/remove/unlink/rmdir/mkdir/makedirs/truncate;
    shutil.copyfile/copy2/copy/copytree/move/rmtree. Documented blind spots:
    raw os.open/os.fdopen on a caller-made fd, and anything a subprocess
    writes outside this process — scripts/router_seed.py has no os.open and
    its subprocess calls are unreachable from the slice before the guard
    trips (verified by the sandbox-leftovers assertion in the never-writes
    pin and the sandbox-positives removal there)."""

    def __enter__(self):
        def guarded_open(file, mode='r', *args, **kwargs):
            if any(m in mode for m in ('w', 'a', 'x', '+')):
                raise _NoWriteDenied(f'open({file!r}, {mode!r}) during the '
                                     'static-probe slice exec')
            return self._saved_open(file, mode, *args, **kwargs)

        self._saved_open = builtins.open
        self._saved_os = {}
        self._saved_shutil = {}
        builtins.open = guarded_open
        io.open = guarded_open  # separate binding, same original function

        def deny(modname, name):
            def _denied(*args, **kwargs):
                raise _NoWriteDenied(f'{modname}.{name} during the '
                                     'static-probe slice exec')
            return _denied

        for name in ('rename', 'replace', 'remove', 'unlink', 'rmdir',
                     'mkdir', 'makedirs', 'truncate'):
            if hasattr(os, name):
                self._saved_os[name] = getattr(os, name)
                setattr(os, name, deny('os', name))
        for name in ('copyfile', 'copy2', 'copy', 'copytree', 'move',
                     'rmtree'):
            if hasattr(shutil, name):
                self._saved_shutil[name] = getattr(shutil, name)
                setattr(shutil, name, deny('shutil', name))
        return self

    def __exit__(self, *exc):
        builtins.open = self._saved_open
        io.open = self._saved_open
        for name, fn in self._saved_os.items():
            setattr(os, name, fn)
        for name, fn in self._saved_shutil.items():
            setattr(shutil, name, fn)
        return False


def _module_constants_static(module_file, link_dir, repo=None):
    """INT-CI-5: the no-execution-of-the-seed-mainline variant of the probe.

    router_seed.py's module body IS the seed (import-time duckdb executemany
    over ~1,959 model rows + apply_overlay over ~3,944 model_perf rows), so
    the runpy probe executed the whole seed just to read a handful of path
    strings and hung CI past the 300s per-test cap. This probe instead reads
    the module SOURCE, parses it, and keeps ONLY declaration statements (the
    _DECLARATION_STMTS allowlist — deliberately NO Try/If/For/While/With/
    Expr/AugAssign; see the comment on that constant for the measured Try
    trap), compiles the slice and execs it with __file__ bound to the
    SYMLINK path. Binding the REAL path would destroy the probe's
    discriminating power: the TR-202 class is exactly a constant that only
    escapes the real tree when __file__ is the link — the contract assertion
    in _assert_symlink_contract needs that escape to be observable.

    Two measured facts shaped the exec half of this probe:

    - Keeping ast.Assign means module-level ASSIGNMENTS still run, and some
      RHSes CALL module-level functions (seed:420 `_undamp_n =
      undamp_probe_benchmarks()`). A bare slice exec ran the TR-232
      benchmarks-undamp pass: it rewrote data/tables/benchmarks.jsonl
      (same bytes) and dropped a /tmp/benchmarks.jsonl.bak-<ts> backup
      OUTSIDE every sandbox plane — a real write, caught only because the
      first verification run surfaced it in captured stdout. So the slice
      is exec'd under _no_write_guard, which turns every write-mode
      open/io.open and every mutating os.* / shutil.* call into an
      exception for the duration of the exec. The first guarded RHS aborts
      the slice (for the seed: the TR-232 pass, whose earlier statements
      never got to write) and nothing anywhere can land on disk. The
      NameError abort measured for the plain allowlist (duckdb import
      dropped with its try:, seed:437 `con = duckdb.connect`) no longer
      even comes into play — the write guard fires first — but the exec is
      try/except'd regardless: declarations only, any abort is fine as
      long as nothing was written.

    - REGISTRY is assigned only inside the module-level
      `if _registry_path_mod is not None: / else:` at seed:41-47, so the
      static slice never reaches it — the coverage guarantee for this file
      is _assert_seed_repo_derived_source_idioms, not this probe.

    Return shape matches _module_constants_through_symlink: the
    {name: absolute-string-value} dict filtered by REPO_DERIVED +
    os.path.isabs. For this file the probe reaches exactly
    {_HERE, _REPO, DATA_DIR} (seed:34/35/48, all before the first guarded
    RHS); the parametrised seed case additionally pins this exact set."""
    repo = repo or REPO
    link = os.path.join(link_dir, os.path.basename(module_file))
    os.makedirs(link_dir, exist_ok=True)
    os.symlink(module_file, link)
    with open(module_file) as f:
        tree = ast.parse(f.read(), filename=module_file)
    kept = [node for node in tree.body if isinstance(node, _DECLARATION_STMTS)]
    filtered = ast.Module(body=kept, type_ignores=[])
    code = compile(filtered, link, 'exec')
    mod = {'__file__': link, '__name__': '_tr202_static_probe'}
    with pytest.MonkeyPatch.context() as mp:
        # Same env-pinning contract as the runpy probe: the declaration
        # slice's env-reading Assigns (NS, _scratch_ns, DATA_DIR ...) must
        # resolve inside `repo`/link_dir, never at operator state.
        mp.setenv('ROUTING_DATA_DIR', os.path.join(repo, 'data', 'tables'))
        mp.setenv('ROUTING_REGISTRY', os.path.join(repo, 'registry.json'))
        mp.setenv('ROUTING_NS', os.path.join(link_dir, 'ns-mirror'))
        mp.delenv('ROUTER_STATE_DIR', raising=False)
        with _no_write_guard():
            try:
                exec(code, mod)
            except BaseException:
                pass  # declarations only: any abort is fine, nothing was written
    return {k: v for k, v in mod.items()
            if k in REPO_DERIVED and isinstance(v, str) and os.path.isabs(v)}


def _assert_symlink_contract(module_file, repo, tmp_path):
    """The TR-202 contract, shared by the parametrised suite and the TR-301
    regressions: exec module_file through a scratch symlink under `repo` and
    assert every repo-derived constant lands inside it (and MUST_EXIST ones
    exist on disk). Keeping ONE body means a regression test exercises the
    exact assertion path the suite runs.

    INT-CI-5: NO_EXEC modules route through the non-executing static probe
    instead of runpy (see that constant's comment for why router_seed.py
    must never be exec'd here)."""
    if module_file in NO_EXEC:
        consts = _module_constants_static(
            os.path.join(repo, 'scripts', module_file), str(tmp_path), repo=repo)
    else:
        consts = _module_constants_through_symlink(
            os.path.join(repo, 'scripts', module_file), str(tmp_path), repo=repo)
    assert consts, f'{module_file}: no repo-derived path constants found — audit list stale'
    for name, val in sorted(consts.items()):
        assert _is_inside(repo, val), (
            f'{module_file}: {name} resolved to {val!r}, outside the real tree '
            f'{repo!r} — abspath(__file__) regression through the live symlink')
        if name in MUST_EXIST:
            assert os.path.exists(val), (
                f'{module_file}: {name} = {val!r} does not exist on disk')
    return consts


def _assert_seed_repo_derived_source_idioms(probe_consts=None):
    """INT-CI-5: source-level coverage net for NO_EXEC modules.

    The static probe cannot see constants assigned inside nested blocks, so
    it must never be the ONLY thing checking a NO_EXEC file. For every
    REPO_DERIVED name the seed ASSIGNS anywhere in the file (any depth),
    this check reads each assignment's RHS and demands the TR-202-safe
    idiom: the path must derive from realpath(__file__) /
    Path(__file__).resolve() or from a name already derived that way
    (_HERE, _REPO, ... — derived transitively, since _REPO itself is
    `os.path.dirname(_HERE)` and mentions no __file__ directly). In
    particular NO abspath(__file__) may appear in any of them — that idiom
    through the live symlink is the TR-202 bug class, and the probe's
    guarded-exec abort means a bad constant assigned after it would never
    even be computed, let alone flagged.

    The no-silent-drop rule (INT-CI-5): a REPO_DERIVED assignment must be
    covered by the probe (its name is in probe_consts — the probe's own
    contract assertion then pinned its VALUE in the same case run) or
    covered by THIS check (RHS anchored on the realpath idiom family,
    abspath-free, hence correct under every install topology). An
    assignment that is neither fails the router_seed.py case — the seed
    cannot grow a new path constant that nothing verifies.

    `probe_consts` is what the static probe actually returned for the seed
    in the same case run (or a falsy stand-in when the check runs without
    the probe — then every assignment must clear the source check)."""
    probe_names = set(probe_consts or ())
    path = os.path.join(SCRIPTS, 'router_seed.py')
    with open(path) as f:
        src = f.read()
    tree = ast.parse(src, filename=path)

    # Every assignment in the file (any depth), as (names, lineno, value).
    all_assigns = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if names:
            all_assigns.append((names, node.lineno, value))

    directly_derived = ('realpath(__file__)', 'Path(__file__).resolve()')
    # Anchor set, transitively: a name is realpath-derived when its own RHS
    # is a direct __file__ idiom, or when its RHS references an already
    # derived name (and contains no abspath — never launder one through a
    # chain). _REPO enters via _HERE exactly this way.
    derived = set()
    for names, _lineno, value in all_assigns:
        rhs = ast.unparse(value)
        if any(idiom in rhs for idiom in directly_derived):
            derived.update(names)
    assert derived, (
        'router_seed.py: no realpath(__file__)/resolve()-derived anchor '
        'names found — the idiom family this check allowlists has drifted; '
        're-derive it')
    for _ in range(len(all_assigns) + 1):
        grew = False
        for names, _lineno, value in all_assigns:
            if set(names) & derived:
                continue
            rhs = ast.unparse(value)
            if 'abspath(' in rhs:
                continue
            tokens = set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', rhs))
            if tokens & derived:
                derived.update(names)
                grew = True
        if not grew:
            break

    seed_assigns = [(names, lineno, value)
                    for names, lineno, value in all_assigns
                    if any(n in REPO_DERIVED for n in names)]
    assert seed_assigns, (
        'router_seed.py: no REPO_DERIVED assignments found anywhere — the '
        'audit list or the seed has drifted; re-check both')

    offenders = []
    for names, lineno, value in seed_assigns:
        rhs = ast.unparse(value)
        tokens = set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', rhs))
        anchored = (any(idiom in rhs for idiom in directly_derived)
                    or bool(tokens & derived))
        if 'abspath(__file__)' in rhs or not anchored:
            offenders.append((sorted(names), lineno, rhs[:120]))
            continue
        if set(names) & probe_names:
            continue  # probe-collected: its VALUE was already asserted
        # Not probe-collectible (assigned in a nested block — REGISTRY, at
        # seed:45/:47 inside the module-level if/else): covered by THIS
        # check only, which is why the anchored-and-abspath-free demand is
        # not relaxed for it.
    assert not offenders, (
        f'router_seed.py: REPO_DERIVED assignment(s) neither collected by '
        f'the static probe nor covered by the source idiom check: '
        f'{offenders} — every repo-derived path constant must derive from '
        f'realpath(__file__)/Path(__file__).resolve()/a realpath-derived '
        f'anchor (never abspath(__file__)) or be probe-verified; no silent '
        f'coverage loss (TR-202 class, INT-CI-5)')


@pytest.mark.parametrize('module_file', FIXED + CONTROLS)
@pytest.mark.timeout(1200)  # INT-CI-20261015-01: the router_seed.py case execs the
                            # full seed body (duckdb executemany over 1,959 model
                            # rows + sync + exports). Fine locally (7s) but on CI
                            # runners it hung past the 300s cap (run 38758188189,
                            # stack: seed:427 executemany) — the same 76% stall that
                            # silently killed four main runs. 1200s headroom keeps
                            # the suite alive; a genuine hang still names itself.
def test_repo_constants_resolve_inside_real_tree_through_symlink(module_file, tmp_path):
    if module_file == 'router_seed.py':
        # TR-301: the seed is the one control case whose module body WRITES
        # (registry.json, data/tables sync, ns-mirror export — all at import
        # time). Its env pins are repo-relative BY DESIGN, so exec'ing it with
        # repo=REPO writes gitignored machine state into the real checkout
        # mid-suite — the healing that made the original cost_backfill failure
        # order-dependent (measured: a single run recreated registry.json and
        # re-synced data/tables byte-identically, so git status stayed clean).
        # Route this one case through a scratch copy of the committed tree;
        # the contract assertions are unchanged (repo=sandbox).
        #
        # INT-CI-5: it no longer RUNS THE SEED MAINLINE here — NO_EXEC
        # routes this case through the static probe, whose declaration-only
        # slice is exec'd under _no_write_guard (the runpy probe executed
        # the whole seed body on CI and hung past the 300s cap, six red
        # main runs). The sandbox is kept: the slice's env-pinned path
        # constants still resolve inside `repo`, and if router_seed.py ever
        # regresses into NO_EXEC-incompatible shape the scratch tree
        # contains the blast radius.
        sandbox = tmp_path / 'seed-sandbox'
        sandbox_scripts, _ = _write_free_scratch_repo(
            str(sandbox), SCRIPTS, REAL_DATA)
        assert os.path.isdir(sandbox_scripts)
        consts = _assert_symlink_contract(module_file, str(sandbox),
                                          str(tmp_path / 'contract-links'))
        # INT-CI-5 probe-reach pin: the guarded declaration slice collects
        # exactly the constants assigned before the first guarded RHS —
        # _HERE/_REPO/DATA_DIR (seed:34/35/48). If this set changes, the
        # slice reached further into the seed: re-verify the guard still
        # held (nothing written), then update this pin and the source-idiom
        # coverage DELIBERATELY, never silently.
        assert set(consts) == {'_HERE', '_REPO', 'DATA_DIR'}, (
            f'router_seed.py: static probe collected {sorted(consts)} — '
            'drifted from the pinned {_HERE, _REPO, DATA_DIR}; the slice '
            'reached further into the seed than INT-CI-5 verified')
        _assert_seed_repo_derived_source_idioms(probe_consts=consts)
        return
    _assert_symlink_contract(module_file, REPO, tmp_path)


# --------------------------------------------------------------------------
# TR-301 regression: the exists-checks must be a function of the COMMITTED
# tree only. registry.json is gitignored machine state (TR-175) — seeded by
# CI/fleet ops, absent in fresh clones and worktrees. Asserting its existence
# (via the old MUST_EXIST entry) failed exactly the cost_backfill.py case on
# any unseeded checkout: it exports REGISTRY without DATA_DIR and sits BEFORE
# the router_seed.py control case, whose import-time seed body writes
# registry.json — silently healing every later run. Order-dependent, local
# full-suite only, never hit CI. Pinned here twice: (a) behaviourally, the
# cost_backfill case passes on a PRISTINE copy with the registry HIDDEN;
# (b) structurally, no MUST_EXIST constant may default to a gitignored path,
# so the ambient-dependence class cannot re-enter through another module.
# --------------------------------------------------------------------------

def _write_free_scratch_repo(tmp_root, src_scripts, src_data):
    """A minimal repo-shaped scratch tree (scripts/ + data/tables/).

    Used by the TR-301 regressions to stand in for an unseeded checkout: the
    seed side-effects below run against the SCRATCH tree, never the real
    checkout. ``data/tables`` is committed content in every clone/worktree,
    so copying it keeps the sandbox self-contained; the modules themselves
    are copied so a scratch write can never touch ``SCRIPTS`` either.
    """
    scratch_scripts = os.path.join(tmp_root, 'scripts')
    scratch_data = os.path.join(tmp_root, 'data', 'tables')
    shutil.copytree(src_scripts, scratch_scripts,
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(src_data, scratch_data)
    return scratch_scripts, scratch_data


def test_cost_backfill_case_passes_on_unseeded_checkout(tmp_path):
    """Behavioural pin for the TR-301 root cause: the exists-half of the
    contract must hold on a checkout WITHOUT gitignored machine state (fresh
    clone / worktree shape). A scratch repo containing ONLY the committed
    cost_backfill.py stands in for the unseeded checkout; the shared contract
    helper runs the exact suite assertions against it. Pre-fix this failed
    verbatim: "cost_backfill.py: REGISTRY = '<scratch>/registry.json' does
    not exist on disk" — reverting the MUST_EXIST change must reproduce it."""
    scratch = tmp_path / 'unseeded-checkout'
    (scratch / 'scripts').mkdir(parents=True)
    shutil.copy2(os.path.join(SCRIPTS, 'cost_backfill.py'),
                 scratch / 'scripts' / 'cost_backfill.py')
    _assert_symlink_contract('cost_backfill.py', str(scratch),
                             str(tmp_path / 'contract-links'))


def test_router_seed_control_case_never_writes_the_real_checkout(tmp_path):
    """TR-301 root cause, second half — INT-CI-5: the seed MAINLINE never
    executes under the probe, and that is the fix for this row.

    History: the parametrised router_seed.py control case was an IMPORT-TIME
    WRITER (its whole seed body is module-level — it wrote
    <repo>/registry.json, data/tables/ and the ns-mirror export). Running it
    in-process against the real checkout wrote gitignored machine state INTO
    the working tree mid-suite, silently healing every later unseeded case
    (the mechanism that made the original TR-301 failure order-dependent).
    TR-301's pins proved those writes landed only in a test-private sandbox;
    INT-CI-5 removed the execution itself, because on CI the runpy probe
    executed the full duckdb seed just to read path strings and hung past
    the 300s per-test cap (six red main runs, stack: seed:1386
    apply_overlay -> :1339 con.execute).

    What this test still proves, honestly labelled:
      1. The real-checkout invariants: no writes into the redirected
         HOME/XDG plane, and the registry.json sentinel of the REAL checkout
         is untouched (absent-stays-absent, present-stays-mtime-identical).
      2. The seed MAINLINE never executes: NO_EXEC routes the seed through
         _module_constants_static — a declaration-only AST slice exec'd
         under _no_write_guard. The slice still evaluates module-level
         Assign RHSes (measured: a bare exec ran the TR-232 undamp pass and
         dropped a /tmp backup outside every sandbox plane), so the guard
         turns the first write attempt into an abort — the duckdb
         executemany/apply_overlay body (Try/If/For/Expr statements,
         dropped from the slice) is unreachable BY CONSTRUCTION. The old
         TR-301 "sandbox positives" assertions (registry.json created,
         tables synced inside the sandbox) proved the seed RAN; asserting
         them now would be pretending an execution that must not happen —
         they are gone, replaced by the no-write invariants below.
      3. Coverage does not silently shrink with the execution: the
         source-idiom net for every REPO_DERIVED assignment in the seed
         runs in the parametrised case itself
         (_assert_seed_repo_derived_source_idioms, fed the probe's own
         const set).

    Attribution note (battery run 1, still true): foreign suite spawners
    legitimately write the SHARED planes mid-suite —
    tests/test_contract.py's _scratch_registry helper spawns router_seed.py
    with ROUTING_REGISTRY pinned but ROUTING_NS unset — so this test
    asserts only planes this test's OWN env pins own, never mtimes on
    shared state."""
    scratch_scripts, scratch_data = _write_free_scratch_repo(
        str(tmp_path / 'sandbox'), SCRIPTS, REAL_DATA)
    scratch = str(tmp_path / 'sandbox')
    assert scratch_scripts and scratch_data  # sandbox built; paths unused below
    escapes = tmp_path / 'escapes'
    escapes.mkdir()
    registry_before = os.path.exists(REGISTRY_SENTINEL)
    registry_mtime_before = (os.path.getmtime(REGISTRY_SENTINEL)
                             if registry_before else None)
    tmp_backups_before = len(glob.glob('/tmp/benchmarks.jsonl.bak-*'))

    with pytest.MonkeyPatch.context() as mp:
        # Everything the seed's declaration slice could resolve from HOME is
        # redirected into the test plane first: any escape lands under
        # escapes/ where the assertion below sees it.
        mp.setenv('HOME', str(escapes))
        mp.setenv('XDG_DATA_HOME', str(escapes / 'xdg-data'))
        # Seed redirects: all three pin into the sandbox per the suite rule.
        mp.setenv('ROUTING_REGISTRY', os.path.join(scratch, 'registry.json'))
        mp.setenv('ROUTING_DATA_DIR', scratch_data)
        mp.setenv('ROUTING_NS', os.path.join(escapes, 'ns-mirror'))
        mp.delenv('ROUTER_STATE_DIR', raising=False)
        _assert_symlink_contract('router_seed.py', scratch,
                                 str(tmp_path / 'contract-links'))

    # 1. No escape landed in the redirected HOME plane — the guarded slice
    #    cannot write anywhere (every write-mode open raises and aborts the
    #    exec), so a file here would mean the guard itself leaked.
    assert not os.listdir(escapes), (
        f'the router_seed.py probe escaped into HOME/XDG planes: '
        f'{sorted(os.listdir(escapes))} — the no-write guard around the '
        'static-probe slice did not hold (INT-CI-5)')
    # 1b. The measured escape class that motivated the guard: the TR-232
    #    undamp pass drops a /tmp/benchmarks.jsonl.bak-<ts> backup OUTSIDE
    #    every sandbox plane (and /tmp is shared with everything else on the
    #    box). Snapshot the litter before and after the probe: the count
    #    must not grow. (Counted, not required-absent — pre-existing backups
    #    from earlier runs are nobody's fault in this window.)
    assert len(glob.glob('/tmp/benchmarks.jsonl.bak-*')) == tmp_backups_before, (
        'a /tmp/benchmarks.jsonl.bak-* backup appeared in-window — the '
        'TR-232 undamp pass EXECUTED past the no-write guard (INT-CI-5)')
    # 2. The real checkout's registry.json sentinel: no suite-spawned seed
    #    writes it (every spawner pins ROUTING_REGISTRY — conftest both
    #    fixtures, capabilities/context/lifecycle/contract helpers), so its
    #    appearance or mtime change in THIS test's window would be a real
    #    write. The guarded slice cannot write at all, so this holds unless
    #    something executed past the guard.
    if registry_before:
        assert os.path.getmtime(REGISTRY_SENTINEL) == registry_mtime_before, (
            f'{REGISTRY_SENTINEL} was rewritten in-window — something wrote '
            'past the no-write guard (INT-CI-5)')
    else:
        assert not os.path.exists(REGISTRY_SENTINEL), (
            f'{REGISTRY_SENTINEL} appeared in-window — something wrote past '
            'the no-write guard (INT-CI-5)')
    # 3. The seed MAINLINE never runs, positively: the old TR-301 "sandbox
    #    positives" block (sandbox registry.json exists, sandbox data/tables
    #    synced) proved the seed RAN and wrote into the sandbox; asserting
    #    it now would demand the execution this row removes. In its place,
    #    the no-write proof: the sandbox holds ONLY the pre-seeded trees it
    #    was built with, no registry.json appears, and no ns-mirror export
    #    lands — if any of these fire, the guard failed or the slice reached
    #    past it.
    leftovers = sorted(
        f for f in os.listdir(scratch)
        if f not in ('scripts', 'data')
    )
    assert leftovers == [], (
        f'router_seed.py probe left writes in the sandbox root: {leftovers} — '
        'the no-write guard around the static-probe slice did not hold '
        '(INT-CI-5)')
    assert not os.path.exists(os.path.join(scratch, 'registry.json')), (
        'sandbox registry.json exists — the seed body EXECUTED; the seed '
        'mainline must stay unreachable under the probe (INT-CI-5)')
    assert not os.path.exists(os.path.join(escapes, 'ns-mirror')), (
        'ns-mirror export appeared — the seed body EXECUTED (INT-CI-5)')


def _declared_env_default(name):
    """Literal default paths of module constant `name` as declared across
    scripts/ (the TR-202 convention: NAME = os.environ.get('X', <literal
    path>)). Returns [(script, relative-path)] using only the quoted path
    segments of the default expression."""
    pat = re.compile(
        r"^%s = os\.environ\.get\('[^']+',\s*(.+)\)\s*$" % re.escape(name))
    hits = []
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith('.py'):
            continue
        with open(os.path.join(SCRIPTS, fn)) as f:
            for line in f:
                m = pat.match(line.strip())
                if m:
                    segs = re.findall(r"'([^']+)'", m.group(1))
                    if segs:
                        hits.append((fn, os.path.join(*segs)))
    return hits


def test_must_exist_never_pins_gitignored_state():
    """Class tripwire: MUST_EXIST may demand EXISTENCE only for constants
    whose DEFAULT resolution is committed (present in every clone/worktree).
    The offender class is a default path that .gitignore declares machine
    state — registry.json (TR-175) is the one that bit: pinning it made the
    suite order-dependent (TR-301). Belt and braces: the historical pair is
    asserted explicitly, then every MUST_EXIST name with a literal env
    default is checked against .gitignore."""
    with open(os.path.join(REPO, '.gitignore')) as f:
        ignored = {line.strip() for line in f
                   if line.strip() and not line.startswith('#')}
    assert 'REGISTRY' not in MUST_EXIST, (
        'REGISTRY re-pinned as MUST_EXIST: registry.json is gitignored '
        'machine state — an unseeded clone/worktree fails the case and the '
        'import-time seed in the later router_seed.py control case silently '
        'heals it (TR-301 order dependence)')
    offenders = []
    for name in sorted(MUST_EXIST):
        for script, rel in _declared_env_default(name):
            if rel in ignored:
                offenders.append((name, script, rel))
    assert not offenders, (
        f'MUST_EXIST pins gitignored defaults: {offenders} — a fresh clone '
        f'or worktree without seeded machine state fails those cases '
        f'(TR-301 class); assert the path lands inside the repo instead')


def test_probe_technique_discriminates_abspath_from_realpath(tmp_path):
    """Non-vacuity: both symlink probes must FLAG the abspath idiom and PASS
    the realpath idiom on the same harness, else the parametrised test above
    proves nothing.

    Two legs (INT-CI-5 added the static one: the router_seed.py case now
    runs through _module_constants_static, so the mechanism self-test must
    prove THAT evaluator discriminating too, not only the runpy one):
      - runpy leg: the tiny synthetic modules are exec'd directly — the
        fidelity reference the other 14 modules still ride;
      - static leg: the same modules through the declaration-only slice,
        __file__ bound to the symlink exactly as in production routing."""
    real = tmp_path / 'real'
    link = tmp_path / 'link'
    (real / 'scripts').mkdir(parents=True)  # modules sit one level down, like the repo
    link.mkdir()
    fake_repo = str(real)
    body = "import os\nREPO = os.path.dirname(os.path.dirname(os.path.{}(__file__)))\n"
    for name in ('bad_mod.py', 'good_mod.py'):
        (real / 'scripts' / name).write_text(body.format('abspath' if name.startswith('bad') else 'realpath'))
        os.symlink(str(real / 'scripts' / name), str(link / name))

    def repo_const(name):
        mod = runpy.run_path(str(link / name), run_name='_tr202_probe')
        return mod['REPO']

    bad = repo_const('bad_mod.py')
    good = repo_const('good_mod.py')
    assert not _is_inside(fake_repo, bad), (
        'mechanism broken: abspath module resolved inside the real tree — '
        'the symlink probe cannot detect the TR-202 class')
    assert _is_inside(fake_repo, good), (
        'mechanism broken: realpath module did not resolve inside the real tree')

    # Static leg (INT-CI-5): the non-executing evaluator must discriminate
    # identically. _module_constants_static binds __file__ to its own
    # symlink (its own link dir, so the runpy-leg links above stay intact)
    # and execs only the declaration slice — for these two modules that is
    # exactly `import os` + the one REPO Assign, i.e. no execution gap.
    static_links = tmp_path / 'link-static'
    static = {}
    for name in ('bad_mod.py', 'good_mod.py'):
        consts = _module_constants_static(
            str(real / 'scripts' / name), str(static_links), repo=fake_repo)
        assert set(consts) == {'REPO'}, (
            f'{name}: static probe returned {sorted(consts)} — expected '
            'exactly REPO; the evaluator drifted and this leg proves nothing')
        static[name] = consts['REPO']
    assert not _is_inside(fake_repo, static['bad_mod.py']), (
        'static probe broken: abspath module resolved inside the real tree — '
        'the NO_EXEC router_seed.py route cannot detect the TR-202 class')
    assert _is_inside(fake_repo, static['good_mod.py']), (
        'static probe broken: realpath module did not resolve inside the '
        'real tree — the NO_EXEC router_seed.py route would flag a good '
        'module')


def _sync_runtime_installed_scripts():
    """The *.py files sync_runtime.sh wires into ~/.hermes/scripts/ (both the
    symlink loop and the copy loop). Source of truth for the source-level guard."""
    with open(SYNC_RUNTIME) as f:
        text = ' '.join(line.strip() for line in f)
    installed = []
    for block in re.findall(r'for f in (.*?); do', text):
        installed.extend(tok for tok in block.split() if tok.endswith('.py'))
    assert 'router_spawn.py' in installed, 'sync_runtime.sh parse drifted — fix the test'
    return installed


def test_no_live_installed_runtime_script_uses_abspath_file():
    """Source-level guard over the LIVE-INSTALL set: no runtime tool that ships
    to ~/.hermes/scripts/ may derive paths with abspath(__file__) — through the
    symlink topology that idiom lands on ~/.hermes (the TR-202 bug). Repo-only
    tools (never installed) are out of scope: abspath is correct there."""
    offenders = []
    for name in _sync_runtime_installed_scripts():
        if name.startswith('test_'):
            continue  # live-installed test copies exercise the live copy on purpose
        with open(os.path.join(SCRIPTS, name)) as f:
            if 'abspath(__file__)' in f.read():
                offenders.append(name)
    assert not offenders, (
        f'abspath(__file__) in live-installed runtime tools: {offenders} — '
        'use realpath (or Path.resolve); see docs/decisions/TR-202-abspath-audit.md')

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

These tests exec the real repo files through a scratch symlink (never the live
~/.hermes/scripts tree — the scheduler and crons own it) and assert that every
repo-derived path constant the module itself computed lands INSIDE the real
checkout. A mechanism self-test proves the technique discriminates: the same
probe run against a deliberately-abspath module flags it, the realpath module
passes.
"""
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
REAL_HOME = os.path.join(os.path.expanduser('~'), '.local', 'share',
                         'task-router')

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
    this checkout's machine state."""
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


def _assert_symlink_contract(module_file, repo, tmp_path):
    """The TR-202 contract, shared by the parametrised suite and the TR-301
    regressions: exec module_file through a scratch symlink under `repo` and
    assert every repo-derived constant lands inside it (and MUST_EXIST ones
    exist on disk). Keeping ONE body means a regression test exercises the
    exact assertion path the suite runs."""
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


@pytest.mark.parametrize('module_file', FIXED + CONTROLS)
def test_repo_constants_resolve_inside_real_tree_through_symlink(module_file, tmp_path):
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
    """TR-301 root cause, second half: the parametrised router_seed.py control
    case is an IMPORT-TIME writer (its whole seed body is module-level — it
    writes <repo>/registry.json and data/tables/, and exports the ns mirror
    tables under ROUTING_NS). Running it in-process against the real checkout
    writes gitignored machine state INTO the working tree mid-suite, silently
    healing every later unseeded case (the mechanism that made the original
    TR-301 failure order-dependent), and can export ns tables outside the
    repo. The probe's env pinning (ROUTING_REGISTRY/ROUTING_DATA_DIR) cannot
    redirect the seed: they point at REPO-relative paths BY DESIGN.

    Pinned by exec'ing the seed control case against a scratch repo with
    EVERY seed-redirect var pinned into tmp_path, then asserting the real
    checkout's registry.json is absent/untouched and no ns export landed
    outside the scratch. The probe helper itself stays unchanged — this test
    exercises the exact entry the suite runs."""
    scratch_scripts, scratch_data = _write_free_scratch_repo(
        str(tmp_path / 'sandbox'), SCRIPTS, os.path.join(REPO, 'data', 'tables'))
    repo_state = ('absent' if not os.path.exists(REGISTRY_SENTINEL)
                  else f'mtime:{os.path.getmtime(REGISTRY_SENTINEL)}')
    data_state = {f: os.path.getmtime(os.path.join(REAL_DATA, f))
                  for f in os.listdir(REAL_DATA) if f.endswith('.jsonl')}
    before = set(os.listdir(REAL_HOME))

    # The default ns-mirror export target on boxes that already ran an
    # unset-ROUTING_NS seed: its TABLES must not be touched either (a repeat
    # export creates no new dirs — only file-level mtimes catch it).
    ns_tables_dir = os.path.join(REAL_HOME, 'scratch', 'ns', 'routing',
                                 'tables')
    ns_state = {f: os.path.getmtime(os.path.join(ns_tables_dir, f))
                for f in (os.listdir(ns_tables_dir)
                          if os.path.isdir(ns_tables_dir) else [])}

    scratch = str(tmp_path / 'sandbox')
    _assert_symlink_contract('router_seed.py', scratch,
                             str(tmp_path / 'contract-links'))

    assert repo_state == 'absent' or repo_state == (
        f'mtime:{os.path.getmtime(REGISTRY_SENTINEL)}'), (
        f'the router_seed.py control case wrote {REGISTRY_SENTINEL} in the '
        'real checkout — the seed module body is an import-time writer; pin '
        'the seed redirects into scratch state (TR-301)')
    data_after = {f: os.path.getmtime(os.path.join(REAL_DATA, f))
                  for f in os.listdir(REAL_DATA) if f.endswith('.jsonl')}
    assert data_after == data_state, (
        'the router_seed.py control case rewrote data/tables/ in the real '
        'checkout (import-time seed body — TR-301)')
    new_home_entries = set(os.listdir(REAL_HOME)) - before
    assert not new_home_entries, (
        f'the router_seed.py control case created {sorted(new_home_entries)} '
        f'under {REAL_HOME} — ns mirror export escaped the scratch '
        '(ROUTING_NS unpinned — TR-301)')
    ns_after = {f: os.path.getmtime(os.path.join(ns_tables_dir, f))
                for f in (os.listdir(ns_tables_dir)
                          if os.path.isdir(ns_tables_dir) else [])}
    assert ns_after == ns_state, (
        f'the router_seed.py control case re-exported ns mirror tables '
        f'under {ns_tables_dir} (ROUTING_NS unpinned — TR-301)')


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
    """Non-vacuity: the symlink probe must FLAG the abspath idiom and PASS the
    realpath idiom on the same harness, else the parametrised test above proves
    nothing."""
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

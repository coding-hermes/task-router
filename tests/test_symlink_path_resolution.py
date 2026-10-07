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
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

SYNC_RUNTIME = os.path.join(SCRIPTS, 'sync_runtime.sh')

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
# overrides): they are the tables/files every consumer reads.
MUST_EXIST = {
    'DATA_DIR', 'REGISTRY', 'PROMPT_DIR', 'BANDS_PATH', 'MODELS', 'PLANS',
    '_HERE', '_SCRIPTS_DIR', 'HEAL_SCRIPT',
}


def _module_constants_through_symlink(module_file, link_dir):
    """Exec repo script via a symlink in link_dir; return its path constants."""
    link = os.path.join(link_dir, os.path.basename(module_file))
    os.symlink(module_file, link)
    with pytest.MonkeyPatch.context() as mp:
        # Hermetic: the modules honour these env overrides; pin them so a test
        # run never reads (or writes) operator state.
        mp.setenv('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))
        mp.setenv('ROUTING_REGISTRY', os.path.join(REPO, 'registry.json'))
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


@pytest.mark.parametrize('module_file', FIXED + CONTROLS)
def test_repo_constants_resolve_inside_real_tree_through_symlink(module_file, tmp_path):
    consts = _module_constants_through_symlink(
        os.path.join(SCRIPTS, module_file), str(tmp_path))
    assert consts, f'{module_file}: no repo-derived path constants found — audit list stale'
    for name, val in sorted(consts.items()):
        assert _is_inside(REPO, val), (
            f'{module_file}: {name} resolved to {val!r}, outside the real tree '
            f'{REPO!r} — abspath(__file__) regression through the live symlink')
        if name in MUST_EXIST:
            assert os.path.exists(val), (
                f'{module_file}: {name} = {val!r} does not exist on disk')


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

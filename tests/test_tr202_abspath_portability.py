"""TR-202 re-pass (2026-10-07) — repo-data portability of the state tools.

The 2026-09-29 TR-202 pass fixed the abspath(__file__) symlink class for the
LIVE-INSTALLED tools (guarded by tests/test_symlink_path_resolution.py). This
re-pass covers what that pass's census left behind: hardcoded
/home/kara/... data paths in the state-side tools — cost_backfill.py,
dummy_scheduler.py, proxy_smoke.py, plan_effective_backfill.py,
router_maintain.py (ns defaults), router_pricing_audit.py (STATE_DB),
router_refresh_resume.py (phase0 reconfigure path) and the bash pipeline
router-data-quality.sh (`cd /home/kara/task-router`). On any other user,
host, second checkout or worktree those pointed at the wrong tree (or exited).

Contract tested here (same one router_seed.py has had since TR-016/TR-049):
  - repo-data defaults resolve INSIDE the importing checkout, from any CWD,
    through a symlink too (realpath idiom);
  - the shared ROUTING_* env overrides steer every state tool identically;
  - the pipeline scripts derive their repo from the script location, not a
    hardcoded user path.

Everything runs in scratch dirs / foreign CWDs; no probe touches live state
(the outcomes store default is gitignored runtime state, and the apply probe
brings its own store via ROUTING_OUTCOMES_FILE).
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
PY = sys.executable

# env that must be scrubbed for the "default resolution" probes: any of these
# present in the ambient environment would override the repo-relative default
# under test and silently weaken the assertion.
ROUTING_ENV = ('ROUTING_OUTCOMES_FILE', 'ROUTING_REGISTRY', 'ROUTING_DATA_DIR',
               'ROUTING_STATE_DB', 'ROUTING_NS', 'TASKROUTER_NS')

_PROBE_DRIVER = (
    "import json, os, runpy, sys\n"
    "mod = runpy.run_path(sys.argv[1], run_name='_tr202_probe_not_main')\n"
    "names = sys.argv[2:]\n"
    "out = {k: str(mod[k]) for k in names if k in mod}\n"
    "print(json.dumps(out))\n"
)


def _module_constants_from_foreign_cwd(script, names, cwd, env=None):
    """Exec a repo script (runpy, main() NOT run) from cwd; return the str
    values of the named module constants as the module itself computed them.
    env=None -> ambient env minus the ROUTING_* overrides."""
    if env is None:
        env = {k: v for k, v in os.environ.items() if k not in ROUTING_ENV}
    p = subprocess.run([PY, '-c', _PROBE_DRIVER, script, *names],
                       cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stderr[-1500:]
    return json.loads(p.stdout)


def _assert_inside_repo(tool, name, val):
    assert val == REPO or val.startswith(REPO + os.sep), (
        f'{tool}: {name} resolved to {val!r}, outside this checkout {REPO!r} '
        f'— repo-data portability regression (TR-202 class)')


# --------------------------------------------------------------------------
# the fixed tools: repo-data defaults from a foreign CWD
# --------------------------------------------------------------------------

@pytest.mark.parametrize('script,names', [
    ('cost_backfill.py', ['LEDGER', 'REGISTRY']),
    ('dummy_scheduler.py', ['LEDGER', 'RAW_OUT']),
    ('proxy_smoke.py', ['LEDGER']),
])
def test_state_tool_defaults_resolve_inside_repo_from_foreign_cwd(script, names,
                                                                  tmp_path):
    got = _module_constants_from_foreign_cwd(
        os.path.join(SCRIPTS, script), names, str(tmp_path))
    assert set(got) == set(names), (
        f'{script}: expected constants {names}, probe saw {sorted(got)}')
    for name, val in got.items():
        _assert_inside_repo(script, name, val)
        assert val.endswith(name.lower().replace('_', '-') + '.jsonl') or \
            val.endswith(name.lower() + '.jsonl') or \
            os.path.basename(val), name  # a real file name, not a stray dir


def test_proxy_smoke_out_default_is_repo_data_state(tmp_path):
    got = _module_constants_from_foreign_cwd(
        os.path.join(SCRIPTS, 'proxy_smoke.py'), ['OUT'], str(tmp_path))
    _assert_inside_repo('proxy_smoke.py', 'OUT', got['OUT'])
    assert os.path.basename(got['OUT']) == 'proxy-smoke.jsonl'


def test_plan_effective_backfill_shares_the_tr049_outcomes_resolver(tmp_path,
                                                                    monkeypatch):
    """The --store default must come from router_outcomes.outcomes_path()
    (ROUTING_OUTCOMES_FILE -> repo gitignored state), not a hardcoded path —
    and the dead ~/.hermes/model-router/../... STORE global stays dead."""
    monkeypatch.syspath_prepend(SCRIPTS)
    import plan_effective_backfill as peb  # noqa: E402
    import router_outcomes as ro  # noqa: E402

    # default (env scrubbed): the repo-relative gitignored store
    monkeypatch.delenv('ROUTING_OUTCOMES_FILE', raising=False)
    assert peb._default_store() == ro._DEFAULT_OUTCOMES
    _assert_inside_repo('plan_effective_backfill.py', 'default store',
                        peb._default_store())
    # no /home/kara anywhere in the module source
    src = open(peb.__file__, encoding='utf-8').read()
    assert '/home/kara' not in src
    assert 'model-router/../' not in src  # the old dead STORE global
    # and the env override steers it like every sibling tool
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'ledger.jsonl'))
    assert peb._default_store() == str(tmp_path / 'ledger.jsonl')


# --------------------------------------------------------------------------
# the full loop: a foreign-CWD backfill run prices a scratch store
# --------------------------------------------------------------------------

def _first_priced_lane():
    """(provider, model, public_in_per_m, public_out_per_m) from the repo's
    own committed models table — the pricing the probe ride reads."""
    path = os.path.join(REPO, 'data', 'tables', 'models.jsonl')
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get('public_in_per_m') is not None and d.get('provider') \
                    and d.get('model'):
                return (d['provider'], d['model'],
                        float(d['public_in_per_m']),
                        float(d.get('public_out_per_m') or 0))
    return None


@pytest.mark.skipif(_first_priced_lane() is None,
                    reason='no priced lane in data/tables/models.jsonl')
def test_plan_effective_backfill_roundtrip_from_foreign_cwd(tmp_path):
    """Dry-run then --apply against a scratch store, invoked from a foreign
    CWD with the ROUTING_OUTCOMES_FILE override: the tool prices the row and
    writes ITS store (backup beside it) — never /home/kara."""
    prov, model, tin, tout = _first_priced_lane()
    store = tmp_path / 'outcomes.jsonl'
    store.write_text(json.dumps({
        'source_system': 'hermes', 'provider': prov, 'model': model,
        'tokens_in': tin, 'tokens_out': tout,
        'cost_usd': None, 'price_basis': None,
    }) + '\n', encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if k not in ROUTING_ENV}
    env['ROUTING_OUTCOMES_FILE'] = str(store)

    def run(*args):
        return subprocess.run(
            [PY, os.path.join(SCRIPTS, 'plan_effective_backfill.py'), *args],
            cwd=str(tmp_path), env=env, capture_output=True, text=True,
            timeout=120)

    dry = run()
    assert dry.returncode == 0, dry.stderr[-1500:]
    report, _ = json.JSONDecoder().raw_decode(dry.stdout)
    assert report['store'] == str(store), (
        f"backfill targeted {report['store']!r}, not the scratch store — "
        f"hardcoded-path regression")
    assert report['priced_now'] == 1, report
    assert report['applied'] is False and not list(tmp_path.glob('*.backup*'))

    app = run('--apply')
    assert app.returncode == 0, app.stderr[-1500:]
    row = json.loads(store.read_text(encoding='utf-8').splitlines()[0])
    assert isinstance(row['cost_usd'], (int, float)) and row['cost_usd'] > 0
    assert 'driver reported $0' in row['price_basis']
    backups = list(tmp_path.glob('outcomes.jsonl.backup-*'))
    assert len(backups) == 1, 'apply must back up the store before writing'


# --------------------------------------------------------------------------
# the bash pipeline: repo derived from the script's own location
# --------------------------------------------------------------------------

def test_router_data_quality_pipeline_runs_from_its_own_location(tmp_path):
    """A COPY of router-data-quality.sh must cd into the COPY's own repo and
    run every pipeline step THERE — never silently run the hardcoded
    /home/kara/task-router main tree like the pre-fix version did.

    The steps execute under a STUB interpreter (PYTHON= env) that only records
    its CWD, so this test is side-effect-free in BOTH directions: post-fix it
    exercises an empty foreign repo; on a reverted (hardcoded) file the stubs
    land in the main tree but touch nothing — the recorded CWDs are the
    failure evidence.
    """
    foreign = tmp_path / 'somewhere' / 'else'
    (foreign / 'scripts').mkdir(parents=True)   # real layout: <repo>/scripts/
    dst = foreign / 'scripts' / 'router-data-quality.sh'
    dst.write_text(open(os.path.join(SCRIPTS, 'router-data-quality.sh'),
                        encoding='utf-8').read(), encoding='utf-8')
    record = tmp_path / 'cwd-records.txt'
    stub = tmp_path / 'stub-py'
    stub.write_text(
        '#!/bin/sh\n'
        f'echo "$(pwd)" >> {json.dumps(str(record))}\n'
        'exit 0\n', encoding='utf-8')
    os.chmod(stub, 0o755)
    env = {k: v for k, v in os.environ.items() if k not in ROUTING_ENV}
    env['PYTHON'] = str(stub)
    p = subprocess.run(['bash', str(dst)], cwd='/', env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-1500:]
    cwds = [l for l in record.read_text(encoding='utf-8').splitlines() if l]
    assert len(cwds) >= 6, (
        f'expected one stub run per pipeline step, saw {len(cwds)}: {cwds}')
    foreign_real = os.path.realpath(str(foreign))
    wrong = [c for c in cwds if os.path.realpath(c) != foreign_real]
    assert not wrong, (
        f'pipeline steps ran OUTSIDE the relocated copy\'s own repo: {wrong} '
        f'— hardcoded-repo regression (pre-fix the steps ran in '
        f'/home/kara/task-router, silently syncing THAT tree)')


# --------------------------------------------------------------------------
# refresh-resume: phase0 reconfigure path resolved, never /home/kara
# --------------------------------------------------------------------------

@pytest.fixture()
def rrr(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    import router_refresh_resume as mod  # noqa: E402
    return mod


def test_reconfigure_prefers_the_checkout_copy(rrr, tmp_path, monkeypatch):
    repo_copy = tmp_path / 'repo' / 'scripts' / 'reconfigure.py'
    repo_copy.parent.mkdir(parents=True)
    repo_copy.write_text('# stub\n', encoding='utf-8')
    monkeypatch.setattr(rrr, 'REPO', tmp_path / 'repo')
    assert rrr._find_reconfigure_script() == repo_copy


def test_reconfigure_fallback_is_home_relative_not_kara(rrr, tmp_path,
                                                        monkeypatch):
    """On a foreign host (no repo copy, HOME elsewhere) the fallback is the
    INVOKING USER's live install — never the hardcoded /home/kara path."""
    monkeypatch.setattr(rrr, 'REPO', tmp_path / 'repo')  # no scripts/ inside
    monkeypatch.setenv('HOME', str(tmp_path / 'stranger'))
    got = rrr._find_reconfigure_script()
    assert got == tmp_path / 'stranger' / '.hermes' / 'scripts' / 'reconfigure.py'
    assert '/home/kara' not in str(got)


def test_pipeline_steps_wire_the_resolved_reconfigure_path(rrr):
    """The phase0 argv must reference the module's resolved constant (never a
    stale /home/kara literal baked into the step table)."""
    argv = rrr.PIPELINE_STEPS[0][1]
    assert argv[1] == str(rrr._RECONFIGURE_SCRIPT)
    assert '/home/kara' not in argv[1] or str(rrr._RECONFIGURE_SCRIPT) == argv[1]


# --------------------------------------------------------------------------
# maintain ns defaults + pricing-audit state db: expanduser, not /home/kara
# --------------------------------------------------------------------------

def test_maintain_ns_defaults_expanduser(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    import router_maintain as rm  # noqa: E402
    assert rm.ROUTING_NS == os.path.expanduser('~/duckbrain/namespaces/routing')
    assert rm.TASKROUTER_NS == os.path.expanduser(
        '~/duckbrain/namespaces/task-router')


def test_maintain_ns_env_still_wins(tmp_path):
    """ROUTING_NS is read at import time: prove via a fresh subprocess that a
    preset env steers BOTH ns defaults (the production path — maintain's
    callers always set them)."""
    env = {k: v for k, v in os.environ.items() if k not in ROUTING_ENV}
    env['ROUTING_NS'] = str(tmp_path / 'ns')
    env['TASKROUTER_NS'] = str(tmp_path / 'tns')
    got = _module_constants_from_foreign_cwd(
        os.path.join(SCRIPTS, 'router_maintain.py'),
        ['ROUTING_NS', 'TASKROUTER_NS'], str(tmp_path), env=env)
    assert got == {'ROUTING_NS': str(tmp_path / 'ns'),
                   'TASKROUTER_NS': str(tmp_path / 'tns')}


def test_pricing_audit_state_db_expanduser():
    src = open(os.path.join(SCRIPTS, 'router_pricing_audit.py'),
               encoding='utf-8').read()
    assert "expanduser('~/.hermes/state.db')" in src
    assert "'/home/kara/.hermes/state.db'" not in src


# --------------------------------------------------------------------------
# the new fixes ride the existing live-symlink guard
# --------------------------------------------------------------------------

def test_new_fixed_tools_are_in_the_symlink_guard_set():
    """tests/test_symlink_path_resolution.py FIXED list must include the
    tools this pass made repo-relative, so the symlink-exec guard covers
    them from now on."""
    src = open(os.path.join(REPO, 'tests', 'test_symlink_path_resolution.py'),
               encoding='utf-8').read()
    for tool in ('cost_backfill.py', 'dummy_scheduler.py', 'proxy_smoke.py'):
        assert f"'{tool}'" in src, (
            f'{tool} missing from the symlink guard FIXED list')

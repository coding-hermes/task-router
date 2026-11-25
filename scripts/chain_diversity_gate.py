#!/usr/bin/env python3
"""Chain-diversity gate (TR-368) — a profile that cannot resolve is a data defect.

WHY THIS EXISTS
---------------
A profile with no chain hops, or a chain built on ONE provider, fails silently:
the resolver answers what it can, the payload is well-formed, and nothing in the
repo ever asked. TR-368's board baseline (measured 2026-11-24) claimed
P4_SECURITY and P5_VISION_E2E at 0 hops and 61/98 hops concentrated on one
free-priced provider. This gate makes that class of starvation FATAL at the CI
boundary instead of a surprise in a dogfood session.

THE RULE
--------
Every profile in data/tables/task_profiles.jsonl must resolve with:

  hops      >= 1   (the chain exists)
  providers >= 2   (at least two DISTINCT providers back it)

Anything less is a violation; the gate prints the per-profile hop count and
provider histogram and exits non-zero. Profiles are never deleted to pass it —
fix the data (fallback_lanes / model_perf / category_levels / task_profile
requirements) so each profile can actually be served by more than one provider.

MEASUREMENT PLANE (hermetic, stated so it is not mistaken for production)
------------------------------------------------------------------------
The gate resolves through the REAL chain-resolution code (router_spawn.resolve)
against the COMMITTED tables only:

  * registry.json is forced AWAY (ROUTING_REGISTRY -> an absent scratch path) so
    resolution reads the committed data/tables/*.jsonl fallback — never a stale
    seeded registry that could disagree with the tables under review;
  * ROUTER_STATE_DIR points at a scratch policy dir seeded ci-mirror style
    (every committed provider OPEN except grok-build/crof — the same policy the
    CI seed step and fresh-clone smoke write), so the fail-closed policy gate
    never gates the measurement while quota/health/circuit state stays out;
  * ROUTING_AVERAGES_FILE points into the scratch dir, so the (gitignored,
    per-deployment) outcome-floor data cannot flip the verdict between boxes.

    `chain_diversity_gate.py`              # report; exit 1 on any violation
    chain_diversity_gate.py --json         # machine-readable report

Read-only over the committed tables and the live code. No state is written in
production locations. Tests: tests/test_chain_diversity_gate.py (red/green on
scratch fixtures + the committed-data pass, same pattern as test_null_debt
_invariants.py).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TABLES_DEFAULT = os.environ.get('ROUTING_DATA_DIR') or os.path.join(ROOT, 'data', 'tables')
#: ci-mirror (TR-343 era): the two deliberately-GATED providers in CI's seeded
#: policy plane. Same list the workflow seeding step and fresh-clone smoke use.
CI_MIRROR_GATED = ('grok-build', 'crof')

MIN_HOPS = 1
MIN_PROVIDERS = 2


def _jsonl(tables_dir: str, name: str) -> list[dict]:
    path = os.path.join(tables_dir, name)
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — a torn line must not kill the gate
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def profile_ids(tables_dir: str) -> list[str]:
    """Every profile id in the committed task_profiles table, in file order."""
    ids = []
    for row in _jsonl(tables_dir, 'task_profiles.jsonl'):
        pid = row.get('id')
        if pid:
            ids.append(str(pid))
    return ids


def _provider_of(hop: dict):
    return hop.get('provider') or hop.get('provider_id')


def evaluate(stats: list[dict]) -> tuple[list[dict], bool]:
    """Pure verdict over per-profile stats. Returns (violations, passed).

    Violations name the profile, the measured shape and the threshold, in the
    gate's own two codes: 'no-hops' (0 hops) and 'single-provider' (>0 hops,
    fewer than MIN_PROVIDERS distinct providers).
    """
    violations = []
    for s in stats:
        hops = s['hops']
        provs = s['providers']
        if hops < MIN_HOPS:
            violations.append({
                'profile': s['profile'],
                'code': 'no-hops',
                'message': (f"{s['profile']}: 0 hops resolved (needs >= {MIN_HOPS}) "
                            '— the profile cannot be served at all'),
            })
        elif len(provs) < MIN_PROVIDERS:
            one = next(iter(provs))
            violations.append({
                'profile': s['profile'],
                'code': 'single-provider',
                'message': (f"{s['profile']}: all {hops} hops on one provider "
                            f"'{one}' (needs >= {MIN_PROVIDERS} distinct)"),
            })
    return violations, not violations


def _seed_policy_state(state_dir: str, tables_dir: str) -> str:
    """Write the ci-mirror policy plane (all committed providers OPEN except
    the two deliberately gated) so the fail-closed policy gate measures DATA,
    not this box's deployment state. Returns the provider count seeded."""
    provs = {}
    for row in _jsonl(tables_dir, 'providers.jsonl'):
        pid = row.get('id')
        if not pid:
            continue
        provs[str(pid)] = ({'status': 'gated', 'reason': 'ci-mirror'}
                           if str(pid) in CI_MIRROR_GATED else {'status': 'open'})
    if not provs:
        # A tables dir with no providers resolves to zero hops everywhere; the
        # resolve payload's exclusions will say so. Keep the file present so the
        # failure reason stays data-shaped ('no hops') not state-shaped.
        provs = {}
    with open(os.path.join(state_dir, 'quota-state.json'), 'w') as fh:
        json.dump({'updated': 'chain-diversity-gate', 'providers': provs}, fh)
    with open(os.path.join(state_dir, 'health-state.json'), 'w') as fh:
        json.dump({'providers': {pid: {'status': 'open'} for pid in provs}}, fh)
    with open(os.path.join(state_dir, 'circuit-state.json'), 'w') as fh:
        json.dump({'version': 1, 'pairs': {}}, fh)
    return len(provs)


def measure(tables_dir: str) -> dict:
    """Resolve every profile via the real resolver; return the full report.

    Must run in a process whose router_spawn import sees the hermetic env below;
    main() re-execs itself when a router_spawn was already imported elsewhere
    (frozen module-level state paths), so both paths stay honest.
    """
    os.environ['ROUTER_STATE_DIR'] = _state_dir
    os.environ['ROUTING_DATA_DIR'] = tables_dir
    # Force the committed-tables fallback: registry.json (seeded, possibly stale
    # relative to the tables under review) must never answer for them.
    os.environ['ROUTING_REGISTRY'] = os.path.join(_state_dir, 'registry-absent.json')
    os.environ['ROUTING_AVERAGES_FILE'] = os.path.join(_state_dir, 'averages-absent.jsonl')
    os.environ['ROUTER_SPAWN_QUIET'] = '1'

    if 'router_spawn' in sys.modules:
        mr = getattr(sys.modules['router_spawn'], 'MR', None)
        if mr and os.path.abspath(mr) != os.path.abspath(_state_dir):
            # router_spawn freezes its state paths at import; another import in
            # this process points elsewhere. Re-exec THIS process with the env
            # above already set so the fresh import measures the plane we chose.
            os.execve(sys.executable, [sys.executable, *sys.argv],
                      {**os.environ})

    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    import router_spawn  # noqa: PLC0415 — lazy import AFTER the env isolation

    tables, source, fallback_used, warn = router_spawn._load_registry_with_meta()
    pids = profile_ids(tables_dir)
    stats = []
    for pid in pids:
        resolved = router_spawn.resolve(profile_id=pid, use_health=False)
        chain = resolved.get('chain') or []
        hops = []
        for h in chain:
            if isinstance(h, dict) and _provider_of(h):
                hops.append(_provider_of(h))
        prov_hist = collections.Counter(hops)
        stats.append({
            'profile': pid,
            'hops': len(hops),
            'providers': prov_hist,
            'error': resolved.get('error'),
        })
    return {
        'tables_dir': tables_dir,
        'source': source,
        'fallback_used': fallback_used,
        'registry_warning': warn,
        'policy_providers': _seed_policy_state(_state_dir, tables_dir),
        'profiles': stats,
    }


def render(report: dict) -> str:  # noqa: PLR0912 — one screen, sequential blocks
    stats = report['profiles']
    violations, passed = evaluate(stats)
    total_hops = sum(s['hops'] for s in stats)
    total_hist = collections.Counter()
    for s in stats:
        total_hist.update(s['providers'])
    lines = []
    lines.append('=' * 72)
    lines.append('CHAIN-DIVERSITY GATE — no profile resolves to 0 hops or one provider')
    lines.append('=' * 72)
    lines.append(f"  data            {report['tables_dir']}"
                 f"   (source={report['source']} fallback_used={report['fallback_used']})")
    if report.get('registry_warning'):
        lines.append(f"  registry note   {report['registry_warning']}")
    lines.append(f"  policy plane    hermetic scratch: {report['policy_providers']} providers "
                 "(ci-mirror: all open, quota/health/circuit state excluded)")
    lines.append(f'  profiles        {len(stats)}   total hops {total_hops}')

    lines.append('  ' + '-' * 68)
    lines.append(f"    {'profile':<16}{'hops':>6}{'distinct':>10}{'top share':>11}   provider histogram")
    for s in stats:
        hist = s['providers']
        top_share = (100.0 * (hist.most_common(1)[0][1] / s['hops'])) if s['hops'] else 0.0
        hist_str = ', '.join(f'{p}:{n}' for p, n in hist.most_common()) or '-'
        err = f"   [resolve error: {s['error']}]" if s.get('error') else ''
        lines.append(f"    {s['profile']:<16}{s['hops']:>6}{len(hist):>10}"
                     f"{top_share:>10.1f}%   {hist_str}{err}")
    lines.append('  ' + '-' * 68)
    total_str = ', '.join(f'{p}:{n}' for p, n in total_hist.most_common()) or '-'
    lines.append(f'    all profiles: {total_hops} hops across {len(total_hist)} providers: {total_str}')

    if violations:
        for v in violations:
            lines.append(f"  FAIL {v['message']}")
        names = ', '.join(sorted({v['profile'] for v in violations}))
        lines.append(f"\nFAIL: {len(violations)} chain-diversity violation(s) on {names}. "
                     'Fix the data (fallback_lanes / model_perf / category_levels / '
                     'requirements) so every profile is servable by >= 2 providers; '
                     'never delete a profile to pass this gate.')
        lines.append('PASS requires: every profile >= 1 hop and >= 2 distinct providers')
    else:
        lines.append('\nOK: every profile resolves with >= 1 hop and '
                     f'>= {MIN_PROVIDERS} distinct providers')
    return '\n'.join(lines)


_state_dir = ''


def main() -> int:
    global _state_dir
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    tables_dir = TABLES_DEFAULT
    _state_dir = tempfile.mkdtemp(prefix='chain-diversity-')
    try:
        # Seed BEFORE the (possible) re-exec so an exec'd child lands on the
        # same scratch state; also keeps measure() free of side-effect ordering.
        _seed_policy_state(_state_dir, tables_dir)
        report = measure(tables_dir)
        violations, passed = evaluate(report['profiles'])

        if args.json:
            payload = {
                'table': {s['profile']: {'hops': s['hops'],
                                         'providers': dict(s['providers'])}
                          for s in report['profiles']},
                'violations': violations,
                'passed': passed,
                'source': report['source'],
                'fallback_used': report['fallback_used'],
                'tables_dir': report['tables_dir'],
            }
            print(json.dumps(payload, indent=1))
        else:
            print(render(report))
        return 0 if passed else 1
    finally:
        shutil.rmtree(_state_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())

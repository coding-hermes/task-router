#!/usr/bin/env python3
"""router_chain_run.py — TR-066 Path A reference executor (side channel).

The side-channel contract in one command: resolve a chain for a DECLARED
complexity (profile id or ad-hoc category levels), then walk that chain
attempt by attempt — exactly what the scheduler / Hermes / any caller does when
it consumes the router's answer without the proxy.

Semantics (spec §2 R3–R5):
- Each attempt runs the caller's own command template; the template receives
  ROUTER_PROVIDER / ROUTER_MODEL / ROUTER_KEY_ENV / ROUTER_HOP in the
  environment (the caller owns auth — the router never touches keys).
- Exit 0 = success: stop, record breaker success + one outcome row (success=true).
- Non-zero exit = transport/HTTP failure: record breaker failure + one outcome
  row (success=false), advance to the next hop, up to --max-hops.
- Content dissatisfaction is NOT a retry trigger (spec R4): a template that
  fails on CONTENT must exit 0 and report success=false itself; this executor
  only walks the chain on transport failures.
- Idempotent write-back: rows are keyed (source_system, session_id, model), so
  re-running a session never double-counts.

Usage:
  python3 scripts/router_chain_run.py --profile P1_CODING \
      --cmd 'my-agent --model {model} --provider {provider}' [--max-hops 3] [--dry-run]
  python3 scripts/router_chain_run.py --profile-req 'code_gen=2 test=1' --sort predicted_cost_per_task ...
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro  # noqa: E402
import router_spawn as rs     # noqa: E402

SOURCE = 'chain-run'


def _breaker(provider, model, ok, reason=''):
    """Forward evidence to the router's breaker store (subprocess: the CLI form
    is the documented interface and honours ROUTER_STATE_DIR)."""
    script = os.path.join(REPO, 'scripts', 'router_circuit.py')
    cmd = [sys.executable, script, 'record-success' if ok else 'record-failure',
           provider, model]
    if not ok:
        cmd.append(reason or 'transport failure')
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — evidence is best-effort, never blocks
        pass


def run_chain(chain, cmd_template, max_hops=3, session_id=None, profile_id=None,
              requirements=None, dry_run=False, timeout_s=1800, env_extra=None,
              store=None, source=SOURCE):
    """Walk the resolved chain. Returns the summary dict (also printed by main)."""
    attempts = []
    result = {'attempts': attempts, 'final': None, 'success': False,
              'session_id': session_id, 'profile_id': profile_id,
              'required_categories': requirements}
    for hop in chain[:max_hops]:
        provider, model = hop.get('provider'), hop.get('model')
        cmd = (cmd_template
               .replace('{provider}', str(provider))
               .replace('{model}', str(model))
               .replace('{hop}', str(hop.get('hop'))))
        attempt = {'hop': hop.get('hop'), 'provider': provider, 'model': model,
                   'usd_1m': hop.get('usd_1m'), 'complexity_sig': (hop.get('outcomes') or {}).get('complexity_sig'),
                   'stats_fallback': (hop.get('outcomes') or {}).get('stats_fallback'),
                   'command': cmd}
        if dry_run:
            attempt['outcome'] = 'planned'
            attempts.append(attempt)
            continue
        env = {**os.environ, 'ROUTER_PROVIDER': str(provider),
               'ROUTER_MODEL': str(model), 'ROUTER_HOP': str(hop.get('hop')),
               'ROUTER_KEY_ENV': str(hop.get('key_env') or ''), **(env_extra or {})}
        t0 = time.time()
        try:
            p = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                               timeout=timeout_s, env=env)
            rc, err = p.returncode, (p.stderr or '')[-400:].strip()
            if rc != 0 and not err:
                err = f'exit code {rc}'   # a silent failure still names its cause
        except subprocess.TimeoutExpired:
            rc, err = 124, f'timeout after {timeout_s}s'
        except OSError as exc:
            rc, err = 127, str(exc)
        latency = round(time.time() - t0, 3)
        ok = (rc == 0)
        attempt.update({'outcome': 'success' if ok else 'transport-failure',
                        'rc': rc, 'latency_s': latency, 'error': None if ok else err})
        attempts.append(attempt)

        # outcome row per attempt — the loop's own measurement
        row = {'source_system': source, 'session_id': session_id or f'{SOURCE}-{t0}',
               'provider': provider, 'model': model, 'complexity': requirements,
               'required_categories': requirements, 'profile_id': profile_id,
               'cost_usd': None, 'wall_time_s': latency, 'turns': 1,
               'tokens_in': None, 'tokens_out': None, 'tokens_reasoning': None,
               'success': ok, 'ts': time.time()}
        if isinstance(requirements, dict) and requirements:
            row['complexity'] = requirements
            row['complexity_sig'] = ro.complexity_sig(requirements)
        try:
            ro.append_rows(store or ro.outcomes_path(), [row])
        except Exception as exc:  # noqa: BLE001
            attempt['writeback_error'] = str(exc)[:200]

        _breaker(provider, model, ok, err if not ok else '')
        if ok:
            result.update({'success': True, 'final': {'provider': provider, 'model': model}})
            return result
    if not dry_run:
        result['exhausted'] = bool(chain[:max_hops]) and not result['success']
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('project', nargs='?')
    ap.add_argument('--profile')
    ap.add_argument('--profile-req')
    ap.add_argument('--sort', default=None)
    ap.add_argument('--window-h', type=int, default=None)
    ap.add_argument('--backend')
    ap.add_argument('--merge-backends', action='store_true')
    ap.add_argument('--cmd', required=False, help='caller command template; '
                    'placeholders {provider} {model} {hop}')
    ap.add_argument('--max-hops', type=int, default=3)
    ap.add_argument('--timeout-s', type=int, default=1800)
    ap.add_argument('--session-id')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--format', choices=['text', 'json'], default='text')
    args = ap.parse_args()

    kwargs = {}
    if args.profile:
        kwargs['profile_id'] = args.profile
    if args.profile_req:
        # resolve() takes the ad-hoc list as 'cat=level' tokens (the CLI form)
        kwargs['adhoc'] = args.profile_req.split()
    if args.project:
        kwargs['project'] = args.project
    if args.sort:
        kwargs['sort'] = args.sort
    if args.window_h:
        kwargs['window_h'] = args.window_h
    if args.backend:
        kwargs['backend'] = args.backend
    if args.merge_backends:
        kwargs['merge_backends'] = True
    resolved = rs.resolve(**kwargs)
    chain = resolved.get('chain') or []
    if not chain:
        print(json.dumps({'error': 'no open hop', 'gate': resolved.get('gate'),
                          'exclusions': (resolved.get('exclusions') or [])[:5]}, indent=1))
        return 0  # fail-open: never block the caller

    reqs = None
    prof = args.profile
    if prof:
        sig = ro.profile_signature(prof)
        reqs = sig
    elif args.profile_req:
        from router_outcomes import canonical_complexity
        reqs = canonical_complexity(args.profile_req.split())
    summary = run_chain(chain, args.cmd or 'true', max_hops=args.max_hops,
                        session_id=args.session_id, profile_id=prof,
                        requirements=reqs, dry_run=args.dry_run,
                        timeout_s=args.timeout_s)
    summary['chain_length'] = len(chain)
    summary['sort'] = resolved.get('sort')
    if args.format == 'json':
        print(json.dumps(summary, indent=1))
    else:
        print(f"chain {len(chain)} hops, walked {len(summary['attempts'])}")
        for a in summary['attempts']:
            print(f"  hop {a['hop']}: {a['provider']}/{a['model']} -> {a['outcome']}"
                  f" ({a.get('latency_s', 0)}s)")
        print('final:', summary['final'] or ('EXHAUSTED' if summary.get('exhausted') else 'none'))
    return 0


if __name__ == '__main__':
    sys.exit(main())

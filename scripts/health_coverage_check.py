#!/usr/bin/env python3
"""health_coverage_check.py — name every ROUTABLE provider the health gate cannot see.

WHY THIS EXISTS (card t_neuralwatt_probe_coverage, 2026-11-21)

`~/.hermes/model-router/health-state.json` is a GATE input: `router_spawn.py`
reads `providers.<p>.status` and treats an ABSENT provider as un-gated
(fail-open — `hs.get(prov, {})` at the hop loop, and the same shape in the
fallback filter). The hourly `provider_health_probe.py` enumerates from
`data/tables/probe_providers.jsonl`, a CURATED credential list, not from the
registry. So a provider that has live lanes but no probe row is silently
un-gated: it can serve traffic while the resolve-time gate has nothing to
filter on. That is the same defect class as a gate that can never fire.

Measured 2026-11-21: neuralwatt answered HTTP 402 "Subscription overage
spending cap reached" on every chat call and 38 lanes stayed in chains (83
hops across 9 of 11 profiles) from the 2026-11-16 cancellation until the lanes
were disabled by hand (72f339f9). The health plane had no neuralwatt row at
all, so nothing could have gated it.

WHAT COUNTS AS A GAP (the three filters, in order)

  1. ACTIVE LANE — a `models.jsonl` row that is not `disabled`, not `archive`,
     not retired (`valid_to` in the past), whose `providers.jsonl` row is not
     `archive`. This mirrors router_spawn's eligibility (`row_is_retired` +
     archive/disabled) plus the provider-archive fact.
  2. ROUTABLE — the provider passes the TR-203 policy gate: it has a
     `quota-state.json` `providers` row with `status == 'open'`. A provider
     ABSENT from quota-state.json is already excluded by the router
     (`policy-gate-missing-row`), so its health gap is not exposure — it is
     reported under `excluded`, never as a gap. A `gated` row is likewise
     excluded. This is what keeps the check from crying wolf: on 2026-11-21
     four providers had active lanes and no health entry, but only ONE
     (muse-code) was actually routable.
  3. UNCOVERED — no `providers.<p>` entry in health-state.json.

Each gap is then CLASSIFIED so the fix is obvious instead of a mystery:

  probe-row            probe_providers.jsonl already carries this id — the
                       probe never ran for it, or its state entry was pruned
  alias                another probe row shares the same base_url (muse-code
                       and meta-model are both https://api.meta.ai/v1) — the
                       fix is one data row, not code
  registry-resolvable  providers.jsonl carries api_base_url + api_key_env, so
                       a probe row can be authored from registry facts
  unprobeable          no base_url anywhere (aws-bedrock needs SigV4, not a
                       bearer key) — a different auth shape, named not hidden

THE DESIGN DECISION (the card asks for one): the probe does NOT auto-enumerate
from the registry. `probe_providers.jsonl` stays the enumeration source because
it carries facts the registry does not — the default model that actually
answers, per-provider contract headers (opencode-go's required
`x-opencode-session`), and the wire-id form. Synthesizing a probe row from
`providers.jsonl` alone would ping a guessed model id, and a dead default makes
the hourly probe stamp the whole provider DOWN (the 2026-10-18 law), which
gates live lanes on a harness artifact. Instead the coverage gap is ASSERTED
every run: this script exits 1 on any gap, and `provider_health_probe.py`
prints the same section in its hourly report. A gap is then a one-line data
row with a verified default model, authored deliberately.

Exit codes: 0 every routable provider is health-covered; 1 at least one gap;
2 the check could not look (no models table, no health-state.json). A check
that cannot look never reports PASS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:  # same fail-open import contract as provider_health_probe.py
    import state_dir as _state_dir_mod
except ImportError:  # live byte-copy not yet synced -> historical default
    _state_dir_mod = None

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _state_dir_mod is not None:
    MR = _state_dir_mod.resolve_state_dir(script_file=__file__)
else:
    MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

# Same candidate order as provider_health_probe.py: repo first (CI, worktrees),
# then the historical home path (the live byte-copy), env override winning.
_DATA_CANDIDATES = (os.path.join(_REPO, 'data', 'tables'),
                    os.path.expanduser('~/task-router/data/tables'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR') or next(
    (d for d in _DATA_CANDIDATES if os.path.isdir(d)), _DATA_CANDIDATES[0])

PASS = 'pass'
FAIL = 'fail'
SKIP = 'skip'


# ---------------------------------------------------------------------------
# pure helpers (unit-tested without a live store)
# ---------------------------------------------------------------------------
def load_jsonl(path: str) -> list[dict]:
    """JSONL -> rows. Missing file = [] (the caller decides if that is fatal)."""
    rows: list[dict] = []
    if not path or not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_json(path: str, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def is_retired(row: dict, today: str) -> bool:
    """`valid_to` is a RETIREMENT date: past = retired, future = still live.

    Same date-compare as router_spawn.row_is_retired — testing `valid_to is not
    None` would retire a lane the moment a future decommission date is stamped.
    """
    vt = row.get('valid_to')
    if not vt:
        return False
    return str(vt)[:10] <= str(today)[:10]


def active_lane_counts(models_rows: list[dict], providers_rows: list[dict],
                       today: str) -> tuple[dict, dict]:
    """({provider: lane_count}, {provider: reason}) for ACTIVE lanes.

    A lane is active when the model row is not disabled/archived/retired and
    its provider row (when one exists) is not archived. Providers whose every
    lane is inactive land in the second map with the reason, so a provider that
    drops out of the check is never silent.
    """
    archived_providers = {r.get('id') for r in providers_rows if r.get('archive')}
    active: dict[str, int] = {}
    dropped: dict[str, str] = {}
    for row in models_rows:
        prov = row.get('provider')
        if not prov:
            continue
        if row.get('disabled'):
            dropped.setdefault(prov, 'all lanes disabled')
            continue
        if row.get('archive'):
            dropped.setdefault(prov, 'all lanes archived')
            continue
        if is_retired(row, today):
            dropped.setdefault(prov, 'all lanes retired (valid_to past)')
            continue
        if prov in archived_providers:
            dropped.setdefault(prov, 'provider row archived')
            continue
        active[prov] = active.get(prov, 0) + 1
    for prov in active:
        dropped.pop(prov, None)
    return active, dropped


def routable_providers(active: dict, quota_providers: dict) -> tuple[dict, dict]:
    """Split active providers by the TR-203 policy gate.

    The router FAIL-CLOSES on a provider with no quota-state row
    (`policy-gate-missing-row`) and skips a `gated` one, so only an `open` row
    can actually serve traffic. Returns ({provider: lanes}, {provider: reason}).
    """
    routable: dict[str, int] = {}
    excluded: dict[str, str] = {}
    for prov, lanes in active.items():
        row = quota_providers.get(prov)
        if not isinstance(row, dict):
            excluded[prov] = 'policy-gate-missing-row (not in quota-state.json)'
            continue
        status = row.get('status')
        if status != 'open':
            excluded[prov] = f'policy gate {status or "unset"}'
            continue
        routable[prov] = lanes
    return routable, excluded


def classify_gap(provider: str, providers_rows: list[dict],
                 probe_rows: list[dict]) -> dict:
    """Why is this provider unprobed, and what is the cheapest fix?

    Order matters: an existing probe row is the strongest fact (the probe has
    credentials and simply never covered it), then a shared base_url (an alias
    of a provider that IS probed), then registry-resolvable credentials, then
    unprobeable.
    """
    probe_by_id = {r.get('id'): r for r in probe_rows if r.get('id')}
    if provider in probe_by_id:
        return {'kind': 'probe-row',
                'detail': 'probe_providers.jsonl has this id — the probe never '
                          'ran for it, or its state entry was pruned'}
    prov_row = next((r for r in providers_rows if r.get('id') == provider), {})
    base = prov_row.get('api_base_url')
    if base:
        twins = sorted(r.get('id') for r in probe_rows
                       if r.get('base_url') and r.get('base_url') == base
                       and r.get('id') != provider)
        if twins:
            return {'kind': 'alias',
                    'detail': f'same base_url as probed provider(s) '
                              f'{", ".join(twins)} — one data row fixes it'}
    if base and prov_row.get('api_key_env'):
        return {'kind': 'registry-resolvable',
                'detail': f'providers.jsonl carries api_base_url + '
                          f'api_key_env ({prov_row.get("api_key_env")}) — a probe '
                          f'row can be authored from registry facts'}
    return {'kind': 'unprobeable',
            'detail': 'no api_base_url in providers.jsonl — this provider has no '
                      'bearer-key HTTP surface the prober can reach (e.g. SigV4); '
                      'it needs its own auth shape, not a probe row'}


def coverage_report(models_rows: list[dict], providers_rows: list[dict],
                    probe_rows: list[dict], health_doc: dict,
                    quota_doc: dict, today: str) -> dict:
    """The whole check as data: routable set, gaps, exclusions, stale entries."""
    active, inactive = active_lane_counts(models_rows, providers_rows, today)
    quota_providers = (quota_doc or {}).get('providers') or {}
    if not isinstance(quota_providers, dict):
        quota_providers = {}
    routable, policy_excluded = routable_providers(active, quota_providers)
    health_providers = (health_doc or {}).get('providers') or {}
    if not isinstance(health_providers, dict):
        health_providers = {}
    gaps = []
    for prov in sorted(routable):
        if prov in health_providers:
            continue
        gap = {'provider': prov, 'lanes': routable[prov]}
        gap.update(classify_gap(prov, providers_rows, probe_rows))
        gaps.append(gap)
    covered = sorted(p for p in routable if p in health_providers)
    stale = sorted(p for p in health_providers if p not in active)
    return {
        'today': today,
        'active_providers': len(active),
        'routable_providers': len(routable),
        'health_providers': len(health_providers),
        'covered': covered,
        'gaps': gaps,
        'policy_excluded': dict(sorted(policy_excluded.items())),
        'inactive': dict(sorted(inactive.items())),
        'stale_health_entries': stale,
        'status': FAIL if gaps else PASS,
    }


def render(report: dict) -> list[str]:
    """Human report. A gap section is always printed, even when empty."""
    out = [f"🩺 Health coverage — {report['routable_providers']} routable providers "
           f"of {report['active_providers']} active · {report['health_providers']} "
           f"health entries · {report['today']}"]
    gaps = report['gaps']
    if gaps:
        out.append('')
        out.append(f'🕳️  ACTIVE BUT UNPROBED ({len(gaps)}) — routable lanes with no '
                   f'health entry, so the resolve-time gate cannot fire:')
        for gap in gaps:
            out.append(f"    {gap['provider']}  {gap['lanes']} lane(s)  "
                       f"[{gap['kind']}] {gap['detail']}")
    else:
        out.append('')
        out.append('✅ every routable provider has a health entry')
    if report['policy_excluded']:
        out.append('')
        out.append(f"⚪ not routable, so not a gap ({len(report['policy_excluded'])}):")
        for prov, reason in report['policy_excluded'].items():
            out.append(f'    {prov}  {reason}')
    if report.get('inactive'):
        # Named, never silent: a provider that dropped out of the active set is
        # exactly the shape that hid neuralwatt (lanes disabled by hand, health
        # row absent) — the reason has to be on the page.
        out.append('')
        out.append(f"⚪ no active lane, so not a gap ({len(report['inactive'])}):")
        for prov, reason in report['inactive'].items():
            out.append(f'    {prov}  {reason}')
    if report['stale_health_entries']:
        out.append('')
        out.append(f"🧹 health entries with no active lane ({len(report['stale_health_entries'])}): "
                   + ', '.join(report['stale_health_entries']))
    return out


def check(data_dir: str | None = None, health_state: str | None = None,
          quota_state: str | None = None, today: str | None = None) -> dict:
    """Run the check against real files. Returns the report (status pass/fail/skip)."""
    data_dir = data_dir or DATA_DIR
    health_state = health_state or os.path.join(MR, 'health-state.json')
    quota_state = quota_state or os.path.join(MR, 'quota-state.json')
    today = today or date.today().isoformat()
    models_path = os.path.join(data_dir, 'models.jsonl')
    if not os.path.exists(models_path):
        return {'status': SKIP, 'reason': f'no models table at {models_path}',
                'gaps': [], 'routable_providers': 0, 'active_providers': 0,
                'health_providers': 0, 'covered': [], 'policy_excluded': {},
                'inactive': {}, 'stale_health_entries': [], 'today': today}
    health_doc = load_json(health_state)
    if not isinstance(health_doc, dict):
        return {'status': SKIP,
                'reason': f'no readable health-state at {health_state} — the gate '
                          f'input is missing, so coverage cannot be judged',
                'gaps': [], 'routable_providers': 0, 'active_providers': 0,
                'health_providers': 0, 'covered': [], 'policy_excluded': {},
                'inactive': {}, 'stale_health_entries': [], 'today': today}
    return coverage_report(
        load_jsonl(models_path),
        load_jsonl(os.path.join(data_dir, 'providers.jsonl')),
        load_jsonl(os.path.join(data_dir, 'probe_providers.jsonl')),
        health_doc, load_json(quota_state, {}) or {}, today)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Name every routable provider with no health-state entry '
                    '(the silent un-gate). Exit 0 covered, 1 gap, 2 cannot look.')
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--health-state', default=None)
    ap.add_argument('--quota-state', default=None)
    ap.add_argument('--today', default=None, help='YYYY-MM-DD (test seam)')
    ap.add_argument('--json', action='store_true', help='machine-readable report')
    args = ap.parse_args(argv)
    report = check(args.data_dir, args.health_state, args.quota_state, args.today)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    elif report['status'] == SKIP:
        print(f'⚠️  health coverage check could not look: {report["reason"]}')
    else:
        print('\n'.join(render(report)))
    if report['status'] == SKIP:
        return 2
    return 0 if report['status'] == PASS else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fleet cooldown policy — matches supervisor skill + Bane directives.

Cooldown matrix (Bane 2026-08-07 — THREE speeds):
  - 900s  (15m)  — PRIORITY. If a project is at 900s, LEAVE IT THERE.
                   Nothing in this script lowers or raises 900.
  - 7200s (2h)   — DEFAULT baseline for the fleet.
  - 43200s (12h) — COMPLETED (no real work; NEVER-DONE perpetual tasks
                   are fine at this tier).

Correction rules (Bane 2026-08-07):
  1. Any project BELOW 900s (e.g. 600) → RAISED back to 900s.
  2. Project at 900s → untouched, always.
  3. Project above 7200s WITH real work (pending board items or open
     stand-in gaps) → moved back to 7200s (2h) — not 900s.
  4. Project at 7200s with no work at all → promoted to 43200s (12h,
     completed tier). Promotions are the only other allowed increase.

Usage: python3 fleet-cooldown-policy.py [--apply]
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

API = 'http://127.0.0.1:9090'
TARGET_ACTIVE = 3600       # FAST — Bane-designated fast projects (1h; was 15m/900)
TARGET_IDLE = 21600        # DEFAULT — fleet baseline (6h; was 2h/7200 — Bane 08-15: "default 4 or 6 hours")
TARGET_COMPLETED = 43200  # COMPLETED — no work, verified done (12h)
TARGET_CI = 1800          # CI failing + CI tasks on board

# PRIORITY tier (900s) is set manually (API PUT) — this script never
# touches projects already at 900. It only enforces the floor (below-900
# → 900), the 2h default, and the completed tier.

# ── Elevated operator pins (SCHED-GAP-012, 2026-08-10) ────────────────
# Projects whose fleet.toml pin deliberately sits ABOVE the 7200 default.
# The policy must NEVER write below the canonical pin: the REDUCE rule and
# the fleet.toml regen both clobbered these (h3: 34 consecutive clobbers,
# h3 board #255→#287, last 2026-08-10 17:49Z; warpfs: restored 06:33 →
# clobbered by ~08:00 same day). Hard-skip, same semantics as the 900 tier.
ELEVATED_PINS = {
    'h3': 43200,      # Bane 2026-08-27: h3 family ticks too fast (5 rows × 6h = 11 ticks/24h) — 12h anti-flood pin
    'h3-sdk-go-foreman': 43200,
    'h3-sdk-python-foreman': 43200,
    'h3-sdk-typescript-foreman': 43200,
    'h3-shim-foreman': 43200,
    'warpfs': 21600,  # Bane 2026-08-19: ALL projects to 6h window for now
}

# OPERATOR_7200 — Bane-designated 2h FAST projects (killer projects under active
# development). The RAISE rule must never lift these to the 6h default, and the
# fleet.toml regen always writes the canonical 7200 (Bane 2026-08-23).
#
# ── Adaptive-cooldown arming (SCHED-GAP-1; goal Bane 2026-09-04, shipped
# 2026-09-09) ──
# The speed-control money lever: foreman-lane projects arm
# adaptive_cooldown — the scheduler doubles cooldown per consecutive
# no-progress tick up to the ceiling, and any committed tick or new board
# row resets it to the floor (= the cooldown pin). The ceiling MUST be
# emitted explicitly: the loader defaults an absent cooldown_ceiling_s to
# 604800s (7 days), not 8x. Arming lives HERE because (a) the loader
# re-pins adaptive_cooldown=false for projects without the key at every
# restart, and (b) hand-edits to fleet.toml are clobbered by the next
# --apply. Sync/qa/pm/dogfood lanes stay OFF until the upstream-quiet
# signal ships — arming them now would only mask upstream outages.
ADAPTIVE_LANES = {"coding-hermes"}
ADAPTIVE_CEILING_MULTIPLIER = 8
OPERATOR_7200 = {
    'hermes-dagger': 7200,   # killer project — active development
    'hermes-canopy': 7200,   # killer project — active development
}

LEDGER_PATH = os.path.expanduser('~/.hermes/stand-in/ledger.json')

def open_ledger_gaps(name):
    """Count stand-in ledger items still open for a project (suffix-tolerant).

    Scheduler names carry a '-foreman' suffix while ledger uses the bare repo
    name — match both. A project with no ledger entries at all counts as
    having no open gaps (nothing found = nothing pending).
    """
    try:
        with open(LEDGER_PATH) as f:
            items = json.load(f).get('items', [])
        cands = {name, name[:-8] if name.endswith('-foreman') else name,
                 name + '-foreman'}
        return sum(1 for it in items
                   if it.get('project') in cands and it.get('status') != 'verified')
    except Exception:
        return 0

# Board source: tasks.md (legacy) or board/tasks.parquet (migrated)
def parse_pending_from_md(md_path):
    """Count real pending tasks in a tasks.md board (section-aware).

    NOTE (2026-09-03): the old shared parser import
    (migrate-board-to-duckdb.py) is RETIRED — it calls _sys.exit(3) at
    import time, which SystemExit bypasses the except Exception guard and
    killed the whole fleet policy run mid-loop. Checkbox fallback below is
    the only md parser.
    """
    import re
    n = 0
    try:
        with open(md_path) as f:
            c = f.read()
        # Count open checkbox headers/list items, minus NEVER-DONE rows.
        # The retired shared parser silently DROPPED format-drifted
        # sections (Kobayashi-Maru "## [ ] KB-GAP-003 — title" blocks
        # parsed as 1 of 3 tasks → 0 pending → wrongly pinned at 43200
        # with 2 real gaps open) — checkbox counting avoids that.
        boxes = len(re.findall(r'^##+ \[ \]|^- \[ \]', c, re.M))
        never = (len(re.findall(r'^##+ \[ \].*NEVER-DONE', c, re.M))
                 + len(re.findall(r'^- \[ \].*NEVER-DONE', c, re.M)))
        n = max(0, boxes - never)
    except Exception:
        n = 0
    return n

def parse_pending_from_jsonl(jsonl_path):
    """Count real pending tasks from the canonical tasks.jsonl store."""
    n = 0
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get('status', '')).lower() in (
                        'pending', 'in_progress', 'blocked', 'open', 'todo'):
                    n += 1
        return n
    except Exception:
        return None

def board_pending(workdir):
    """Return real-pending count for a project workdir, or None if unreadable.

    2026-09-03: tasks.jsonl is the canonical store (board.db/parquet caches
    retired fleet-wide, JSONL-only doctrine). Legacy tasks.md mirror is the
    only fallback.
    """
    cd = os.path.join(workdir, '.coding-hermes')
    if not os.path.isdir(cd):
        return None
    jl = os.path.join(cd, 'board', 'tasks.jsonl')
    if os.path.exists(jl):
        n = parse_pending_from_jsonl(jl)
        if n is not None:
            return n
    md = os.path.join(cd, 'tasks.md')
    if os.path.exists(md):
        n = parse_pending_from_md(md)
        if n is not None:
            return n
    return None

def api_get(path):
    with urllib.request.urlopen(API + path, timeout=10) as r:
        return json.loads(r.read())

def api_put(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'}, method='PUT')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def main():
    apply = '--apply' in sys.argv
    fleet_pins = read_fleet_pins()
    projects = api_get('/api/v1/projects').get('projects', [])

    print(f"mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"{'PROJECT':32s} {'PENDING':8s} {'COOLDOWN':10s} {'TARGET':8s} {'ACTION'}")
    actions = []
    for p in sorted(projects, key=lambda x: x.get('name', x.get('name', ''))):
        name = p.get('name', p.get('name', '?'))
        if not p.get('enabled', p.get('enabled')):
            continue
        workdir = p.get('workdir', p.get('workdir', ''))
        if workdir.startswith('local:'):
            workdir = workdir[6:]
        cooldown = p.get('cooldown_s', p.get('cooldown_s', 0))
        pin = fleet_pins.get(name)
        # HARD GUARD: a fleet.toml pin of 900 is operator intent (Bane hot
        # tier) — skip ALL evaluation for it. No rule, no wake-revert, no
        # race can move it. (Proven 2026-08-09: hermes-canopy 900→7200 during
        # an apply — evaluation raced a concurrent board write.)
        if pin == TARGET_ACTIVE:
            print(f"{name:32s} {'-':8s} {cooldown:10d} {cooldown:8d} ok (operator 900 pin — hard-skipped)")
            continue
        # ELEVATED-PIN GUARD (SCHED-GAP-012): a fleet.toml pin above the
        # 7200 default is operator intent (h3=21600 anti-flood, warpfs=43200
        # completed). Skip ALL evaluation — no REDUCE, no wake-revert, no
        # promotion race. The canonical pin is written to fleet.toml by
        # write_fleet_pins below, so restarts re-pin to it.
        elevated = ELEVATED_PINS.get(name)
        if elevated is None and pin is not None and pin not in (TARGET_ACTIVE, TARGET_IDLE, TARGET_COMPLETED, 7200):
            # Any fleet.toml pin outside the canonical policy set (900/21600/
            # 43200/7200) is operator intent (e.g. weekly 604800 cadence for
            # muster/temple-runner/release-engineer). Policy never writes those
            # values, so a non-canonical pin must be honored — never REDUCE/
            # PROMOTE/RAISE against it. (2026-09-04 supervisor: REDUCE false-
            # fired on 604800-pinned rows.)
            elevated = pin
        if elevated is not None:
            print(f"{name:32s} {'-':8s} {cooldown:10d} {elevated:8d} ok (operator elevated pin {elevated} — hard-skipped)")
            continue
        pending = board_pending(workdir)

        if pending is None:
            print(f"{name:32s} {'?':8s} {cooldown:10d} {'—':8s} board-unreadable (skip)")
            continue

        # Cooldown correction rules (Bane 2026-08-07):
        # 1. below 900 → RAISE to 900 (minimum floor)
        # 2. at 900 → untouched (priority tier)
        # 3. above 7200 with real work → REDUCE to 7200 (2h default)
        # 4. 7200 (or less) with no work → PROMOTE to 43200 (completed)
        gaps = open_ledger_gaps(name)
        work_exists = pending > 0 or gaps > 0
        target = cooldown  # default: no change

        if cooldown < TARGET_ACTIVE:
            target = TARGET_ACTIVE
            action = f"RAISE {cooldown}→{TARGET_ACTIVE} (below minimum floor)"
            if apply:
                api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                action += " ✓"
            actions.append((name, cooldown, target, pending))
        elif cooldown == TARGET_ACTIVE:
            # 3600 = fast tier. Two origins: (a) operator pin in fleet.toml
            # → untouched; (b) stand-in WAKE (PUT 3600 on a project whose pin
            # says otherwise — standin-pick.py "wake the foreman") → the
            # wake must be TEMPORARY: revert to the default once no work
            # remains, otherwise projects silently run hot forever.
            pin = fleet_pins.get(name)
            if pin == TARGET_ACTIVE:
                action = "ok (operator fast tier — untouched)"
            elif pin == TARGET_IDLE:
                # Operator pin is 21600 (6h default tier) — the wake must NOT
                # override it (hermes-canopy, INFRA-001 tick 286). Revert
                # regardless of pending work — operator pin wins.
                target = TARGET_IDLE
                action = f"REVERT wake 3600→21600 (operator pin=21600; {pending} pending)"
                if apply:
                    api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                    action += " ✓"
                actions.append((name, cooldown, target, pending))
            elif work_exists:
                action = "ok (stand-in wake active — work exists)"
            else:
                target = TARGET_IDLE
                action = f"REVERT wake 3600→21600 (no work; pin={pin})"
                if apply:
                    api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                    action += " ✓"
                actions.append((name, cooldown, target, pending))
        elif work_exists and cooldown > TARGET_IDLE:
            target = TARGET_IDLE
            action = f"REDUCE {cooldown}→{TARGET_IDLE} (work exists: {pending} pending, {gaps} gaps)"
            if apply:
                api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                action += " ✓"
            actions.append((name, cooldown, target, pending))
        elif work_exists and cooldown < TARGET_IDLE:
            # Bane 08-15: default = 6h. Projects running faster than the
            # default (e.g. legacy 7200/2h) get raised to the default when
            # they have work — the fast tier is 3600 and operator-pinned only.
            # Operator 7200 pins (OPERATOR_7200) are admin intent and are
            # NEVER raised (Bane 2026-08-23: hermes-dagger + hermes-canopy).
            if name in OPERATOR_7200 or fleet_pins.get(name) == 7200:
                action = "ok (operator 7200/2h pin — untouched)"
            else:
                target = TARGET_IDLE
                action = f"RAISE {cooldown}→{TARGET_IDLE} (work exists; default is now 6h)"
                if apply:
                    api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                    action += " ✓"
                actions.append((name, cooldown, target, pending))
        elif not work_exists and cooldown < TARGET_COMPLETED and fleet_pins.get(name) != TARGET_IDLE and name not in OPERATOR_7200 and fleet_pins.get(name) != 7200:
            # Rule 4: promote idle 7200s to 43200 — BUT only when the 2h tier
            # was policy-set, not operator-set. An explicit fleet.toml pin of
            # 7200 is admin intent ("keep this at 2h") and must not be
            # promoted away (Bane 2026-08-07: bunker/chimera-v2/duckbrain/
            # h3-sdk-* are operator 2h projects; Bane 2026-08-23:
            # hermes-dagger + hermes-canopy in OPERATOR_7200). Policy-promoted
            # projects get their pin regenerated to 43200, so pin==7200
            # uniquely marks operator intent.
            target = TARGET_COMPLETED
            action = f"PROMOTE {cooldown}→43200 (completed: 0 pending, 0 open gaps)"
            if apply:
                api_put(f"/api/v1/projects/{name}", {"cooldown_s": target})
                action += " ✓"
            actions.append((name, cooldown, target, pending))
        else:
            action = "ok"

        print(f"{name:32s} {pending:8d} {cooldown:10d} {target:8d} {action}")

    print(f"\n{len(actions)} projects need cooldown reduction" +
          (" (APPLIED)" if apply else " — run with --apply"))

    if apply:
        # Re-fetch projects AFTER the PUTs so fleet.toml pins reflect the
        # corrected state, not the pre-PUT snapshot. (Proven 2026-08-07:
        # pins for h3/muster/uhlp/dexdat-memory were written stale and
        # would have reverted the reductions on daemon restart.)
        projects = api_get('/api/v1/projects').get('projects', [])
        # Namespace config must survive the regen too (Bane 2026-08-27:
        # default_prompt / model_chain / max_concurrent are data in the DB).
        namespaces = api_get('/api/v1/namespaces').get('namespaces', [])
        # Regenerate fleet.toml pins from the corrected state so daemon
        # restarts re-pin to the policy decision, not a stale snapshot.
        # (Daemon must run with -config pointing at this file.)
        n = write_fleet_pins(projects, namespaces)
        print(f"fleet.toml: regenerated {n} project pins + {len(namespaces)} namespaces (durable across restarts)")


def read_fleet_pins(path=None):
    """Read {name: cooldown_s} from fleet.toml (operator-set pins)."""
    import re as _re
    path = path or os.path.expanduser('~/.hermes/fleet.toml')
    pins = {}
    try:
        txt = open(path).read()
    except OSError:
        return pins
    for block in _re.findall(r'\[\[projects\]\](.*?)(?=\[\[|$)', txt, _re.S):
        n = _re.search(r'name\s*=\s*"([^"]+)"', block)
        c = _re.search(r'cooldown_s\s*=\s*(\d+)', block)
        if n:
            pins[n.group(1)] = int(c.group(1)) if c else None
    return pins

def write_fleet_pins(projects, namespaces=None):
    """Write [[projects]] pins for all enabled projects from API state.

    When namespaces is provided (list of namespace dicts from
    /api/v1/namespaces), [[namespaces]] blocks are emitted first so the
    namespace-level config (default_prompt, model_chain, max_concurrent —
    Bane 2026-08-27) survives policy regens. The regen must never drop
    namespace config the operator set in the DB.
    """
    import urllib.parse
    enabled = [p for p in projects if p.get('enabled', p.get('enabled'))]
    out = [
        "# Fleet configuration — cooldown overrides",
        "# These entries ensure cooldowns survive scheduler restarts.",
        "# Auto-generated by fleet-cooldown-policy.py --apply — do not edit by hand.",
        "# Policy: 3600s fast (1h) / 21600s default (6h) / 43200s idle (12h).",
        "",
        "# ── Scheduler (root config) ────────────────────────────────────────",
        "# DeepSeek peak-pricing windows (UTC): cooldown ×2 inside 01:00-04:00",
        "# and 06:00-10:00 (2× price hours). Merged in 49d4478, activated 08-10.",
        "[scheduler]",
        "blackout_windows = [",
        '  { start = "01:00", end = "04:00", multiplier = 2.0 },',
        '  { start = "06:00", end = "10:00", multiplier = 2.0 },',
        "]",
        "",
    ]
    if namespaces:
        out.append("# ── Namespaces ───────────────────────────────────────────────")
        out.append("# Namespace-level config (prompts, chains, caps) is data in the")
        out.append("# scheduler DB; the regen mirrors it so restarts re-pin it.")
        for ns in sorted(namespaces, key=lambda x: x.get('id', '')):
            out.append("[[namespaces]]")
            out.append(f'id = "{ns.get("id", "")}"')
            out.append(f'weight = {ns.get("weight", 10)}')
            out.append(f'reserved = {ns.get("reserved", 1)}')
            out.append(f'hard_cap = {ns.get("hard_cap", 100)}')
            out.append(f'max_concurrent = {ns.get("max_concurrent", 0)}')
            out.append(f'enabled = {"true" if ns.get("enabled", True) else "false"}')
            desc = ns.get("description") or ""
            if desc and '"' not in desc:
                out.append(f'description = "{desc}"')
            dp = ns.get("default_prompt") or ""
            if dp and "'''" not in dp:
                out.append("default_prompt = '''" + dp + "'''")
            mc = ns.get("model_chain") or ""
            if mc and '"' in mc:
                out.append(f'model_chain = {mc}')
            out.append("")
    for p in sorted(enabled, key=lambda x: x.get('name', x.get('name', ''))):
        pname = p.get('name', p.get('Name', '?'))
        out.append("[[projects]]")
        out.append(f'name = "{pname}"')
        out.append(f'repo_url = "{p.get("repo_url", p.get("RepoURL", "")) or "local:" + p.get("workdir", p.get("Workdir", ""))}"')
        out.append(f'workdir = "{p.get("workdir", p.get("Workdir", ""))}"')
        out.append(f'weight = {p.get("weight", p.get("Weight", 10))}')
        out.append(f'priority = {p.get("priority", p.get("Priority", 5))}')
        # ELEVATED-PIN OVERRIDE (SCHED-GAP-012): write the canonical pin for
        # whitelisted projects even if the live API was already clobbered —
        # the regen must never fossilize a below-pin value into fleet.toml.
        cooldown = ELEVATED_PINS.get(pname, OPERATOR_7200.get(pname, p.get("cooldown_s", p.get("CooldownS", 7200))))
        out.append(f'cooldown_s = {cooldown}')
        # DYNAMIC-ONLY (Bane 2026-08-28): NEVER emit a model/provider default.
        # The task-router resolves the model at spawn time; a present static
        # value SHADOWS the router chain tier and pins every spawn to one
        # hardcoded lane. Emit the pin ONLY when the project actually has one.
        m = p.get("model", p.get("Model", "")) or ""
        prov = p.get("provider", p.get("Provider", "")) or ""
        if m:
            out.append(f'model = "{m}"')
        if prov:
            out.append(f'provider = "{prov}"')
        ns = p.get('namespace_id', p.get('NamespaceID'))
        if ns:
            out.append(f'namespace_id = "{ns}"')
        # SCHED-GAP-1 arming: foreman lane only, explicit 8x ceiling.
        if ns in ADAPTIVE_LANES:
            out.append('adaptive_cooldown = true')
            out.append(f'cooldown_ceiling_s = {cooldown * ADAPTIVE_CEILING_MULTIPLIER}')
        if p.get('deliver', p.get('Deliver')):
            out.append(f'deliver = "{p.get("deliver", p.get("Deliver", ""))}"')
        out.append(f'enabled = {"true" if p.get("enabled", p.get("Enabled")) else "false"}')
        out.append("")
    path = os.path.expanduser('~/.hermes/fleet.toml')
    with open(path, 'w') as f:
        f.write('\n'.join(out))
    return len(enabled)

if __name__ == '__main__':
    main()

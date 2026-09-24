#!/usr/bin/env python3
"""router_spawn.py — THE TASK ROUTER (runtime lookup for the fleet).

Resolves a project (or ad-hoc profile) to the chain of (provider, model) pairs the
scheduler/foreman may use, cross-checked against live gate state:

  quota-state.json   — provider policy gates (GATED = blocked, reason)
                       + optional "quota_exhausted" plan-window gates (TR-060):
                         {"<provider>": {"status": "gated", "reason": <str>,
                                         "reset_at": <ISO ts>,
                                         "detected_at": <ISO ts>}}
                       + optional "diversity" knobs (TR-007):
                         {"max_consecutive_per_provider": N|null,
                          "max_total_per_provider":       N|null,
                          "model_concurrency_limit":      N|null}
                       + optional per-model limits (TR-007):
                         "models": {"<provider>/<model>": {"concurrency_limit": N}}
  health-state.json  — hourly provider/model ping results (DOWN/SLOW = skip)
  circuit-state.json — circuit breakers (open_until in future = skip)

Diversity (TR-007, Bane design): two knobs applied as PRUNING on the price-ordered
eligible chain — walk the survivors, drop violators with a reported reason,
preserve price order among survivors. NEVER a provider-wide pre-filter. A
null/absent knob is unbounded (output identical to pre-TR-007).

Concurrency is per-MODEL: a model at its concurrency limit is skipped individually
(like a circuit exclusion); the provider's other models stay eligible — a busy
model NEVER removes the whole provider. In-flight counts derive from the spawn
ledger (~/.hermes/model-router/ledger.jsonl, wired via scripts/router_ledger.py):
a trace whose LAST row is outcome='started' is in flight; 'started' rows older
than 30 minutes are stale (crash without `end`) and do not count.

Chain = eligible models ORDER BY (plan_tier, normalized_price * token_factor).
PAYG (deepseek) is a legitimate fallback hop — it appears where price ranks it;
the gate/health/breaker/busy filters decide admission, not the ordering.

Usage:
  router_spawn.py <project> [--format json|text] [--no-health]
  router_spawn.py --profile 'reasoning=5 debug=3 vision=-2' [--format json]
  router_spawn.py --list-profiles
  router_spawn.py --explain <project>     # show WHY each pair is excluded

Output (json): {project, profile, resolved_at, head, chain[], exclusions[],
gate_reasons[], gate, settings{max_consecutive_per_provider,
max_total_per_provider, model_concurrency_limit, overrides},
quota_gates{source, gated[], expired[]}}
TR-046: {data_home{registry, data_dir, state_dir, source, fallback,
bootstrap, note}} names exactly WHERE the data and gate state live, and
bootstrap=true + note flag SOLO/first-run SAMPLE state (committed data/tables
fallback and/or the first-run bootstrap quota-state with every provider OPEN)
so the chain is never mistaken for discovered policy.
Exit 0 always (fail-open: on any error prints {"error": ...} and exits 0) — the
scheduler must NEVER be blocked by the router.
--format json = PURE JSON on stdout, every path (TR-046 dogfood): diagnostics
go to stderr; the no-input usage line and --list-profiles also emit JSON.
"""
import json, os, sys, argparse, datetime, contextlib

# Chain truncation cap (default). 2026-09-10 RCA: a cap below the eligible lane
# count silently drops the price-sorted TAIL from every resolve (deepseek-foreman
# fell past position 34/74 → spurious degraded_fallback). 2026-09-12 TR-039:
# vendor-prefixed lanes (commandcode/aws-bedrock/fireworks-ai) gained tier rows,
# eligible P1_CODING lanes went ~94 → ~260, so the cap moved 96 → 320 with
# headroom. 2026-09-17 TR-043: the alias/case-folded lane-id lookup (see
# _alias_tiers) lets alias-mapped and case-variant lanes tier and enter chains,
# pushing eligible lanes to 323 (measured with limit=10**6) — the cap moved
# 320 → 400 so the tail stays complete.
# tests/test_regression.py::test_chain_default_limit_covers_registry
# asserts eligible < this value — raise it whenever the registry outgrows it.
# 2026-09-19 PAYG-LAST (Bane): normalized=99.0 ordering sentinel puts the PAYG
# lanes after EVERY plan lane, so the cap must clear the whole price-sorted
# eligible list or the ultimate fallback silently vanishes (TR-039 class).
# Live: eligible grew past 512 with the sentinel -> 400 → 1024 (deepseek
# positions measured at ~513+).
DEFAULT_CHAIN_LIMIT = 1024


# TR-033 / TR-055: --quiet / ROUTER_SPAWN_QUIET=1 suppresses stderr telemetry.
# TR-055 (2026-09-17 re-measure): the DEFAULT FLIPPED to quiet.  Per-lane
# ROUTER-MISS lines flooded every ad-hoc resolve (1466 stderr lines, 1166 of
# them tier=None for `--profile-req 'reasoning=5 debug=3 min_context=100000'`)
# and drowned any real warning.  The audit trail is now OPT-IN via
# ROUTER_MISS_VERBOSE=1 — quiet by default, loud on request.
#
# Truth table (env values '1'/'true'/'yes'):
#   ROUTER_SPAWN_QUIET truthy         -> True   (--quiet sets this)
#   else ROUTER_MISS_VERBOSE truthy   -> False  (telemetry restored)
#   else                              -> True   (the new default)
# False is returned ONLY when ROUTER_MISS_VERBOSE is set and ROUTER_SPAWN_QUIET
# is not, so an explicit --quiet still wins over the verbose opt-in.
def _truthy_env(val):
    """Exact-match truthiness for the router's env flags ('1'/'true'/'yes')."""
    return val in ('1', 'true', 'yes')


def _quiet():
    if _truthy_env(os.environ.get('ROUTER_SPAWN_QUIET', '')):
        return True
    return not _truthy_env(os.environ.get('ROUTER_MISS_VERBOSE', ''))


def _err(msg):
    """Write a diagnostic line to stderr unless quiet mode is enabled."""
    if not _quiet():
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass

# Text registry (Bane 2026-08-27): the live store is a gitignored JSON file in
# the task-router repo — NOT a binary duckdb. Env-overridable for hermetic
# tests. registry.json is produced by router_seed.py (version 3: {"version",
# "generated_at", "tables": {name: [row...]}}).
# Repo-relative defaults: the project is self-contained (clone → use).
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
# State dir (quota/health/circuit/ledger). Env-overridable so tests are hermetic
# and ops can point at a scratch dir; default identical to the historical path.
MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

# Metrics file location (TR-021).  TASK_ROUTER_HOME wins for container/hermetic
# isolation; otherwise repo-local data/metrics.jsonl (gitignored runtime state).
_METRICS_HOME = os.environ.get('TASK_ROUTER_HOME')
METRICS_FILE = os.path.join(_METRICS_HOME, 'metrics.jsonl') if _METRICS_HOME else os.path.join(_REPO, 'data', 'metrics.jsonl')

# A 'started' ledger row older than this is stale (crashed without `end`) and
# does not count as in-flight.
STALE_MS = 30 * 60 * 1000

# --- TR-049 components 4/5: outcomes-driven ordering --------------------------
# Resolve-time stats come from the rolling-averages table written by
# scripts/outcomes_averages.py (see docs/outcomes-schema.md). Path resolution
# mirrors the store's: env override > repo-relative runtime default.
AVERAGES = os.environ.get('ROUTING_AVERAGES_FILE',
                          os.path.join(_REPO, 'data', 'state', 'outcomes-averages.jsonl'))
DEFAULT_WINDOW_H = 24
#: Ordering used when --sort is not given.
#:
#: Deliberately the historical 'price' order (plan_tier, effective price), NOT
#: 'predicted_cost_per_task'. This script is SYMLINKED into the live fleet
#: (~/.hermes/scripts/router_spawn.py), so a different default silently re-ranks
#: every fleet resolution — and cost-per-task ranking is not safe as a DEFAULT
#: yet either: a lane that fails fast records cost 0.0 and would sort first,
#: while the Hermes backend reports no completion signal to filter on. Opt in
#: per call with --sort <key> (or set ROUTER_SPAWN_SORT / flip this constant
#: deliberately).
DEFAULT_SORT = os.environ.get('ROUTER_SPAWN_SORT') or 'price'


def row_is_retired(row, today=None):
    """Has this registry row passed its retirement date?

    `valid_to` on models.jsonl is the model's RETIREMENT date (20 rows carry
    one, all past-dated = genuinely retired). Eligibility used to test
    `valid_to is not None`, which retires a lane the moment a FUTURE date is
    stamped — the announced-decommission case would hide the lane weeks early.
    Date-compare instead: past = retired, future = still live.
    """
    vt = row.get('valid_to')
    if not vt:
        return False
    if today is None:
        today = datetime.date.today().isoformat()
    return str(vt)[:10] <= str(today)[:10]


RETIRE_WARN_DAYS = int(os.environ.get('ROUTER_RETIRE_WARN_DAYS', '14'))


def _today():
    """Testable clock seam (TR-069): frozen-clock tests monkeypatch this."""
    return datetime.date.today().isoformat()


def _days_until(date_str, today):
    """Days from today until date_str (negative = past). None = unparseable."""
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
        t = datetime.date.fromisoformat(str(today)[:10])
        return (d - t).days
    except Exception:
        return None


def lifecycle_state(row, today=None):
    """TR-069 wave 2: the four lifecycle states, derived from DATES only.

    coming_soon — available_from is a FUTURE date (announced, not yet routable)
    live        — no dates, or inside the [available_from, valid_to) window
    retiring    — live today but valid_to is within RETIRE_WARN_DAYS
    retired     — valid_to has passed (day-of counts as retired; same rule as
                  row_is_retired, which this shares semantics with)

    Unknown dates stay None/absent = live: dates decide, nothing invented.
    """
    if today is None:
        today = datetime.date.today().isoformat()
    today = str(today)[:10]
    vt = row.get('valid_to')
    if vt and str(vt)[:10] <= today:
        return 'retired'
    af = row.get('available_from')
    if af and str(af)[:10] > today:
        return 'coming_soon'
    if vt:
        left = _days_until(vt, today)
        if left is not None and left <= RETIRE_WARN_DAYS:
            return 'retiring'
    return 'live'


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


# ---------------------------------------------------------------- aliases ----
# TR-043: data/tables/model_aliases.jsonl maps serving-lane variants, vendor /
# snapshot / HF-mirrored ids to their canonical weights ({"model": variant,
# "inherits": base}).  The spawn path consults it when a lane's model id has no
# tier evidence of its own: the variant otherwise shows tier=None for every
# required category and is dropped BEFORE the gate stage — invisible in the
# chain AND in exclusions (the ROUTER-MISS flood).  Inheriting the base's tier
# lets an alias-mapped variant tier and enter the chain.
#
# Cache: module-level, keyed by the resolved file path, so a test/ops override
# of ROUTING_DATA_DIR (or a monkeypatched DATA_DIR) gets its own entry and can
# never read a stale map.  Read errors are NOT cached (a transient failure is
# retried on the next resolve); a missing file caches {} = no alias
# inheritance, exactly the pre-TR-043 behavior (fail-open, never raises).
_ALIAS_CACHE = {}


def _alias_map():
    """{variant_lower: base_lower} from data/tables/model_aliases.jsonl.

    Module-cached per path.  Fail-open: missing/unreadable/malformed file
    returns {} so the router keeps resolving (aliases are an enrichment, never
    a gate).  Keys/values are case-folded — registry model ids drift in case
    (e.g. `hf:Qwen/Qwen3.6-27B`) and a case mismatch must not silently disable
    an existing mapping.
    """
    path = os.path.join(DATA_DIR, 'model_aliases.jsonl')
    cached = _ALIAS_CACHE.get(path)
    if cached is not None:
        return cached
    if not os.path.exists(path):
        _ALIAS_CACHE[path] = {}
        return _ALIAS_CACHE[path]
    try:
        amap = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                var = row.get('model') or row.get('variant')
                base = row.get('inherits') or row.get('base')
                if var and base and str(var).lower() != str(base).lower():
                    amap[str(var).lower()] = str(base).lower()
    except Exception:  # noqa: BLE001 — fail-open, never block a resolve
        return {}
    _ALIAS_CACHE[path] = amap
    return amap


def _alias_chain(model):
    """[model, base, base-of-base, ...] — TRANSITIVE alias resolution.

    Registry history chains renames (deepseek-v4-flash-flex -> deepseek-v4-flash
    -> deepseek-flash), so a single hop would miss the tiered name.  Returns
    [model] for an unmapped/empty model.  Cycle-guarded (a malformed map can
    never spin) and case-folded for the lookups after the first element.
    """
    if not model:
        return []
    amap = _alias_map()
    chain = [model]
    seen = {str(model).lower()}
    cur = str(model).lower()
    while True:
        nxt = amap.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        chain.append(nxt)
        cur = nxt
    return chain


def _fold_tier_names(tiers):
    """{lower(model): {category: tier}} companion index for alias lookups."""
    folded = {}
    for name, cats in (tiers or {}).items():
        folded.setdefault(str(name).lower(), cats)
    return folded


def _alias_tiers(tiers, folded, model):
    """Tier dict for a lane's model id, alias-aware (TR-043).

    The lane's OWN tier evidence always wins (a variant benchmarked in its own
    right overrides the base); categories it has no row for are filled from its
    alias base, then that base's base.  A lane with no alias mapping gets
    exactly `tiers.get(model)` — byte-identical to the pre-TR-043 behavior.

    Lookups are case-folded because tier evidence is per MODEL and the registry
    matches models by `lower(model)` everywhere (router_seed.py); the committed
    tables carry both casings of the same weights (e.g. an openrouter lane
    `stepfun/step-3.5-flash` alongside the tier table's `stepfun/Step-3.5-Flash`),
    so an exact-match-only lookup silently blanks a lane that HAS evidence.
    """
    own = tiers.get(model)
    if own is None:
        own = folded.get(str(model).lower())
    chain = _alias_chain(model)
    if len(chain) < 2:
        return own or {}
    merged = dict(own or {})
    for alt in chain[1:]:
        base = tiers.get(alt) or folded.get(alt) or {}
        for cat, tier in base.items():
            if merged.get(cat) is None:
                merged[cat] = tier
    return merged


def _parse_utc(ts):
    """Best-effort ISO-8601 → aware UTC datetime; None on any failure."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


# ---------------------------------------------- TR-060 quota exhaustion ------
# A plan-limit 429 ('usage limit has been reached' / 'Weekly/Monthly Limit
# Exhausted ... reset at <ts>') is NOT an api_down blip: the PLAN WINDOW lasts
# hours to days while the api_down circuit cools in 30 minutes. Before TR-060
# the lane was therefore re-picked on the next tick, re-failed, and every
# affected fleet session fell back to the PAYG default (~200 sessions/day,
# live audit 2026-09-17).
#
# quota-state.json carries policy gates ('providers.<p>.status' != 'open') with
# no expiry; TR-060 adds a PLAN-WINDOW gate that expires by itself:
#
#   "quota_exhausted": {
#     "<provider>": {"status": "gated", "reason": "<why>",
#                    "reset_at": "<ISO 8601>", "detected_at": "<ISO 8601>"}}
#
# Semantics (documented, tested in tests/test_quota_gate.py):
#   GATED   — status not in ('open', 'cleared', 'expired') AND (reset_at is
#             missing/unparseable OR reset_at > now).
#   OPEN    — reset_at in the PAST (auto-clear: the plan window refilled, the
#             entry stays in the file for audit) or an explicit open/cleared
#             status. NOTHING has to be edited for a lane to come back.
#   A nested providers.<p>.quota_exhausted object is honored too (merged over
#   the top-level section), so a hand-written gate lands wherever the operator
#   naturally looks.
# Fail-open: absent/unreadable/malformed state never raises and never gates.
QUOTA_GATE_OPEN_STATUSES = frozenset(('open', 'cleared', 'expired'))
QUOTA_GATE_FIELDS = ('provider', 'status', 'reason', 'reset_at', 'detected_at')


def load_quota_gates(qdoc, now=None):
    """{provider: gate} for the plan-window gates in quota-state.json (TR-060).

    `now` is an aware datetime, an ISO string, or None (= wall clock). Each
    gate carries: provider, status, reason, reset_at, detected_at, active
    (this lane is excluded RIGHT NOW), expired (was gated, reset_at passed —
    auto-cleared) and the ready-made gate_reason string emitted in
    exclusions/gate_reasons:
        'quota exhausted: <reason> (resets <reset_at>)'  — future reset
        'quota exhausted: <reason> (no reset time)'      — no/unparseable reset
    Never raises: a non-dict quota-state document yields {}.
    """
    gates = {}
    if isinstance(qdoc, dict):
        section = qdoc.get('quota_exhausted')
        if isinstance(section, dict):
            for prov, ent in section.items():
                if isinstance(ent, dict):
                    gates[str(prov)] = dict(ent)
        provs = qdoc.get('providers')
        if isinstance(provs, dict):
            for prov, row in provs.items():
                nested = row.get('quota_exhausted') if isinstance(row, dict) else None
                if isinstance(nested, dict):
                    merged = dict(gates.get(str(prov)) or {})
                    merged.update(nested)
                    gates[str(prov)] = merged
    now_dt = now if isinstance(now, datetime.datetime) else _parse_utc(now)
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    out = {}
    for prov, ent in gates.items():
        status = str(ent.get('status') or 'gated').strip().lower()
        reset_at = ent.get('reset_at')
        reset_dt = _parse_utc(reset_at) if reset_at else None
        declared_gated = status not in QUOTA_GATE_OPEN_STATUSES
        # A gated entry with NO reset time stays gated (the documented
        # fallback: until the operator clears it) — never silently open.
        active = bool(declared_gated and (reset_dt is None or reset_dt > now_dt))
        reason = ent.get('reason') or 'plan limit reached'
        out[prov] = {
            'provider': prov,
            'status': status,
            'reason': ent.get('reason') or '',
            'reset_at': reset_at,
            'detected_at': ent.get('detected_at'),
            'active': active,
            'expired': bool(declared_gated and not active),
            'gate_reason': ('quota exhausted: ' + str(reason)
                            + (f' (resets {reset_at})' if reset_at
                               else ' (no reset time)')),
        }
    return out


def quota_gate_summary(gates):
    """{source-less summary} of a load_quota_gates() map for the resolve output.

    Two sorted lists: `gated` = actively excluded providers (the fact an
    operator must be able to see — an invisible gate is a silent zero-chain),
    `expired` = recorded-but-auto-cleared entries (audit trail + proof the
    mechanism is reading the file). Deterministic order.
    """
    gated, expired = [], []
    for prov in sorted(gates or {}):
        g = gates[prov]
        row = {k: g.get(k) for k in QUOTA_GATE_FIELDS}
        row['gate_reason'] = g.get('gate_reason')
        if g.get('active'):
            gated.append(row)
        elif g.get('expired'):
            expired.append(row)
    return {'gated': gated, 'expired': expired}


def ledger_has_traces(state_dir):
    """True when the spawn ledger exists AND holds at least one non-empty line.

    Path resolution: LEDGER_FILE env (the router_ledger.py shared contract)
    wins; otherwise <state_dir>/ledger.jsonl.
    Visibility only (TR-026): a file that exists but has zero rows has never
    received a start/end call — the spawn ledger is not wired. Never raises
    (fail-open) — same policy as ledger_in_flight.
    """
    path = os.environ.get('LEDGER_FILE') or os.path.join(state_dir, 'ledger.jsonl')
    try:
        with open(path) as f:
            for line in f:
                if line.strip():
                    return True
        return False
    except Exception:
        return False


def ledger_in_flight(state_dir):
    """{(provider, model): in_flight_count} derived from the spawn ledger.

    Traces are reconstructed by trace_id across ALL rows (terminal rows carry
    no provider/model): a trace's LAST row decides its outcome, while its pair
    comes from whichever row carries one ('start'). A trace whose final outcome
    is 'started' is in flight; 'started' rows older than STALE_MS are stale
    (crash without `end`) and do not count. Any read/parse error degrades to {}
    — fail-open, the router must never raise on state reads.
    """
    try:
        last = {}
        # LEDGER_FILE env (router_ledger.py shared contract) wins over state_dir
        _lpath = os.environ.get('LEDGER_FILE') or os.path.join(state_dir, 'ledger.jsonl')
        with open(_lpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                tid = row.get('trace_id')
                if not tid:
                    continue
                rec = last.setdefault(tid, [None, None, None, None])  # prov, mdl, outcome, ts
                if row.get('provider'):
                    rec[0] = row['provider']
                if row.get('model'):
                    rec[1] = row['model']
                if row.get('outcome') is not None:
                    rec[2] = row['outcome']
                if row.get('ts') is not None:
                    rec[3] = row['ts']
        now = datetime.datetime.now(datetime.timezone.utc)
        counts = {}
        for prov, mdl, outcome, ts in last.values():
            if outcome != 'started' or not prov or not mdl:
                continue
            dt = _parse_utc(ts)
            if dt is None or (now - dt).total_seconds() * 1000 > STALE_MS:
                continue
            counts[(prov, mdl)] = counts.get((prov, mdl), 0) + 1
        return counts
    except Exception:
        return {}


def _effective_caps(profiles, qdoc, pid):
    """Effective two-knob diversity caps + per-model concurrency default.

    Per-profile task_profiles columns beat the global 'diversity' defaults in
    quota-state.json; NULL/absent everywhere = unbounded (None). Pre-TR-007
    schemas degrade to the globals — fail-open.
    """
    diversity = qdoc.get('diversity') or {}
    if not isinstance(diversity, dict):
        diversity = {}
    g_cons = diversity.get('max_consecutive_per_provider')
    g_tot = diversity.get('max_total_per_provider')
    p_cons = p_tot = None
    if pid:
        row = profiles.get(pid)
        if row:
            p_cons = row.get('max_consecutive_per_provider')
            p_tot = row.get('max_total_per_provider')
    cons = p_cons if p_cons is not None else g_cons
    tot = p_tot if p_tot is not None else g_tot
    return {
        'max_consecutive_per_provider': cons,
        'max_total_per_provider': tot,
        'model_concurrency_limit': diversity.get('model_concurrency_limit'),
        'overrides': {
            'profile': p_cons is not None or p_tot is not None,
            'consecutive': p_cons is not None,
            'total': p_tot is not None,
        },
    }


def _model_limit(models_cfg, diversity, prov, model):
    """Effective per-model concurrency limit for a pair.

    Precedence: explicit quota-state 'models' entry ('<provider>/<model>' →
    'concurrency_limit') beats the global diversity.model_concurrency_limit.
    No entry anywhere → None = never busy (unbounded).
    """
    entry = models_cfg.get(f'{prov}/{model}')
    if isinstance(entry, dict):
        lim = entry.get('concurrency_limit')
        if lim is not None:
            return lim
    lim = diversity.get('model_concurrency_limit')
    return lim if lim is not None else None


def _prune_diversity(out_chain, exclusions, reasons, cons_cap, tot_cap):
    """Walk the price-ordered survivor chain; drop diversity violators.

    Per provider: consecutive_run counts hops IN A ROW (resets when the
    provider changes); total counts hops across the whole chain. Over-cap hops
    move to exclusions with an explicit reason ('consecutive cap N' /
    'chain cap N'); survivors keep their relative price order. Both caps unset
    → no-op (identical output to pre-TR-007).
    """
    if cons_cap is None and tot_cap is None:
        return
    survivors = []
    run_prov, run_len = None, 0
    totals = {}
    for ent in out_chain:
        prov = ent['provider']
        if prov != run_prov:
            run_prov, run_len = prov, 0
        run_len += 1
        totals[prov] = totals.get(prov, 0) + 1
        why = []
        if cons_cap is not None and run_len > cons_cap:
            why.append(f'consecutive cap {cons_cap}')
        if tot_cap is not None and totals[prov] > tot_cap:
            why.append(f'chain cap {tot_cap}')
        if why:
            exclusions.append({'hop': ent['hop'], 'provider': prov,
                               'model': ent['model'], 'why': why})
            reasons.append(f"hop {ent['hop']} {prov}/{ent['model']}: "
                           + '; '.join(why))
        else:
            survivors.append(ent)
    out_chain[:] = survivors


def _metrics_path():
    """Return metrics file path; env-overridable for tests."""
    env = os.environ.get('TASK_ROUTER_HOME')
    if env:
        return os.path.join(env, 'metrics.jsonl')
    return METRICS_FILE


def _routing_env_names():
    """Names of ROUTING_* / ROUTER_* / TASK_ROUTER_* env vars present (no values)."""
    return sorted(k for k in os.environ if k.startswith(('ROUTING_', 'ROUTER_', 'TASK_ROUTER_')))


def _append_metrics(rows, result):
    """Append one row per chain hop to the metrics JSONL file (TR-021).

    rows is the list of tuples as produced by _build_chain (each tuple is
    (hop, provider, model, price, data_class, model_row)).  result is the
    resolve output dict.  outcome per hop: 'resolved' if the hop survived
    gates, 'excluded' if it appears in result['exclusions'], 'error' if
    result has an 'error' key (one error row, chain_length=0, order=0).

    This function swallows ALL its own errors silently: stdout and exit code of
    router_spawn.py must remain identical to today's.
    """
    try:
        path = _metrics_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        project = result.get('project')
        profile = result.get('profile')
        gates = result.get('gates_loaded') or {}
        src = result.get('source')
        if isinstance(src, str):
            src = os.path.basename(src)
        snapshot = {
            'registry_source': src,
            'gates_loaded': gates,
            'chain_length': len(result.get('chain') or []),
            'routing_env_vars': _routing_env_names(),
        }
        error_obj = result.get('error')
        lines = []
        if error_obj:
            err_text = error_obj
            if isinstance(error_obj, dict):
                err_text = error_obj.get('error') or json.dumps(error_obj)
            lines.append({
                'ts': ts,
                'project': project,
                'profile': profile,
                'provider': None,
                'model': None,
                'order': 0,
                'price_usd_per_m': None,
                'outcome': 'error',
                'exclusion_reason': str(err_text),
                'config_snapshot': snapshot,
            })
        else:
            excluded_map = {}
            for ex in (result.get('exclusions') or []):
                excluded_map[ex.get('hop')] = ex
            chain_by_hop = {c.get('hop'): c for c in (result.get('chain') or [])}
            for ent in (rows or []):
                hop, prov, model, price, dc, mrow = ent
                ex = excluded_map.get(hop)
                if ex is not None and ex.get('provider') == prov and ex.get('model') == model:
                    outcome = 'excluded'
                    why = ex.get('why') or []
                    if isinstance(why, list):
                        reason = '; '.join('; '.join(w) if isinstance(w, list) else str(w) for w in why)
                    else:
                        reason = str(why)
                    if not reason:
                        reason = None
                else:
                    outcome = 'resolved'
                    reason = None
                # price per hop: prefer the public/effective price used by the chain output
                price_used = chain_by_hop.get(hop, {}).get('usd_1m')
                if price_used is None:
                    price_used = round(float(price), 4) if price is not None else None
                lines.append({
                    'ts': ts,
                    'project': project,
                    'profile': profile,
                    'provider': prov,
                    'model': model,
                    'order': hop,
                    'price_usd_per_m': price_used,
                    'outcome': outcome,
                    'exclusion_reason': reason,
                    'config_snapshot': snapshot,
                })
        with open(path, 'a') as f:
            for row in lines:
                f.write(json.dumps(row) + '\n')
    except Exception:
        pass


def _validate_adhoc(adhoc, tables):
    """Validate --profile-req requirements (TR-023). Returns (reqs, error).

    Honest input validation: a typo'd category or out-of-range level must be
    a visible error (code INVALID_REQUIREMENT, retryable false), never a
    silently-weakened requirement. Syntax (int level, 'cat=level' shape) is
    always enforced; category membership and the -5..+5 scale come from DATA
    (category_levels.jsonl / level_defs.jsonl). Missing data degrades to
    syntax-only validation — fail-open, never a traceback.

    TR-015: 'min_context' is a synthetic requirement category (int tokens)
    that is NOT part of the tier/category_levels scale; it is accepted here
    and handled specially by _build_chain.
    """
    known = {r.get('category') for r in (tables.get('category_levels') or [])
             if r.get('category')}
    known.add('min_context')
    lv = {r.get('level') for r in (tables.get('level_defs') or [])
          if r.get('level') is not None}
    lo, hi = (min(lv), max(lv)) if lv else (-5, 5)
    reqs = []
    for kv in adhoc:
        parts = str(kv).split()  # tolerate 'a=1 b=2' arriving as one arg
        if not parts:
            return None, {'error': f'invalid requirement {kv!r}: '
                           'expected category=level',
                           'code': 'INVALID_REQUIREMENT', 'retryable': False}
        for part in parts:
            cat, sep, lvl = part.partition('=')
            cat = cat.strip()
            if not sep:
                return None, {'error': f'invalid requirement {part!r}: '
                               'expected category=level',
                               'code': 'INVALID_REQUIREMENT', 'retryable': False}
            if not cat:
                return None, {'error': f'invalid requirement {part!r}: '
                               'empty category',
                               'code': 'INVALID_REQUIREMENT', 'retryable': False}
            if cat == 'min_context':
                try:
                    level = int(lvl)
                except ValueError:
                    return None, {'error': f'invalid min_context {lvl!r}: must be integer tokens',
                                   'code': 'INVALID_REQUIREMENT', 'retryable': False}
                reqs.append((cat, level))
                continue
            if known and cat not in known:
                return None, {'error': f'unknown category {cat!r} '
                               f'(known: {", ".join(sorted(known))})',
                               'code': 'INVALID_REQUIREMENT', 'retryable': False}
            try:
                level = int(lvl)
            except ValueError:
                return None, {'error': f'invalid level {lvl!r} for {cat}: '
                               f'must be an integer in {lo}..{hi}',
                               'code': 'INVALID_REQUIREMENT', 'retryable': False}
            if not (lo <= level <= hi):
                return None, {'error': f'level {level} out of range for {cat}: '
                               f'must be in {lo}..{hi}',
                               'code': 'INVALID_REQUIREMENT', 'retryable': False}
            reqs.append((cat, level))
    return reqs, None


def _stamp_payg_providers(tables):
    """Snapshot the PAYG provider set + stamp every lane row (Bane 2026-09-19).

    Data-driven from the providers table: `plan = 'PAYG'` names the pay-per-
    token billing class. _legacy_sort_key sends those lanes to the terminal
    chain bucket (after every plan lane). Called once per registry load; the
    set is stored under tables['_payg_providers'] (never exported/serialized
    back), and rows carry a boolean _payg so the sort path does no lookups.
    """
    try:
        payg = {p.get('id') for p in (tables.get('providers') or [])
                if isinstance(p, dict) and str(p.get('plan') or '').upper() == 'PAYG'}
        tables['_payg_providers'] = payg
        for m in tables.get('models') or []:
            if isinstance(m, dict):
                m['_payg'] = m.get('provider') in payg
    except Exception:  # noqa: BLE001 — stamping must never break a resolve
        tables['_payg_providers'] = set()


def _load_registry_with_meta():
    """(tables, source, fallback_used, warning) — registry.json, else data/tables.

    TR-025: resolve output must SAY where its data came from. registry.json
    missing/corrupt/empty → committed data/tables/*.jsonl (same keyed-record
    format, fresh-clone stdlib usability unchanged) with source='data/tables',
    fallback_used=True, and a warning naming the registry failure. Both
    unreadable → empty tables with a warning stating the gap — fail-open
    preserved, never fabricated data, never a silent pass.
    """
    try:
        with open(REGISTRY) as f:
            doc = json.load(f)
        tables = doc.get('tables') if isinstance(doc, dict) else None
        if isinstance(tables, dict) and tables:
            _stamp_payg_providers(tables)
            return tables, 'registry.json', False, None
        if not isinstance(doc, dict):
            err = 'registry.json present but not an object (corrupt)'
        else:
            err = ('registry.json present but empty tables key '
                   '(corrupt or unseeded)')
    except FileNotFoundError:
        err = 'registry.json missing'
    except Exception as e:  # noqa: BLE001 — fail-open: any read error is visible, never fatal
        err = f'registry.json unreadable: {type(e).__name__}: {e}'
    try:
        tables = {}
        for fn in sorted(os.listdir(DATA_DIR)):
            if fn.endswith('.jsonl'):
                name = fn[:-len('.jsonl')]
                rows = []
                with open(os.path.join(DATA_DIR, fn)) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
                tables[name] = rows
        if tables:
            _stamp_payg_providers(tables)
            return tables, 'data/tables', True, \
                f'{err} — using committed data/tables fallback'
        return {}, 'data/tables', True, \
            f'{err} AND data/tables unreadable/empty — empty registry (fail-open)'
    except Exception as e:  # noqa: BLE001 — fail-open, visible
        return {}, 'data/tables', True, \
            f'{err} AND data/tables unreadable: {type(e).__name__}: {e} ' \
            '— empty registry (fail-open)'


def _load_registry():
    """registry.json → {tables: {name: [row...]}}; any error → {} (fail-open).

    Bare-tables shim over _load_registry_with_meta() — kept for callers that
    only need the tables (router_maintain.py); resolve() uses the meta form so
    its output can report source + fallback_used.
    """
    return _load_registry_with_meta()[0]


def _resolve_profile_tag(profiles, ref):
    """TR-020: resolve a profile reference (tag or exact id) to an id.

    profiles is a dict keyed by profile id. Each row may contain 'tag' and
    'version'. A tag matches exactly one row (the tagged version). If the ref
    matches a tag, return that row's id; otherwise return the ref as an exact
    id (backward compatible with legacy ids like P0_FORE)."""
    if not ref:
        return ref
    # tag path: a tag takes precedence over an id collision so that retagging
    # an existing id changes resolution without renaming project rows.
    matches = [(r.get('id'), r.get('version') or 0)
               for r in profiles.values()
               if r.get('tag') == ref]
    if matches:
        matches.sort(key=lambda x: -x[1])
        return matches[0][0]
    # fall back to exact id
    if ref in profiles:
        return ref
    return ref


def _profile_ref_matches(profiles, ref):
    """TR-059: does `ref` name a profile — an exact id OR a tag?

    Projects and profiles live in DIFFERENT tables, so the project positional
    is a natural place for a caller to put a bare profile name
    (`router spawn P1_CODING`, GET /resolve?project=P1_CODING`). resolve()
    uses this to tell "typo'd project" from "right id, wrong flag".

    Tag matching mirrors _resolve_profile_tag (a tag takes precedence over an
    id collision), so `matches ⇒ _resolve_profile_tag returns a real profile`.
    """
    if not ref:
        return False
    if ref in profiles:
        return True
    return any(r.get('tag') == ref for r in profiles.values())


def _profile_near_miss(profiles, ref):
    """TR-059: the profile id `ref` approximately names, else None.

    CASE-INSENSITIVE exact match on id or tag only — never fuzzy/partial.
    A wrong "did you mean" hint is worse than no hint (the caller would edit
    the wrong token), so anything less than an exact-match-ignoring-case
    returns None and the caller adds no hint at all.
    """
    if not ref:
        return None
    low = str(ref).lower()
    for pid in sorted(profiles):
        row = profiles.get(pid) or {}
        if pid.lower() == low or str(row.get('tag') or '').lower() == low:
            return pid
    return None


def _averages_path():
    """Resolve-time stats path (env override wins, resolved per call)."""
    return os.environ.get('ROUTING_AVERAGES_FILE') or AVERAGES


def _canonical_complexity(value):
    """Stable string for a complexity reference: a profile id, a per-category
    level map ({category: level}), or None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps({str(k): v for k, v in sorted(value.items())},
                          sort_keys=True, separators=(',', ':'))
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _complexity_keys(profile_id, tables):
    """The complexity references a lane's stats row may be keyed by: the profile
    id, plus the canonical form of its DECLARED per-category levels (the same
    reference shape the outcome store documents). Never invented — a profile
    with no requirement rows contributes only its id."""
    keys = set()
    if profile_id:
        keys.add(str(profile_id))
        sig = {}
        for r in tables.get('task_profile_requirements') or []:
            if r.get('task_id') == profile_id:
                sig[str(r.get('category'))] = r.get('level')
        if sig:
            keys.add(_canonical_complexity(sig))
    return keys


def load_outcome_stats(backend=None, merge_backends=None, path=None):
    """(index, meta) — the resolve-time view of the rolling averages.

    index = {(provider, model): [row, ...]}
    meta  = {path, rows, source, error}

    Isolation is the DEFAULT whenever a `backend` is named (keep only that
    source_system's rows, `source='backend:<name>'`); without a backend the rows
    are MERGED across backends, sample-count weighted — the documented default.
    Pass merge_backends=True to force the merge even with a backend named.

    Fail-open: an absent or unreadable table yields an empty index plus the
    reason; a resolve NEVER fails because the stats are missing.
    """
    p = path or _averages_path()
    meta = {'path': p, 'rows': 0, 'source': 'merged', 'error': None}
    rows = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('provider') and row.get('model'):
                    rows.append(row)
    except OSError as exc:
        meta['error'] = f'unreadable averages: {exc}'
        return {}, meta
    if backend and not merge_backends:
        meta['source'] = f'backend:{backend}'
        rows = [r for r in rows if r.get('source_system') == backend]
        if not rows:
            meta['error'] = f'no samples for backend {backend!r}'
    elif rows:
        try:
            import router_outcomes  # optional stats dependency (stdlib-only)
            rows = router_outcomes.merge_average_rows(rows)
        except Exception as exc:  # noqa: BLE001 — fail-open, keep the resolve
            meta['error'] = f'merge failed: {exc}'
            return {}, meta
    index = {}
    for r in rows:
        index.setdefault((r.get('provider'), r.get('model')), []).append(r)
    meta['rows'] = len(rows)
    return index, meta


def lane_stats(index, provider, model, keys):
    """(row, match) for a lane: the stats row matching the task's complexity
    reference, else the unconditioned bucket, else a weighted merge of the
    lane's buckets. (None, None) when the store has no sample — never invented.
    """
    rows = index.get((provider, model)) or []
    if not rows:
        return None, None
    want_sigs = []
    for want in keys or ():
        want_sigs.append(_canonical_complexity(want))
        try:
            import router_outcomes
            sig = router_outcomes.complexity_sig(want)
            if sig:
                want_sigs.append(sig)
        except Exception:  # noqa: BLE001 — stats are optional; fail-open
            pass
    for want in want_sigs:
        for r in rows:
            if want and (r.get('complexity_sig') == want
                         or _canonical_complexity(r.get('required_categories')) == want
                         or _canonical_complexity(r.get('complexity')) == want):
                return r, 'complexity'
    for want in keys or ():
        for r in rows:
            if _canonical_complexity(r.get('complexity')) == want:
                return r, 'complexity'
    for r in rows:
        if r.get('complexity') is None:
            return r, 'unconditioned'
    if len(rows) > 1:
        try:
            import router_outcomes
            merged = router_outcomes.merge_average_rows(rows)
            if merged:
                return merged[0], 'merged'
        except Exception:  # noqa: BLE001
            pass
    return rows[0], 'fallback'


def lane_metric(m, ctx, metric):
    """(value, provenance) for one metric of one lane. (None, None) when the
    store has no sample for the lane."""
    ctx = ctx or {}
    window = ctx.get('window_h', DEFAULT_WINDOW_H)
    template = METRIC_FIELDS.get(metric)
    if template is None:
        raise ValueError(f'unknown metric {metric!r} (known: '
                         f'{", ".join(sorted(METRIC_FIELDS))})')
    field = template.format(w=window)
    row, match = lane_stats(ctx.get('index') or {}, m.get('provider'),
                            m.get('model'), ctx.get('keys'))
    if row is None:
        return None, None
    return row.get(field), {'match': match, 'n_samples': row.get('n_samples'),
                            'source': (ctx.get('meta') or {}).get('source'),
                            'window_h': window}


def outcome_note(m, ctx):
    """Per-hop stats provenance for the resolve response."""
    ctx = ctx or {}
    window = ctx.get('window_h', DEFAULT_WINDOW_H)
    row, match = lane_stats(ctx.get('index') or {}, m.get('provider'),
                            m.get('model'), ctx.get('keys'))
    # TR-066 R5: the fallback kind is NAMED, never implicit — a caller must be
    # able to see that an ordering rested on a weaker bucket.
    _FALLBACK_KIND = {'complexity': None, 'unconditioned': 'unconditioned',
                      'merged': 'merged-backends', 'fallback': 'weighted-fallback',
                      None: 'no-samples'}
    note = {'window_h': window, 'matched': match,
            'stats_fallback': _FALLBACK_KIND.get(match, 'weighted-fallback'),
            'stats_source': (ctx.get('meta') or {}).get('source')}
    if row is not None:
        note['n_samples'] = row.get('n_samples')
        note['complexity_sig'] = row.get('complexity_sig')
        note['required_categories'] = row.get('required_categories')
        note['predicted_cost_per_task'] = row.get(f'avg_cost_task_{window}h')
        note['avg_wall_time_s'] = row.get(f'avg_wall_time_{window}h')
        note['avg_turns'] = row.get(f'avg_turns_{window}h')
        note['avg_tokens_in'] = row.get(f'avg_tokens_in_{window}h')
        note['avg_tokens_out'] = row.get(f'avg_tokens_out_{window}h')
        note['avg_tokens_total'] = row.get(f'avg_tokens_total_{window}h')
        note['success_rate'] = row.get('success_rate')
    return note


def _effective_price(m):
    return (m.get('normalized_price') or 0.0) * (m.get('token_factor') or 1.0)


def _context_sort_key(m):
    ctx = m.get('context_limit')
    return -(ctx if isinstance(ctx, int) else 0)


def _legacy_sort_key(m):
    """The historical chain order: (plan_tier, effective price, larger context
    first, model, provider) — unchanged since TR-015.

    2026-09-19 PAYG-LAST (Bane): PAYG providers sort into a terminal bucket,
    after EVERY plan lane — PAYG is the ultimate fallback, reached only after
    all healthy plan combos. Data-driven: the providers table's `plan` column
    names the billing class ('PAYG'); the set is snapshotted by the registry
    loader into tables['_payg_providers'] (no I/O in the sort path). The
    bucket key goes BEFORE plan_tier so even tier-1 plan hops rank ahead of
    PAYG, and public prices are untouched (reporting stays official)."""
    if m.get('_payg'):
        return (1, 1 << 30, 0, _effective_price(m), _context_sort_key(m),
                m.get('model') or '', m.get('provider') or '')
    return (0,
            m.get('plan_tier') if m.get('plan_tier') is not None else 1 << 30,
            0,
            _effective_price(m), _context_sort_key(m),
            m.get('model') or '', m.get('provider') or '')


def _sort_price(arg, lanes, ctx):
    """The legacy ordering (the default)."""
    return _legacy_sort_key


def _sort_predicted_cost_per_task(arg, lanes, ctx):
    """Cheapest measured cost PER COMPLETED TASK first. A lane with no sample
    keeps its price-proxy rank — unknown is not free."""
    def key(m):
        value, _prov = lane_metric(m, ctx, 'cost')
        return _effective_price(m) if value is None else value
    return key


def _sort_wall_time(arg, lanes, ctx):
    """Fastest measured wall time per task first; a lane with no sample sorts
    LAST (unknown is worst, never best)."""
    def key(m):
        value, _prov = lane_metric(m, ctx, 'wall')
        return float('inf') if value is None else value
    return key


def _sort_turns(arg, lanes, ctx):
    """Fewest measured turns per task first; no sample sorts LAST."""
    def key(m):
        value, _prov = lane_metric(m, ctx, 'turns')
        return float('inf') if value is None else value
    return key


#: metric registry (TR-065 R6): caller-facing metric -> averages-row field
#: template. Adding a metric in router_outcomes.compute_averages + one line here
#: makes it sortable (and usable in ratio mixes) with no other code change.
METRIC_FIELDS = {
    'cost': 'avg_cost_task_{w}h',
    'wall': 'avg_wall_time_{w}h',
    'turns': 'avg_turns_{w}h',
    'tokens_in': 'avg_tokens_in_{w}h',
    'tokens_out': 'avg_tokens_out_{w}h',
    'tokens_total': 'avg_tokens_total_{w}h',
}
#: ratio term aliases: caller-facing metric name -> internal metric
_TERM_ALIASES = {'cost': 'cost', 'predicted_cost_per_task': 'cost',
                 'time': 'wall', 'wall': 'wall', 'wall_time': 'wall',
                 'turns': 'turns', 'tokens': 'tokens_total',
                 'tokens_in': 'tokens_in', 'tokens_out': 'tokens_out',
                 'tokens_total': 'tokens_total'}


def _parse_ratio(spec):
    """'0.7*cost+0.3*time' -> [(0.7, 'cost'), (0.3, 'wall')].

    The mix is CALLER data, never a baked-in constant (TR-049 c5). Raises
    ValueError for anything malformed so the caller can degrade visibly.
    """
    if not spec or not str(spec).strip():
        raise ValueError('ratio needs a mix, e.g. ratio:0.7*cost+0.3*time')
    terms = []
    for raw in str(spec).replace(',', '+').split('+'):
        part = raw.strip()
        if not part:
            continue
        if '*' not in part:
            raise ValueError(f'ratio term {part!r} must be <weight>*<metric>')
        weight, _, metric = part.partition('*')
        try:
            w = float(weight)
        except ValueError:
            raise ValueError(f'ratio weight {weight!r} is not a number')
        term = _TERM_ALIASES.get(metric.strip().lower())
        if term is None:
            raise ValueError(f'unknown ratio metric {metric!r} '
                             f'(known: {", ".join(sorted(_TERM_ALIASES))})')
        terms.append((w, term))
    if not terms:
        raise ValueError('ratio needs at least one term')
    return terms


def _sort_ratio(arg, lanes, ctx):
    """ratio:<w1>*<cost>+<w2>*<time> — a caller-supplied mix of the measured
    metrics. Each term is min-max NORMALIZED across the eligible lanes before
    blending (dollars, seconds and turns are not comparable raw), and a lane
    missing a term counts as unknown = worst for that term."""
    terms = _parse_ratio(arg)
    observed = {}
    for _w, term in terms:
        for m in lanes:
            value, _prov = lane_metric(m, ctx, term)
            if value is not None:
                observed.setdefault(term, []).append(value)

    def norm(term, m):
        value, _prov = lane_metric(m, ctx, term)
        values = observed.get(term) or []
        if value is None or not values:
            return 1.0
        lo, hi = min(values), max(values)
        return 0.0 if hi == lo else (value - lo) / (hi - lo)

    def key(m):
        return sum(w * norm(term, m) for w, term in terms)
    return key


#: Sort-key DISPATCH — adding a key is one entry here, never a new branch
#: inside the chain comparator (TR-049 c5). Each factory takes
#: (arg, lanes, ctx) and returns a key callable over a lane row.
SORT_KEYS = {
    'price': _sort_price,
    'predicted_cost_per_task': _sort_predicted_cost_per_task,
    'wall_time': _sort_wall_time,
    'turns': _sort_turns,
    'ratio': _sort_ratio,
}


def make_sort_key(spec, lanes, ctx):
    """(key callable, resolved name) for a sort spec. Raises ValueError on an
    unknown key or a malformed ratio — the caller degrades visibly (fail-open,
    never a hard stop)."""
    name, _, arg = str(spec or DEFAULT_SORT).partition(':')
    name = name.strip()
    factory = SORT_KEYS.get(name)
    if factory is None:
        raise ValueError(f'unknown sort key {name!r} (known: '
                         f'{", ".join(sorted(SORT_KEYS))}, or '
                         f'ratio:<w>*<cost>+<w>*<time>)')
    return factory(arg, lanes, ctx), name


def _build_chain(tables, reqs, limit=DEFAULT_CHAIN_LIMIT, sort_spec=None, sort_ctx=None,
                 lifecycle_counts=None):
    """Replicates v_task_chain exactly, in pure python.

    reqs = [(category, level), ...] (profile requirements or ad-hoc).
    Eligible = active models (not archived, not past their valid_to
    retirement date, priced) with a
    tier >= level for EVERY requirement. Order: plan_tier ASC,
    normalized_price * token_factor ASC, model ASC, provider ASC (the SQL
    view's tie-breaks). Returns rows [(hop, provider, model, price, dclass, mrow)].

    TR-015: additionally applies min_context requirement (category 'min_context'
    with integer token level). Lanes whose context_limit is known and below the
    requirement are EXCLUDED before ordering and reported as
    'context_limit N < min_context M'. Lanes with unknown context_limit (NULL)
    are allowed to pass but flagged via a 'context_unknown' note so callers can
    see the gap; they are never silently dropped for a non-strict min_context
    check."""
    models = tables.get('models') or []
    # Evidence is per MODEL (Bane 2026-08-27): tiers keyed by model name only;
    # every provider lane of the same weights inherits the same tier. A lane
    # with a bad deployment is disabled EXPLICITLY via models.disabled.
    # TR-043: an alias-mapped variant (see _alias_tiers) also inherits its
    # base's tier rows for categories it has none of its own.
    tiers = {}
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    tiers_folded = _fold_tier_names(tiers)

    # TR-015: split out the min_context requirement from tier-based reqs.
    min_context = None
    tier_reqs = []
    for cat, lvl in reqs:
        if cat == 'min_context':
            min_context = lvl
        else:
            tier_reqs.append((cat, lvl))
    reqs = tier_reqs

    eligible = []
    ls_counts = lifecycle_counts  # None = caller does not want counts
    for m in models:
        _st = lifecycle_state(m)
        if ls_counts is not None:
            ls_counts[_st] = ls_counts.get(_st, 0) + 1
        if m.get('archive') or row_is_retired(m):
            continue
        af = m.get('available_from')
        if af and str(af)[:10] > _today():
            # TR-069 wave 2: announced-but-unreleased lanes never route, but
            # they SAY SO on stderr and are COUNTED — nothing vanishes silently.
            _err(f'{m.get("provider")}/{m.get("model")}: coming_soon '
                 f'(available_from {str(af)[:10]}) — not yet routable')
            continue
        if m.get('disabled'):
            continue  # explicit per-provider lane disable (bad deployment)
        price = m.get('normalized_price')
        if price is None:
            continue
        prov, model = m.get('provider'), m.get('model')
        mt = _alias_tiers(tiers, tiers_folded, model)
        # BLANK default (Bane 2026-08-27): a missing tier = -1 (no data = slightly
        # below median — clears lenient bars, fails 0 and up). NEVER 0, never an
        # inflated neutral.
        misses = [(cat, lvl, mt.get(cat))
                  for cat, lvl in reqs
                  if (mt.get(cat) if mt.get(cat) is not None else -1) < lvl]
        if misses:
            # ROUTER-MISS (Bane 2026-09-01): profile-requirement failures are
            # filtered BEFORE the gate stage, so they never appear in
            # exclusions/gate_reasons — a head model can vanish from the chain
            # with zero trace (proven: deepseek-v4-flash review 0.60 vs a q10
            # boundary that moved to 0.61). Surface every miss on stdout as a
            # ROUTER-MISS line; fail-open — never blocks the resolve.
            try:
                for cat, lvl, tier in misses:
                    _err(f"ROUTER-MISS: {prov}/{model} fails {cat}>={lvl} "
                         f"(tier={tier})")
            except Exception:
                pass
            continue

        # TR-015: min_context gating. NULL passes (with a note); known value
        # below requirement is an exclusion.
        ctx = m.get('context_limit')
        ctx_note = None
        if min_context is not None:
            if ctx is None:
                ctx_note = 'context_unknown: context_limit missing; allowed by lenient min_context rule'
            elif ctx < min_context:
                _err(f"ROUTER-MISS: {prov}/{model} fails min_context>={min_context} "
                     f"(context_limit={ctx})")
                continue

        m = dict(m)
        if ctx_note:
            m['_context_note'] = ctx_note
        eligible.append(m)

    # TR-015: P3_DOCS / P2_AGENTIC prefer large-context lanes when prices are
    # close — that is the secondary component of the LEGACY ordering key.
    # TR-049 c5: ordering goes through the SORT_KEYS dispatch (never a branch
    # inside the comparator); 'price' is the default entry.
    eligible.sort(key=_legacy_sort_key)
    sort_used, sort_warning = 'price', None
    if sort_spec and sort_spec != 'price':
        try:
            order_key, sort_used = make_sort_key(sort_spec, eligible, sort_ctx or {})
            eligible.sort(key=lambda m: (order_key(m), _legacy_sort_key(m)))
        except ValueError as exc:
            # Fail-open: an unknown/malformed key degrades to the price order
            # and SAYS SO (stderr + sort_stats.warning) — it never blocks a
            # resolve and never silently reorders.
            sort_used, sort_warning = 'price', f'{sort_spec!r} ignored: {exc}'
            _err(f'WARNING: --sort {sort_spec!r} ignored — {exc}')
            eligible.sort(key=_legacy_sort_key)
    if sort_ctx is not None:
        sort_ctx['used'] = sort_used
        sort_ctx['warning'] = sort_warning
    # 6th element = the full model row, so callers can expose PUBLIC prices
    # (usd_1m/in_per_m/out_per_m) without a second lookup.
    return [(i + 1, m.get('provider'), m.get('model'),
             m.get('normalized_price'), m.get('data_class'), m)
            for i, m in enumerate(eligible[:limit])]


def _pub_prices(m):
    """Public-price triplet for a model row: (usd_1m, in_per_m, out_per_m).

    Bane 2026-08-27: cost reporting ("what did it cost to build feature X")
    quotes the provider's PUBLIC list price, not the internal normalized rate.
    usd_1m = public blended price when known, else the normalized effective
    rate (fail-open — a priced lane never reports None). in/out are the
    public per-1M split; None when only a blended price is known. Chain
    ORDERING still uses normalized_price — public prices are for reporting.
    """
    pub = m.get('public_price')
    norm = m.get('normalized_price')
    if pub == 0 and norm:
        # Plan-coverage artifact (Bane 2026-09-19, kimi-for-coding/k3): a lane
        # included in a paid plan was stamped public_price 0.0, which made cost
        # reporting say FREE. A plan is not free — it costs the subscription
        # real money and the provider meters usage against it. The effective
        # rate is the normalized price; truly-free lanes (norm == 0) pass
        # through untouched. Data-driven: fires on the (pub==0, norm>0) shape,
        # never on a provider name.
        pub = None
    if pub is None:
        pub = norm
    return pub, m.get('public_in_per_m'), m.get('public_out_per_m')


def _resolve_fallback(tables, qs, hs, cs, reqs, limit=DEFAULT_CHAIN_LIMIT, profile_id=None,
                      qgates=None):
    """FALLBACK LANES (Bane 2026-08-27): when the primary chain is fully
    gated/down, resolve the always-run lanes from data/tables/fallback_lanes.jsonl
    (registry table `fallback_lanes`: {provider, model, order, key_env, profiles?}).

    This is a DEGRADED path, not a normal chain (gpt-5.6-sol review 2026-08-27):
    - fallback lanes may serve a profile they don't fully clear — but that is
      REPORTED, never silent: each hop carries `requirements_unmet` and the
      resolve response sets `degraded_fallback=true` when it fires.
    - the same gates as the primary chain apply: quota GATED, plan-window
      quota exhaustion (TR-060, `qgates`), health DOWN/SLOW, circuit OPEN,
      model-level health, and the lane must exist/be priced. A quota-exhausted
      provider must not reappear as the FALLBACK head — that is the same
      oscillation with a different label.
    - `profiles` field (optional) restricts a lane to specific profiles (e.g.
      the vision-exp lane serves only P5_VISION_E2E so a text model never
      handles vision E2E); absent = generic lane for all profiles.
    - profile-specific matching lanes resolve BEFORE generic lanes."""
    lanes = sorted(tables.get('fallback_lanes') or [],
                   key=lambda r: (r.get('order') or 1 << 30))
    by_lane = {}
    for m in tables.get('models') or []:
        by_lane[(m.get('provider'), m.get('model'))] = m
    tiers = {}
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    tiers_folded = _fold_tier_names(tiers)  # TR-043: alias-aware lane tiers
    generic, specific = [], []
    for f in lanes:
        profs = f.get('profiles') or []
        if profs:
            if profile_id and profile_id in profs:
                specific.append(f)  # curated for THIS profile
        else:
            generic.append(f)  # default lane, all profiles
    ordered = specific + generic  # curated-for-this-profile first, then default
    out = []
    for f in ordered:
        key = (f.get('provider'), f.get('model'))
        m = by_lane.get(key)
        if m is None:
            continue  # lane doesn't exist in registry — gap, not a fabrication
        if m.get('archive') or row_is_retired(m) or m.get('disabled'):
            continue
        if m.get('normalized_price') is None:
            continue
        # ---- gates, same as the primary chain ----
        q = qs.get(f.get('provider')) or {}
        if q.get('status') != 'open':
            continue
        qg = (qgates or {}).get(f.get('provider')) or {}
        if qg.get('active'):
            continue  # TR-060: plan window exhausted — not a fallback either
        h = hs.get(f.get('provider')) or {}
        if h.get('status') in ('DOWN', 'SLOW'):
            continue
        mm = (h.get('models') or {}).get(f.get('model')) or {}
        if mm.get('status') in ('DOWN', 'SLOW'):
            continue
        if (cs.get((f.get('provider'), f.get('model'))) or
                cs.get(f'{f.get("provider")}/{f.get("model")}')):
            continue  # circuit OPEN for this exact pair
        mt = _alias_tiers(tiers, tiers_folded, f.get('model'))
        unmet = [(c, lvl, mt.get(c) if mt.get(c) is not None else -1)
                 for c, lvl in reqs
                 if (mt.get(c) if mt.get(c) is not None else -1) < lvl]
        out.append({'hop': len(out) + 1, 'provider': f.get('provider'),
                    'model': f.get('model'),
                    'usd_1m': round(float(_pub_prices(m)[0]), 4),
                    'in_per_m': _pub_prices(m)[1], 'out_per_m': _pub_prices(m)[2],
                    'data_class': m.get('data_class'),
                    'fallback': True, 'key_env': f.get('key_env'),
                    'requirements_unmet': unmet})
        if len(out) >= limit:
            break
    return out


def _data_home_meta(source, fallback_used):
    """TR-046 (dogfood 2026-09-12): data/gate-state provenance for the JSON.

    {registry, data_dir, state_dir, source, fallback, bootstrap, note}:
    - registry/data_dir/state_dir name exactly where the resolve read from —
      env overrides (ROUTING_REGISTRY / ROUTING_DATA_DIR / ROUTER_STATE_DIR)
      are reflected, so `router status` and `router spawn` can never silently
      disagree about the live data home (TR-044 follow-up).
    - fallback=True ⇔ the registry came from the committed data/tables
      sample tables instead of a seeded registry.json.
    - bootstrap=True flags SOLO/first-run SAMPLE state: the data/tables
      fallback above and/or a quota-state.json written by the `router` CLI
      first-run bootstrap (`updated: 'bootstrap'`, every provider OPEN).
      Sample policy, not discovered gates. Purely informational — gate
      behavior is untouched. Fail-open: any error → visible-False, no note.
    """
    meta = {
        'registry': REGISTRY,
        'data_dir': DATA_DIR,
        'state_dir': MR,
        'source': source,
        'fallback': (source == 'data/tables') or bool(fallback_used),
        'bootstrap': False,
        'note': None,
    }
    notes = []
    if meta['fallback']:
        notes.append(
            'registry came from the committed data/tables sample tables '
            '(no seeded registry.json) — run `router seed` for real state')
    try:
        qdoc = load_json(os.path.join(MR, 'quota-state.json'), None)
        if isinstance(qdoc, dict) and qdoc.get('updated') == 'bootstrap':
            notes.append(
                "quota-state.json is the first-run bootstrap sample "
                "(all providers OPEN) — edit it to apply real gates")
    except Exception:  # noqa: BLE001 — visibility only, never raise
        pass
    if notes:
        meta['bootstrap'] = True
        meta['note'] = '; '.join(notes)
    return meta


def resolve(project=None, profile_id=None, adhoc=None, use_health=True, limit=DEFAULT_CHAIN_LIMIT,
            allow_training=False, allow_slow=None, sort=None, backend=None,
            merge_backends=False, window_h=DEFAULT_WINDOW_H):
    tables, src, fb, warn = _load_registry_with_meta()
    warnings = [warn] if warn else []
    projects = {r.get('id'): r for r in tables.get('projects') or []}
    profiles = {r.get('id'): r for r in tables.get('task_profiles') or []}
    reqs_rows = tables.get('task_profile_requirements') or []
    reqs_by_profile = {}
    for r in reqs_rows:
        reqs_by_profile.setdefault(r.get('task_id'), []).append(
            (r.get('category'), r.get('level')))
    # --- 1. project → profile -------------------------------------------------
    # TR-059: HOW the profile was chosen, emitted on the resolve doc so a
    # caller can see why their input produced this chain:
    #   'project'     — the positional matched a PROJECT row
    #   'profile'     — the positional was a bare PROFILE name (auto-resolved)
    #   'profile-arg' — --profile was passed
    #   'adhoc'       — --profile-req was passed
    #   'default'     — neither given (direct API call; falls back to P0_FORE)
    # `profile_hint` = the canonical form for this input, when the PROJECT
    # slot actually named a profile (TR-059-FIX criterion: the caller must be
    # told `use --profile X` whether the resolve succeeds or dead-ends).
    profile_hint = None
    pid = profile_id
    if adhoc:
        resolved_as = 'adhoc'
        pid = None
        reqs, err = _validate_adhoc(adhoc, tables)
        if err:
            err.setdefault('data_home',
                           _data_home_meta(src, fb))
            return err
    elif project:
        row = projects.get(project)
        if row is None:
            # TR-059 (dogfood 2026-09-16/17): projects and profiles live in
            # DIFFERENT tables, so a bare profile name in the project slot
            # used to dead-end at {"error": "project P1_CODING not in
            # registry"} with no hint that --profile is the documented path —
            # hit live on `router spawn P1_CODING` AND on
            # GET /resolve?project=P1_CODING (the server passes the query
            # straight through, so both surfaces share this one fix).
            if _profile_ref_matches(profiles, project):
                # A ref that NAMES a profile (id or tag) resolves AS that
                # profile, with --profile's exact semantics (tag -> version
                # row via _resolve_profile_tag, exact-id fallback), so
                # `router spawn P1_CODING` == `router spawn --profile
                # P1_CODING` == GET /resolve?project=P1_CODING. An exact
                # PROJECT id still wins (the lookup above), so no existing
                # project changes meaning.
                pid = _resolve_profile_tag(profiles, project)
                reqs = reqs_by_profile.get(pid, [])
                resolved_as = 'profile'
                profile_hint = f'use --profile {pid}'
                _err(f'WARNING: {project} is a profile, not a project — '
                     f'resolving via profile (same as --profile {pid})')
            else:
                # Not a project AND not a profile: the pre-TR-059 error,
                # byte-identical (error + data_home; no code/retryable — the
                # fail-open error shape is pinned by TR-023's battery) unless
                # the name is a real approximate match of a profile.
                err = {'error': f'project {project} not in registry',
                       'data_home': _data_home_meta(src, fb)}
                near = _profile_near_miss(profiles, project)
                if near:
                    # Bonus hint, ONLY on a real approximate match: the dead
                    # end above is exactly the confusion TR-059 is about.
                    err['error'] += (f' — matches profile {near}, '
                                     f'use --profile {near}')
                    err['hint'] = f'use --profile {near}'
                    err['matched_profile'] = near
                return err
        else:
            # TR-020: tag-based profile resolution. A project references a profile
            # tag (or legacy id). Resolve tag -> version row; fall back to exact id
            # for backward compatibility with existing rows like P0_FORE.
            pid = _resolve_profile_tag(profiles, row.get('profile') or 'P0_FORE')
            reqs = reqs_by_profile.get(pid, [])
            resolved_as = 'project'
    else:
        # --profile argument may be a tag or an exact id.
        resolved_as = 'profile-arg' if profile_id else 'default'
        pid = _resolve_profile_tag(profiles, pid or 'P0_FORE')
        reqs = reqs_by_profile.get(pid, [])
    if not pid and not adhoc:
        pid = 'P0_FORE'
    if pid and pid not in profiles:
        return {'error': f'profile {pid} not in registry',
                'code': 'PROFILE_NOT_FOUND', 'retryable': False,
                'data_home': _data_home_meta(src, fb)}

    # TR-054 (Bane 2026-09-16): latency-tolerant lanes. allow_slow resolves in
    # the same precedence as allow_training: explicit per-call arg wins, else
    # the profile row's allow_slow flag (P1_WORKER), else quota-state knob.
    # Semantics: 'model SLOW' and provider 'health SLOW' stop excluding a hop
    # (the slowness is KNOWN and accepted by the caller); health DOWN / model
    # DOWN / quota / circuit still exclude — SLOW-tolerant never means
    # broken-tolerant.
    prof_row = profiles.get(pid) or {}
    _allow_slow = bool(allow_slow) or bool(prof_row.get('allow_slow'))

    # --- 2. chain from the registry -------------------------------------------
    # TR-049 c4/c5: when a stats-based sort is requested, load the rolling
    # averages ONCE and give the ordering its context (index + the task's
    # complexity reference keys). Fail-open: unreadable stats never block a
    # resolve — the ordering degrades to the price keys and says why.
    sort_spec = sort or DEFAULT_SORT
    sort_ctx = None
    if sort_spec != 'price':
        stats_index, stats_meta = load_outcome_stats(
            backend=backend, merge_backends=merge_backends)
        sort_ctx = {'index': stats_index, 'meta': stats_meta, 'window_h': window_h,
                    'keys': _complexity_keys(pid, tables),
                    'used': 'price', 'warning': None}
        if stats_meta.get('error'):
            _err(f'WARNING: outcome stats degraded — {stats_meta["error"]}')
    # Profiles with NO requirement rows resolve to an empty chain — identical
    # to v_task_eligible (its task list comes from DISTINCT requirements).
    _lc_counts = {}
    chain_rows = _build_chain(tables, reqs, limit=limit, lifecycle_counts=_lc_counts, sort_spec=sort_spec,
                              sort_ctx=sort_ctx) if reqs else []
    sort_used = (sort_ctx or {}).get('used') or 'price'
    # TR-021: keep the raw price-ordered eligible list for metrics before gates.
    chain = list(chain_rows)

    # --- 2.5 settings: diversity caps + per-profile overrides ------------------
    # TR-025 gates_loaded: presence of each state file is reported LOUDLY —
    # a missing file never silently passes (DATA>CODE: missing fact = visible
    # gap), and gate BEHAVIOR is unchanged (absent != open, fail-open sacred).
    # load_json() itself is blind to absence (default {} on any error), so
    # presence is checked explicitly here; a present-but-unparseable file
    # counts as loaded (the parse outcome surfaces through the gates).
    def _present(name):
        try:
            return os.path.isfile(os.path.join(MR, name))
        except Exception:  # noqa: BLE001 — visibility only, never raise
            return False

    qdoc = load_json(f'{MR}/quota-state.json', {})
    if not isinstance(qdoc, dict):
        qdoc = {}
    caps = _effective_caps(profiles, qdoc, pid)
    if not chain:
        return {'error': 'no chain — profile has no eligible models',
                'profile': pid,
                'data_home': _data_home_meta(src, fb)}

    # --- 3. gates: quota + health + circuit + per-model busy --------------------
    qs = qdoc.get('providers') or {}
    if not isinstance(qs, dict):
        qs = {}
    diversity = qdoc.get('diversity') or {}
    if not isinstance(diversity, dict):
        diversity = {}
    models_cfg = qdoc.get('models') or {}
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    # TR-060: plan-window exhaustion gates (auto-clearing, reset_at-bounded).
    quota_gates = load_quota_gates(qdoc)
    hsrc = load_json(f'{MR}/health-state.json', {}) if use_health else {}
    hs = hsrc.get('providers', {}) if use_health else {}
    cstate = load_json(f'{MR}/circuit-state.json', {})
    cs = cstate.get('pairs', {})
    # TR-014 provider-level breakers (v2 section; absent = {} = no provider gates)
    _v2 = cstate.get('v2') if isinstance(cstate.get('v2'), dict) else {}
    cprov = _v2.get('provider_breakers') if isinstance(_v2.get('provider_breakers'), dict) else {}
    # TR-032 soft concurrency gate: default OFF (quota-state.json knob).
    # ON adds the per-model busy-count exclusion below; OFF never rejects.
    soft_gate_on = bool(qdoc.get('soft_gate'))
    inflight = ledger_in_flight(MR)  # fail-open: {} on any error
    ledger_wired = ledger_has_traces(MR)  # rows exist ⇒ start/end calls land
    # Training-data opt-in (Bane 2026-09-01): default EXCLUDES lanes whose
    # provider/model terms train on your prompts/completions. Opt in via
    # quota-state.json knob allow_training: true, or per-call
    # allow_training=1 (CLI flag / server query param) — for projects like
    # 9router/rethinkdb where open-source work makes training a fair trade
    # for contributor-tier pricing.
    allow_training = bool(qdoc.get('allow_training')) or allow_training
    if not ledger_wired:
        # TR-026 visible disable: the spawn ledger is NOT wired by the
        # scheduler yet (TASK-ROUTER-002 call side). Concurrency knobs are a
        # no-op until it is — say so LOUDLY instead of silently passing.
        warnings.append(
            'spawn ledger NOT WIRED: ledger.jsonl has no trace rows — the '
            "'model busy' concurrency gate cannot fire; TR-007 knobs are "
            'inactive until the scheduler calls router_ledger.py start/end '
            'around spawns (cross-repo: coding-hermes-scheduler '
            'TASK-ROUTER-002 call side)'
        )
        _err('WARNING: spawn ledger NOT WIRED — model busy gate cannot fire')
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

    # Training gate (Bane 2026-09-01): provider facts live in the providers
    # table (trains_on_hosted); index once for the per-hop check below.
    provs_by_id = {r.get('id'): r for r in (tables.get('providers') or [])}

    out_chain, exclusions, reasons = [], [], []
    for hop, prov, model, price, dc, mrow in chain:
        prov_row = provs_by_id.get(prov) or {}
        why = []
        q = qs.get(prov, {})
        if not isinstance(q, dict):
            q = {}
        if q.get('status') != 'open':
            why.append(f'quota GATED: {q.get("reason", "blocked")}')
        qg = quota_gates.get(prov)
        if qg and qg.get('active'):
            # TR-060: plan-window exhaustion (reset_at in the future, or no
            # reset time at all) — the lane is skipped until the plan refills.
            why.append(qg['gate_reason'])
        h = hs.get(prov, {})
        if not isinstance(h, dict):
            h = {}
        if h.get('status') == 'DOWN':
            why.append(f'health DOWN ({h.get("ts", "?")})')
        elif h.get('status') == 'SLOW' and not _allow_slow:
            why.append(f'health SLOW ({h.get("latency_ms")}ms)')
        # model-level health (probe v2 writes providers.<p>.models.<m>.status;
        # gpt-5.6-sol review 2026-08-27: the router previously ignored it and
        # routed onto 22 DOWN pairs)
        hm = (h.get('models') or {}).get(model) or {}
        # TR-104: render the model's own transition ts (probe v3 stamps every
        # model entry); entries written before stamping fall back to the
        # provider-level probe ts — never a bare '?'.
        mt = hm.get('ts') or h.get('ts') or '?'
        if hm.get('status') == 'DOWN':
            why.append(f'model DOWN ({mt})')
        elif hm.get('status') == 'SLOW' and not _allow_slow:
            why.append(f'model SLOW ({hm.get("latency_ms")}ms)')
        c = cs.get(f'{prov}/{model}')
        if c and c.get('open_until') and c['open_until'] > now:
            why.append(f'circuit OPEN until {c["open_until"]} ({c.get("failures", 0)} failures)')
        # TR-014: provider-level breaker (api_down/out_of_credit across models)
        pb = cprov.get(prov)
        if isinstance(pb, dict) and pb.get('open_until') and pb['open_until'] > now:
            why.append(f'circuit OPEN (provider-level, {pb.get("class", "api_down")}) '
                       f'until {pb["open_until"]} ({pb.get("failures", 0)} failures)')
        mlim = _model_limit(models_cfg, diversity, prov, model) if soft_gate_on else None
        if mlim is not None:
            nf = inflight.get((prov, model), 0)
            if nf >= mlim:
                why.append(f'model busy ({nf} in-flight >= limit {mlim})')
        # Training-data gate (Bane 2026-09-01): excluded UNLESS the caller
        # opted in. A lane trains when the MODEL is a global trainer
        # (training_model_level — e.g. any *-contributor checkpoint, every
        # provider/proxy) OR this PROVIDER trains on what it hosts
        # (training_provider_level / providers.trains_on_hosted).
        if not allow_training:
            mrow_t = mrow if isinstance(mrow, dict) else {}
            trains = bool(mrow_t.get('training_model_level')) or \
                bool(mrow_t.get('training_provider_level')) or \
                bool(prov_row.get('trains_on_hosted'))
            if trains:
                why.append('training on prompts/completions (opt in with '
                           'allow_training to include)')
        if why:
            exclusions.append({'hop': hop, 'provider': prov, 'model': model, 'why': why})
            reasons.append(f'hop {hop} {prov}/{model}: ' + '; '.join(why))
        else:
            pub_usd, pub_in, pub_out = _pub_prices(mrow)
            ent = {'hop': hop, 'provider': prov, 'model': model,
                   'usd_1m': round(float(pub_usd), 4) if pub_usd is not None else None,
                   'in_per_m': pub_in, 'out_per_m': pub_out,
                   'data_class': dc}
            # TR-069 wave 2: retiring lanes warn on EVERY hop (date + successor);
            # capacity is never silently cut, but callers see the deadline.
            _ls = lifecycle_state(mrow)
            if _ls == 'retiring':
                ent['lifecycle'] = 'retiring'
                ent['retires_on'] = str(mrow.get('valid_to'))[:10]
                if mrow.get('replaced_by'):
                    ent['replaced_by'] = mrow.get('replaced_by')
            # TR-015: expose per-lane context window; preserve unknown as None.
            ctx = mrow.get('context_limit')
            if ctx is not None:
                ent['context_limit'] = ctx
            else:
                ent['context_limit'] = None
            note = mrow.get('_context_note')
            if note:
                ent['context_note'] = note
            if sort_ctx is not None:
                # TR-049: the stats this hop was ranked by (additive key; the
                # sort itself never changes gate behavior).
                ent['outcomes'] = outcome_note(mrow, sort_ctx)
            out_chain.append(ent)

    # --- 4. diversity pruning: two-knob caps on the survivor chain -------------
    _prune_diversity(out_chain, exclusions, reasons,
                     caps['max_consecutive_per_provider'],
                     caps['max_total_per_provider'])

    head = out_chain[0] if out_chain else None

    # --- 4.5 FALLBACK LANES (Bane 2026-08-27): crons must ALWAYS run ---------
    # When every eligible hop is gated/down, fall back to the designated
    # always-available lanes (deepseek-v4 + cron key). Cheap subs first,
    # deepseek as the guaranteed last hop — never a None chain for a cron.
    fb_used = []
    if not head:
        # TR-046 fix: local name `lanes` — this used to assign to `fb`,
        # destroying the registry-loader's fallback flag. Downstream,
        # fallback_used/data_home then reported False (no always-run lane
        # found) even though the resolve READ the data/tables sample tables —
        # a provenance lie in exactly the solo fresh-clone case TR-046 flags.
        lanes = _resolve_fallback(tables, qs, hs, cs, reqs, limit=limit,
                                  profile_id=pid, qgates=quota_gates)
        if lanes:
            fb_used = lanes
            head = lanes[0]
            out_chain = lanes
            reasons.append(
                f'FALLBACK: all {len(exclusions)} eligible hops gated — using '
                f'{head["provider"]}/{head["model"]} (always-run lane; '
                f'DEGRADED — requirements_unmet: '
                f'{[(c, lvl, have) for c, lvl, have in head.get("requirements_unmet", [])]})')

    # TR-046: computed BEFORE the return; `fb` is the registry-loader's
    # fallback flag (data/tables sample read), never rebound by the
    # fallback-lane block above.
    dh = _data_home_meta(src, fb)

    return {'project': project, 'profile': pid, 'resolved_at': now,
            # TR-069 wave 2: per-state model counts — the hiding is always
            # reported ("nothing vanishes silently").
            'lifecycle_counts': _lc_counts,
            # TR-059: how the profile was chosen ('project' | 'profile' |
            # 'profile-arg' | 'adhoc' | 'default') — additive; gate behavior is
            # untouched. `hint` names the canonical form when the project slot
            # actually carried a profile name (null otherwise).
            'resolved_as': resolved_as,
            'hint': profile_hint,
            'head': head, 'chain': out_chain, 'exclusions': exclusions,
            'gate_reasons': reasons,
            'degraded_fallback': bool(fb_used),
            # TR-025 runtime visibility: WHERE the data came from + which
            # gate-state files were actually present. Additive only — gate
            # behavior and fail-open are untouched.
            'source': src,
            'fallback_used': bool(fb),
            # TR-046: data/gate-state provenance — where the data came from
            # (paths), whether it is the data/tables SAMPLE fallback, and
            # whether first-run BOOTSTRAP sample policy (all-OPEN quota-state)
            # is in effect. Informational only; gates untouched. The flat
            # bootstrap/note mirrors exist so consumers can check one key;
            # they are the SAME computed values as data_home (never static).
            'data_home': dh,
            'bootstrap': dh['bootstrap'],
            'note': dh['note'],
            'gates_loaded': {
                'health': bool(_present('health-state.json')),
                'circuit': bool(_present('circuit-state.json')),
                'quota': bool(_present('quota-state.json')),
                # TR-026: wired = ledger file exists WITH trace rows. An
                # empty ledger means start/end is never called — the
                # 'model busy' gate cannot fire. Emitted loudly (warning
                # above + gates_loaded.ledger=false) until the scheduler
                # wires the spawn path.
                'ledger': ledger_wired,
                'ledger_rows': len(inflight),
            },
            # TR-060: which plan-window quota gates are in effect right now,
            # and which recorded entries have auto-cleared (reset_at passed).
            # A NEW top-level key on purpose: TR-025's battery pins the exact
            # gates_loaded dict, and an invisible gate is a silent zero-chain.
            'quota_gates': dict(quota_gate_summary(quota_gates),
                                source=f'{MR}/quota-state.json'),
            'warnings': warnings,
            'gate': 'OPEN' if head else ('NO-OPEN-HOP' if out_chain or exclusions else 'NO-CHAIN'),
            'settings': caps,
            # TR-049 c4/c5: which ordering was actually applied, and where the
            # stats came from. Additive — gate behavior is untouched.
            # NB: the stats-degradation reason is `problem`, not `error` — the
            # flat `error` key is reserved for "this resolve FAILED" (fail-open
            # contract), and consumers/text-scrapers treat a literal "error" in
            # the payload as a failed resolve.
            'sort': sort_used,
            'sort_stats': {
                'window_h': window_h,
                'backend': backend if (backend and not merge_backends) else None,
                'merge_backends': bool(merge_backends or not backend),
                'loaded': sort_ctx is not None,
                'source': (sort_ctx or {}).get('meta', {}).get('source'),
                'rows': (sort_ctx or {}).get('meta', {}).get('rows', 0),
                'path': (sort_ctx or {}).get('meta', {}).get('path'),
                'problem': (sort_ctx or {}).get('meta', {}).get('error'),
                'warning': (sort_ctx or {}).get('warning'),
            },
            # TR-021: carry the raw chain rows to the metrics hook without
            # recomputing.  This key is intentionally NOT part of the public
            # contract and is stripped before JSON serialization in main().
            '_chain_rows': chain_rows}

COMPLEXITY_SCORERS = ('auto', 'classifier', 'jev')


def complexity_requirements(text, scorer='auto', classify_fn=None, classify_name=None):
    """Requirement levels derived from the TASK's own complexity.

    Bane 2026-09-23: a profile alone made the spawn path a fixed list. The router
    has to look at the task itself, so this scores the task text and turns the
    result into the SAME requirement list the proxy path already uses
    (router_server._proxy_requirements -> _proxy_chain: the scorer's signed
    matrix IS the requirement list, one level per category, 1:1).

    Returns (adhoc, meta):
      adhoc = ['cat=level', ...] ready for the resolver's ad-hoc channel, so all
              existing validation, tier-gating and reporting apply unchanged;
              None when no matrix could be produced.
      meta  = always says what happened, including the degrade reason.

    Fail-open by contract: never raises, because router_spawn must never block
    the scheduler. A scorer that is down degrades to the caller's profile with a
    VISIBLE reason, never to a silently different chain.
    """
    meta = {'scorer': scorer, 'source': None, 'matrix': None, 'adhoc': None,
            'problems': [], 'degraded': False, 'degrade_reason': None}
    text = (text or '').strip()
    if not text:
        meta.update(degraded=True, degrade_reason='empty task text')
        return None, meta

    if classify_fn is not None:
        attempts = [(classify_name or 'stub', classify_fn)]
    else:
        attempts = []
        want = (scorer or 'auto').lower()
        if want in ('auto', 'classifier'):
            try:
                import router_classify
                attempts.append(('classifier', router_classify.classify))
            except Exception as exc:  # noqa: BLE001
                meta['problems'].append(f'classifier import failed: {str(exc)[:160]}')
        if want in ('auto', 'jev', 'decisions'):
            try:
                import router_jev
                attempts.append(('jev', router_jev.classify))
            except Exception as exc:  # noqa: BLE001
                meta['problems'].append(f'jev import failed: {str(exc)[:160]}')
    if not attempts:
        meta.update(degraded=True, degrade_reason='no scorer available')
        return None, meta

    for name, fn in attempts:
        try:
            res = fn(text)
        except Exception as exc:  # noqa: BLE001
            meta['problems'].append(f'{name}: {str(exc)[:200]}')
            continue
        if not isinstance(res, dict) or not res.get('matrix'):
            why = '; '.join(str(x) for x in (res or {}).get('problems') or [])
            meta['problems'].append(f'{name}: {why or "no matrix returned"}')
            continue
        matrix = {}
        for cat, lvl in res['matrix'].items():
            if isinstance(lvl, bool) or not isinstance(lvl, (int, float)):
                continue
            matrix[str(cat)] = int(lvl)
        if not matrix:
            meta['problems'].append(f'{name}: matrix had no numeric levels')
            continue
        adhoc = [f'{c}={v}' for c, v in sorted(matrix.items())]
        meta.update(source=name, matrix=matrix, adhoc=adhoc, degraded=False,
                    degrade_reason=None,
                    confidence=res.get('confidence'),
                    complexity_sig=res.get('complexity_sig'),
                    model=res.get('model'), band=res.get('band'),
                    score=res.get('score'),
                    prompt_version=res.get('prompt_version'))
        return adhoc, meta

    meta.update(degraded=True,
                degrade_reason='; '.join(meta['problems']) or 'scorer produced no matrix')
    return None, meta


def task_text_for(project=None, task_id=None, board=None):
    """Task text the router should score: the board row's own title+description.

    Bane 2026-09-23: "the board is outlining the task" — so the board row IS the
    complexity input. Reads the project's board (or an explicit --board path).
    Returns (text, source_path) or (None, reason).
    """
    if not task_id:
        return None, 'no task id'
    cands = []
    if board:
        cands.append(board)
    cands.append(os.path.join(os.getcwd(), '.coding-hermes', 'board', 'tasks.jsonl'))
    if project:
        # The routing registry keys projects by id+profile only (no workdir), so
        # conventional locations are tried rather than guessed from it.
        home = os.path.expanduser('~')
        cands.append(os.path.join(home, project, '.coding-hermes', 'board', 'tasks.jsonl'))
        cands.append(os.path.join(home, 'coding-hermes', project,
                                  '.coding-hermes', 'board', 'tasks.jsonl'))
    for path in cands:
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if str(row.get('id')) != str(task_id):
                        continue
                    parts = [str(row.get('title') or '')]
                    desc = row.get('description')
                    if desc:
                        parts.append(str(desc))
                    tags = row.get('tags')
                    if tags:
                        parts.append('tags: ' + ' '.join(map(str, tags)))
                    return '\n'.join(p for p in parts if p), path
        except OSError:
            continue
    return None, f'task {task_id} not found in {len(cands)} board path(s)'


def main():
    ap = argparse.ArgumentParser(description='Task router — resolve chain for project/profile')
    ap.add_argument('project', nargs='?')
    ap.add_argument('--profile', dest='profile_id')
    ap.add_argument('--profile-req', dest='adhoc', nargs='+', help="ad-hoc 'cat=level' list")
    ap.add_argument('--prompt', help='task text to score for complexity (this task, '
                                     'not a fixed profile)')
    ap.add_argument('--prompt-file', help='read the task text from a file (- = stdin)')
    ap.add_argument('--from-task', dest='from_task',
                    help='score a board task row (title+description) by id')
    ap.add_argument('--board', help='board path for --from-task '
                                    '(default: the project workdir, else cwd)')
    ap.add_argument('--scorer', choices=list(COMPLEXITY_SCORERS), default=None,
                    help='complexity scorer: auto (classifier, JEV fallback), '
                         'classifier, or jev (cheap)')
    ap.add_argument('--list-profiles', action='store_true')
    ap.add_argument('--explain', action='store_true')
    ap.add_argument('--format', choices=['json', 'text'], default='json')
    ap.add_argument('--no-health', action='store_true')
    ap.add_argument('--allow-training', action='store_true',
                    help='include lanes whose terms train on prompts/completions '
                         '(default: excluded; e.g. muse-spark-1.2-contributor)')
    ap.add_argument('--allow-slow', action='store_true',
                    help='include lanes the probe marked SLOW (latency-tolerant '
                         'callers, e.g. worker batches on free lanes; TR-054). '
                         'DOWN/quoted/circuit-open lanes still excluded.')
    ap.add_argument('--quiet', action='store_true',
                    help='suppress stderr telemetry (ROUTER-MISS, warnings); '
                         'this is now the DEFAULT — the audit trail is opt-in '
                         'via ROUTER_MISS_VERBOSE=1; env ROUTER_SPAWN_QUIET=1 '
                         'also works')
    # TR-049 c4/c5: outcomes-driven ordering.
    ap.add_argument('--sort', default=None, metavar='KEY',
                    help='chain ordering: price (default) | predicted_cost_per_task '
                         '(cheapest measured cost per COMPLETED task) | wall_time | '
                         'turns | ratio:<w>*<cost>+<w>*<time>. Stats-based keys read '
                         'the rolling averages ($ROUTING_AVERAGES_FILE); an unknown '
                         'key degrades to price with a visible warning.')
    ap.add_argument('--backend', default=None, metavar='NAME',
                    help='use only this source_system\'s outcome stats (per-backend '
                         'isolation). Default: merge every backend.')
    ap.add_argument('--merge-backends', action='store_true',
                    help='aggregate outcome stats across all backends — the default '
                         'when --backend is absent; wins over --backend when both '
                         'are given.')
    ap.add_argument('--window-h', type=int, default=DEFAULT_WINDOW_H, metavar='H',
                    help=f'average window (half-life, hours) for stats-based sorts '
                         f'(default {DEFAULT_WINDOW_H})')
    args = ap.parse_args()

    # TR-033 / TR-055: --quiet takes precedence; set the env so the rest of the
    # code observes a single source of truth (the default is already quiet).
    if args.quiet:
        os.environ['ROUTER_SPAWN_QUIET'] = '1'

    if args.list_profiles:
        tables = _load_registry()
        profs = {r.get('id'): r for r in tables.get('task_profiles') or []}
        reqs = {}
        for r in tables.get('task_profile_requirements') or []:
            reqs.setdefault(r.get('task_id'), []).append(
                (r.get('category'), r.get('level')))
        # TR-046: --format json = pure JSON even for --list-profiles (the
        # table printed human lines on stdout regardless of --format).
        rows = [
            {'id': pid, 'title': profs[pid].get('title', ''),
             'requirements': dict(sorted(reqs.get(pid, [])))}
            for pid in sorted(profs)
        ]
        if args.format == 'json':
            print(json.dumps({'profiles': rows}, indent=1))
            return
        for row in rows:
            rs = ' '.join(f"{c}={'+'*l if l>0 else ('-'*-l if l<0 else '0')}"
                          for c, l in sorted(row['requirements'].items(),
                                             key=lambda x: (-x[1], x[0])))
            print(f"{row['id']:<10} {row['title']}")
            print(f'           {rs}')
        return

    # TR-124 (Bane 2026-09-23): look at the TASK, not just its profile. The task
    # text is scored into requirement levels so the chain follows the task's own
    # complexity; a scorer outage degrades to the profile WITH a visible reason.
    task_text, text_src = None, None
    if args.prompt is not None:
        task_text, text_src = args.prompt, '--prompt'
    elif args.prompt_file:
        try:
            if args.prompt_file == '-':
                task_text = sys.stdin.read()
            else:
                with open(args.prompt_file) as fh:
                    task_text = fh.read()
            text_src = args.prompt_file
        except OSError as exc:
            task_text, text_src = None, f'--prompt-file unreadable: {exc}'
    elif args.from_task:
        task_text, text_src = task_text_for(project=args.project,
                                            task_id=args.from_task, board=args.board)
    complexity_meta = None
    if task_text:
        adhoc, complexity_meta = complexity_requirements(
            task_text, scorer=args.scorer or os.environ.get('ROUTER_SCORER') or 'auto')
        complexity_meta['text_source'] = text_src
        complexity_meta['text_chars'] = len(task_text)
        if adhoc:
            if args.adhoc:
                _err('WARNING: --profile-req ignored — task complexity requirements win')
            args.adhoc = adhoc
        else:
            _err(f"WARNING: complexity scoring degraded to the profile — "
                 f"{complexity_meta.get('degrade_reason')}")
    elif complexity_meta is None and (args.prompt_file or args.from_task):
        _err(f"WARNING: no task text available ({text_src}) — resolving from the profile")

    if not args.project and not args.profile_id and not args.adhoc:
        # TR-046: usage text on stdout breaks --format json consumers
        # (`| python3 -m json.tool`). JSON mode gets a structured error
        # (still fail-open, exit 0); human text stays for text mode/stderr.
        if args.format == 'json':
            print(json.dumps({
                'error': 'no project/profile given — pass a project, '
                         '--profile, or --profile-req (see --help)',
                'code': 'NO_INPUT', 'retryable': False}, indent=1))
        else:
            ap.print_usage()
        return

    if args.project and args.profile_id:
        _err(f"WARNING: both --profile {args.profile_id} and project "
             f"{args.project} given — resolving via project "
             f"(project profile wins)")

    r = resolve(project=args.project, profile_id=args.profile_id,
                adhoc=args.adhoc, use_health=not args.no_health,
                allow_training=args.allow_training,
                allow_slow=args.allow_slow,
                sort=args.sort, backend=args.backend,
                merge_backends=args.merge_backends, window_h=args.window_h)
    # TR-021: metrics append is best-effort; any failure is swallowed so
    # router_spawn stdout + exit code stay identical.  Strip the internal
    # _chain_rows helper key before serialization.
    try:
        _append_metrics(r.get('_chain_rows'), r)
    except Exception:
        pass
    r.pop('_chain_rows', None)
    if complexity_meta is not None:
        r['complexity'] = complexity_meta
    if args.format == 'json':
        print(json.dumps(r, indent=1))
        return
    if r.get('error'):
        print(f'ERROR: {r["error"]}')
        if r.get('code'):
            print(f'code={r.get("code")}  retryable={r.get("retryable")}')
        return
    cx = r.get('complexity')
    if cx:
        if cx.get('degraded'):
            print(f'  COMPLEXITY: DEGRADED to the profile — {cx.get("degrade_reason")}')
        else:
            print(f'  COMPLEXITY: {cx.get("source")} conf={cx.get("confidence")} '
                  f'({cx.get("text_chars")} chars from {cx.get("text_source")})')
    print(f'▶ {r.get("project", r.get("profile"))}  profile={r.get("profile")}  gate={r.get("gate")}')
    h = r.get('head')
    if h:
        print(f'  HEAD: {h["provider"]}/{h["model"]}  ${h["usd_1m"]}/M')
    for c in r.get('chain', [])[1:6]:
        print(f'  hop {c["hop"]}: {c["provider"]}/{c["model"]}  ${c["usd_1m"]}/M')
    for g in r.get('gate_reasons', []):
        print(f'  EXCLUDED: {g}')

if __name__ == '__main__':
    main()

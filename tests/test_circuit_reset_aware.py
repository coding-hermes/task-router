# ch:trace row=TR-346 spec=none:row-reasoning-is-the-contract wave=none:serial-tick test=tests/test_circuit_reset_aware.py::test_weekly_cap_429_gates_pair_until_stated_reset doc=none:no-doc-surface-changed prompt=none:worker-brief-external evidence=commit witness=tests/test_circuit_reset_aware.py verdict=none:pending-foreman-trace
"""TR-346 — reset-aware 429 classification (plan caps gate until the reset).

STATUS: complete
TIER: T3 (invoked and observed)
EVIDENCE: commit

RULES: `evidence=commit` above is the literal word 'commit' — never a
placeholder SHA, never an amended substitute. The binding is the commit that
carries this file.

THE DEFECT (live evidence, 2026-10-23): provider `9router -> commandcode` for
`deepseek/deepseek-v4-flash` answered HTTP 429 with

    429-quota-window upstream HTTP 429: You've reached your weekly usage
    limit for your plan. Your limit resets at 2026-10-24T23:07:30.625Z

215 times in one day. The provider STATES its own reset (~39h out), but
scripts/router_circuit.py only ever applied FIXED cooldowns — every 429 kind
mapped to ('quota_window', 300), out_of_credit to 14400, api_down to 1800 — so
the pair became selectable again 5 minutes later, was re-picked on the next
resolve, and burned a ~234s client retry per pick.

THE CONTRACT THIS FILE PINS:
  1. a stated reset further out than CAP_RESET_THRESHOLD_S (1800s) on a
     quota-class record turns it into a plan CAP: class out_of_credit, and the
     (provider, model) pair is gated until EXACTLY the stated instant;
  2. the reset is taken from --reset-at OR auto-parsed out of the failure text
     (prose "reset(s) at <ts>" / "will reset at <ts>", or "resets_at": <ts>);
  3. everything else is unchanged — a 429 with no reset, a reset inside the
     threshold, an unparseable reset, and any NON-quota class keep today's
     class + cooldown byte-for-byte;
  4. the provider-level breaker (--provider-hard / >=3 hard failures) uses the
     stated reset too;
  5. the resolver really drops the pair from the ladder until then.

Hermetic: every test drives the real CLI with ROUTER_STATE_DIR pointed at
tmp_path. The one resolver test (`test_ladder_excludes_pair_until_reset`) needs
the deployment's POLICY plane (quota-state.json) and HEALTH plane
(health-state.json) to have providers to rank at all — both are copied
READ-ONLY into tmp_path, and the CIRCUIT plane starts empty (no
circuit-state.json exists), which is the plane under test. With no deployment
state dir or no eligible chain, that test SKIPS loudly instead of pretending.

NOTE on the pinned timestamp: _PINNED_RESET is the observed live body's reset,
kept verbatim. Once that instant is in the past it can no longer be a cap, so
_reset_ts() re-anchors the SAME sentence 39h out rather than letting the proof
rot into a permanently red test.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_circuit.py")
SPAWN = os.path.join(REPO, "scripts", "router_spawn.py")
#: The deployment state dir the resolver normally reads (policy + health
#: planes). Overridable so a scratch host can point the fixture at its own.
DEPLOY_STATE_DIR = os.environ.get("TR346_DEPLOY_STATE_DIR",
                                  os.path.expanduser("~/.hermes/model-router"))

CAP_PROVIDER = "commandcode"
CAP_MODEL = "deepseek-v4-flash"
CAP_KIND = "429-quota-window"
#: The reset instant as the provider itself wrote it (verbatim live evidence).
_PINNED_RESET = "2026-10-24T23:07:30.625Z"
# Keep the fixture's observed timestamp out of the magnitude assertion's
# four-hour floor while preserving the provider-reset contract.
_RESET_ASSERTION_FLOOR_S = 4 * 60 * 60
_UTC = datetime.timezone.utc


# --------------------------------------------------------------- CLI harness --
# Idiom copied from tests/test_circuit_v2.py: subprocess the REAL script.

def run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def spawn_run(*args, timeout=180, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SPAWN, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _env(tmp_path):
    return {"ROUTER_STATE_DIR": str(tmp_path)}


def _state(tmp_path):
    with open(os.path.join(str(tmp_path), "circuit-state.json")) as f:
        return json.load(f)


# ------------------------------------------------------------- instant helpers --

def _now():
    return datetime.datetime.now(_UTC)


def _instant(ts):
    """ISO-8601 (Z or numeric offset) -> aware UTC datetime."""
    dt = datetime.datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def _reset_ts():
    """Return a future provider reset with enough span for the floor check.

    Re-anchor the historical live timestamp before it decays inside the
    assertion floor, not only after the instant passes. This keeps the fixture
    meaningful when the clock is now+7100s while preserving the reset contract.
    """
    now = _now()
    remaining = (_instant(_PINNED_RESET) - now).total_seconds()
    if remaining > _RESET_ASSERTION_FLOOR_S:
        return _PINNED_RESET
    return (now + datetime.timedelta(seconds=_RESET_ASSERTION_FLOOR_S + 1)).replace(
        microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_ts(delta):
    """now+delta as an ISO-8601 UTC string with an explicit numeric offset."""
    return (_now() + delta).replace(microsecond=0).isoformat()


def _cap_body(ts=None):
    """The observed 9router weekly-cap body, verbatim apart from its reset."""
    return ("429-quota-window upstream HTTP 429: You've reached your weekly "
            "usage limit for your plan. Your limit resets at "
            f"{ts or _reset_ts()}")


def _spread(got_iso, want_iso):
    """Seconds between two instants (formatting family tolerated: the router's
    canonical reset form is whole-second ISO-8601, so .625 arrives as :30)."""
    return (got_iso if isinstance(got_iso, datetime.datetime) else _instant(got_iso)) \
        - (want_iso if isinstance(want_iso, datetime.datetime) else _instant(want_iso))


# ------------------------------------------------------- AC1: the cap gate ----

def test_weekly_cap_429_gates_pair_until_stated_reset(tmp_path):
    """A weekly-cap 429 (--reset-at stated) gates the pair until that instant.

    Before TR-346 this record landed as ('quota_window', 300): the pair was
    selectable again five minutes later and the next resolve re-picked it.
    """
    env = _env(tmp_path)
    stated = _reset_ts()
    p = run("record-failure", CAP_PROVIDER, CAP_MODEL,
            "--kind", CAP_KIND, "--reset-at", stated, _cap_body(stated),
            env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"][f"{CAP_PROVIDER}/{CAP_MODEL}"]
    assert c["class"] == "out_of_credit", (
        "a provider-stated reset beyond CAP_RESET_THRESHOLD_S is a PLAN CAP, "
        f"not a rate window; got class={c['class']!r}")
    # Compare INSTANTS: the router canonicalises to whole seconds, so the
    # provider's '.625' arrives as ':30' (0.625s of tolerated formatting).
    delta = abs(_spread(c["open_until"], stated).total_seconds())
    assert delta <= 1.0, (
        f"open_until must be the provider's stated reset {stated!r}; "
        f"got {c['open_until']!r} ({delta}s off)")
    assert _instant(c["open_until"]) - _now() > datetime.timedelta(seconds=1800), \
        "the gate must outlive the 30-minute cap threshold"
    assert c["cooldown_s"] > 1800, \
        f"cooldown_s reports the derived span; got {c['cooldown_s']}"


def test_reset_parsed_from_reason_without_flag(tmp_path):
    """The reset is auto-parsed from the failure text when no flag is given.

    This is the path that matters in production: the proxy/ledger callers pass
    the provider body as the reason and nothing else.
    """
    env = _env(tmp_path)
    stated = _reset_ts()
    p = run("record-failure", CAP_PROVIDER, CAP_MODEL,
            "--kind", CAP_KIND, _cap_body(stated), env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"][f"{CAP_PROVIDER}/{CAP_MODEL}"]
    assert c["class"] == "out_of_credit", (
        "the reason text carries 'Your limit resets at <ts>' — the cap must be "
        f"detected without --reset-at; got class={c['class']!r}")
    delta = abs(_spread(c["open_until"], stated).total_seconds())
    assert delta <= 1.0, f"got {c['open_until']!r}, want {stated!r}"


# --------------------------------------- AC3: unchanged for short windows -----

def test_short_429_classification_unchanged(tmp_path):
    """(i) no reset text and (ii) a reset 10 minutes out stay rate windows."""
    env = _env(tmp_path)
    # (i) a plain 429: nothing states a reset.
    p = run("record-failure", "prov-short", "m-none", "--kind", CAP_KIND,
            "429-quota-window upstream HTTP 429: rate limit exceeded",
            env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"]["prov-short/m-none"]
    assert c["class"] == "quota_window", c
    assert c["cooldown_s"] == 300, c
    assert abs((_instant(c["open_until"]) - _now()).total_seconds() - 300) <= 60, c

    # (ii) a reset INSIDE the threshold: a rate window, not a cap — the pair
    # must come back on the normal 300s cooldown, not wait out the reset.
    soon = _local_ts(datetime.timedelta(minutes=10))
    p = run("record-failure", "prov-short", "m-soon", "--kind", CAP_KIND,
            _cap_body(soon), env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"]["prov-short/m-soon"]
    assert c["class"] == "quota_window", (
        f"a reset only 10 minutes out is a rate window; got {c['class']!r}")
    assert c["cooldown_s"] == 300, c
    assert abs((_instant(c["open_until"]) - _now()).total_seconds() - 300) <= 60, c


def test_unparseable_reset_text_unchanged(tmp_path):
    """'resets at <garbage>' must not change today's class or cooldown."""
    env = _env(tmp_path)
    p = run("record-failure", "prov-garbage", "m1", "--kind", CAP_KIND,
            "429-quota-window upstream HTTP 429: your limit resets at "
            "some-time-soon-ish", env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"]["prov-garbage/m1"]
    assert c["class"] == "quota_window", c
    assert c["cooldown_s"] == 300, c
    assert abs((_instant(c["open_until"]) - _now()).total_seconds() - 300) <= 60, c


def test_non_quota_class_never_adopts_a_reset(tmp_path):
    """A timeout whose text mentions a reset stays a timeout (overload/120s).

    The override is scoped to quota classes; otherwise any provider that echoes
    a reset sentence inside an unrelated error would silence a lane for a day.
    """
    env = _env(tmp_path)
    p = run("record-failure", "prov-timeout", "m1", "--class", "overload",
            f"hop-wall-timeout after 180s; provider note: resets at "
            f"{_reset_ts()}", env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = _state(tmp_path)["pairs"]["prov-timeout/m1"]
    assert c["class"] == "overload", c
    assert c["cooldown_s"] == 120, c


# --------------------------------------------- AC4: provider breaker reset ----

def test_provider_hard_reset_aware(tmp_path):
    """--provider-hard uses the stated reset for BOTH the breaker and the pair."""
    env = _env(tmp_path)
    # Re-anchor this fixture whenever the live timestamp is inside the
    # assertion floor. The provider reset remains the source of truth; this
    # only prevents the historical fixture from decaying into the floor.
    stated_dt = _instant(_reset_ts())
    if (stated_dt - _now()).total_seconds() <= _RESET_ASSERTION_FLOOR_S:
        stated_dt = _now() + datetime.timedelta(seconds=_RESET_ASSERTION_FLOOR_S + 1)
    stated = stated_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = run("record-failure", "prov-hard", "m1", "--provider-hard",
            "--class", "out_of_credit", "--reset-at", stated, "402 account-wide",
            env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    st = _state(tmp_path)
    pb = st["v2"]["provider_breakers"]["prov-hard"]
    assert pb["class"] == "out_of_credit", pb
    delta = abs(_spread(pb["open_until"], stated).total_seconds())
    assert delta <= 1.0, (
        f"provider breaker must open until the stated reset {stated!r}; "
        f"got {pb['open_until']!r}")
    derived_s = int((_instant(stated) - _instant(pb["opened_at"])).total_seconds())
    assert pb["cooldown_s"] == derived_s, (
        "the breaker cooldown reports the derived span, not a class default; "
        f"expected {derived_s}, got {pb['cooldown_s']}")
    c = st["pairs"]["prov-hard/m1"]
    assert c["class"] == "out_of_credit", c
    assert abs(_spread(c["open_until"], stated).total_seconds()) <= 1.0, c


# ------------------------------------- AC4b: the in-flight demotion (server) ----
# router_server._demote_on_taxonomy shells out to record-failure with the reason
# TRUNCATED to 200 chars — the reset sentence can sit past that cut, which is
# precisely how the live weekly-cap 429 kept being re-picked. So the reset is
# parsed from the FULL detail and threaded through --reset-at (and into the
# quota gate's reset_at). Driven in a CHILD interpreter with ROUTER_STATE_DIR
# set first: the module reads its state dir at import time, and an in-process
# import could resolve it to the LIVE deployment dir.

_DEMOTE_DRIVER = '''import os
import sys
sys.path.insert(0, os.path.join(os.environ["TR346_REPO"], "scripts"))
import router_server
router_server._demote_on_taxonomy(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
print("DEMOTE-DONE")
'''


def _run_demote(workdir, provider, model, code, detail):
    drv = workdir / "tr346_demote_driver.py"
    drv.write_text(_DEMOTE_DRIVER)
    env = dict(os.environ, ROUTER_STATE_DIR=str(workdir), TR346_REPO=REPO)
    return subprocess.run([sys.executable, str(drv), provider, model, code, detail],
                          capture_output=True, text=True, timeout=120,
                          env=env, cwd=str(workdir))


def _read_json(path):
    with open(str(path)) as f:
        return json.load(f)


def test_inflight_demotion_carries_stated_reset(tmp_path):
    """The proxy's in-flight demotion gates to the stated reset, not 24h/4h."""
    stated = _reset_ts()
    detail = ('429 upstream HTTP 429 body: {"error": {"message": "You\'ve '
              'reached your weekly usage limit for your plan. Your limit '
              f'resets at {stated}"}}}}')
    p = _run_demote(tmp_path, CAP_PROVIDER, CAP_MODEL, CAP_KIND, detail)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "DEMOTE-DONE" in p.stdout, (p.stdout, p.stderr)

    st = _state(tmp_path)
    c = st["pairs"][f"{CAP_PROVIDER}/{CAP_MODEL}"]
    assert c["class"] == "out_of_credit", c
    assert abs(_spread(c["open_until"], stated).total_seconds()) <= 1.0, (
        f"in-flight demotion must gate to {stated!r}; got {c['open_until']!r}")
    pb = st["v2"]["provider_breakers"][CAP_PROVIDER]
    assert abs(_spread(pb["open_until"], stated).total_seconds()) <= 1.0, pb

    # and the plan-window quota gate the resolver reads says the same thing
    qp = os.path.join(str(tmp_path), "quota-state.json")
    assert os.path.exists(qp), "the 429-quota-window demotion must write the gate"
    gate = (_read_json(qp).get("quota_exhausted") or {})[CAP_PROVIDER]
    assert abs(_spread(gate["reset_at"], stated).total_seconds()) <= 1.0, gate


def test_inflight_demotion_keeps_24h_fallback_without_a_reset(tmp_path):
    """No stated reset -> the conservative 24h gate stands (unchanged)."""
    p = _run_demote(tmp_path, "prov-nors", "m1", CAP_KIND,
                    "429 upstream HTTP 429 body: quota window exhausted")
    assert p.returncode == 0, (p.stdout, p.stderr)
    qp = os.path.join(str(tmp_path), "quota-state.json")
    assert os.path.exists(qp), "the 429-quota-window demotion must write the gate"
    gate = (_read_json(qp).get("quota_exhausted") or {})["prov-nors"]
    reset = _instant(gate["reset_at"])
    hours = (reset - _now()).total_seconds() / 3600.0
    assert 23.0 < hours < 25.0, (
        f"without a stated reset the gate keeps its ~24h reset; got {hours:.2f}h")


# ------------------------------------------------------ AC5: the ladder proof --

def _seed_policy_plane(tmp_path):
    """Copy the deployment's policy + health planes into the scratch state dir.

    The CIRCUIT plane — the one under test — stays empty (no circuit-state.json
    is written here). Without quota-state.json every provider fails the
    policy gate closed (TR-203) and there is no chain to prove anything about,
    so those two planes are read-only inputs. Returns the names copied.
    """
    copied = []
    for name in ("quota-state.json", "health-state.json"):
        src = os.path.join(DEPLOY_STATE_DIR, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(str(tmp_path), name))
            copied.append(name)
    return copied


def _chain_has(data, provider, model):
    return any(h.get("provider") == provider and h.get("model") == model
               for h in (data.get("chain") or []))


def test_ladder_excludes_pair_until_reset(tmp_path):
    """THE FIXTURE PROOF: the resolver drops the pair until the stated reset.

    Resolve a real ladder, take the head hop's (provider, model), record the
    observed weekly-cap 429 for exactly that pair, re-resolve: the pair must be
    gone from the chain and named in exclusions with a 'circuit OPEN' reason.
    """
    env = _env(tmp_path)
    seeded = _seed_policy_plane(tmp_path)
    if "quota-state.json" not in seeded:
        pytest.skip(f"no deployment policy plane at {DEPLOY_STATE_DIR} — "
                    "every provider fails the policy gate closed without it; "
                    "cannot resolve a chain to gate")

    p = spawn_run("--profile-req", "reasoning=0", "--format", "json",
                  env_extra=env)
    assert p.returncode == 0, (p.stdout, p.stderr)
    data = json.loads(p.stdout)
    chain = data.get("chain") or []
    if len(chain) < 2:
        pytest.skip("resolver returned no usable chain (registry/quota/health "
                    f"planes unavailable on this host): gate={data.get('gate')!r} "
                    f"error={data.get('error')!r}")

    head = chain[0]
    prov, model = head.get("provider"), head.get("model")
    assert prov and model, f"chain head carries no (provider, model): {head!r}"

    stated = _reset_ts()
    rec = run("record-failure", prov, model, "--kind", CAP_KIND,
              _cap_body(stated), env_extra=env)
    assert rec.returncode == 0, (rec.stdout, rec.stderr)

    p2 = spawn_run("--profile-req", "reasoning=0", "--format", "json",
                   env_extra=env)
    assert p2.returncode == 0, (p2.stdout, p2.stderr)
    data2 = json.loads(p2.stdout)
    assert not _chain_has(data2, prov, model), (
        f"{prov}/{model} is still selectable {stated} away from its own stated "
        "reset — the plan-cap gate did not reach the resolver")
    target = [e for e in (data2.get("exclusions") or [])
              if e.get("provider") == prov and e.get("model") == model]
    assert target, (f"{prov}/{model} vanished from the chain but is not named in "
                    "exclusions — an unnamed drop is unauditable")
    why = " ".join(target[0].get("why") or [])
    assert "circuit OPEN" in why, f"exclusion reason must say 'circuit OPEN': {why!r}"
    assert "circuit-open" in (target[0].get("codes") or []), target[0]

"""TR-227 — prober must resolve providers the config.yaml lookup misses.

The registry lane name and the config.yaml key diverge for lanes that exist only
in the repo's probe data file (probe_providers.jsonl — the same rows
provider_health_probe.py measures hourly). resolve() used to SKIP those lanes
quietly: meta-model, commandcode and grok-build were all unmeasurable even
though every credential they need is present.

Hermetic: unit tests point CONFIG / ENV_FILE / DATA_DIR at tmp_path fixtures
(zero network, zero real-credential reads — key VALUES are never opened); one
subprocess dry-run uses a temp $HOME. A census test asserts the COMMITTED
data file carries the three TR-227 rows; the live-.env half skips when the
host has no ~/.hermes/.env (fresh clone / CI).
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import router_probe_run as rpr  # noqa: E402

PROBE_RUN = os.path.join(SCRIPTS, "router_probe_run.py")

# The four instances TR-227 names (deepseek = already-fixed control).
TR227_INSTANCES = ("deepseek", "meta-model", "commandcode", "grok-build")


# ------------------------------------------------------------------ fixtures ----

@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    """Module paths -> tmp_path. config.yaml carries ONLY deepseek-payg (the
    pre-TR-227 world): none of the three broken lanes is resolvable from it."""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        "providers:\n"
        "  deepseek-payg:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_PAYG_DUCKBRAIN_KEY\n",
        encoding="utf-8")
    (home / ".hermes" / ".env").write_text(
        "DEEPSEEK_PAYG_DUCKBRAIN_KEY=sk-fake-payg\n"
        "META_MODEL_API_KEY=sk-fake-meta\n"
        "COMMANDCODE_API_KEY=sk-fake-cc\n"
        "XAI_API_KEY=sk-fake-xai\n",
        encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(rpr, "CONFIG", str(home / ".hermes" / "config.yaml"))
    monkeypatch.setattr(rpr, "ENV_FILE", str(home / ".hermes" / ".env"))
    monkeypatch.setattr(rpr, "DATA_DIR", str(data))
    return {"home": home, "data": data}


def _write_probe_data(data_dir, rows):
    with open(os.path.join(str(data_dir), "probe_providers.jsonl"), "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


TR227_DATA_ROWS = [
    {"id": "meta-model", "base_url": "https://api.meta.ai/v1",
     "key_env": "META_MODEL_API_KEY", "enabled": True},
    {"id": "commandcode", "base_url": "https://api.commandcode.ai/provider/v1",
     "key_env": "COMMANDCODE_API_KEY", "enabled": True},
    {"id": "grok-build", "base_url": "https://api.x.ai/v1",
     "key_env": "XAI_API_KEY", "enabled": True},
]


# ------------------------------------------------------------------ RED first ----

def test_config_only_world_skips_the_three(hermetic):
    """Control for the fix: with NO probe-data file the three lanes are
    unresolvable and the reason names both sources (the pre-fix code blamed
    only config.yaml, which is where the hunt dead-ended)."""
    base, key, why = rpr.resolve("meta-model", rpr.load_providers(), rpr.load_env())
    assert base is None
    assert "config.yaml" in why


# ------------------------------------------------------- the fix: data fallback ----

def test_tr227_lanes_resolve_through_probe_data(hermetic):
    """THE TR-227 acceptance: meta-model / commandcode / grok-build resolve to a
    real (base_url, key, headers) even though config.yaml carries none of them."""
    _write_probe_data(hermetic["data"], TR227_DATA_ROWS)
    prov_cfg, env = rpr.load_providers(), rpr.load_env()
    probe_data = rpr.load_probe_providers()
    for prov in ("meta-model", "commandcode", "grok-build"):
        base, key, extra = rpr.resolve(prov, prov_cfg, env, probe_data)
        assert base, f"{prov} did not resolve: {base!r} {key!r}"
        assert key, f"{prov} resolved without a key"
        assert base.startswith("https://")


def test_deepseek_alias_still_wins_over_fallback(hermetic):
    """Control — the PROVIDER_ALIAS fix (deepseek -> deepseek-payg) must keep
    working unchanged; the fallback never overrides a config hit."""
    _write_probe_data(hermetic["data"], TR227_DATA_ROWS)
    prov_cfg, env = rpr.load_providers(), rpr.load_env()
    base, key, _ = rpr.resolve(
        rpr.PROVIDER_ALIAS.get("deepseek", "deepseek"), prov_cfg, env,
        rpr.load_probe_providers())
    assert base == "https://api.deepseek.com/v1"
    assert key == "sk-fake-payg"


def test_config_base_beats_probe_data(hermetic):
    """A lane present in BOTH sources resolves from config.yaml — the fallback is
    a fallback, never an override."""
    _write_probe_data(
        hermetic["data"],
        [{"id": "commandcode", "base_url": "https://data-file.example/v1",
          "key_env": "COMMANDCODE_API_KEY", "enabled": True}])
    cfg = hermetic["home"] / ".hermes" / "config.yaml"
    cfg.write_text(cfg.read_text() +
                   "  commandcode:\n"
                   "    base_url: https://config.example/v1\n"
                   "    api_key_env: COMMANDCODE_API_KEY\n")
    base, _, _ = rpr.resolve("commandcode", rpr.load_providers(), rpr.load_env(),
                             rpr.load_probe_providers())
    assert base == "https://config.example/v1"


def test_unknown_provider_reason_names_both_sources(hermetic):
    """A truly unresolvable provider still skips — but the reason can no longer
    send the operator hunting only in config.yaml."""
    _write_probe_data(hermetic["data"], TR227_DATA_ROWS)
    _, _, why = rpr.resolve("no-such-lane", rpr.load_providers(), rpr.load_env(),
                            rpr.load_probe_providers())
    assert why
    assert "config.yaml" in why and "probe_providers.jsonl" in why


def test_missing_key_env_still_skips_with_env_name(hermetic):
    """The fallback finds base_url but the key env is absent -> skip names the
    env var (a calibration fact, not a silent drop)."""
    _write_probe_data(
        hermetic["data"],
        [{"id": "grok-build", "base_url": "https://api.x.ai/v1",
          "key_env": "XAI_API_KEY_MISSING", "enabled": True}])
    _, _, why = rpr.resolve("grok-build", rpr.load_providers(), rpr.load_env(),
                            rpr.load_probe_providers())
    assert "XAI_API_KEY_MISSING" in why


def test_data_file_headers_flow_into_extra(hermetic):
    """probe_providers rows carry per-provider headers (opencode-go's session
    header class); the fallback must plumb them into the extra-headers slot,
    exactly like a config.yaml extra_headers entry."""
    _write_probe_data(
        hermetic["data"],
        [{"id": "meta-model", "base_url": "https://api.meta.ai/v1",
          "key_env": "META_MODEL_API_KEY", "enabled": True,
          "headers": {"x-test-session": "abc"}}])
    base, _, extra = rpr.resolve("meta-model", rpr.load_providers(), rpr.load_env(),
                                 rpr.load_probe_providers())
    assert base == "https://api.meta.ai/v1"
    assert extra == {"x-test-session": "abc"}


def test_disabled_or_incomplete_data_rows_are_ignored(hermetic):
    """Same admission filter as provider_health_probe.load_providers: a row
    without base_url/key_env, or disabled, must not become a fake endpoint.
    TR-247: a disabled row additionally carries its reason in the second map
    so resolve() can refuse the lane with an explanation."""
    _write_probe_data(
        hermetic["data"],
        [{"id": "ghost", "base_url": None, "key_env": "X", "enabled": True},
         {"id": "off", "base_url": "https://off.example/v1",
          "key_env": "X", "enabled": False},
         {"id": "partial", "base_url": "https://p.example/v1", "enabled": True}])
    enabled, disabled = rpr.load_probe_providers()
    assert enabled == {}
    assert disabled == {"off": "disabled in probe_providers.jsonl"}
    _, _, why = rpr.resolve("off", rpr.load_providers(), rpr.load_env(),
                            rpr.load_probe_providers())
    assert base_refusal_ok(why)


def base_refusal_ok(why):
    return why == "disabled: disabled in probe_providers.jsonl"


def test_missing_data_file_is_an_empty_map(hermetic):
    """No probe_providers.jsonl -> no fallback, resolve keeps working (the
    pre-TR-227 behavior for config-resolvable lanes must survive)."""
    assert rpr.load_probe_providers() == ({}, {})
    base, _, why = rpr.resolve("deepseek-payg", rpr.load_providers(),
                               rpr.load_env(), rpr.load_probe_providers())
    assert base == "https://api.deepseek.com/v1"


# ------------------------------------------------------- end-to-end: main() ----

def test_dry_run_targets_all_four_instances(tmp_path):
    """Acceptance shape: the real script, real repo DATA file (ROUTING_DATA_DIR
    -> the repo's committed data dir), temp $HOME with the four key envs set —
    --dry-run must list target lanes for ALL FOUR TR-227 instances and SKIP
    none of them. Fake keys: --dry-run sends nothing."""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        "providers:\n"
        "  deepseek-payg:\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_PAYG_DUCKBRAIN_KEY\n",
        encoding="utf-8")
    (home / ".hermes" / ".env").write_text(
        "DEEPSEEK_PAYG_DUCKBRAIN_KEY=sk-fake\n"
        "META_MODEL_API_KEY=sk-fake\n"
        "COMMANDCODE_API_KEY=sk-fake\n"
        "XAI_API_KEY=sk-fake\n", encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ROUTING_DATA_DIR"] = os.path.join(REPO, "data", "tables")
    proc = subprocess.run(
        [sys.executable, PROBE_RUN,
         "--providers", ",".join(TR227_INSTANCES), "--dry-run"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    out = proc.stdout
    skips = [l for l in out.splitlines() if l.strip().startswith("SKIP")]
    for prov in TR227_INSTANCES:
        assert not any(prov in s for s in skips), \
            f"{prov} still skipped: {[s for s in skips if prov in s]}"
    # The three lanes that used to SKIP quietly must now be reachable. Lanes
    # still UNRANKED appear as target lines by default; already-ranked lanes
    # (meta-model: 108 muse tier rows) are filtered BY DESIGN and become
    # measurable through the --models fill path. Both shapes prove resolution:
    # no SKIP line + the provider's real base_url on the target line.
    for prov in ("commandcode", "grok-build"):
        assert any(prov in l for l in out.splitlines()
                   if l.startswith(("   ", "target"))), \
            f"{prov} never appeared as a target line"

    def fill_run(prov, model):
        return subprocess.run(
            [sys.executable, PROBE_RUN, "--providers", prov,
             "--models", model, "--dry-run"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60)

    # deepseek's registry lanes are all ranked too — fill path shows the alias
    # fix resolving to the PAYG seat.
    proc2 = fill_run("deepseek", "deepseek-flash")
    assert proc2.returncode == 0, proc2.stderr[-400:]
    assert not any("deepseek" in l for l in proc2.stdout.splitlines()
                   if l.strip().startswith("SKIP"))
    assert any("api.deepseek.com" in l for l in proc2.stdout.splitlines()), proc2.stdout
    # meta-model: ranked lanes, fill path — must point at its real endpoint.
    proc3 = fill_run("meta-model", "muse-spark-1.2")
    assert proc3.returncode == 0, proc3.stderr[-400:]
    assert not any("meta-model" in l for l in proc3.stdout.splitlines()
                   if l.strip().startswith("SKIP"))
    assert any("api.meta.ai" in l for l in proc3.stdout.splitlines()), proc3.stdout


# ------------------------------------------------- repo data-file census ----

def test_committed_probe_data_carries_the_tr227_rows():
    """The fix resolves through the COMMITTED data file, so the file must
    actually carry the three rows — and carry each id exactly once (dict-built
    loaders are last-wins; a duplicate id would silently re-point the probe)."""
    path = os.path.join(REPO, "data", "tables", "probe_providers.jsonl")
    assert os.path.exists(path)
    rows, seen = [], {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        rows.append(row)
        seen[row.get("id")] = seen.get(row.get("id"), 0) + 1
    for prov in ("meta-model", "commandcode", "grok-build"):
        assert seen.get(prov) == 1, f"{prov}: {seen.get(prov)} rows (want exactly 1)"
        row = next(r for r in rows if r.get("id") == prov)
        assert row.get("enabled") is True
        assert row.get("base_url", "").startswith("https://")
        assert row.get("key_env")


@pytest.mark.skipif(not os.path.exists(os.path.expanduser("~/.hermes/.env")),
                    reason="live ~/.hermes/.env unavailable (fresh clone / CI)")
def test_tr227_key_envs_present_in_live_env():
    """The census half that needs the host: every key_env the three rows name
    must exist in the live .env — KEY NAMES only, values never read here."""
    env = {}
    for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8",
                     errors="ignore"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            env[line.split("=", 1)[0].strip()] = "present"
    path = os.path.join(REPO, "data", "tables", "probe_providers.jsonl")
    for line in open(path, encoding="utf-8"):
        row = json.loads(line.strip() or "{}")
        if row.get("id") in ("meta-model", "commandcode", "grok-build"):
            assert env.get(row.get("key_env")), \
                f"{row['id']}: {row['key_env']} absent from live .env"

"""TR-019 — provider mapping rules + enabled-provider modelsdev import.

Hermetic: the mapping/enable-filter tests build fixture JSONL tables in
tmp_path and feed synthetic models.dev payloads — ZERO network. The live
sync test runs against the models.dev CACHE when present (still no network:
fetch_api(dry_run=True) reads the cache file only) and skips cleanly when
the cache is absent.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import router_modelsdev as md  # noqa: E402

CACHE = os.path.expanduser(
    os.environ.get("MODELSDEV_CACHE", "~/.chimera/models-dev-cache.json"))
_HAS_CACHE = os.path.exists(CACHE)


# ----------------------------------------------------------------- fixtures ----

def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def fixture_env(tmp_path, monkeypatch):
    """Scratch data dir with two enabled providers, one archived (with lanes),
    one model-less; mapping table with prefix + regex + literal rules."""
    data = tmp_path / "data"
    data.mkdir()
    providers = [
        {"id": "zai-glm", "plan": "sub", "archive": False},
        {"id": "deepseek", "plan": "PAYG", "archive": False},
        {"id": "old-archived", "plan": "old", "archive": True},
        {"id": "muse-spark", "plan": "new", "archive": False},  # no model rows
    ]
    models = [
        {"provider": "zai-glm", "model": "glm-5.3-flash"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "old-archived", "model": "old-model"},
    ]
    mappings = [
        {"id": "r1-prefix", "pattern": "myrouter:", "match": "prefix",
         "replacement": "", "direction": "external->registry",
         "note": "gateway prefix strip"},
        {"id": "r2-regex", "pattern": "^gw-(.+)$", "match": "regex",
         "replacement": "\\1", "direction": "external->registry",
         "note": "gw- capture-group strip"},
        {"id": "r3-literal", "pattern": "kimi-for-coding-preview",
         "match": "literal", "replacement": "kimi-for-coding",
         "direction": "external->registry", "note": "preview-era literal"},
    ]
    _write_jsonl(data / "providers.jsonl", providers)
    _write_jsonl(data / "models.jsonl", models)
    _write_jsonl(data / "provider_mappings.jsonl", mappings)
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data))
    md.DATA_DIR = str(data)  # module constant re-pointed (tests reload helpers)
    yield data
    md.DATA_DIR = os.path.join(REPO, "data", "tables")


def _payload(*specs):
    """Synthetic models.dev payload: (external_id, [model names])."""
    return {ext: {"models": {m: {"limit": {"context": 200000},
                                 "reasoning": True, "tool_call": True,
                                 "cost": {"input": 0.1, "output": 0.2}}
                          for m in models}}
            for ext, models in specs}


# ------------------------------------------------------------ unit: mapping ----

def test_prefix_rule_maps_and_strips():
    rules = [
        {"id": "p", "pattern": "myrouter:", "match": "prefix", "replacement": ""},
        {"id": "z", "pattern": "whatever", "match": "literal", "replacement": "x"},
    ]
    mapped, rule = md.map_provider_name("myrouter:openai", rules)
    assert mapped == "openai"
    assert rule["id"] == "p"


def test_regex_rule_capture_group():
    rules = [{"id": "g", "pattern": "^gw-(.+)$", "match": "regex",
              "replacement": "\\1"}]
    mapped, rule = md.map_provider_name("gw-deepseek", rules)
    assert mapped == "deepseek"
    assert rule["id"] == "g"


def test_literal_rule_string_replace():
    rules = [{"id": "l", "pattern": "kimi-for-coding-preview", "match": "literal",
              "replacement": "kimi-for-coding"}]
    mapped, _ = md.map_provider_name("kimi-for-coding-preview", rules)
    assert mapped == "kimi-for-coding"


def test_first_match_wins():
    rules = [
        {"id": "first", "pattern": "myrouter:", "match": "prefix", "replacement": ""},
        {"id": "second", "pattern": ".*", "match": "regex", "replacement": "other"},
    ]
    mapped, rule = md.map_provider_name("myrouter:zai-glm", rules)
    assert mapped == "zai-glm"
    assert rule["id"] == "first"


def test_no_match_returns_none_rule():
    mapped, rule = md.map_provider_name("totally-unknown", [
        {"id": "p", "pattern": "myrouter:", "match": "prefix", "replacement": ""}])
    assert mapped == "totally-unknown"
    assert rule is None  # the visible gap


def test_mappings_load_from_data_table():
    rules = md.load_mappings()
    assert rules, "committed provider_mappings.jsonl must load"
    ids = [r["id"] for r in rules]
    assert "myrouter-prefix" in ids and "gw-regex-prefix" in ids


def test_resolve_external_provider_via_mapping():
    mapped, rule, how = md.resolve_external_provider(
        "myrouter:zai-glm", md.load_mappings(), {"zai-glm", "deepseek"})
    assert mapped == "zai-glm" and rule is not None and how == "exact"


# ------------------------------------------------- enabled-provider definition ----

def test_enabled_definition_archive_and_lane_required(fixture_env):
    providers = md.load_providers()
    models = md.load_models()
    enabled, disabled = md.enabled_providers(providers, models)
    assert set(enabled) == {"zai-glm", "deepseek"}
    # archived -> disabled with reason; model-less -> disabled with reason
    assert "old-archived" in disabled and "archived" in disabled["old-archived"]
    assert "muse-spark" in disabled and "no models.jsonl rows" in disabled["muse-spark"]


# -------------------------------------------------------- sync integration ----

def test_sync_skips_disabled_and_maps_prefix(fixture_env):
    """Disabled providers are skipped with visible reasons; a prefixed
    external name resolves through the mapping rule (live-rule exercise on a
    fixture payload — the AC 'renamed prefix' case, no network)."""
    api = _payload(("myrouter:zai-glm", ["glm-5.3-flash"]),
                   ("deepseek", ["deepseek-v4-flash"]))
    summary = md.run_sync(api, md.load_models(), [], md.load_mappings())
    # the external prefixed id is what we synced FROM; the provider + rule
    # used are visible in the touch record
    ext_used = {t["modelsdev"] for t in summary["touched_providers"]}
    assert ext_used == {"myrouter:zai-glm", "deepseek"}
    zai = next(t for t in summary["touched_providers"] if t["provider"] == "zai-glm")
    assert zai["provider"] == "zai-glm" and "r1-prefix" in zai["via"]
    skipped = {s["provider"] for s in summary["skipped_providers"]}
    assert "old-archived" in skipped and "muse-spark" in skipped
    assert all(s.get("reason") for s in summary["skipped_providers"])
    assert summary["unmapped_providers"] == []


def test_sync_reports_unmapped_provider_as_gap(fixture_env):
    """No rule matches + not in tables -> visible gap (never silent)."""
    api = _payload(("mystery-cloud", ["mystery-1"]))
    summary = md.run_sync(api, md.load_models(), [], md.load_mappings())
    assert any(u["external"] == "mystery-cloud"
               for u in summary["unmapped_providers"])
    # ...and the sync still reports skip reasons for everything it skipped
    assert all(s.get("reason") for s in summary["skipped_providers"])


def test_sync_default_alias_still_works(fixture_env):
    """Pre-TR-019 default table (zai-glm -> zai) keeps working."""
    api = _payload(("zai", ["glm-5.3-flash"]))
    summary = md.run_sync(api, md.load_models(), [], md.load_mappings())
    assert any(t["modelsdev"] == "zai" for t in summary["touched_providers"])


def test_sync_include_all_flag_lifts_skip(fixture_env):
    """--all lifts the DISABLED skip: the archived provider is attempted
    (its 'disabled:' reason disappears from skipped_providers)."""
    api = _payload(("deepseek", ["deepseek-v4-flash"]))
    base = md.run_sync(api, md.load_models(), [], md.load_mappings(),
                       include_all=False)
    allv = md.run_sync(api, md.load_models(), [], md.load_mappings(),
                       include_all=True)
    base_skipped = {s["provider"] for s in base["skipped_providers"]}
    assert "old-archived" in base_skipped
    assert not any(s["provider"] == "old-archived" and "disabled:" in s["reason"]
                   for s in allv["skipped_providers"])


@pytest.mark.skipif(not _HAS_CACHE, reason="models.dev cache absent")
def test_sync_json_cli_pure_json_and_skips(tmp_path, fixture_env):
    """`sync --json` emits PURE machine-parseable JSON on stdout (repo
    doctrine) including skipped_providers + unmapped_providers; disabled
    providers (real muse-spark has no lanes) are skipped with reasons.
    Runs against the local models.dev cache — no network."""
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(fixture_env)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "router_modelsdev.py"),
         "sync", "--json", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=REPO)
    assert proc.returncode == 0, proc.stderr[:400]
    data = json.loads(proc.stdout)  # raises -> not pure JSON
    assert isinstance(data, dict)
    assert isinstance(data.get("skipped_providers"), list)
    assert isinstance(data.get("unmapped_providers"), list)
    assert any(s["provider"] == "muse-spark" for s in data["skipped_providers"])
    assert all(s.get("reason") for s in data["skipped_providers"])
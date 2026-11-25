"""TR-1000976681059309 AC3 — the first-run quota seed must cover registry-only
providers.

The defect (measured 2026-11-24, re-measured 2026-11-25 on origin/main): a fresh
`TASK_ROUTER_HOME` + `router spawn` writes an all-OPEN starter
quota-state.json from `data/tables/providers.jsonl` (30 rows) while the resolver
enumerates lanes from the MODELS table — which names a 31st provider,
`deepseek-duckbrain-sync`. Hop 2 of every fresh resolve was therefore excluded

    policy-gate-missing-row: provider not listed in quota-state.json

on a file the operator never had a chance to populate. `router_spawn.py`'s own
isolated-dir bootstrap was fixed for this by TR-345 (it passes the registry
provider set explicitly); the `router` CLI path passes no list and kept seeding
from the providers table alone.

What is pinned here:

  * a provider present ONLY in models.jsonl lands in the bootstrap sample
    (synthetic tmp tables — hermetic, no dependence on fleet data);
  * the REAL committed tables seed a sample that includes
    `deepseek-duckbrain-sync` (guards the live repo state, the exact lane from
    the defect);
  * an explicit caller list still wins verbatim (no model-table union) so the
    TR-345 contract is unchanged;
  * an existing quota-state.json is never overwritten (live deployments stay
    byte-stable).
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import state_dir as sd  # noqa: E402  (the module under test)

REGISTRY_ONLY = "registry-only-lane"
TABLE_PROVIDER = "table-provider"


def _write_table(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _synthetic_root(tmp_path):
    """A repo-shaped tree whose providers.jsonl lacks the models-only lane."""
    root = tmp_path / "repo"
    tables = root / "data" / "tables"
    _write_table(str(tables / "providers.jsonl"),
                 [{"id": TABLE_PROVIDER}])
    _write_table(str(tables / "models.jsonl"), [
        {"id": "m/one", "provider": TABLE_PROVIDER},
        {"id": "m/two", "provider": REGISTRY_ONLY},
    ])
    return root


def test_model_only_provider_lands_in_the_bootstrap_sample(tmp_path):
    root = _synthetic_root(tmp_path)
    state = tmp_path / "state"
    res = sd.bootstrap_quota_state(str(state), repo_root=str(root))
    assert res["written"] is True, res
    doc = json.load(open(os.path.join(str(state), sd.QUOTA_STATE_NAME),
                         encoding="utf-8"))
    providers = doc["providers"]
    assert TABLE_PROVIDER in providers
    assert REGISTRY_ONLY in providers, (
        "a provider named only by models.jsonl is routable, so the starter "
        f"policy plane must carry a row for it; got {sorted(providers)}")
    assert res["providers"] == len(providers)


def test_committed_tables_seed_deepseek_duckbrain_sync(tmp_path):
    """The real repo tables, the real defect lane: hop 2 of a fresh resolve."""
    state = tmp_path / "state-real"
    res = sd.bootstrap_quota_state(str(state), repo_root=REPO)
    assert res["written"] is True, res
    doc = json.load(open(os.path.join(str(state), sd.QUOTA_STATE_NAME),
                         encoding="utf-8"))
    assert "deepseek-duckbrain-sync" in doc["providers"], (
        "the committed models table names deepseek-duckbrain-sync; a fresh "
        "bootstrap that omits it re-creates the policy-gate-missing-row "
        "exclusion for hop 2")


def test_explicit_provider_list_still_wins_verbatim(tmp_path):
    """TR-345 contract: an authoritative caller list is used as given — the
    models-table union only applies when the caller supplied nothing."""
    root = _synthetic_root(tmp_path)
    state = tmp_path / "state-list"
    res = sd.bootstrap_quota_state(str(state), repo_root=str(root),
                                  provider_ids_list=["only-this"])
    assert res["written"] is True, res
    doc = json.load(open(os.path.join(str(state), sd.QUOTA_STATE_NAME),
                         encoding="utf-8"))
    assert list(doc["providers"]) == ["only-this"]


def test_existing_sample_is_never_rewritten(tmp_path):
    root = _synthetic_root(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    path = os.path.join(str(state), sd.QUOTA_STATE_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"updated": "live", "providers": {"table-provider": '
                 '{"status": "closed"}}}')
    res = sd.bootstrap_quota_state(str(state), repo_root=str(root))
    assert res["written"] is False and res["reason"] == "exists"
    assert json.load(open(path, encoding="utf-8"))["updated"] == "live"

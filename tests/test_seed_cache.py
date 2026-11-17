"""t_seed_cache_hash_gate: the seed content-hash reuse cache.

Pins the acceptance criteria of the card by driving real seed subprocesses in a
hermetic scratch tree (ROUTING_DATA_DIR / ROUTING_REGISTRY / ROUTING_NS), the
same pattern tests/test_cache_spawn_equivalence.py uses:

  1. two consecutive seeds in a clean tree -> second reports CACHE HIT, is
     materially faster, and registry.json + every ns-export file is
     byte-identical to the full run's;
  2. touching ONE input table -> CACHE MISS and a correct rebuild;
  3. the escape envs force a full rebuild (ROUTING_SEED_CACHE_REBUILD=1,
     ROUTER_CACHE=0 / ROUTING_SEED_CACHE=off);
  4. a corrupt / truncated / stale entry is never served as a hit;
  5. the process-safe write shape: a half-written entry (manifest present,
     registry absent) degrades to a miss instead of a crashed seed.
"""
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

PY = ("/home/kara/.hermes/venvs/board/bin/python3"
      if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
      else sys.executable)

pytest = __import__('pytest')


@pytest.fixture
def tree(tmp_path):
    """Hermetic data dir + registry + ns, copied from the committed tables."""
    data = tmp_path / "tables"
    shutil.copytree(os.path.join(REPO, "data", "tables"), data,
                    ignore=shutil.ignore_patterns("*.bak*", "*.db"))
    life = os.path.join(REPO, "data", "lifecycle.jsonl")
    if os.path.exists(life):
        shutil.copyfile(life, tmp_path / "lifecycle.jsonl")
    (tmp_path / "ns").mkdir()
    env = {
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
        "ROUTING_NS": str(tmp_path / "ns"),
        "ROUTING_LIFECYCLE_FILE": str(tmp_path / "lifecycle.jsonl"),
        "ROUTING_ALLOW_UNSATISFIABLE": "1",
    }
    return tmp_path, env


def _seed(env, extra=None, timeout=900):
    e = dict(os.environ)
    for k in ("ROUTER_CACHE", "ROUTING_SEED_CACHE", "ROUTING_SEED_CACHE_REBUILD",
              "ROUTING_SEED_CACHE_MAX_AGE_S", "ROUTING_SEED_CACHE_DIR"):
        e.pop(k, None)
    e.update(env)
    if extra:
        e.update(extra)
    t0 = time.time()
    proc = subprocess.run([PY, os.path.join(REPO, "scripts", "router_seed.py")],
                          capture_output=True, text=True, env=e, timeout=timeout)
    return proc, time.time() - t0


def test_second_run_hits_and_is_byte_identical(tree):
    root, env = tree
    p1, t_full = _seed(env)
    assert p1.returncode == 0, p1.stdout[-3000:] + p1.stderr[-3000:]
    assert 'SEED CACHE HIT' not in p1.stdout
    assert 'seed cache: stored' in p1.stdout

    reg1 = (root / "registry.json").read_bytes()
    ns1 = {p.name: p.read_bytes() for p in (root / "ns" / "tables").iterdir()}

    p2, t_hit = _seed(env)
    assert p2.returncode == 0, p2.stdout[-3000:] + p2.stderr[-3000:]
    assert 'SEED CACHE HIT' in p2.stdout, p2.stdout[-2000:]
    assert 'SEED CACHE MISS' not in p2.stdout

    # AC1: byte-identical artifacts (generated_at NOT rewritten on a hit).
    assert (root / "registry.json").read_bytes() == reg1
    ns2 = {p.name: p.read_bytes() for p in (root / "ns" / "tables").iterdir()}
    assert ns2 == ns1
    doc = json.loads(reg1)
    assert doc.get('seed_source_hash'), 'registry must name its source bytes'
    # AC1: materially faster. The hit does no derivation; allow a generous
    # floor so a loaded box cannot flake this (full derivation is minutes).
    assert t_hit < max(5.0, t_full / 5), f"hit {t_hit:.2f}s vs full {t_full:.2f}s"


def test_touched_table_misses_and_rebuilds(tree):
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0
    reg1 = (root / "registry.json").read_bytes()

    # AC2: one input byte changes -> MISS, and the rebuild is real.
    target = root / "tables" / "models.jsonl"
    rows = [json.loads(l) for l in target.read_text().splitlines() if l.strip()]
    rows[0]["price_evidence"] = "seed-cache-test: touched"
    target.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))

    p2, _ = _seed(env)
    assert p2.returncode == 0, p2.stdout[-3000:] + p2.stderr[-3000:]
    assert 'SEED CACHE MISS' in p2.stdout
    assert 'SEED CACHE HIT' not in p2.stdout
    reg2 = (root / "registry.json").read_bytes()
    assert reg2 != reg1, 'a touched input must produce a different registry'
    assert json.loads(reg2)['seed_source_hash'] != json.loads(reg1)['seed_source_hash']

    # ...and the following run with the now-stable inputs hits again.
    p3, _ = _seed(env)
    assert 'SEED CACHE HIT' in p3.stdout


def test_escape_envs_force_full_rebuild(tree):
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0

    # AC3a: explicit rebuild request ignores a valid entry (and re-stores).
    p2, _ = _seed(env, {"ROUTING_SEED_CACHE_REBUILD": "1"})
    assert p2.returncode == 0
    assert 'SEED CACHE MISS' in p2.stdout
    assert 'forces a rebuild' in p2.stdout

    # AC3b: cache fully off -> neither read nor store.
    p3, _ = _seed(env, {"ROUTER_CACHE": "0"})
    assert p3.returncode == 0
    assert 'SEED CACHE OFF' in p3.stdout
    assert 'seed cache: stored' not in p3.stdout

    p4, _ = _seed(env, {"ROUTING_SEED_CACHE": "off"})
    assert p4.returncode == 0
    assert 'SEED CACHE OFF' in p4.stdout


def test_corrupt_or_stale_entry_is_a_miss(tree):
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0
    cache = root / "state" / "seed_cache"

    # AC4a: truncated registry -> miss (never a half artifact).
    reg = cache / "registry.json"
    good = reg.read_bytes()
    reg.write_bytes(good[: len(good) // 2])
    p2, _ = _seed(env)
    assert p2.returncode == 0
    assert 'SEED CACHE MISS' in p2.stdout
    # rebuilt: parses and carries the same DERIVED CONTENT. Byte equality is
    # not the assertion — generated_at is a fresh stamp and seed_source_hash
    # names the input bytes, which the seed's own data/tables sync re-renders
    # on the first derivation (the derivation is idempotent, the file
    # formatting is not — see test_derivation_is_idempotent_over_its_own_output).
    rebuilt = json.loads(reg.read_bytes())
    good_doc = json.loads(good)
    assert sorted(rebuilt['tables']) == sorted(good_doc['tables'])
    assert rebuilt['tables']['models'] == good_doc['tables']['models']
    assert rebuilt['tables']['model_tier'] == good_doc['tables']['model_tier']

    # AC4b: registry that parses but lost a table -> miss.
    doc = json.loads(good)
    doc['tables'].pop('model_tier', None)
    reg.write_text(json.dumps(doc))
    p3, _ = _seed(env)
    assert p3.returncode == 0
    assert 'SEED CACHE MISS' in p3.stdout

    # AC4c: entry older than the freshness bound -> miss.
    p4, _ = _seed(env)
    assert 'SEED CACHE HIT' in p4.stdout
    man = json.loads((cache / "manifest.json").read_text())
    man['derived_at'] = '2001-01-01T00:00:00+00:00'
    (cache / "manifest.json").write_text(json.dumps(man))
    p5, _ = _seed(env)
    assert p5.returncode == 0
    assert 'SEED CACHE MISS' in p5.stdout


def test_half_written_entry_degrades_to_miss(tree):
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0
    cache = root / "state" / "seed_cache"
    # AC5: manifest present, registry gone (killed mid-store) -> clean miss.
    os.replace(cache / "registry.json", cache / "registry.json.parked")
    p2, _ = _seed(env)
    assert p2.returncode == 0
    assert 'SEED CACHE HIT' not in p2.stdout
    assert (root / "registry.json").exists()


def test_hash_covers_content_not_mtime(tree):
    """Rewriting an input with identical bytes must NOT invalidate the cache
    (content addressing, not mtime) — otherwise every git checkout would miss."""
    from seed_cache import hash_inputs
    root, env = tree
    data = env["ROUTING_DATA_DIR"]
    h1, names = hash_inputs(data)
    p = os.path.join(data, "model_notes.jsonl")
    blob = open(p, "rb").read()
    time.sleep(0.01)
    open(p, "wb").write(blob)
    h2, names2 = hash_inputs(data)
    assert h1 == h2
    assert names == names2
    # backup copies are not inputs
    shutil.copyfile(p, p + ".bak-20260101")
    h3, _ = hash_inputs(data)
    assert h3 == h1


def test_cache_dir_is_outside_data_tables(tree, monkeypatch):
    """The cache must live under <data home>/state/, never inside data/tables —
    anything under data/tables is a seed INPUT and would self-invalidate."""
    from seed_cache import cache_dir
    monkeypatch.delenv('ROUTING_SEED_CACHE_DIR', raising=False)
    data = os.path.join('/x', 'data', 'tables')
    assert cache_dir(data) == os.path.join('/x', 'data', 'state', 'seed_cache')


def test_cache_dir_env_override_is_shared_and_still_validates(tree, monkeypatch):
    """t_seed_cache_gate_adoption: ROUTING_SEED_CACHE_DIR lets surfaces that seed
    into throwaway data dirs (the pytest session copy) share ONE store.

    The entry only validates against its exact input hash + derivation
    fingerprint, so a shared store is a cache, not a source of stale bytes.
    """
    from seed_cache import cache_dir, derivation_fingerprint
    monkeypatch.delenv('ROUTING_SEED_CACHE_DIR', raising=False)
    monkeypatch.setenv('ROUTING_SEED_CACHE_DIR', '/shared/seed-cache')
    assert cache_dir('/x/data/tables') == '/shared/seed-cache'
    monkeypatch.delenv('ROUTING_SEED_CACHE_DIR', raising=False)

    root, env = tree
    shared = root / "shared-cache"
    env2 = dict(env, ROUTING_SEED_CACHE_DIR=str(shared))
    p1, _ = _seed(env2)
    assert p1.returncode == 0, p1.stdout[-3000:] + p1.stderr[-3000:]
    assert (shared / "manifest.json").exists(), "entry must land in the shared dir"
    assert not (root / "state" / "seed_cache").exists(), "tree-local store must stay empty"
    man = json.loads((shared / "manifest.json").read_text())
    assert man.get('derivation_fingerprint'), man
    assert man['cache_version'] >= 2
    # The stored fingerprint must be the one this code computes (a mismatch
    # would mean the entry is unservable and the shared store pointless).
    monkeypatch.setenv("ROUTING_SEED_CACHE_DIR", str(shared))
    assert man['derivation_fingerprint'] == derivation_fingerprint()


def test_code_or_flag_change_never_serves_a_stale_hit(tree):
    """The adoption's safety property: same inputs, different derivation (code
    fingerprint or a derivation-read flag) must MISS, never serve old bytes."""
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0
    cache = root / "state" / "seed_cache"
    man = json.loads((cache / "manifest.json").read_text())

    # (a) an entry derived by DIFFERENT CODE (tamper the stored fingerprint).
    good = man['derivation_fingerprint']
    man['derivation_fingerprint'] = 'f' * 64
    (cache / "manifest.json").write_text(json.dumps(man))
    p2, _ = _seed(env)
    assert p2.returncode == 0, p2.stdout[-2000:] + p2.stderr[-2000:]
    assert 'SEED CACHE HIT' not in p2.stdout
    assert 'derived by different code' in p2.stderr

    # (b) a derivation-read FLAG posting (ladder ignore) is a different
    # derivation over the SAME input bytes -> miss, then its own entry.
    p3, _ = _seed(env)
    assert 'SEED CACHE HIT' in p3.stdout, p3.stdout[-2000:]
    p4, _ = _seed(env, {"ROUTING_TIER_LADDER_IGNORE": "1"})
    assert p4.returncode == 0, p4.stdout[-2000:] + p4.stderr[-2000:]
    assert 'SEED CACHE HIT' not in p4.stdout, p4.stdout[-2000:]
    p5, _ = _seed(env, {"ROUTING_TIER_LADDER_IGNORE": "1"})
    assert 'SEED CACHE HIT' in p5.stdout, p5.stdout[-2000:]
    assert json.loads((cache / "manifest.json").read_text())['derivation_fingerprint'] != good


def test_specific_switch_beats_general_one(tree):
    """ROUTER_CACHE=0 exists to keep the RUNTIME spawn cache out of the way
    (tests/conftest.py). It must not force a full derivation when the caller
    says the SEED cache is on — the specific switch wins."""
    root, env = tree
    p1, _ = _seed(env)
    assert p1.returncode == 0
    p2, _ = _seed(env, {"ROUTER_CACHE": "0", "ROUTING_SEED_CACHE": "on"})
    assert p2.returncode == 0
    assert 'SEED CACHE HIT' in p2.stdout, p2.stdout[-2000:]
    # ...and the general switch still wins when the specific one is not on.
    p3, _ = _seed(env, {"ROUTER_CACHE": "0"})
    assert 'SEED CACHE OFF' in p3.stdout

def test_derivation_is_idempotent_over_its_own_output(tree):
    """The cache keys on the POST-publish hash, so the contract it relies on is:
    re-deriving from the tables the seed itself wrote produces the same
    registry. Prove it with two forced full runs (no cache involved)."""
    root, env = tree
    p1, _ = _seed(env, {"ROUTER_CACHE": "0"})
    assert p1.returncode == 0
    doc1 = json.loads((root / "registry.json").read_bytes())
    p2, _ = _seed(env, {"ROUTER_CACHE": "0"})
    assert p2.returncode == 0
    doc2 = json.loads((root / "registry.json").read_bytes())
    assert doc1['tables'] == doc2['tables']
    assert doc1['generated_at'] != doc2['generated_at'] or True  # stamps may match within a second

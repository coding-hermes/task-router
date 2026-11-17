#!/usr/bin/env python3
"""Content-hash reuse cache for router_seed.py (card t_seed_cache_hash_gate).

Every seed spawn pays the full ~6s CPU re-derivation (duckdb UNPIVOT of
models.jsonl + the 918-rating satisfiability pass + an 8.6MB registry rewrite)
even when NOTHING under data/tables/ changed — measured at ~4 seed spawns per
worker card x 6 cards per 20-minute run. This module keys the derived outputs
on a deterministic content hash of the seed's INPUT tables, so a repeat spawn
with identical inputs copies the previous run's registry verbatim instead of
re-deriving it.

Design rules (house data doctrine):
  * The hash covers CONTENT, never mtimes: sorted *.jsonl directly under
    DATA_DIR (backup copies excluded) plus the lifecycle overlay the seed
    reads from DATA_DIR's parent.
  * A cache entry is: registry.json (byte-for-byte the artifact the graded
    gate published), tables/*.jsonl (the staged per-table export), and a
    manifest.json carrying the source hash, column order and provenance.
  * manifest.json is written LAST and is the commit point: a reader that
    cannot read a complete, hash-matching manifest treats the cache as absent.
  * Every write is temp-file + os.replace (process safe: two seeds racing
    produce the same bytes; a reader never observes a half-written entry).
  * generated_at is NOT rewritten on a hit — the cached artifact stays
    byte-identical across consecutive runs. Traceability rides on
    registry.json's `seed_source_hash` (stamped at derivation time) plus the
    manifest's derived_at.

Escape hatches (all documented in the module and printed by the seed):
  ROUTER_CACHE=0 | ROUTING_SEED_CACHE=0|off   -> no read AND no store
  ROUTING_SEED_CACHE=1|on|true|yes            -> cache ON even when
                                                 ROUTER_CACHE=0 (the specific
                                                 switch beats the general one)
  ROUTING_SEED_CACHE_REBUILD=1                -> force a full rebuild (stores)
  ROUTING_SEED_CACHE_MAX_AGE_S=<seconds>      -> freshness bound (default 30d)
  ROUTING_SEED_CACHE_DIR=<path>               -> where entries live (default
                                                 <data home>/state/seed_cache)

Adoption (card t_seed_cache_gate_adoption): the entry is keyed on the INPUT
bytes AND on a derivation fingerprint (the seed's own source modules + the
duckdb version), so a shared cache directory is safe — a code change that
alters derivation invalidates every entry instead of serving a stale registry.
That is what lets a test session point ROUTING_SEED_CACHE_DIR at one stable
directory while every test seeds into its own temp data dir.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sys

CACHE_DIRNAME = 'seed_cache'
MANIFEST = 'manifest.json'
REGISTRY = 'registry.json'
TABLES_DIR = 'tables'

# Tables the seed always emits into registry.json['tables'] (ALL_TABLES in
# router_seed.py plus the two text sidecars). A cached registry missing any of
# them is a truncated artifact, never a hit.
REQUIRED_TABLES = [
    'providers', 'models', 'archetypes', 'benchmarks', 'projects',
    'level_defs', 'category_levels', 'model_perf', 'model_tier',
    'task_profiles', 'task_profile_requirements', 'cost_effort',
    'fallback_lanes', 'unmeasured_categories',
]

DEFAULT_MAX_AGE_S = 30 * 24 * 3600  # 30 days

#: cache schema version. Bumped when the entry layout changes; an entry from
#: another version can never validate (no manifest match), so a bump is the
#: cheap invalidation switch.
CACHE_VERSION = 2

#: Source modules whose bytes are part of the DERIVATION identity. A change to
#: any of them can change the registry produced from unchanged inputs, so the
#: entry must not survive it. Listed explicitly (not `scripts/*.py`) so an edit
#: to an unrelated tool does not throw the cache away.
DERIVATION_MODULES = (
    'router_seed.py',
    'seed_cache.py',
    'capability_vocab.py',
    'lifecycle_gate.py',
    'tier_cost_cube.py',
    'registry_path.py',
    'router_satisfiability.py',
)

_HERE = os.path.dirname(os.path.realpath(__file__))

#: Env inputs the seed's DERIVATION reads. Same inputs + different flag = a
#: different registry (or no registry at all), so an entry derived under one
#: posting must not be served under another.
DERIVATION_ENV = (
    'ROUTING_TIER_LADDER_IGNORE',
    'ROUTING_ALLOW_UNSATISFIABLE',
)


def _env_off() -> bool:
    specific = os.environ.get('ROUTING_SEED_CACHE', '').strip().lower()
    if specific in ('1', 'on', 'true', 'yes'):
        # The SPECIFIC switch beats the general one. ROUTER_CACHE=0 exists to
        # keep the runtime spawn cache out of the way (tests/conftest.py sets
        # it for an import-lock deadlock), and that must not force a full
        # derivation on every seed spawn.
        return False
    if os.environ.get('ROUTER_CACHE') == '0':
        return True
    return specific in ('0', 'off', 'false', 'no')


def disabled() -> bool:
    """True when the caller asked for the cache to stay out of the way."""
    return _env_off()


def force_rebuild() -> bool:
    return os.environ.get('ROUTING_SEED_CACHE_REBUILD', '') not in ('', '0')


def max_age_s() -> int:
    try:
        return int(os.environ.get('ROUTING_SEED_CACHE_MAX_AGE_S', DEFAULT_MAX_AGE_S))
    except ValueError:
        return DEFAULT_MAX_AGE_S


def _overlay_path(data_dir: str) -> str:
    """The lifecycle overlay the seed reads: DATA_DIR's parent lifecycle.jsonl
    (env override ROUTING_LIFECYCLE_FILE wins, same as the seed)."""
    env = os.environ.get('ROUTING_LIFECYCLE_FILE')
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.normpath(data_dir)), 'lifecycle.jsonl')


def hash_inputs(data_dir: str) -> tuple[str, list[str]]:
    """Deterministic content hash over the seed's input tables.

    Inputs: every regular `*.jsonl` directly under DATA_DIR except backup
    copies (`*.jsonl.bak*`, `*.bak`, `*.orig`, `*.tmp`), plus the lifecycle
    overlay when it exists. Sorted by name; each file contributes
    `<name>\\0<size>\\0<bytes>`. Returns (hexdigest, sorted file names).
    """
    names: list[str] = []
    try:
        names = sorted(n for n in os.listdir(data_dir)
                       if n.endswith('.jsonl')
                       and not n.endswith('.bak')
                       and '.bak-' not in n
                       and not n.endswith('.tmp')
                       and os.path.isfile(os.path.join(data_dir, n)))
    except FileNotFoundError:
        names = []
    overlay = _overlay_path(data_dir)
    h = hashlib.sha256()
    for n in names:
        p = os.path.join(data_dir, n)
        with open(p, 'rb') as f:
            blob = f.read()
        h.update(n.encode() + b'\x00' + str(len(blob)).encode() + b'\x00')
        h.update(blob)
    if os.path.isfile(overlay):
        with open(overlay, 'rb') as f:
            blob = f.read()
        h.update(b'@overlay:' + os.path.basename(overlay).encode() + b'\x00')
        h.update(str(len(blob)).encode() + b'\x00')
        h.update(blob)
    return h.hexdigest(), names


def cache_dir(data_dir: str) -> str:
    """Where entries live: `ROUTING_SEED_CACHE_DIR` when set, else
    `<data home>/state/seed_cache` — STATE_DIR's sibling of DATA_DIR (the
    seed's own `_STATE_DIR` convention: dirname(DATA_DIR)/state).

    The override exists so surfaces that seed into a THROWAWAY data dir (the
    test suite copies data/tables into a tempdir per session) can still share
    one content-addressed store. Sharing is safe because an entry only
    validates against an exact input hash + derivation fingerprint.
    """
    override = os.environ.get('ROUTING_SEED_CACHE_DIR', '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.dirname(os.path.normpath(data_dir)), 'state', CACHE_DIRNAME)


def derivation_fingerprint() -> str:
    """Identity of the DERIVATION, not of its inputs.

    sha256 over the cache schema version, the bytes of every module that can
    change what the seed derives, the duckdb version (its UNPIVOT/datetime
    rendering is part of the output bytes), and the derivation-read env flags.
    An entry derived by different code must never be served: inputs unchanged +
    code changed is exactly the case where a stale registry would look like a
    green run.
    """
    h = hashlib.sha256()
    h.update(f'cache_version={CACHE_VERSION}\x00'.encode())
    for name in DERIVATION_MODULES:
        path = os.path.join(_HERE, name)
        try:
            with open(path, 'rb') as f:
                blob = f.read()
        except OSError:
            h.update(name.encode() + b'\x00absent\x00')
            continue
        h.update(name.encode() + b'\x00' + str(len(blob)).encode() + b'\x00')
        h.update(blob)
    try:
        import duckdb  # noqa: F401
        h.update(b'duckdb=' + str(duckdb.__version__).encode())
    except Exception:
        h.update(b'duckdb=absent')
    for key in DERIVATION_ENV:
        h.update(b'env:' + key.encode() + b'=' +
                 (os.environ.get(key) or '<unset>').encode() + b'\x00')
    return h.hexdigest()


def _atomic_write(path: str, data: bytes) -> None:
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_manifest(cdir: str) -> dict | None:
    try:
        with open(os.path.join(cdir, MANIFEST)) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def load(data_dir: str) -> tuple[dict, dict] | None:
    """Return (manifest, registry_doc) for a VALID cache entry, else None.

    Valid = manifest readable, source hash matches the live inputs, entry
    within the freshness bound, registry parses, every required table present.
    """
    current, _names = hash_inputs(data_dir)
    cdir = cache_dir(data_dir)
    m = _read_manifest(cdir)
    if not m or m.get('source_hash') != current:
        return None
    if m.get('cache_version') != CACHE_VERSION:
        print(f'seed cache: entry rejected — cache_version {m.get("cache_version")!r} '
              f'!= {CACHE_VERSION}', file=sys.stderr)
        return None
    fingerprint = derivation_fingerprint()
    if m.get('derivation_fingerprint') != fingerprint:
        print('seed cache: entry rejected — derived by different code '
              f'(fingerprint {str(m.get("derivation_fingerprint"))[:12]} != '
              f'{fingerprint[:12]})', file=sys.stderr)
        return None
    derived_at = m.get('derived_at')
    if not derived_at:
        return None
    try:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(derived_at)).total_seconds()
    except ValueError:
        return None
    if age < 0 or age > max_age_s():
        return None
    try:
        with open(os.path.join(cdir, REGISTRY)) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    tables = doc.get('tables') if isinstance(doc, dict) else None
    if not isinstance(tables, dict):
        return None
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        print(f'seed cache: entry rejected — registry is missing table(s) {missing}',
              file=sys.stderr)
        return None
    return m, doc


NS_TABLES = ['level_defs', 'model_perf', 'category_levels', 'model_tier',
             'task_profiles', 'task_profile_requirements']


def _ns_bytes_from_staged(cdir: str, table: str, columns: dict) -> bytes:
    """Fallback ns-export bytes when the entry stored none (the deriving run
    had no ns mirror). Rebuilds the array-per-line shape from the staged
    keyed rows in manifest column order."""
    src = os.path.join(cdir, TABLES_DIR, f'{table}.jsonl')
    cols = columns.get(table) or []
    out = []
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.append([rec.get(c) for c in cols] if cols else list(rec.values()))
    return b''.join(json.dumps(r, default=str).encode() + b'\n' for r in out)


def publish_hit(data_dir: str, registry_path: str, ns: str, ns_redirected: bool,
                manifest: dict) -> None:
    """Publish a cache hit exactly as _publish_run would: ns mirror, then
    registry.json. data/tables is NOT rewritten — by construction its bytes are
    the inputs this hash covers. The ns mirror is replayed from the bytes the
    deriving run wrote (stored under ns_tables/), so a hit is byte-identical to
    a full run including duckdb's datetime rendering."""
    cdir = cache_dir(data_dir)
    columns = manifest.get('columns') or {}
    if os.path.isdir(ns) or ns_redirected:
        os.makedirs(f'{ns}/tables', exist_ok=True)
        for t in NS_TABLES:
            stored = os.path.join(cdir, 'ns_tables', f'{t}.jsonl')
            if os.path.isfile(stored):
                blob = open(stored, 'rb').read()
            else:
                blob = _ns_bytes_from_staged(cdir, t, columns)
            _atomic_write(f'{ns}/tables/{t}.jsonl', blob)
        print('exported tables to', ns)
    else:
        print('ns mirror absent — skipped ns export (fresh clone is fine)')
    os.makedirs(os.path.dirname(os.path.abspath(registry_path)), exist_ok=True)
    _atomic_write(registry_path, open(os.path.join(cdir, REGISTRY), 'rb').read())


def store(data_dir: str, doc: dict, staged_registry: str, staged_tables: str,
          ns_bytes: dict | None = None) -> str:
    """Persist this run's outputs as the cache entry for the CURRENT inputs.

    The hash is recomputed here: the seed republishes data/tables from the
    registry it just built, so the hash that a following run will see is the
    POST-publish one. Returns the stored hash.
    """
    if disabled():
        # ROUTER_CACHE=0 / ROUTING_SEED_CACHE=off means the cache stays out of
        # the way in BOTH directions: a disabled run must not leave an entry a
        # later run would trust.
        return ''
    cdir = cache_dir(data_dir)
    os.makedirs(os.path.join(cdir, TABLES_DIR), exist_ok=True)
    post_hash, _names = hash_inputs(data_dir)
    # tables/*.jsonl first (they are the bulk), registry, then the manifest.
    for fn in sorted(os.listdir(staged_tables)):
        _atomic_write(os.path.join(cdir, TABLES_DIR, fn),
                      open(os.path.join(staged_tables, fn), 'rb').read())
    _atomic_write(os.path.join(cdir, REGISTRY), open(staged_registry, 'rb').read())
    if ns_bytes:
        os.makedirs(os.path.join(cdir, 'ns_tables'), exist_ok=True)
        for t, blob in ns_bytes.items():
            _atomic_write(os.path.join(cdir, 'ns_tables', f'{t}.jsonl'), blob)
    columns = {}
    for t, rows in (doc.get('tables') or {}).items():
        if rows and isinstance(rows[0], dict):
            columns[t] = list(rows[0].keys())
    manifest = {
        'cache_version': CACHE_VERSION,
        'source_hash': post_hash,
        'derivation_fingerprint': derivation_fingerprint(),
        'registry_seed_source_hash': doc.get('seed_source_hash'),
        'derived_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'registry_generated_at': doc.get('generated_at'),
        'tables': sorted((doc.get('tables') or {}).keys()),
        'columns': columns,
        'input_files': _names,
    }
    _atomic_write(os.path.join(cdir, MANIFEST),
                  (json.dumps(manifest, indent=1, sort_keys=True) + '\n').encode())
    return post_hash


def try_hit(data_dir: str, registry_path: str, ns: str, ns_redirected: bool) -> bool:
    """Cache lookup + publish. True means: the caller should stop (hit).

    Prints a loud one-line verdict either way — a silent cache is how a stale
    artifact ships unnoticed.
    """
    if disabled():
        print('SEED CACHE OFF (ROUTER_CACHE=0 / ROUTING_SEED_CACHE=off) — full derivation')
        return False
    current, names = hash_inputs(data_dir)
    if force_rebuild():
        print(f'SEED CACHE MISS {current[:12]} — ROUTING_SEED_CACHE_REBUILD=1 forces a rebuild '
              f'({len(names)} input tables)')
        return False
    entry = load(data_dir)
    if entry is None:
        print(f'SEED CACHE MISS {current[:12]} — no reusable entry '
              f'({len(names)} input tables); deriving')
        return False
    manifest, doc = entry
    publish_hit(data_dir, registry_path, ns, ns_redirected, manifest)
    print(f'SEED CACHE HIT {current[:12]} — reused registry derived {manifest.get("derived_at")} '
          f'({len(names)} input tables unchanged); set ROUTER_CACHE=0 to force a full rebuild')
    return True

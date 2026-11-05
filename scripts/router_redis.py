#!/usr/bin/env python3
"""router_redis.py — the Redis read plane for the routing registry (TR-388).

Design authority: docs/specs/SPEC-REDIS-READ-PLANE.md (v0.5, R1-R80).
JSONL stays the source of truth (R4); this module only ever READS data/tables and
registry.json and WRITES derived keys under one data-home scope.

Verbs
  load    [--if-stale] [--force]   parse JSONL -> publish a generation -> CAS the pointer
  status  [--json] [--diff] [--detail N --table T] [--verbose]
                                   JSONL-vs-artifact-vs-cache comparison, exit 0..6
  get     model|tier|category|profile|project|provider|levels|fallback <args...>
  flush   --yes                    delete only this scope's keys

States (R11) and exit codes (R14)
  MATCH 0 | STALE 1 | EMPTY 2 | UNREACHABLE 3 | GHOST 4 | FOREIGN 5 | SCHEMA-AHEAD 6
  (UNSTAMPED is reported as a degraded artifact leg: 0 when JSONL==cache, else 1)

Stdlib only (R39): a minimal RESP2 client over sockets. No third-party redis package.

Environment
  ROUTING_REDIS            redis|file|0  (unset => file, R78: the default is CODE-level)
  ROUTING_REDIS_URL        default redis://127.0.0.1:6391/0
  ROUTING_REDIS_PORT       default 6391 (the only port accepted unless allowlisted)
  ROUTING_REDIS_ALLOW_PORT comma list of additional accepted ports
  ROUTING_REDIS_PASSWORD_FILE  default ~/.hermes/secrets/router-redis.pass
  ROUTING_REDIS_TIMEOUT_MS default 20 (read path) / 2000 (load, status)
  ROUTING_REDIS_MAX_AGE_S  default 900
  ROUTING_REDIS_SCOPE_TTL_S default 604800
  ROUTING_REDIS_PUBLISH_STALE_S default 120
  ROUTING_REDIS_GEN_KEEP_S default 900
  ROUTING_REDIS_ALLOW_SCRATCH 1 to publish from a data dir outside the repo
  ROUTING_DATA_DIR         table dir (same precedence as router_spawn)
  ROUTING_REGISTRY         registry path override (shared resolver)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import socket
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:  # shared resolver; a stale live byte-copy is the only reason this fails
    import registry_path as _registry_path_mod
except ImportError:  # pragma: no cover - fresh checkout before sync_runtime
    _registry_path_mod = None

DATA_DIR = os.environ.get('ROUTING_DATA_DIR') or os.path.join(_REPO, 'data', 'tables')
if _registry_path_mod is not None:
    REGISTRY = _registry_path_mod.resolve_registry_path(repo_root=_REPO)
else:  # pragma: no cover
    REGISTRY = os.environ.get('ROUTING_REGISTRY') or os.path.join(_REPO, 'registry.json')

STATE_DIR = os.path.join(os.path.dirname(DATA_DIR), 'state')
INSTANCE_FILE = os.path.join(STATE_DIR, 'redis-instance.json')

# R9: the cached table set is EXACTLY these twelve. fallback_lanes is a sidecar
# the seed folds into the artifact's tables dict — it is inside the twelve and
# must not be counted twice.
CACHED_TABLES = (
    'providers', 'models', 'archetypes', 'benchmarks', 'projects', 'level_defs',
    'category_levels', 'model_perf', 'model_tier', 'task_profiles',
    'task_profile_requirements', 'fallback_lanes',
)

SCHEMA = 1
OWNER_ID = 'task-router/SPEC-REDIS-READ-PLANE'
DENY_PORTS = {6379, 6380, 16379}
MODEL_COLUMNS = (
    'provider', 'model', 'normalized_price', 'price_evidence', 'public_price',
    'public_in_per_m', 'public_out_per_m', 'public_cache_read_per_m',
    'public_cache_write_per_m', 'data_class', 'plan_tier', 'perf_agent_tick',
    'perf_long_doc', 'perf_debug', 'perf_schema', 'perf_e2e_vision', 'perf_review',
    'perf_delegation', 'perf_guard', 'perf_mock', 'perf_reasoning', 'valid_from',
    'available_from', 'valid_to', 'archive', 'release_date', 'lifecycle_source',
    'lifecycle_checked_at', 'replaced_by', 'token_factor', 'disabled',
    'disabled_reason', 'context_limit', 'api_type', 'vision', 'thinking',
    'training_model_level', 'training_provider_level', 'note',
)


# --------------------------------------------------------------------------- utils
def current_data_dir() -> str:
    """Resolve at CALL time: the CLI and tests set env after import."""
    return os.environ.get('ROUTING_DATA_DIR') or DATA_DIR


def current_registry() -> str:
    if _registry_path_mod is not None:
        try:
            return _registry_path_mod.resolve_registry_path(repo_root=_REPO)
        except Exception:  # noqa: BLE001
            pass
    return os.environ.get('ROUTING_REGISTRY') or REGISTRY


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:  # noqa: BLE001 - a bad override must not break a resolve
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except Exception:  # noqa: BLE001
        return default


def _now() -> int:
    return int(time.time())


def _iso(ts: Any) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).isoformat(timespec='seconds')
    except Exception:  # noqa: BLE001
        return '?'


def git_toplevel(path: str) -> str | None:
    cur = os.path.realpath(path)
    while True:
        if os.path.isdir(os.path.join(cur, '.git')) or os.path.isfile(os.path.join(cur, '.git')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def checkout_id() -> dict:
    top = git_toplevel(DATA_DIR) or ''
    head = None
    if top:
        try:
            import subprocess
            head = subprocess.run(['git', '-C', top, 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception:  # noqa: BLE001
            head = None
    return {'toplevel': top, 'head': head}


def scope_id(data_dir: str | None = None, registry: str | None = None) -> str:
    """R8: the data-home scope hashes BOTH resolved paths, not just the table dir."""
    dd = os.path.realpath(data_dir or current_data_dir())
    rg = os.path.realpath(registry or current_registry())
    return hashlib.sha256(f'{dd}|{rg}'.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ stamps (R9)
def _table_lines(path: str) -> list:
    out = []
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(line)
    return out


def table_fingerprint(path: str) -> tuple:
    """(sha256, count) over the sorted line LIST — multiplicity preserved.

    A sorted *set* would collapse duplicate lines; model_notes.jsonl already
    carries duplicates, so a multiplicity-blind hash is a demonstrated miss class.
    """
    lines = _table_lines(path)
    digest = hashlib.sha256('\n'.join(sorted(lines)).encode()).hexdigest()
    return digest, len(lines)


def jsonl_stamp(data_dir: str | None = None, registry: str | None = None) -> dict:
    """The reference stamp, computed from the committed JSONL (never cached)."""
    dd = data_dir or current_data_dir()
    tables, counts = {}, {}
    for name in CACHED_TABLES:
        path = os.path.join(dd, f'{name}.jsonl')
        if not os.path.exists(path):
            tables[name] = None
            counts[name] = 0
            continue
        tables[name], counts[name] = table_fingerprint(path)
    aggregate = hashlib.sha256('\n'.join(
        f'{name}:{tables[name]}:{counts[name]}' for name in sorted(tables)).encode()).hexdigest()
    present = set()
    try:
        present = {f[:-len('.jsonl')] for f in os.listdir(dd) if f.endswith('.jsonl')}
    except OSError:
        present = set()
    return {
        'jsonl_hash': aggregate,
        'tables': tables,
        'row_counts': counts,
        'not_cached': sorted(present - set(CACHED_TABLES)),
        'schema': SCHEMA,
        'generation': None,
        'created_at': _now(),
        'data_home': os.path.realpath(dd),
        'registry_path': os.path.realpath(registry or REGISTRY),
        'checkout': checkout_id(),
    }


def stamp_equality(a: Any, b: Any) -> tuple:
    """R9: equality is over {tables, row_counts, schema} — not_cached is reported only."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False, ['stamp-missing']
    drift = []
    if a.get('schema') != b.get('schema'):
        drift.append('schema')
    ta, tb = a.get('tables') or {}, b.get('tables') or {}
    for name in sorted(set(ta) | set(tb)):
        if ta.get(name) != tb.get(name):
            drift.append(name)
    ca, cb = a.get('row_counts') or {}, b.get('row_counts') or {}
    for name in sorted(set(ca) | set(cb)):
        if ca.get(name) != cb.get(name) and name not in drift:
            drift.append(name)
    return (not drift), drift


def artifact_stamp(registry: str | None = None) -> dict | None:
    """The stamp the seed wrote into registry.json, or None when absent (today)."""
    try:
        with open(registry or REGISTRY, 'r', encoding='utf-8') as fh:
            doc = json.load(fh)
        stamp = doc.get('stamp') if isinstance(doc, dict) else None
        return stamp if isinstance(stamp, dict) else None
    except Exception:  # noqa: BLE001 - absence is a state, not an error
        return None


# ----------------------------------------------------------------- rows / keys
def _j(v: Any) -> str:
    """Values round-trip exactly through JSON text (null vs '' vs 0 vs false)."""
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def read_table(name: str, data_dir: str | None = None) -> list:
    path = os.path.join(data_dir or DATA_DIR, f'{name}.jsonl')
    rows = []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    return rows


def load_tables(data_dir: str | None = None) -> dict:
    return {name: read_table(name, data_dir) for name in CACHED_TABLES}


def tier_index(rows: list) -> dict:
    """(model, category) -> row, from model_tier.jsonl (no provider column)."""
    out = {}
    for row in rows:
        model, cat = row.get('model'), row.get('category')
        if model is None or cat is None:
            continue
        out[(model, cat)] = row
    return out


def build_category_values(tables: dict) -> dict:
    """R34: cat:<category> field value = the whole decision tuple for a model.

    Joins model_tier's tier with the models row columns the chain build filters or
    sorts on, so one HGETALL per required category feeds the entire chain build and
    the reader never issues a per-model read.
    """
    models = {}
    for row in tables.get('models') or []:
        prov, model = row.get('provider'), row.get('model')
        if prov is None or model is None:
            continue
        models[f'{prov}/{model}'] = row
    out: dict = {}
    for row in tables.get('model_tier') or []:
        model, cat = row.get('model'), row.get('category')
        if model is None or cat is None:
            continue
        mrow = models.get(model) or {}
        out.setdefault(cat, {})[model] = {
            'level': row.get('tier'),
            'perf': row.get('perf'),
            'tier_source': row.get('tier_source'),
            'plan_tier': mrow.get('plan_tier'),
            'normalized_price': mrow.get('normalized_price'),
            'token_factor': mrow.get('token_factor'),
            'context_limit': mrow.get('context_limit'),
            'archive': mrow.get('archive'),
            'valid_to': mrow.get('valid_to'),
            'disabled': mrow.get('disabled'),
        }
    return out


# R15: the diff/parity primary key per table. One declaration, used by publish
# (to store a full-row audit copy) and by reconstruction (so a diff compares
# like with like and never reports a projection mismatch as "changed").
ROW_KEYS = {
    'models': ('provider', 'model'), 'providers': ('id',), 'archetypes': ('id',),
    'benchmarks': ('model', 'category', 'source'), 'level_defs': ('level',),
    'model_perf': ('model', 'category'), 'model_tier': ('model', 'category'),
    'category_levels': ('category', 'level'), 'task_profiles': ('id',),
    'task_profile_requirements': ('task_id', 'category'), 'projects': ('id',),
    'fallback_lanes': ('order',),
}


def row_pk(name: str, row: dict) -> str:
    cols = ROW_KEYS.get(name, ())
    if not cols:
        return hashlib.sha256(_j(row).encode()).hexdigest()[:32]
    return '|'.join('' if row.get(c) is None else str(row.get(c)) for c in cols)


# ------------------------------------------------------------ RESP2 client (R39)
class RedisError(Exception):
    pass


class RedisConn:
    """Minimal RESP2 client. One socket; `commands` counts round trips."""

    def __init__(self, url: str | None = None, timeout_ms: int | None = None):
        self.host, self.port, self.db, self.url_password = self._parse_url(
            url or os.environ.get('ROUTING_REDIS_URL') or 'redis://127.0.0.1:6391/0')
        self.timeout = (timeout_ms if timeout_ms is not None else 2000) / 1000.0
        self.sock = None
        self.commands = 0
        self._buf = b''

    @staticmethod
    def _parse_url(url: str) -> tuple:
        url = url.strip()
        if '://' not in url:
            url = 'redis://' + url
        scheme, rest = url.split('://', 1)
        password = None
        if '@' in rest:
            creds, rest = rest.rsplit('@', 1)
            if ':' in creds:
                password = creds.split(':', 1)[1] or None
        hostport, _, dbpart = rest.partition('/')
        host, _, port = hostport.partition(':')
        db = int(dbpart or 0) if dbpart.strip().isdigit() else 0
        return host or '127.0.0.1', int(port or 6379), db, password

    # -- guardrails (R43/R44) ------------------------------------------------
    def assert_allowed(self) -> None:
        allowed = {_env_int('ROUTING_REDIS_PORT', 6391)}
        extra = os.environ.get('ROUTING_REDIS_ALLOW_PORT') or ''
        for chunk in extra.replace(';', ',').split(','):
            chunk = chunk.strip()
            if chunk.isdigit():
                allowed.add(int(chunk))
        if self.port in DENY_PORTS:
            raise RedisError(f'port {self.port} is a product instance (denylist '
                             '6379/6380/16379) — refusing to touch it')
        if self.port not in allowed:
            raise RedisError(f'port {self.port} is not allowlisted '
                             f'(ROUTING_REDIS_PORT={sorted(allowed)}); set ROUTING_REDIS_ALLOW_PORT '
                             'if this is really the task-router plane')

    def password(self) -> str | None:
        if self.url_password:
            return self.url_password
        path = os.path.expanduser(os.environ.get(
            'ROUTING_REDIS_PASSWORD_FILE', '~/.hermes/secrets/router-redis.pass'))
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    # -- transport -----------------------------------------------------------
    def connect(self) -> 'RedisConn':
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._buf = b''
        pw = self.password()
        if not pw:
            raise RedisError('no AUTH password configured (ROUTING_REDIS_PASSWORD_FILE) — '
                             'R43 requires AUTH')
        self.cmd('AUTH', pw)
        if self.db:
            self.cmd('SELECT', self.db)
        return self

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        finally:
            self.sock = None

    def __enter__(self) -> 'RedisConn':
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _encode(args: tuple) -> bytes:
        out = [b'*%d\r\n' % len(args)]
        for arg in args:
            data = arg if isinstance(arg, bytes) else str(arg).encode()
            out.append(b'$%d\r\n%s\r\n' % (len(data), data))
        return b''.join(out)

    def _read_line(self) -> bytes:
        while b'\r\n' not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RedisError('connection closed by server')
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b'\r\n')
        return line

    def _read_reply(self) -> Any:
        line = self._read_line()
        kind, rest = line[:1], line[1:]
        if kind == b'+':
            return rest.decode(errors='replace')
        if kind == b'-':
            raise RedisError(rest.decode(errors='replace'))
        if kind == b':':
            return int(rest)
        if kind == b'$':
            size = int(rest)
            if size == -1:
                return None
            while len(self._buf) < size + 2:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise RedisError('connection closed mid-bulk')
                self._buf += chunk
            data, self._buf = self._buf[:size], self._buf[size + 2:]
            return data.decode(errors='replace')
        if kind == b'*':
            count = int(rest)
            if count == -1:
                return None
            return [self._read_reply() for _ in range(count)]
        raise RedisError(f'unsupported RESP reply: {line[:40]!r}')

    def cmd(self, *args: Any) -> Any:
        if self.sock is None:
            self.connect()
        self.sock.sendall(self._encode(args))
        self.commands += 1
        return self._read_reply()

    def pipeline(self, batches: list) -> list:
        """Send every command in ONE round trip, then read all replies."""
        if self.sock is None:
            self.connect()
        payload = b''.join(self._encode(tuple(b)) for b in batches)
        self.sock.sendall(payload)
        self.commands += 1
        return [self._read_reply() for _ in batches]

    def run_multi(self, cmds: list) -> list:
        self.cmd('MULTI')
        for c in cmds:
            self.cmd(*c)
        return self.cmd('EXEC')


# --------------------------------------------------------------- publish (R20)
def _key(scope: str, suffix: str) -> str:
    return f'{scope}:{suffix}'


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def publish(tables: dict, stamp: dict, scope: str, conn: RedisConn,
            stale_s: int | None = None) -> dict:
    """Write one generation, then CAS the pointer. Returns a receipt dict."""
    stale_s = stale_s if stale_s is not None else _env_int('ROUTING_REDIS_PUBLISH_STALE_S', 120)
    pressure = memory_pressure(conn)
    if pressure >= 0.95:
        # R20 fail-loud: never publish into a full instance and let half the keys
        # land. Readers keep the previous generation (R6/R21).
        record_publish_error(scope, None, f'memory-pressure {pressure:.2f} of maxmemory')
        return {'published': False, 'reason': 'memory-pressure', 'pressure': pressure,
                'gen': None, 'keys_written': 0}
    gen = int(conn.cmd('INCR', _key(scope, 'meta:generation')))
    intent = {'gen': gen, 'started_at': _now(), 'pid': os.getpid(), 'host': socket.gethostname()}
    conn.cmd('SET', _key(scope, 'meta:publishing'), _j(intent))
    conn.cmd('SET', _key(scope, 'meta:owner'), _j({'owner': OWNER_ID, 'schema': SCHEMA}))

    prefix = _key(scope, f'g{gen}')
    written = 0

    def stage(cmds: list) -> None:
        nonlocal written
        for batch in _chunks(cmds, 400):
            conn.pipeline(batch)
            written += len(batch)

    # per-table meta (hash + count) — cheap, one key each
    stage([['SET', f'{prefix}:table:{t}', _j({'hash': stamp['tables'].get(t),
                                              'rows': stamp['row_counts'].get(t)})]
           for t in CACHED_TABLES])

    # models / providers / projects / profiles / leveldefs / levels / fallback
    model_cmds = []
    for row in tables.get('models') or []:
        key = f"{row.get('provider')}/{row.get('model')}"
        fields = []
        for col in MODEL_COLUMNS:
            fields += [col, _j(row.get(col))]
        model_cmds.append(['HSET', f'{prefix}:model:{key}'] + fields)
    stage(model_cmds)

    stage([['HSET', f"{prefix}:provider:{row.get('id')}"] +
           [x for col in ('id', 'plan', 'quota_unit', 'windows', 'concurrency', 'tos_class',
                          'data_class', 'trains_on_hosted', 'valid_from', 'valid_to',
                          'archive', 'api_base_url', 'api_key_env')
            for x in (col, _j(row.get(col)))]
           for row in (tables.get('providers') or []) if row.get('id')])

    stage([['HSET', f"{prefix}:project:{row.get('id')}"] +
           [x for col in ('id', 'sensitivity', 'board_type', 'profile')
            for x in (col, _j(row.get(col)))]
           for row in (tables.get('projects') or []) if row.get('id')])

    prof_cmds, req_cmds = [], []
    for row in tables.get('task_profiles') or []:
        pid = row.get('id')
        if not pid:
            continue
        prof_cmds.append(['HSET', f'{prefix}:profile:{pid}'] +
                         [x for col in ('id', 'title', 'version', 'tag', 'allow_slow',
                                        'max_consecutive_per_provider', 'max_total_per_provider')
                          for x in (col, _j(row.get(col)))])
    for row in tables.get('task_profile_requirements') or []:
        tid, cat = row.get('task_id'), row.get('category')
        if tid and cat:
            req_cmds.append(['HSET', f'{prefix}:profile:{tid}:req', cat, _j(row.get('level'))])
    stage(prof_cmds + req_cmds)

    stage([['HSET', f"{prefix}:leveldef:{row.get('level')}", 'lo', _j(row.get('lo')),
            'hi', _j(row.get('hi'))]
           for row in (tables.get('level_defs') or []) if row.get('level') is not None])

    # category thresholds (levels:<cat>) + model->tier (cat:<cat>) + per-model index
    level_fields: dict = {}
    for row in tables.get('category_levels') or []:
        cat, level = row.get('category'), row.get('level')
        if cat is None or level is None:
            continue
        level_fields.setdefault(cat, []).extend([str(level), _j({k: row.get(k) for k in
                                       ('level', 'threshold', 'pct', 'score')})])
    stage([['HSET', f'{prefix}:levels:{cat}'] + fields for cat, fields in level_fields.items()])

    cat_values = build_category_values(tables)
    cat_cmds, tier_cmds = [], {}
    for cat, model_map in cat_values.items():
        cat_cmds.append(['HSET', f'{prefix}:cat:{cat}'] +
                        [x for model, payload in model_map.items() for x in (model, _j(payload))])
    for row in tables.get('model_tier') or []:
        model, cat = row.get('model'), row.get('category')
        if model and cat:
            tier_cmds.setdefault(model, []).extend([cat, _j(row)])
    stage(cat_cmds)
    stage([['HSET', f'{prefix}:tiers:{model}'] + fields for model, fields in tier_cmds.items()])

    stage([['HSET', f'{prefix}:fallback', str(row.get('order')), _j(
        {'provider': row.get('provider'), 'model': row.get('model'),
         'key_env': row.get('key_env')})]
        for row in (tables.get('fallback_lanes') or [])])

    # R16/R49: a full-row audit copy per table, so --diff and the parity test
    # compare full rows for ALL twelve tables (not just the ones with a fast
    # lookup key). Lookup keys above stay the hot path.
    # models and model_tier are already stored in full under model:/tiers:
    # duplicating them here cost 2x the biggest share of a generation (measured
    # 16.5 MiB/generation), so those two reconstruct from their lookup keys.
    row_cmds = []
    for name in CACHED_TABLES:
        if name in ('models', 'model_tier'):
            continue
        for row in tables.get(name) or []:
            if not isinstance(row, dict):
                continue
            fields = []
            for col, value in row.items():
                fields += [str(col), _j(value)]
            if fields:
                row_cmds.append(['HSET', f'{prefix}:row:{name}:{row_pk(name, row)}'] + fields)
    stage(row_cmds)

    stage([['SET', f'{prefix}:__ts', _now()],
           ['EXPIRE', _key(scope, 'meta:heartbeat'),
            _env_int('ROUTING_REDIS_SCOPE_TTL_S', 604800)]])

    # ---- CAS the pointer: WATCH is per-connection, so this conn must be the one
    pointer = _key(scope, 'meta:version')
    stamp = dict(stamp, generation=gen, created_at=_now())
    conn.cmd('WATCH', pointer)
    current = conn.cmd('GET', pointer)
    current_gen = 0
    if current:
        try:
            current_gen = int((json.loads(current) or {}).get('generation') or 0)
        except Exception:  # noqa: BLE001 - a corrupt pointer is replaced, never trusted
            current_gen = 0
    if current_gen > gen:
        conn.cmd('UNWATCH')
        _drop_generation(conn, scope, gen)
        conn.cmd('DEL', _key(scope, 'meta:publishing'))
        return {'published': False, 'reason': 'pointer-regression', 'gen': gen,
                'current_gen': current_gen, 'keys_written': written}
    conn.run_multi([['SET', pointer, _j(stamp)]])
    conn.cmd('DEL', _key(scope, 'meta:publishing'))
    reaped = reap(conn, scope, stamp)
    return {'published': True, 'gen': gen, 'keys_written': written,
            'stamp': stamp, 'orphans_reaped': reaped}


def _drop_generation(conn: RedisConn, scope: str, gen: int) -> int:
    return _drop_prefix(conn, f'{scope}:g{gen}:')


def _drop_prefix(conn: RedisConn, prefix: str) -> int:
    removed, cursor = 0, '0'
    while True:
        cursor, keys = conn.cmd('SCAN', cursor, 'MATCH', prefix + '*', 'COUNT', 1000)
        keys = [k for k in (keys or []) if isinstance(k, str)]
        for batch in _chunks(keys, 200):
            if batch:
                removed += int(conn.cmd('DEL', *batch) or 0)
        if str(cursor) in ('0', ''):
            return removed


def reap(conn: RedisConn, scope: str, pointer_stamp: dict,
         stale_s: int | None = None) -> int:
    """R20 step 6 / R76: drop old generations and crashed-publish orphans."""
    stale_s = stale_s if stale_s is not None else _env_int('ROUTING_REDIS_PUBLISH_STALE_S', 120)
    keep_s = _env_int('ROUTING_REDIS_GEN_KEEP_S', 900)
    now = _now()
    gens: dict = {}
    cursor = '0'
    while True:
        cursor, keys = conn.cmd('SCAN', cursor, 'MATCH', f'{scope}:g*:__ts', 'COUNT', 1000)
        for key in keys or []:
            try:
                gen = int(str(key).split(f'{scope}:g', 1)[1].split(':', 1)[0])
            except Exception:  # noqa: BLE001
                continue
            try:
                gens[gen] = int(conn.cmd('GET', key) or 0)
            except Exception:  # noqa: BLE001
                gens[gen] = 0
        if str(cursor) in ('0', ''):
            break
    current = int(pointer_stamp.get('generation') or 0)
    removed = 0
    # Count cap first: a generation measured 16.5 MiB on the live tables, so an
    # age-only rule lets a dev/test loop fill a 256 MiB noeviction instance and
    # start refusing writes. Keep at most MAX_GENERATIONS, then apply the age rule.
    keep_count = max(1, _env_int('ROUTING_REDIS_MAX_GENERATIONS', 4))
    live_gens = [g for g in sorted(gens) if g <= current]
    keep = set(live_gens[-keep_count:]) | {current}
    for gen, ts in sorted(gens.items()):
        if gen in keep:
            continue
        too_old = (now - ts) > keep_s
        orphan = gen > current and (now - ts) > stale_s
        if too_old or orphan or gen < (current - keep_count):
            removed += _drop_generation(conn, scope, gen)
    return removed


def memory_pressure(conn: RedisConn) -> float:
    """used_memory / maxmemory (0.0 when maxmemory is unset)."""
    used, limit = 0, 0
    for line in str(conn.cmd('INFO', 'memory') or '').splitlines():
        if line.startswith('used_memory:'):
            used = int(line.split(':', 1)[1])
        elif line.startswith('maxmemory:'):
            limit = int(line.split(':', 1)[1])
    return (used / limit) if limit else 0.0


def publish_errors_path() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, 'redis-publish-errors.jsonl')


def record_publish_error(scope: str, gen: Any, reason: str, tables: Any = None,
                         orphans: Any = None) -> None:
    rec = {'ts': _iso(_now()), 'scope': scope, 'gen': gen, 'reason': reason,
           'tables': tables, 'orphans': orphans}
    try:
        with open(publish_errors_path(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


# ------------------------------------------------------------------ status (R11)
def _connect_for(verb_timeout: int) -> RedisConn:
    conn = RedisConn(timeout_ms=verb_timeout)
    conn.assert_allowed()
    return conn.connect()


def instance_fingerprint(conn: RedisConn) -> dict:
    info = conn.cmd('INFO', 'server') or ''
    run_id, version = None, None
    for line in str(info).splitlines():
        if line.startswith('run_id:'):
            run_id = line.split(':', 1)[1].strip()
        elif line.startswith('redis_version:'):
            version = line.split(':', 1)[1].strip()
    return {'run_id': run_id, 'redis_version': version, 'port': conn.port,
            'host': conn.host, 'checked_at': _now()}


def check_instance(conn: RedisConn, scope: str, *, record: bool = True) -> tuple:
    """(state_or_None, fingerprint, stored_fingerprint). FOREIGN when a different
    instance holds keys but not our owner key; a restart (same owner key, new
    run_id) is EMPTY-and-reload, not a refusal."""
    fp = instance_fingerprint(conn)
    stored = None
    try:
        with open(INSTANCE_FILE, 'r', encoding='utf-8') as fh:
            stored = json.load(fh)
    except Exception:  # noqa: BLE001
        stored = None
    owner = conn.cmd('GET', _key(scope, 'meta:owner'))
    has_keys = int(conn.cmd('DBSIZE') or 0) > 0
    verdict = None
    if stored and stored.get('run_id') and fp.get('run_id') and stored['run_id'] != fp['run_id']:
        if has_keys and not owner:
            verdict = 'FOREIGN'
    if record and verdict != 'FOREIGN':
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(INSTANCE_FILE, 'w', encoding='utf-8') as fh:
                json.dump(dict(fp, scope=scope, owner_seen=bool(owner)), fh)
        except OSError:
            pass
    return verdict, fp, stored


def write_health(ok: bool, scope: str, fp: Any = None, note: Any = None) -> None:
    """R28: written by status/load/timer only — never by the read path."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, 'redis-health.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'ok': bool(ok), 'checked_at': _now(), 'scope': scope,
                       'run_id': (fp or {}).get('run_id'), 'note': note}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def status(data_dir: str | None = None, registry: str | None = None,
           diff: bool = False, detail: int = 0, table: str | None = None) -> dict:
    dd = data_dir or current_data_dir()
    rg = registry or current_registry()
    scope = scope_id(dd, rg)
    reference = jsonl_stamp(dd, rg)
    result: dict = {'scope': scope, 'state': None, 'exit': 1, 'jsonl': reference,
                    'artifact': None, 'cache': None, 'drift_class': None,
                    'drifted_tables': [], 'diff': None, 'ages': {},
                    'orphans': None, 'scopes_live': None, 'dbsize': None,
                    'used_memory_human': None, 'run_id': None}
    artifact = artifact_stamp(rg)
    result['artifact'] = artifact

    try:
        conn = _connect_for(2000)
    except Exception as exc:  # noqa: BLE001 - unreachable is a state
        write_health(False, scope, note=f'{type(exc).__name__}: {exc}')
        result.update(state='UNREACHABLE', exit=3, message=str(exc))
        return result

    try:
        foreign, fp, stored = check_instance(conn, scope)
        result['run_id'] = fp.get('run_id')
        info = conn.cmd('INFO', 'memory') or ''
        for line in str(info).splitlines():
            if line.startswith('used_memory_human:'):
                result['used_memory_human'] = line.split(':', 1)[1].strip()
        result['dbsize'] = int(conn.cmd('DBSIZE') or 0)
        try:
            result['scopes_live'] = len([k for k in (
                conn.cmd('SCAN', '0', 'MATCH', '*:meta:version', 'COUNT', 1000) or [None, []])[1]])
        except Exception:  # noqa: BLE001
            result['scopes_live'] = None
        if foreign == 'FOREIGN':
            result.update(state='FOREIGN', exit=5,
                          message='a different Redis instance holds keys without our owner key')
            write_health(False, scope, fp, note='FOREIGN')
            return result

        raw = conn.cmd('GET', _key(scope, 'meta:version'))
        if not raw:
            result.update(state='EMPTY', exit=2, message='no pointer for this scope')
            write_health(True, scope, fp, note='EMPTY')
            return result
        try:
            cache = json.loads(raw)
        except Exception:  # noqa: BLE001
            result.update(state='GHOST', exit=4, message='pointer is not valid JSON')
            write_health(False, scope, fp, note='corrupt-pointer')
            return result
        result['cache'] = cache
        if int(cache.get('schema') or 0) > SCHEMA:
            result.update(state='SCHEMA-AHEAD', exit=6,
                          message=f"cache schema {cache.get('schema')} > reader {SCHEMA}")
            write_health(False, scope, fp, note='SCHEMA-AHEAD')
            return result
        gen = int(cache.get('generation') or 0)
        if not conn.cmd('EXISTS', _key(scope, f'g{gen}:__ts')):
            result.update(state='GHOST', exit=4,
                          message=f'pointer names generation {gen} whose keys are gone')
            write_health(False, scope, fp, note='GHOST')
            return result

        age = _now() - int(cache.get('created_at') or 0)
        result['ages'] = {'created_at_s': age, 'jsonl_now': _iso(reference['created_at']),
                          'max_age_s': _env_int('ROUTING_REDIS_MAX_AGE_S', 900)}

        same_cache, drift_cache = stamp_equality(reference, cache)
        if artifact is None:
            # No artifact stamp (today's registry.json): the artifact leg is
            # degraded, ALWAYS — the state names that, and the exit code only
            # reflects whether the reference and the cache agree (R11).
            result.update(state='UNSTAMPED',
                          exit=0 if same_cache else 1,
                          message='registry.json carries no stamp — artifact leg degraded',
                          drift_class=None if same_cache else 'cache-behind',
                          drifted_tables=[] if same_cache else drift_cache)
        else:
            same_artifact, drift_artifact = stamp_equality(reference, artifact)
            same_cache_artifact, _ = stamp_equality(artifact, cache)
            if same_cache and same_artifact:
                result.update(state='MATCH', exit=0)
            else:
                drift_class = 'cache-behind' if not same_cache else 'artifact-behind'
                if same_cache and same_artifact is False:
                    drift_class = 'artifact-behind'
                if artifact.get('created_at') and cache.get('created_at') and \
                        int(cache['created_at']) > int(reference['created_at']):
                    drift_class = 'cache-ahead'
                result.update(state='STALE', exit=1, drift_class=drift_class,
                              drifted_tables=sorted(set(drift_cache) | set(drift_artifact)))
        if diff:
            result['diff'] = diff_tables(reference, conn, scope, dd, detail=detail, table=table)
        result['orphans'] = count_orphans(conn, scope, cache)
        write_health(True, scope, fp, note=result['state'])
        return result
    finally:
        conn.close()


def count_orphans(conn: RedisConn, scope: str, pointer_stamp: dict) -> int:
    stale_s = _env_int('ROUTING_REDIS_PUBLISH_STALE_S', 120)
    now, count = _now(), 0
    current = int(pointer_stamp.get('generation') or 0)
    cursor = '0'
    while True:
        cursor, keys = conn.cmd('SCAN', cursor, 'MATCH', f'{scope}:g*:__ts', 'COUNT', 1000)
        for key in keys or []:
            try:
                gen = int(str(key).split(f'{scope}:g', 1)[1].split(':', 1)[0])
                ts = int(conn.cmd('GET', key) or 0)
            except Exception:  # noqa: BLE001
                continue
            if gen > current and (now - ts) > stale_s:
                count += 1
        if str(cursor) in ('0', ''):
            return count


def diff_tables(reference: dict, conn: RedisConn, scope: str, data_dir: str,
                detail: int = 0, table: str | None = None) -> dict:
    """R14-R17: +added −removed ~changed per cached table, full-row compares."""
    keys_by_table = {
        'models': ('provider', 'model'), 'providers': ('id',), 'archetypes': ('id',),
        'benchmarks': ('model', 'category', 'source'), 'level_defs': ('level',),
        'model_perf': ('model', 'category'), 'model_tier': ('model', 'category'),
        'category_levels': ('category', 'level'), 'task_profiles': ('id',),
        'task_profile_requirements': ('task_id', 'category'), 'projects': ('id',),
        'fallback_lanes': ('order',),
    }
    gen = int((json.loads(conn.cmd('GET', _key(scope, 'meta:version')) or '{}')
               or {}).get('generation') or 0)
    out = {}
    for name in ([table] if table else list(keys_by_table)):
        if name not in keys_by_table:
            continue
        local = read_table(name, data_dir)
        remote = rows_from_cache(conn, scope, gen, name)
        lmap = {_row_key(r, keys_by_table[name]): r for r in local}
        rmap = {_row_key(r, keys_by_table[name]): r for r in remote}
        added = sorted(set(lmap) - set(rmap))
        removed = sorted(set(rmap) - set(lmap))
        changed = sorted(k for k in set(lmap) & set(rmap) if lmap[k] != rmap[k])
        entry = {'added': len(added), 'removed': len(removed), 'changed': len(changed)}
        if detail and changed:
            entry['detail'] = [
                {'key': k, 'fields': _field_delta(rmap[k], lmap[k])} for k in changed[:detail]]
        out[name] = entry
    return out


def _row_key(row: dict, cols: tuple) -> str:
    return '|'.join(str(row.get(c)) for c in cols)


def _field_delta(before: dict, after: dict) -> dict:
    delta = {}
    for field in sorted(set(before) | set(after)):
        if before.get(field) != after.get(field):
            delta[field] = {'cache': before.get(field), 'jsonl': after.get(field)}
    return delta


def rows_from_cache(conn: RedisConn, scope: str, gen: int, name: str) -> list:
    """Reconstruct a table from its generation keys (R16 full-row compare).

    models -> model:* (all 39 columns), model_tier -> tiers:<model> (full row per
    category), everything else -> row:<table>:<pk>. No table is duplicated.
    """
    prefix = f'{scope}:g{gen}'
    rows: list = []
    pattern = (f'{prefix}:model:*' if name == 'models'
               else f'{prefix}:tiers:*' if name == 'model_tier'
               else f'{prefix}:row:{name}:*')
    cursor = '0'
    while True:
        cursor, keys = conn.cmd('SCAN', cursor, 'MATCH', pattern, 'COUNT', 500)
        keys = [k for k in keys or [] if isinstance(k, str)]
        if keys:
            payloads = conn.pipeline([['HGETALL', k] for k in keys])
            for key, flat in zip(keys, payloads):
                pairs = _hash_pairs(flat)
                if name == 'model_tier':
                    for cat, payload in pairs.items():
                        row = _un(payload)
                        if isinstance(row, dict):
                            rows.append(row)
                else:
                    row = {k: _un(v) for k, v in pairs.items()}
                    if row:
                        rows.append(row)
        if str(cursor) in ('0', ''):
            return rows


def _hash_pairs(flat: Any) -> dict:
    if not isinstance(flat, list):
        return {}
    out = {}
    for i in range(0, len(flat) - 1, 2):
        out[flat[i]] = flat[i + 1]
    return out


def _hash_to_row(flat: Any) -> dict:
    return {k: _un(v) for k, v in _hash_pairs(flat).items()}


def _un(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001 - a raw string is still a usable value
        return value


# ------------------------------------------------------------------- get (R33)
def get(kind: str, args: list, data_dir: str | None = None, registry: str | None = None) -> Any:
    scope = scope_id(data_dir or current_data_dir(), registry or current_registry())
    with _connect_for(2000) as conn:
        foreign, _fp, _stored = check_instance(conn, scope, record=False)
        if foreign == 'FOREIGN':
            raise RedisError('FOREIGN instance — refusing to read')
        raw = conn.cmd('GET', _key(scope, 'meta:version'))
        if not raw:
            raise RedisError('EMPTY — run `router_redis.py load` first')
        gen = int((json.loads(raw) or {}).get('generation') or 0)
        prefix = f'{scope}:g{gen}'
        if kind == 'model':
            flat = conn.cmd('HGETALL', f'{prefix}:model:{args[0]}')
            return _hash_to_row(flat) or None
        if kind == 'tier':
            flat = conn.cmd('HMGET', f'{prefix}:tiers:{args[0]}', args[1])
            return _un(flat[0]) if flat and flat[0] is not None else None
        if kind == 'category':
            flat = conn.cmd('HGETALL', f'{prefix}:cat:{args[0]}')
            return {k: _un(v) for k, v in _hash_pairs(flat).items()}
        if kind == 'profile':
            return {'profile': _hash_to_row(conn.cmd('HGETALL', f'{prefix}:profile:{args[0]}')),
                    'requirements': {k: _un(v) for k, v in _hash_pairs(
                        conn.cmd('HGETALL', f'{prefix}:profile:{args[0]}:req')).items()}}
        if kind == 'project':
            return _hash_to_row(conn.cmd('HGETALL', f'{prefix}:project:{args[0]}')) or None
        if kind == 'provider':
            return _hash_to_row(conn.cmd('HGETALL', f'{prefix}:provider:{args[0]}')) or None
        if kind == 'levels':
            return {k: _un(v) for k, v in _hash_pairs(
                conn.cmd('HGETALL', f'{prefix}:levels:{args[0]}')).items()}
        if kind == 'fallback':
            return {k: _un(v) for k, v in _hash_pairs(
                conn.cmd('HGETALL', f'{prefix}:fallback')).items()}
        raise RedisError(f'unknown lookup kind: {kind}')


# ----------------------------------------------------------------- verbs (R22)
def verb_load(data_dir: str | None = None, registry: str | None = None,
              if_stale: bool = False, force: bool = False) -> int:
    dd = data_dir or current_data_dir()
    rg = registry or current_registry()
    scope = scope_id(dd, rg)
    top = git_toplevel(dd)
    if top is None and os.environ.get('ROUTING_REDIS_ALLOW_SCRATCH') != '1':
        print('load: data dir is outside a git checkout (scratch) — set '
              'ROUTING_REDIS_ALLOW_SCRATCH=1 to publish a scratch scope', file=sys.stderr)
        return 5
    reference = jsonl_stamp(dd, rg)
    tables = load_tables(dd)
    try:
        conn = _connect_for(5000)
    except Exception as exc:  # noqa: BLE001
        write_health(False, scope, note=f'load: {type(exc).__name__}: {exc}')
        record_publish_error(scope, None, f'unreachable: {type(exc).__name__}: {exc}')
        print(f'load: UNREACHABLE: {exc}', file=sys.stderr)
        return 3
    try:
        foreign, fp, _stored = check_instance(conn, scope)
        if foreign == 'FOREIGN':
            record_publish_error(scope, None, 'FOREIGN instance')
            print('load: FOREIGN instance — refusing to write', file=sys.stderr)
            return 5
        if if_stale and not force:
            raw = conn.cmd('GET', _key(scope, 'meta:version'))
            if raw:
                try:
                    cache = json.loads(raw)
                except Exception:  # noqa: BLE001
                    cache = None
                if isinstance(cache, dict):
                    same, _drift = stamp_equality(reference, cache)
                    age = _now() - int(cache.get('created_at') or 0)
                    half = _env_int('ROUTING_REDIS_MAX_AGE_S', 900) // 2
                    if same and age <= half:
                        reaped = reap(conn, scope, cache)
                        write_health(True, scope, fp, note='fresh')
                        print(f'cache: {scope} g{cache.get("generation")} fresh '
                              f'(age {age}s) — nothing to publish; orphans reaped {reaped}')
                        return 0
        receipt = publish(tables, reference, scope, conn)
        conn.cmd('EXPIRE', _key(scope, 'meta:heartbeat'),
                 _env_int('ROUTING_REDIS_SCOPE_TTL_S', 604800))
        write_health(True, scope, fp, note='published' if receipt.get('published') else 'aborted')
        if not receipt.get('published'):
            record_publish_error(scope, receipt.get('gen'), receipt.get('reason'))
            print(f'load: ABORTED ({receipt.get("reason")}) pointer g{receipt.get("current_gen")} '
                  f'> own g{receipt.get("gen")} — readers untouched', file=sys.stderr)
            return 4
        stamp = receipt['stamp']
        print(f"cache: {scope} g{stamp['generation']} jsonl={stamp['jsonl_hash'][:12]} "
              f"tables={len(CACHED_TABLES)} keys={receipt['keys_written']} "
              f"orphans_reaped={receipt['orphans_reaped']}")
        return 0
    finally:
        conn.close()


def verb_status(args: argparse.Namespace) -> int:
    result = status(diff=args.diff, detail=args.detail, table=args.table)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
        return result['exit']
    state = result['state']
    if args.state_only:
        print(state)
        return result['exit']
    if state == 'MATCH' and not args.verbose:
        return result['exit']
    parts = [f'{state}']
    if result.get('drift_class'):
        parts.append(f'({result["drift_class"]})')
    if result.get('drifted_tables'):
        parts.append('drift=' + ','.join(result['drifted_tables'][:6]))
    if result.get('ages', {}).get('created_at_s') is not None:
        parts.append(f'age={result["ages"]["created_at_s"]}s')
    parts.append(f'scope={result["scope"]}')
    if result.get('orphans'):
        parts.append(f'orphans={result["orphans"]}')
    if result.get('message'):
        parts.append(result['message'])
    if args.diff and result.get('diff'):
        for name, entry in result['diff'].items():
            parts.append(f'{name}: +{entry["added"]} -{entry["removed"]} ~{entry["changed"]}')
    print('redis-status: ' + ' '.join(parts))
    if args.state_only:
        print(state)
    return result['exit']


def verb_flush(args: argparse.Namespace) -> int:
    scope = scope_id()
    if not args.yes:
        print('flush: --yes required', file=sys.stderr)
        return 2
    with _connect_for(5000) as conn:
        owner_raw = conn.cmd('GET', _key(scope, 'meta:owner'))
        if not owner_raw:
            print(f'flush: no owner key at {scope}:meta:owner — refusing', file=sys.stderr)
            return 5
        try:
            owner = json.loads(owner_raw)
        except Exception:  # noqa: BLE001
            owner = {}
        if owner.get('owner') != OWNER_ID:
            print(f'flush: owner {owner.get("owner")!r} is not {OWNER_ID!r} — refusing',
                  file=sys.stderr)
            return 5
        removed = 0
        for pattern in (f'{scope}:g*', f'{scope}:meta:*'):
            removed += _drop_prefix(conn, pattern[:-1] if pattern.endswith('*') else pattern)
        print(f'flush: removed {removed} key(s) under {scope}')
        return 0


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description='Redis read plane for the routing registry (TR-388)')
    sub = ap.add_subparsers(dest='verb', required=True)

    p_load = sub.add_parser('load', help='parse JSONL, publish a generation, CAS the pointer')
    p_load.add_argument('--if-stale', action='store_true')
    p_load.add_argument('--force', action='store_true')

    p_status = sub.add_parser('status', help='compare JSONL vs artifact vs cache')
    p_status.add_argument('--json', action='store_true')
    p_status.add_argument('--diff', action='store_true')
    p_status.add_argument('--detail', type=int, default=0)
    p_status.add_argument('--table', default=None)
    p_status.add_argument('--verbose', action='store_true')
    p_status.add_argument('--state-only', action='store_true')

    p_get = sub.add_parser('get', help='read one lookup from the cache')
    p_get.add_argument('kind', choices=['model', 'tier', 'category', 'profile', 'project',
                                        'provider', 'levels', 'fallback'])
    p_get.add_argument('args', nargs='*')

    p_flush = sub.add_parser('flush', help='delete only this scope')
    p_flush.add_argument('--yes', action='store_true')

    args = ap.parse_args(argv)
    if args.verb == 'load':
        return verb_load(if_stale=args.if_stale, force=args.force)
    if args.verb == 'status':
        return verb_status(args)
    if args.verb == 'flush':
        return verb_flush(args)
    if args.verb == 'get':
        if not args.args:
            print('get: a lookup key is required', file=sys.stderr)
            return 2
        try:
            value = get(args.kind, args.args)
        except RedisError as exc:
            print(f'get: {exc}', file=sys.stderr)
            return 3
        # A miss must NOT exit 0: `get` is the proof-verb a CI/cron reads, and a
        # silent `null` + rc=0 is indistinguishable from a successful hit (the
        # unrepresentable-failure class). 1 = not found, printed machine-readably.
        if value is None or (isinstance(value, dict) and not value):
            print(json.dumps({'kind': args.kind, 'args': list(args.args), 'found': False},
                             indent=1, sort_keys=True))
            return 1
        print(json.dumps(value, indent=1, sort_keys=True, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())

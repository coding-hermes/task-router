#!/usr/bin/env python3
"""TR-277: runtime data cache for registry.json + data/tables/*.jsonl.

Every CLI invocation and every server request used to re-read (and re-parse)
registry.json (4.4 MB) and the JSONL tables (6.4 MB) from disk. This module is
the read-once layer between the files and their consumers. The contract:

AC0  Entries are keyed by path and validated against a file identity of
     (mtime_ns, size, inode) — a replace-rename (atomic writer) or an in-place
     edit both invalidate. Every load bumps a process-wide generation counter
     and the swap is a single dict assignment under the cache lock: a reader
     sees generation N or N+1, never a partially-loaded file.
AC1  Invalidation is CHANGE-DRIVEN. When a watcher backend is importable
     (watchdog, else inotify_simple) directory events schedule a reload;
     a stat-poll thread runs BESIDE it as the safety net. There is NO TTL in
     this module: freshness is identity comparison per read, so a file that
     never changes is never re-parsed and a file that changes is never stale
     for longer than poll_interval_s. (Callers may keep their own short TTL
     memos ON TOP — that is a request-scoped optimization, not the
     invalidation mechanism.)
AC2  Reloads after a change happen OFF the caller's path: a single-flight
     worker pool (bounded) parses and installs; concurrent readers are served
     the previous generation immediately (meta.cache_stale +
     cache_reload_pending). The FIRST population of a path is synchronous and
     itself single-flight (a request cannot be served without data; N first
     requests parse once). Every served meta carries cache_generation and
     cache_age_s.
AC3  Bounded: at most one in-flight reload per path, a fixed worker ceiling,
     a per-file byte cap on the default loader, and a defined failure mode —
     a failed reload keeps the last good generation and records
     last_reload_error (+ meta.cache_error for the served entry). A reload
     NEVER empties a table.
AC4  stats() reports generation, loaded_at, reload_count, last_reload_ms,
     hits/misses (+ served_stale, reload_errors, inotify_mode, poll state).
     The server exposes it at /health and /status as `runtime_cache`.
AC5  tests/test_runtime_cache.py proves rewrite->next-request-serves-new and
     N-concurrent->exactly-ONE-reload by counting, never by timing.
AC6  Consumers: router_spawn._load_registry_with_meta (registry + tables),
     router_server._read_jsonl (per-request table reads) and the server's
     registry reads.

Fail-open is sacred: get() never raises for an I/O problem — failures come
back as data-with-error (never an empty table when a previous generation
exists) and callers keep their own fallback semantics.

Mutation contract (what makes sharing safe): entries are served SHARED (one
parse, zero copies). Consumers must treat returned rows as read-only.
`router_spawn` honours ROUTER_CACHE_COPY_MODE=deepcopy to hand every caller a
private deep copy instead — the defensive mode the equivalence test runs to
prove no current caller mutates (if that test goes red, someone mutated).
"""
import datetime
import json
import os
import threading
import time
from pathlib import Path

__all__ = ['DataCache', 'CacheEntry', 'file_identity', 'make_dir_watcher',
           'env_float', 'env_int', 'COPY_MODE_SHARE', 'COPY_MODE_DEEPCOPY']

COPY_MODE_SHARE = 'share'
COPY_MODE_DEEPCOPY = 'deepcopy'

#: Ceiling on bytes the DEFAULT loader will read for one file. Loader-carrying
#: consumers (spawn's two-arm registry loader) own their own budgets.
DEFAULT_MAX_BYTES_PER_FILE = 64 * 1024 * 1024


def env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return val if val >= 0 else float(default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def file_identity(path) -> tuple | None:
    """The AC0 identity triple, or None when the path does not stat."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


# ---------------------------------------------------------------------------
# AC1: the watcher backends. make_dir_watcher(callback) returns an object with
# watch_file(path) / start() / stop(), or raises ImportError when no backend
# exists — the cache then runs on the stat-poll fallback alone.
# ---------------------------------------------------------------------------

def _make_watchdog_watcher(callback):
    from watchdog.observers import Observer  # noqa: PLC0415 (optional backend)
    from watchdog.events import FileSystemEventHandler  # noqa: PLC0415

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if getattr(event, 'is_directory', False):
                return
            for attr in ('dest_path', 'src_path'):
                path = getattr(event, attr, None)
                if path:
                    callback(path)

    observer = Observer(timeout=0.2)

    class _Watcher:
        def __init__(self):
            self._dirs = set()

        def watch_file(self, path):
            directory = str(Path(path).parent)
            if directory not in self._dirs:
                self._dirs.add(directory)
                observer.schedule(_Handler(), directory, recursive=False)

        def start(self):
            observer.start()

        def stop(self):
            observer.stop()

    return _Watcher()


def _make_inotify_simple_watcher(callback):
    import select  # noqa: PLC0415 (stdlib, backend-local)
    import inotify_simple  # noqa: PLC0415 (optional backend)
    from inotify_simple import flags  # noqa: PLC0415

    mask = flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE | flags.DELETE

    class _Watcher:
        def __init__(self):
            self._inotify = inotify_simple.INotify()
            self._dirs = {}
            self._thread = None
            self._stop = threading.Event()

        def watch_file(self, path):
            directory = str(Path(path).parent)
            if directory not in self._dirs:
                wd = self._inotify.add_watch(directory, mask)
                self._dirs[wd] = directory

        def _loop(self):
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select([self._inotify.fd], [], [], 0.5)
                except (OSError, ValueError):
                    return
                if not readable:
                    continue
                try:
                    events = self._inotify.read(timeout=0)
                except OSError:
                    return
                for event in events:
                    directory = self._dirs.get(event.wd)
                    if directory and event.name:
                        callback(os.path.join(directory, event.name))

        def start(self):
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name='router-data-cache-inotify')
            self._thread.start()

        def stop(self):
            self._stop.set()

    return _Watcher()


def make_dir_watcher(callback):
    """Best-effort directory watcher: watchdog, then inotify_simple, else raise."""
    try:
        return _make_watchdog_watcher(callback)
    except ImportError:
        pass
    return _make_inotify_simple_watcher(callback)


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class CacheDisabledError(RuntimeError):
    """ROUTER_CACHE=0: the threaded singleton is refused.

    Kept as a public marker for callers that want to special-case disabled
    cache; get_cache() no longer raises it (d024cc1 broke 22 server tests:
    health/status surfaces call get_cache().stats() unguarded).
    """


def _plain_load(path):
    """Thread-free jsonl/json parse for _SynchronousCache (no byte cap)."""
    try:
        if path.suffix == '.jsonl':
            rows = []
            with path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            return rows, None
        if path.suffix == '.json':
            return json.load(path.open('r', encoding='utf-8')), None
        return None, f'unsupported suffix: {path.suffix}'
    except (OSError, json.JSONDecodeError) as exc:
        return None, f'load failed: {exc}'


class _SynchronousCache:
    """Thread-free stand-in used when ROUTER_CACHE=0.

    Same .get()/.stats() surface as DataCache, but every load is inline and
    memoized — zero daemon threads, so a later in-process seed exec cannot
    deadlock on the import lock (INT-CI-20261015-01). Serve-last-good and
    single-flight semantics are deliberately not replicated: a synchronous
    reader re-parses on identity change, which is exactly the no-cache
    contract's freshness.
    """

    def __init__(self):
        self._entries = {}

    def get(self, path, loader=None, identity_fn=None) -> tuple:
        path = Path(path)
        ident = (identity_fn(path) if identity_fn
                 else self._default_identity(path))
        entry = self._entries.get(path)
        if entry is not None and entry[0] == ident:
            return entry[1], {'cache_generation': entry[2], 'cache_hit': 'hit'}
        if loader is not None:
            data = loader()
            error = None
        else:
            data, error = _plain_load(path)
        if data is None:
            raise FileNotFoundError(f'cache disabled and load failed: {error}')
        gen = (self._entries.get(path) or (None, None, 0))[2] + 1
        self._entries[path] = (ident, data, gen)
        return data, {'cache_generation': gen, 'cache_hit': 'miss',
                      'cache_error': error}

    @staticmethod
    def _default_identity(path):
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)

    def stats(self) -> dict:
        return {'mode': 'synchronous (ROUTER_CACHE=0)',
                'entries': len(self._entries),
                'threads': 0}

    def shutdown(self) -> None:
        self._entries.clear()



class CacheEntry:
    """One installed generation of one path (immutable after install)."""

    __slots__ = ('path', 'data', 'generation', 'identity', 'loaded_at',
                 'loaded_at_iso', 'bytes', 'error')

    def __init__(self, path, data, generation, identity, loaded_at, nbytes, error=None):
        self.path = path
        self.data = data
        self.generation = generation
        self.identity = identity
        self.loaded_at = loaded_at
        self.loaded_at_iso = _utc_now_iso()
        self.bytes = nbytes
        self.error = error

    def age_s(self) -> float:
        return max(0.0, time.time() - self.loaded_at)


class DataCache:
    """See module docstring for the AC contract. One instance per consumer."""

    def __init__(self, source='default',
                 max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
                 stat_poll_interval_s: float | None = None,
                 copy_mode: str = COPY_MODE_SHARE,
                 max_workers: int = 2,
                 enable_watcher: bool = True,
                 enable_poller: bool = True,
                 first_load_wait_s: float = 10.0):
        self.source = source
        self._max_bytes = int(max_bytes_per_file)
        self._copy_mode = copy_mode if copy_mode in (COPY_MODE_SHARE, COPY_MODE_DEEPCOPY) \
            else COPY_MODE_SHARE
        self._poll_s = (env_float('ROUTER_CACHE_POLL_S', 1.0)
                        if stat_poll_interval_s is None else float(stat_poll_interval_s))
        self._first_load_wait_s = float(first_load_wait_s)
        self._max_workers = max(1, int(max_workers))

        self._cond = threading.Condition()
        self._entries = {}                                   # path -> CacheEntry
        self._pending: dict = {}                             # path -> (loader, identity_fn)
        self._inflight: set = set()                          # paths being parsed right now
        self._watched: set = set()                           # paths with identity tracked
        self._closed = False
        self._generation = 0

        self._stats = {'hits': 0, 'misses': 0, 'served_stale': 0,
                       'reload_count': 0, 'reload_errors': 0,
                       'last_reload_ms': 0.0, 'last_reload_error': None}

        # Loader bookkeeping so the generic poller can re-arm the RIGHT loader
        # for loader-carrying consumers (spawn's registry loader).
        self._loaders = {}                                   # str(path) -> (loader, identity_fn)

        self._watcher = None
        self._watcher_mode = 'none'
        if enable_watcher:
            try:
                self._watcher = make_dir_watcher(self._note_file_event)
                self._watcher_mode = type(self._watcher).__module__ or 'watcher'
                # Name the backend honestly for stats().
                try:
                    import watchdog.observers  # noqa: F401,PLC0415
                    self._watcher_mode = 'watchdog'
                except ImportError:
                    self._watcher_mode = 'inotify_simple'
                self._watcher.start()
            except ImportError:
                self._watcher = None
                self._watcher_mode = 'none'
            except Exception:  # noqa: BLE001 — a broken watcher degrades to poll-only
                self._watcher = None
                self._watcher_mode = 'none'

        self._workers = []
        for i in range(self._max_workers):
            thread = threading.Thread(target=self._worker_loop, daemon=True,
                                      name=f'router-data-cache-{source}-{i}')
            thread.start()
            self._workers.append(thread)

        self._poller = None
        if enable_poller and self._poll_s > 0:
            self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                            name=f'router-data-cache-poll-{source}')
            self._poller.start()

    # ------------------------------------------------------------------ get

    def get(self, path, loader=None, identity_fn=None) -> tuple:
        """Return (data, meta) for path. Never raises for an I/O problem.

        loader: () -> data  — parses the file (spawn's two-arm registry loader).
        identity_fn: (path) -> hashable — composite identity when validity
        spans several files; defaults to file_identity(path).

        The one deliberate SYNCHRONOUS window: a change detected on the caller's
        own read is re-parsed inline (bounded by the ~50ms parse, single-flight),
        so the very next request after a writer finishes serves the new content.
        Requests that merely overlap the change are served the previous
        generation while the background reload settles (AC2's off-path rule is
        about N-readers-N-reloads and request-path stalls, not about serving
        stale data one request longer than the caller's own stat proved).
        """
        path = Path(path)
        ident = self._identity(path, identity_fn)
        with self._cond:
            entry = self._entries.get(path)
            if entry is not None and entry.identity == ident:
                self._stats['hits'] += 1
                return self._serve(entry), self._meta(entry, hit='hit')
            if entry is not None:
                # AC2/AC3: single-flight — THIS caller re-parses (bounded,
                # inline, exactly once); concurrent readers of the same changed
                # state take the _inflight wait below and are served the new
                # generation without a second parse.
                if self._schedule_locked(path, loader, identity_fn):
                    self._inflight.add(path)
                    # fall through to the inline parse
                else:
                    # Someone is already parsing this path: wait for their
                    # generation instead of queueing a duplicate reload.
                    self._cond.wait_for(
                        lambda: path not in self._inflight,
                        timeout=self._first_load_wait_s)
                    fresh = self._entries.get(path)
                    if fresh is not None:
                        self._stats['misses'] += 1
                        return self._serve(fresh), self._meta(fresh, hit='miss')
                    raise RuntimeError('single-flight reload did not settle')
            # First population: synchronous, single-flight (a request cannot be
            # answered without data; N first requests parse exactly once).
            if path in self._inflight:
                self._cond.wait_for(
                    lambda: path not in self._inflight,
                    timeout=self._first_load_wait_s)
                entry = self._entries.get(path)
                if entry is not None:
                    self._stats['misses'] += 1
                    return self._serve(entry), self._meta(entry, hit='miss')
                # The in-flight load failed without installing anything: fall
                # through and attempt it here (the fail-open arm).
            self._inflight.add(path)
        try:
            return self._load_install(path, loader, identity_fn,
                                      background=False)
        finally:
            with self._cond:
                self._inflight.discard(path)
                self._cond.notify_all()

    # --------------------------------------------------------------- serving

    def _serve(self, entry):
        """AC0/mutation contract: shared by default, private copy in deepcopy mode."""
        if self._copy_mode == COPY_MODE_DEEPCOPY:
            import copy  # noqa: PLC0415 (defensive mode only)
            return copy.deepcopy(entry.data)
        return entry.data

    def _meta(self, entry, hit: str) -> dict:
        meta = {'cache_generation': entry.generation,
                'cache_age_s': round(entry.age_s(), 3),
                'cache_hit': hit,
                'cache_source': hit,  # compat alias (reload|cache|miss|stale)
                'cache_loaded_at': entry.loaded_at_iso,
                'cache_path': str(entry.path)}
        if entry.error:
            meta['cache_error'] = entry.error
        return meta

    def _identity(self, path, identity_fn):
        try:
            return identity_fn(path) if identity_fn else file_identity(path)
        except Exception:  # noqa: BLE001 — identity failure must not break a read
            return None

    # ---------------------------------------------------------------- loading

    def _default_load(self, path: Path) -> tuple:
        """Parse one .jsonl/.json file. Returns (data, identity, error, nbytes)."""
        try:
            st = os.stat(path)
        except OSError as exc:
            return None, None, f'load failed: {exc}', 0
        if st.st_size > self._max_bytes:
            return None, None, (f'file exceeds cache byte cap '
                                f'({st.st_size} > {self._max_bytes}); serving no data'), st.st_size
        try:
            if path.suffix == '.jsonl':
                rows = []
                with path.open('r', encoding='utf-8') as handle:
                    for line in handle:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
                return rows, (st.st_mtime_ns, st.st_size, st.st_ino), None, st.st_size
            if path.suffix == '.json':
                with path.open('r', encoding='utf-8') as handle:
                    return json.load(handle), (st.st_mtime_ns, st.st_size, st.st_ino), None, st.st_size
            with path.open('r', encoding='utf-8') as handle:
                return handle.read(), (st.st_mtime_ns, st.st_size, st.st_ino), None, st.st_size
        except Exception as exc:  # noqa: BLE001 — parse failures are a named error, not a crash
            return None, None, f'load failed: {type(exc).__name__}: {exc}', st.st_size

    def _load_install(self, path: Path, loader, identity_fn, background: bool) -> tuple:
        t0 = time.time()
        error = None
        nbytes = 0
        if loader is not None:
            try:
                data = loader()
            except Exception as exc:  # noqa: BLE001 — AC3 serve-last-good/empty-with-error
                data, error = None, f'loader failed: {type(exc).__name__}: {exc}'[:200]
        else:
            data, _ident, error, nbytes = self._default_load(path)
        # Re-stat AFTER the load: install the identity the data actually has, so
        # a write racing the parse invalidates on the next read instead of
        # poisoning this generation.
        ident = self._identity(path, identity_fn)
        if data is None and error is None:
            error = 'loader returned no data'
        with self._cond:
            previous = self._entries.get(path)
            if background and data is None and previous is not None:
                # AC3: a failed background reload NEVER replaces the last good
                # generation with emptiness — record the error, keep serving.
                self._stats['reload_errors'] += 1
                self._stats['last_reload_error'] = error
                previous.error = error
                self._stats['last_reload_ms'] = round((time.time() - t0) * 1000, 3)
                return previous.data, self._meta(previous, hit='stale')
            if data is None:
                # First load failed (or a foreground reload failed with nothing
                # to fall back to): install an error entry so the NEXT caller
                # sees the named reason instead of re-parsing a broken file.
                data = [] if (loader is None and path.suffix == '.jsonl') else None
                nbytes = 0
            self._generation += 1
            entry = CacheEntry(path=path, data=data, generation=self._generation,
                               identity=ident, loaded_at=time.time(),
                               nbytes=nbytes, error=error)
            self._entries[path] = entry
            self._watched.add(path)
            self._watch_path(path)
            self._stats['reload_count'] += 1
            self._stats['last_reload_ms'] = round((time.time() - t0) * 1000, 3)
            if error:
                self._stats['last_reload_error'] = error
            self._stats['loaded_at'] = entry.loaded_at_iso
            hit = 'miss' if previous is None else 'reload'
            return self._serve(entry), self._meta(entry, hit=hit)

    # ------------------------------------------------------- scheduling machinery

    def _schedule_locked(self, path, loader, identity_fn) -> bool:
        """Single-flight dedup: True only when THIS call scheduled a reload."""
        if path in self._inflight or path in self._pending:
            return False
        self._pending[path] = (loader, identity_fn)
        self._cond.notify()
        return True

    def schedule_reload(self, path, loader=None, identity_fn=None) -> bool:
        with self._cond:
            return self._schedule_locked(Path(path), loader, identity_fn)

    def invalidate(self, path) -> bool:
        """Schedule a reload of path (change-driven; deduped single-flight)."""
        return self.schedule_reload(path)

    def _worker_loop(self):
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait(0.5)
                if self._closed:
                    return
                path, payload = next(iter(self._pending.items()))
                del self._pending[path]
                self._inflight.add(path)
            try:
                loader, identity_fn = payload
                self._load_install(path, loader, identity_fn, background=True)
            except Exception:  # noqa: BLE001 — a worker never dies on one load
                pass
            finally:
                with self._cond:
                    self._inflight.discard(path)
                    self._cond.notify_all()

    # ------------------------------------------------------------- AC1 machinery

    def _note_file_event(self, path_str) -> None:
        """Watcher/poller hook: schedule a reload when we track this path."""
        try:
            path = Path(path_str)
        except (TypeError, ValueError):
            return
        with self._cond:
            if path not in self._watched:
                return
            entry = self._entries.get(path)
            ident = self._identity(path, None)
            if entry is not None and entry.identity == ident:
                return  # no real change (event storms, partial writes closed clean)
            self._schedule_locked(path, self._loader_for(path),
                                  self._identity_fn_for(path))

    def _watch_path(self, path: Path) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        try:
            watcher.watch_file(path)
        except Exception:  # noqa: BLE001 — watch registration is best-effort
            pass

    def _poll_loop(self):
        while True:
            time.sleep(self._poll_s)
            with self._cond:
                if self._closed:
                    return  # INT-CI-20261015-01: close() must actually stop us
                watched = list(self._watched)
            for path in watched:
                with self._cond:
                    entry = self._entries.get(path)
                    if entry is None:
                        continue
                    ident = self._identity(path, None)
                    if entry.identity != ident:
                        # The loader/identity_fn are unknown here (generic
                        # poller); a default-load reload is correct for every
                        # default-loader path. Loader-carrying consumers get
                        # the same treatment: their cached data re-parses via
                        # the default loader ONLY when they registered one —
                        # so we re-arm through the recorded loader.
                        self._schedule_locked(path, self._loader_for(path),
                                              self._identity_fn_for(path))

    # Loader bookkeeping accessors — the generic watcher/poller re-arms the
    # loader recorded at get() time so a reload re-parses the RIGHT way.
    def remember_loader(self, path, loader, identity_fn=None) -> None:
        self._loaders[str(path)] = (loader, identity_fn)

    def _loader_for(self, path):
        return self._loaders.get(str(path), (None, None))[0]

    def _identity_fn_for(self, path):
        return self._loaders.get(str(path), (None, None))[1]

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        """AC4: the observable surface (/health + /status `runtime_cache`)."""
        with self._cond:
            files = {}
            for path, entry in self._entries.items():
                files[str(path)] = {
                    'generation': entry.generation,
                    'loaded_at': entry.loaded_at_iso,
                    'age_s': round(entry.age_s(), 3),
                    'bytes': entry.bytes,
                    'error': entry.error,
                    'identity': list(entry.identity) if isinstance(entry.identity, tuple) else None,
                }
            total = self._stats['hits'] + self._stats['misses']
            out = {
                'enabled': True,
                'source': self.source,
                'generation': self._generation,
                'reload_count': self._stats['reload_count'],
                'reloads': self._stats['reload_count'],  # compat alias
                'cached_paths': len(self._entries),  # compat alias
                'inotify_available': self._watcher_mode != 'none',  # compat alias
                'generations': self._generation,  # compat alias
                'reload_errors': self._stats['reload_errors'],
                'last_reload_ms': self._stats['last_reload_ms'],
                'last_reload_error': self._stats['last_reload_error'],
                'loaded_at': self._stats.get('loaded_at'),
                'hits': self._stats['hits'],
                'misses': self._stats['misses'],
                'served_stale': self._stats['served_stale'],
                'hit_rate': round(self._stats['hits'] / total, 4) if total else None,
                'cached_files': len(self._entries),
                'pending_reloads': len(self._pending),
                'inflight_reloads': len(self._inflight),
                'inotify_mode': self._watcher_mode,
                'poll_interval_s': self._poll_s if self._poller else 0,
                'copy_mode': self._copy_mode,
                'max_bytes_per_file': self._max_bytes,
                'files': files,
            }
            return out

    # ------------------------------------------------------------------ misc

    def clear(self) -> None:
        """Drop every entry (tests; identity checks re-populate on demand)."""
        with self._cond:
            self._entries.clear()
            self._pending.clear()
            self._loaders.clear()

    def entry(self, path) -> CacheEntry | None:
        with self._cond:
            return self._entries.get(Path(path))

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:  # noqa: BLE001
                pass
        # INT-CI-20261015-01: workers exit on _closed; the poller now checks it
        # too. Join (bounded) so a shutdown test actually leaves zero threads —
        # leaked poll-default threads deadlocked the later in-process seed exec
        # on the import lock (the four silent 75-min CI kills).
        for thread in [*self._workers, self._poller]:
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)

    shutdown = close  # compat alias (TR-277 contract tests)


def _approx_bytes(data) -> int:
    """Cheap size estimate for the stats surface."""
    if isinstance(data, str):
        return len(data.encode('utf-8', errors='replace'))
    if isinstance(data, list):
        return len(data)
    return 0


# --------------------------------------------------------------------------
# TR-277 compat layer: module-level singleton + helpers the server and the
# contract tests consume. The worker's rewrite replaced this API; restoring it
# on top of the richer DataCache so both surfaces exist.
# --------------------------------------------------------------------------

_cache_instance = None
_cache_lock = threading.Lock()


def get_cache() -> DataCache:
    """Process-wide cache singleton for server per-request reads."""
    global _cache_instance
    # INT-CI-20261015-01: honour the kill switch HERE, not just at call sites —
    # router_server._registry_categories checked the env before calling, but
    # every other get_cache()/cached_read_jsonl() caller bypassed it, so tests
    # still spawned the singleton's daemon threads even with ROUTER_CACHE=0.
    # Those leaked threads deadlock a later in-process seed exec on the import
    # lock (the four silent 75-min CI kills). d024cc1 raised instead — wrong:
    # 22 server tests call get_cache().stats() unguarded. The honest shape is
    # a thread-free stand-in with the same surface.
    if os.environ.get('ROUTER_CACHE', '1') in ('0', 'false', 'no', 'off'):
        if _cache_instance is None or isinstance(_cache_instance, DataCache):
            _cache_instance = _SynchronousCache()
        return _cache_instance
    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                _cache_instance = DataCache(source='server')
    return _cache_instance


def cached_read_jsonl(path) -> tuple:
    """Read a JSONL file through the cache. Returns (rows, metadata)."""
    cache = get_cache()
    data, meta = cache.get(path)
    if data is None:
        raise FileNotFoundError(f"cache could not load {path}: {meta.get('cache_error')}")
    return data, meta


def cached_read_json(path) -> tuple:
    """Read a JSON file through the cache. Returns (data, metadata)."""
    return cached_read_jsonl(path)

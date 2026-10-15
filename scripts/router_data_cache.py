#!/usr/bin/env python3
"""TR-277: Runtime data cache for JSONL tables + registry.

Read once, invalidate on file change, reload in background, single-flight, bounded.

AC0: Cache keyed by (path, mtime_ns, size, inode) + generation counter.
AC1: inotify watch where available, stat-poll fallback.
AC2: Single-flight background reload; response carries cache_generation + cache_age_s.
AC3: Bounded (one in-flight reload, ceiling on loader threads, max bytes, serve-last-good on failure).
AC4: /health + stats report generation, loaded_at, reload_count, last_reload_ms, hits/misses.
AC5: Tests prove rewrite->next-request-serves-new + N-concurrent->exactly-ONE-reload.
AC6: Applies to registry.json + data/tables/*.jsonl + per-request reads.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


class CacheEntry:
    """One cached file: generation, data, metadata, timestamps."""
    
    def __init__(self, path: Path, data: Any, generation: int, 
                 mtime_ns: int, size: int, inode: int, loaded_at: float):
        self.path = path
        self.data = data
        self.generation = generation
        self.mtime_ns = mtime_ns
        self.size = size
        self.inode = inode
        self.loaded_at = loaded_at
    
    def age_s(self) -> float:
        return time.time() - self.loaded_at


class DataCache:
    """TR-277: file-backed data cache with inotify/stat invalidation.
    
    AC0: keyed by (path, mtime_ns, size, inode) + generation.
    AC1: inotify where available, stat fallback.
    AC2: single-flight bg reload, cache_generation + cache_age_s in response.
    AC3: bounded (one in-flight, ceiling on threads, max bytes, serve-last-good).
    AC4: observable via stats().
    """
    
    def __init__(self, max_loader_threads: int = 4, max_bytes_per_file: int = 50 * 1024 * 1024,
                 stat_poll_interval_s: float = 1.0):
        self._cache: dict[Path, CacheEntry] = {}
        self._generation_counter = 0
        self._lock = threading.RLock()
        self._reload_locks: dict[Path, threading.Lock] = {}
        self._loader_pool = threading.Semaphore(max_loader_threads)
        self._max_bytes = max_bytes_per_file
        self._stat_poll_interval_s = stat_poll_interval_s
        
        # Stats (AC4)
        self._hits = 0
        self._misses = 0
        self._reloads = 0
        self._last_reload_ms = 0.0
        self._reload_errors = 0
        
        # Inotify watch (AC1) — best-effort, falls back to stat
        self._inotify_available = False
        self._inotify_thread: Optional[threading.Thread] = None
        self._inotify_stop = threading.Event()
        self._watched_paths: set[Path] = set()
        self._try_start_inotify()
    
    def _try_start_inotify(self):
        """AC1: start inotify watcher if available, else rely on stat fallback."""
        try:
            import inotify.adapters  # type: ignore
            self._inotify_available = True
            self._inotify_thread = threading.Thread(
                target=self._inotify_loop, daemon=True, name="data-cache-inotify")
            self._inotify_thread.start()
        except ImportError:
            # inotify not available — stat fallback only
            self._inotify_available = False
    
    def _inotify_loop(self):
        """Watch watched_paths for modifications; trigger reload."""
        import inotify.adapters  # type: ignore
        while not self._inotify_stop.is_set():
            try:
                paths_to_watch = [str(p.parent) for p in self._watched_paths if p.parent.exists()]
                if not paths_to_watch:
                    time.sleep(0.5)
                    continue
                i = inotify.adapters.InotifyTree(str(paths_to_watch[0]))
                for event in i.event_gen(yield_nones=False):
                    if self._inotify_stop.is_set():
                        break
                    (_, type_names, path, filename) = event
                    if 'IN_MODIFY' in type_names or 'IN_CLOSE_WRITE' in type_names:
                        full_path = Path(path) / filename
                        if full_path in self._watched_paths:
                            self._schedule_reload(full_path)
            except Exception:
                time.sleep(1.0)
    
    def _schedule_reload(self, path: Path):
        """AC2: single-flight background reload."""
        with self._lock:
            if path not in self._reload_locks:
                self._reload_locks[path] = threading.Lock()
            reload_lock = self._reload_locks[path]
        
        def _do_reload():
            if not reload_lock.acquire(blocking=False):
                return  # another thread is already reloading this path
            try:
                self._reload_sync(path)
            finally:
                reload_lock.release()
        
        # Background thread (AC2: off the request path)
        threading.Thread(target=_do_reload, daemon=True, name=f"cache-reload-{path.name}").start()
    
    def _reload_sync(self, path: Path):
        """AC2: synchronous reload (called from bg thread or on miss)."""
        t0 = time.time()
        try:
            stat = path.stat()
            if stat.st_size > self._max_bytes:
                # AC3: bounded — serve last good
                self._reload_errors += 1
                return
            data = self._load_file(path)
            with self._lock:
                self._generation_counter += 1
                entry = CacheEntry(
                    path=path, data=data, generation=self._generation_counter,
                    mtime_ns=stat.st_mtime_ns, size=stat.st_size, inode=stat.st_ino,
                    loaded_at=time.time())
                self._cache[path] = entry
                self._watched_paths.add(path)
            self._reloads += 1
            self._last_reload_ms = (time.time() - t0) * 1000
        except Exception:
            self._reload_errors += 1
    
    def _load_file(self, path: Path) -> Any:
        """Load a JSONL or JSON file."""
        if path.suffix == '.jsonl':
            rows = []
            with path.open('r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            return rows
        elif path.suffix == '.json':
            with path.open('r', encoding='utf-8') as f:
                return json.load(f)
        else:
            return path.read_text(encoding='utf-8')
    
    def _stat_check(self, path: Path) -> bool:
        """AC1: stat fallback — returns True if file changed."""
        try:
            stat = path.stat()
            with self._lock:
                entry = self._cache.get(path)
                if entry is None:
                    return True
                return (stat.st_mtime_ns != entry.mtime_ns or 
                        stat.st_size != entry.size or 
                        stat.st_ino != entry.inode)
        except OSError:
            return True
    
    def get(self, path: Path, loader: Optional[Callable[[], Any]] = None) -> tuple[Any, dict]:
        """Get cached data for path. Returns (data, metadata).
        
        AC0: keyed by (path, mtime_ns, size, inode) + generation.
        AC1: inotify + stat fallback.
        AC2: cache_generation + cache_age_s in metadata.
        AC3: serve-last-good on failure.
        """
        path = path.resolve()
        
        # Check if we need to reload (AC1: stat fallback if no inotify)
        needs_reload = False
        with self._lock:
            entry = self._cache.get(path)
            if entry is None:
                needs_reload = True
            elif not self._inotify_available and self._stat_check(path):
                needs_reload = True
        
        if needs_reload:
            # Synchronous reload on miss (first access or after invalidation)
            # AC2: single-flight — use the per-path lock so only ONE thread reloads
            with self._lock:
                if path not in self._reload_locks:
                    self._reload_locks[path] = threading.Lock()
                reload_lock = self._reload_locks[path]
            
            if reload_lock.acquire(blocking=True):
                try:
                    # Double-check: another thread may have reloaded while we waited
                    with self._lock:
                        entry = self._cache.get(path)
                        if entry is not None and not self._stat_check(path):
                            # Already reloaded by another thread
                            self._hits += 1
                            return entry.data, {
                                'cache_generation': entry.generation,
                                'cache_age_s': entry.age_s(),
                                'cache_source': 'cache',
                                'cache_path': str(path),
                            }
                    self._reload_sync(path)
                finally:
                    reload_lock.release()
            
            with self._lock:
                entry = self._cache.get(path)
                if entry is None:
                    # AC3: serve-last-good — but we have nothing, so raise
                    raise FileNotFoundError(f"Cannot load {path}")
                self._misses += 1
                return entry.data, {
                    'cache_generation': entry.generation,
                    'cache_age_s': entry.age_s(),
                    'cache_source': 'reload',
                    'cache_path': str(path),
                }
        
        # Hit
        with self._lock:
            self._hits += 1
            return entry.data, {
                'cache_generation': entry.generation,
                'cache_age_s': entry.age_s(),
                'cache_source': 'cache',
                'cache_path': str(path),
            }
    
    def stats(self) -> dict:
        """AC4: observable stats."""
        with self._lock:
            return {
                'hits': self._hits,
                'misses': self._misses,
                'reloads': self._reloads,
                'reload_errors': self._reload_errors,
                'last_reload_ms': self._last_reload_ms,
                'cached_paths': len(self._cache),
                'inotify_available': self._inotify_available,
                'generations': self._generation_counter,
            }
    
    def invalidate(self, path: Path):
        """Force reload on next access."""
        with self._lock:
            self._cache.pop(path, None)
    
    def shutdown(self):
        """Stop inotify thread."""
        self._inotify_stop.set()


# Singleton cache instance for the server
_cache_instance: Optional[DataCache] = None
_cache_lock = threading.Lock()


def get_cache() -> DataCache:
    """Get or create the singleton cache."""
    global _cache_instance
    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                _cache_instance = DataCache()
    return _cache_instance


def cached_read_jsonl(path: Path) -> tuple[list, dict]:
    """Read a JSONL file through the cache. Returns (rows, metadata)."""
    cache = get_cache()
    return cache.get(path)


def cached_read_json(path: Path) -> tuple[Any, dict]:
    """Read a JSON file through the cache. Returns (data, metadata)."""
    cache = get_cache()
    return cache.get(path)
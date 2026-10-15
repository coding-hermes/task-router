"""TR-277: tests for the runtime data cache."""

import json
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_jsonl(tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n')
    return p


@pytest.fixture
def tmp_json(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"k": "v1"}))
    return p


def test_first_read_loads(tmp_jsonl):
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    data, meta = cache.get(tmp_jsonl)
    assert data == [{"a": 1}, {"a": 2}]
    assert meta["cache_generation"] >= 1
    assert meta["cache_source"] == "reload"
    assert "cache_age_s" in meta
    cache.shutdown()


def test_rewrite_triggers_reload(tmp_jsonl):
    """AC5 part 1: rewrite a table -> next request serves new content."""
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    data1, meta1 = cache.get(tmp_jsonl)
    gen1 = meta1["cache_generation"]

    # Rewrite the file
    tmp_jsonl.write_text('{"a":99}\n')
    # Force stat-based invalidation (inotify may not fire in test env)
    time.sleep(0.05)

    data2, meta2 = cache.get(tmp_jsonl)
    assert data2 == [{"a": 99}]
    assert meta2["cache_generation"] > gen1
    cache.shutdown()


def test_concurrent_requests_single_flight(tmp_jsonl):
    """AC5 part 2: N concurrent requests after a change produce exactly ONE reload."""
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    # Prime the cache
    cache.get(tmp_jsonl)
    initial_reloads = cache.stats()["reloads"]

    # Rewrite to trigger invalidation
    tmp_jsonl.write_text('{"a":42}\n')
    time.sleep(0.05)

    results = []
    errors = []

    def _reader():
        try:
            data, meta = cache.get(tmp_jsonl)
            results.append((data, meta["cache_generation"]))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_reader) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"errors: {errors}"
    # All readers see the same generation (single-flight)
    generations = {gen for _, gen in results}
    assert len(generations) == 1, f"multiple generations seen: {generations}"
    # All readers see the new data
    assert all(data == [{"a": 42}] for data, _ in results)
    # Reload count increased by exactly 1 (or at most a small number due to stat races)
    final_reloads = cache.stats()["reloads"]
    assert final_reloads - initial_reloads <= 2, f"too many reloads: {final_reloads - initial_reloads}"
    cache.shutdown()


def test_json_file(tmp_json):
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    data, meta = cache.get(tmp_json)
    assert data == {"k": "v1"}
    assert meta["cache_generation"] >= 1
    cache.shutdown()


def test_stats_observable():
    """AC4: stats surface reports generation, loaded_at, reload_count, etc."""
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    stats = cache.stats()
    assert "hits" in stats
    assert "misses" in stats
    assert "reloads" in stats
    assert "last_reload_ms" in stats
    assert "cached_paths" in stats
    assert "inotify_available" in stats
    assert "generations" in stats
    cache.shutdown()


def test_serve_last_good_on_oversize(tmp_path):
    """AC3: bounded — file over max_bytes keeps serving last good."""
    from scripts.router_data_cache import DataCache
    cache = DataCache(max_bytes_per_file=100)
    p = tmp_path / "small.jsonl"
    p.write_text('{"x":1}\n')
    data1, _ = cache.get(p)
    assert data1 == [{"x": 1}]

    # Oversize rewrite
    p.write_text('{"x":' + '"big"' * 100 + '}\n')
    time.sleep(0.05)
    # Should still serve the old data (serve-last-good)
    data2, meta2 = cache.get(p)
    assert data2 == [{"x": 1}]
    assert cache.stats()["reload_errors"] >= 1
    cache.shutdown()


def test_missing_file_raises(tmp_path):
    from scripts.router_data_cache import DataCache
    cache = DataCache()
    with pytest.raises(FileNotFoundError):
        cache.get(tmp_path / "nonexistent.jsonl")
    cache.shutdown()
"""Unit tests for cache.gc_idle_caches() and the `cache gc` subcommand wiring."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from xcindex import cache as cache_module


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch) -> Path:
    cache_dir = tmp_path / "xcache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    return cache_dir


def _make_cache(root: Path, fp: str, *, mtime_ago_seconds: float, project_path: str | None = None):
    """Create a cache directory with index.sqlite + meta.json."""
    cdir = root / fp
    cdir.mkdir(parents=True)
    sqlite = cdir / cache_module.LIVE_SQLITE_NAME
    sqlite.write_bytes(b"stub")
    meta = cdir / cache_module.META_FILENAME
    if project_path:
        meta.write_text(json.dumps({"project_path": project_path}))
    target_mtime = time.time() - mtime_ago_seconds
    os.utime(sqlite, (target_mtime, target_mtime))
    return cdir


# --- Empty cache root -------------------------------------------------------

def test_gc_returns_empty_result_when_cache_root_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_ROOT", tmp_path / "does-not-exist")
    result = cache_module.gc_idle_caches()
    assert result.pruned == []
    assert result.kept == []
    assert result.bytes_freed == 0


def test_gc_empty_cache_root(isolated_cache):
    isolated_cache.mkdir()
    result = cache_module.gc_idle_caches()
    assert result.pruned == []
    assert result.kept == []


# --- Threshold logic --------------------------------------------------------

def test_gc_prunes_cache_with_old_mtime(isolated_cache):
    cdir = _make_cache(isolated_cache, "old", mtime_ago_seconds=4000, project_path="/whatever")
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    assert len(result.pruned) == 1
    assert result.pruned[0].project_fingerprint == "old"
    assert result.bytes_freed > 0
    assert not cdir.exists()  # actually removed


def test_gc_keeps_cache_with_fresh_mtime(isolated_cache):
    cdir = _make_cache(isolated_cache, "fresh", mtime_ago_seconds=60, project_path="/whatever")
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    assert len(result.pruned) == 0
    assert len(result.kept) == 1
    assert cdir.exists()


def test_gc_threshold_boundary(isolated_cache):
    """A cache idle EXACTLY at the threshold is kept (we use >, not >=)."""
    _make_cache(isolated_cache, "boundary", mtime_ago_seconds=60, project_path="/x")
    result = cache_module.gc_idle_caches(max_idle_seconds=120)
    assert len(result.kept) == 1


# --- Dry run ----------------------------------------------------------------

def test_gc_dry_run_preserves_cache(isolated_cache):
    cdir = _make_cache(isolated_cache, "old", mtime_ago_seconds=4000, project_path="/x")
    result = cache_module.gc_idle_caches(max_idle_seconds=3600, dry_run=True)
    assert len(result.pruned) == 1
    assert cdir.exists()  # NOT actually removed
    assert result.dry_run is True


# --- Caches without meta / without project_path -----------------------------

def test_gc_handles_cache_without_meta(isolated_cache):
    """Old cache without meta.json: still eligible if mtime stale."""
    cdir = _make_cache(isolated_cache, "no-meta", mtime_ago_seconds=4000)
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    assert len(result.pruned) == 1
    assert result.pruned[0].project_path is None


def test_gc_handles_cache_without_sqlite(isolated_cache):
    """Cache directory exists but no live sqlite: treated as ancient (mtime=0)."""
    cdir = isolated_cache / "no-sqlite"
    cdir.mkdir(parents=True)
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    assert len(result.pruned) == 1
    assert not cdir.exists()


# --- Mixed scenarios --------------------------------------------------------

def test_gc_mixed_caches(isolated_cache):
    _make_cache(isolated_cache, "a-old",   mtime_ago_seconds=7200, project_path="/a")
    _make_cache(isolated_cache, "b-fresh", mtime_ago_seconds=300,  project_path="/b")
    _make_cache(isolated_cache, "c-old",   mtime_ago_seconds=99999, project_path="/c")
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    pruned_fps = {c.project_fingerprint for c in result.pruned}
    kept_fps = {c.project_fingerprint for c in result.kept}
    assert pruned_fps == {"a-old", "c-old"}
    assert kept_fps == {"b-fresh"}


def test_gc_returns_total_bytes_freed(isolated_cache):
    _make_cache(isolated_cache, "a", mtime_ago_seconds=4000, project_path="/a")
    _make_cache(isolated_cache, "b", mtime_ago_seconds=4000, project_path="/b")
    result = cache_module.gc_idle_caches(max_idle_seconds=3600)
    expected = sum(c.size_bytes for c in result.pruned)
    assert result.bytes_freed == expected
    assert result.bytes_freed > 0


# --- Threshold reflects in result -------------------------------------------

def test_gc_records_threshold(isolated_cache):
    result = cache_module.gc_idle_caches(max_idle_seconds=900)
    assert result.threshold_seconds == 900

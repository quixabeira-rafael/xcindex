"""Schema migration tests: v1 cache layout → v2 with legacy preservation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xcindex import cache as cache_module
from xcindex import schema


def _write_v1_sqlite(path: Path) -> None:
    """Write a sqlite file with the v1 schema marker (schema_version=1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        conn.commit()
    finally:
        conn.close()


def test_migrate_v1_caches_renames_existing(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    cache_dir = cache_module.project_cache_dir(project)
    cache_dir.mkdir(parents=True)

    (cache_dir / "abc123.sqlite").write_bytes(b"x")
    (cache_dir / "def456.sqlite").write_bytes(b"x")
    renamed = cache_module.migrate_v1_caches(project)

    assert renamed == 2
    assert (cache_dir / "legacy_abc123.sqlite").is_file()
    assert (cache_dir / "legacy_def456.sqlite").is_file()
    assert not (cache_dir / "abc123.sqlite").exists()


def test_migrate_v1_caches_preserves_live_and_legacy(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    cache_dir = cache_module.project_cache_dir(project)
    cache_dir.mkdir(parents=True)

    (cache_dir / "index.sqlite").write_bytes(b"live")
    (cache_dir / "legacy_old.sqlite").write_bytes(b"old")
    (cache_dir / "fresh_v1.sqlite").write_bytes(b"v1")

    renamed = cache_module.migrate_v1_caches(project)

    assert renamed == 1
    assert (cache_dir / "index.sqlite").read_bytes() == b"live"
    assert (cache_dir / "legacy_old.sqlite").read_bytes() == b"old"
    assert (cache_dir / "legacy_fresh_v1.sqlite").is_file()


def test_migrate_v1_caches_idempotent(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    cache_dir = cache_module.project_cache_dir(project)
    cache_dir.mkdir(parents=True)
    (cache_dir / "abc.sqlite").write_bytes(b"x")

    assert cache_module.migrate_v1_caches(project) == 1
    assert cache_module.migrate_v1_caches(project) == 0


def test_gc_caches_keeps_live_and_trims_legacy(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    cache_dir = cache_module.project_cache_dir(project)
    cache_dir.mkdir(parents=True)

    live = cache_dir / "index.sqlite"
    live.write_bytes(b"live")
    legacy = []
    for i in range(5):
        f = cache_dir / f"legacy_h{i}.sqlite"
        f.write_bytes(bytes([i]))
        legacy.append(f)
    # Stagger mtimes so we know which are newest
    import os
    for i, f in enumerate(legacy):
        os.utime(f, ns=(i * 1_000_000_000, i * 1_000_000_000))

    removed = cache_module.gc_caches(project)
    assert removed == 2  # 5 legacy → keep 3
    assert live.is_file()
    survivors = sorted(p.name for p in cache_dir.glob("*.sqlite") if p.name != "index.sqlite")
    # Only the 3 newest should remain (h2, h3, h4)
    assert survivors == ["legacy_h2.sqlite", "legacy_h3.sqlite", "legacy_h4.sqlite"]


def test_list_caches_distinguishes_live_and_legacy(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    cache_dir = cache_module.project_cache_dir(project)
    cache_dir.mkdir(parents=True)

    (cache_dir / "index.sqlite").write_bytes(b"live")
    (cache_dir / "legacy_oldhash.sqlite").write_bytes(b"old")
    cache_module.write_meta(project, latest_hash="oldhash")

    entries = cache_module.list_caches(project)
    by_role = {e.role: e for e in entries}
    assert {"live", "legacy"} == set(by_role)
    assert by_role["live"].sqlite_path.name == "index.sqlite"
    assert by_role["legacy"].index_hash == "oldhash"


def test_canonical_sqlite_path_consistent(tmp_path: Path):
    project = tmp_path / "Sample.xcodeproj"
    project.mkdir()
    a = cache_module.canonical_sqlite_path(project)
    b = cache_module.canonical_sqlite_path(project)
    assert a == b
    assert a.name == "index.sqlite"


def test_schema_read_version_matches_written(tmp_path: Path):
    sqlite_path = tmp_path / "x.sqlite"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        schema.apply_schema(conn)
        schema.write_meta(conn, schema_version=schema.SCHEMA_VERSION)
        version = schema.read_schema_version(conn)
    finally:
        conn.close()
    assert version == schema.SCHEMA_VERSION


def test_schema_read_version_returns_none_on_missing_table(tmp_path: Path):
    sqlite_path = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        version = schema.read_schema_version(conn)
    finally:
        conn.close()
    assert version is None


def test_apply_schema_creates_unit_files_table(tmp_path: Path):
    sqlite_path = tmp_path / "x.sqlite"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        schema.apply_schema(conn)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unit_files'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_units_table_includes_size_bytes_column(tmp_path: Path):
    sqlite_path = tmp_path / "x.sqlite"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        schema.apply_schema(conn)
        cursor = conn.execute("PRAGMA table_info(units)")
        cols = {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()
    assert "size_bytes" in cols

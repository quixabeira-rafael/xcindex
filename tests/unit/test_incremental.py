"""Unit tests for the Python-side incremental machinery.

Covers delta detection (`compute_unit_delta`). The actual DELETE+INSERT
work runs in the Swift helper and is covered by the integration suite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from xcindex import incremental
from xcindex import schema


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    schema.apply_schema(conn)
    schema.apply_indexes(conn)
    return conn


def _make_index_store(tmp_path: Path, units: dict[str, bytes]) -> Path:
    """Build a directory matching <store>/v5/units/<name> with given content."""
    store = tmp_path / "DataStore"
    units_dir = store / "v5" / "units"
    units_dir.mkdir(parents=True)
    for name, content in units.items():
        (units_dir / name).write_bytes(content)
    return store


def _seed_units(conn: sqlite3.Connection, rows: list[tuple[str, int, int]]) -> None:
    conn.executemany(
        "INSERT INTO units(name, size_bytes, mtime_ns) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


# --- compute_unit_delta -----------------------------------------------------

def test_delta_empty_when_disk_matches_cache(tmp_path: Path):
    store = _make_index_store(tmp_path, {"u1": b"abc", "u2": b"defg"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    rows = []
    for name in ("u1", "u2"):
        st = (store / "v5" / "units" / name).stat()
        rows.append((name, st.st_size, st.st_mtime_ns))
    _seed_units(conn, rows)
    conn.close()

    delta = incremental.compute_unit_delta(sqlite_path, store)
    assert delta.is_empty


def test_delta_detects_added_units(tmp_path: Path):
    store = _make_index_store(tmp_path, {"u1": b"abc", "u2": b"def"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    st = (store / "v5" / "units" / "u1").stat()
    _seed_units(conn, [("u1", st.st_size, st.st_mtime_ns)])
    conn.close()

    delta = incremental.compute_unit_delta(sqlite_path, store)
    assert delta.added == frozenset({"u2"})
    assert delta.needs_full_redump


def test_delta_detects_removed_units(tmp_path: Path):
    store = _make_index_store(tmp_path, {"u1": b"x"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    st = (store / "v5" / "units" / "u1").stat()
    _seed_units(conn, [
        ("u1", st.st_size, st.st_mtime_ns),
        ("u_gone", 100, 12345),
    ])
    conn.close()

    delta = incremental.compute_unit_delta(sqlite_path, store)
    assert delta.removed == frozenset({"u_gone"})
    assert not delta.added


def test_delta_detects_modified_units(tmp_path: Path):
    store = _make_index_store(tmp_path, {"u1": b"original"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    _seed_units(conn, [("u1", 999, 0)])  # different size + mtime
    conn.close()

    delta = incremental.compute_unit_delta(sqlite_path, store)
    assert delta.modified == frozenset({"u1"})

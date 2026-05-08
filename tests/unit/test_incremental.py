"""Unit tests for the incremental update logic.

These tests run entirely against in-memory / on-disk SQLite, with the helper
mocked. They validate the schema-level operations: delta detection, DELETE
scoping, INSERT replay, units snapshot maintenance.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pytest

from xcindex import dumper
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


# --- apply_incremental_update -----------------------------------------------

class FakeHelperRecords:
    """Provides `stream_dump_files` returning a fixed iterable."""

    def __init__(self, records: list[dict]):
        self._records = records
        self.calls: list[list[str]] = []

    def __call__(self, index_store, files, *, include_system=False, helper_path=None):
        self.calls.append([str(f) for f in files])
        return iter(self._records)


def _seed_full_state(conn: sqlite3.Connection):
    """Populate one symbol + 2 occurrences in 2 different files + relations."""
    conn.executemany(
        "INSERT INTO symbols(usr, name, kind, language, file, line) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("usr_foo", "foo", "instance-method", "swift", "/A.swift", 1),
            ("usr_bar", "bar", "instance-method", "swift", "/B.swift", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO occurrences(id, symbol_usr, file, line, column, roles) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "usr_foo", "/A.swift", 1, 1, 2),
            (2, "usr_foo", "/B.swift", 5, 1, 4),
            (3, "usr_bar", "/B.swift", 1, 1, 2),
        ],
    )
    conn.executemany(
        "INSERT INTO relations(occurrence_id, related_usr, kind, roles) VALUES (?, ?, ?, ?)",
        [(2, "usr_bar", "calledBy", 0)],
    )
    conn.executemany(
        "INSERT INTO unit_files(unit_name, file) VALUES (?, ?)",
        [("uA", "/A.swift"), ("uB", "/B.swift")],
    )
    conn.commit()


def test_apply_incremental_deletes_only_modified_files(tmp_path: Path, monkeypatch):
    store = _make_index_store(tmp_path, {"uA": b"new", "uB": b"unchanged"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    _seed_full_state(conn)
    # Seed snapshot to show uA is "modified" vs current disk
    _seed_units(conn, [("uA", 999, 0), ("uB", 9, 0)])
    conn.close()

    fake = FakeHelperRecords([
        {"type": "symbol", "usr": "usr_foo", "name": "foo", "kind": "instance-method",
         "language": "swift", "module": None, "file": "/A.swift", "line": 1,
         "is_system": False, "properties": 0},
        {"type": "occurrence", "id": 100, "symbol_usr": "usr_foo", "file": "/A.swift",
         "line": 1, "column": 1, "roles": 2, "container_usr": None},
        {"type": "file_unit", "file": "/A.swift", "unit_name": "uA"},
    ])
    monkeypatch.setattr("xcindex.helper.stream_dump_files", fake)

    delta = incremental.UnitDelta(modified=frozenset({"uA"}))
    incremental.apply_incremental_update(
        sqlite_path, delta, store, Path("/fake/helper")
    )

    assert fake.calls == [["/A.swift"]]
    conn = sqlite3.connect(str(sqlite_path))
    try:
        files_in_occ = {row[0] for row in conn.execute("SELECT DISTINCT file FROM occurrences")}
        assert "/A.swift" in files_in_occ
        # /B.swift's data must remain untouched
        assert "/B.swift" in files_in_occ
        # Old occurrences in /A.swift were replaced; only one survives, with the
        # helper's id remapped past the previous MAX(id).
        a_rows = list(conn.execute(
            "SELECT id, symbol_usr FROM occurrences WHERE file = '/A.swift'"
        ))
        assert len(a_rows) == 1
        assert a_rows[0][1] == "usr_foo"
        assert a_rows[0][0] >= 100  # offset applied past pre-existing ids
        # /B.swift's relation chain is untouched
        rels = list(conn.execute("SELECT occurrence_id, related_usr FROM relations"))
        assert (2, "usr_bar") in rels
    finally:
        conn.close()


def test_apply_incremental_handles_removed_units(tmp_path: Path, monkeypatch):
    store = _make_index_store(tmp_path, {"uB": b"unchanged"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    _seed_full_state(conn)
    st_b = (store / "v5" / "units" / "uB").stat()
    _seed_units(conn, [("uA", 100, 0), ("uB", st_b.st_size, st_b.st_mtime_ns)])
    conn.close()

    fake = FakeHelperRecords([])
    monkeypatch.setattr("xcindex.helper.stream_dump_files", fake)

    delta = incremental.UnitDelta(removed=frozenset({"uA"}))
    incremental.apply_incremental_update(
        sqlite_path, delta, store, Path("/fake/helper")
    )
    # Removed-only deltas don't need helper invocation
    assert fake.calls == []

    conn = sqlite3.connect(str(sqlite_path))
    try:
        units = {row[0] for row in conn.execute("SELECT name FROM units")}
        assert units == {"uB"}
        unit_files = {row[0] for row in conn.execute("SELECT unit_name FROM unit_files")}
        assert unit_files == {"uB"}
    finally:
        conn.close()


def test_apply_incremental_rejects_added_units(tmp_path: Path):
    store = _make_index_store(tmp_path, {"uA": b"x"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    _seed_full_state(conn)
    conn.close()

    delta = incremental.UnitDelta(added=frozenset({"uA"}))
    with pytest.raises(ValueError, match="added units"):
        incremental.apply_incremental_update(
            sqlite_path, delta, store, Path("/fake/helper")
        )


def test_refresh_units_snapshot_replaces_table(tmp_path: Path):
    store = _make_index_store(tmp_path, {"u1": b"a", "u2": b"bcd"})
    sqlite_path = tmp_path / "cache.sqlite"
    conn = _make_db(sqlite_path)
    _seed_units(conn, [("stale", 5, 5)])

    incremental.refresh_units_snapshot(conn, store)

    rows = conn.execute("SELECT name, size_bytes FROM units ORDER BY name").fetchall()
    assert {r[0] for r in rows} == {"u1", "u2"}
    assert all(int(r[1]) > 0 for r in rows)
    conn.close()

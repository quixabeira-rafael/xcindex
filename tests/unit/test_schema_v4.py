"""v4 schema invariant tests — usrs interning, FK integrity, version bump."""
from __future__ import annotations

import sqlite3

import pytest

from xcindex import schema


@pytest.fixture
def empty_v4_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    schema.apply_indexes(conn)
    return conn


# --- Schema version ---------------------------------------------------------

def test_schema_version_is_4():
    assert schema.SCHEMA_VERSION == 4


def test_schema_version_persists_via_meta(empty_v4_conn):
    schema.write_meta(empty_v4_conn, schema_version=schema.SCHEMA_VERSION)
    assert schema.read_schema_version(empty_v4_conn) == 4


# --- usrs table -------------------------------------------------------------

def test_usrs_table_exists(empty_v4_conn):
    rows = empty_v4_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='usrs'"
    ).fetchall()
    assert len(rows) == 1


def test_usrs_text_column_is_unique(empty_v4_conn):
    empty_v4_conn.execute("INSERT INTO usrs(text) VALUES (?)", ("s:Foo",))
    with pytest.raises(sqlite3.IntegrityError):
        empty_v4_conn.execute("INSERT INTO usrs(text) VALUES (?)", ("s:Foo",))


def test_usrs_id_is_autoincrement(empty_v4_conn):
    cur = empty_v4_conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('s:A')")
    a_id = cur.lastrowid
    cur.execute("INSERT INTO usrs(text) VALUES ('s:B')")
    b_id = cur.lastrowid
    assert b_id == a_id + 1


# --- v4 column renames ------------------------------------------------------

def test_symbols_uses_usr_id(empty_v4_conn):
    cols = {row[1] for row in empty_v4_conn.execute("PRAGMA table_info(symbols)")}
    assert "usr_id" in cols
    assert "usr" not in cols  # legacy v3 column gone


def test_occurrences_uses_symbol_usr_id_and_container_usr_id(empty_v4_conn):
    cols = {row[1] for row in empty_v4_conn.execute("PRAGMA table_info(occurrences)")}
    assert "symbol_usr_id" in cols
    assert "container_usr_id" in cols
    assert "symbol_usr" not in cols
    assert "container_usr" not in cols


def test_relations_uses_related_usr_id(empty_v4_conn):
    cols = {row[1] for row in empty_v4_conn.execute("PRAGMA table_info(relations)")}
    assert "related_usr_id" in cols
    assert "related_usr" not in cols


# --- Index list -------------------------------------------------------------

def test_indexes_present(empty_v4_conn):
    rows = empty_v4_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
    ).fetchall()
    names = {row[0] for row in rows}
    expected = {
        "idx_sym_module", "idx_sym_kind", "idx_sym_name_nocase",
        "idx_sym_name", "idx_sym_file",
        "idx_occ_symbol", "idx_occ_file_line", "idx_occ_container",
        "idx_occ_unit",
        "idx_rel_related_kind", "idx_rel_occ",
        "idx_unit_files_file", "idx_unit_files_unit",
    }
    assert expected.issubset(names)


# --- FK integrity (no orphan ids in a populated cache) ----------------------

def test_no_orphan_symbol_usr_ids(populated_conn):
    """Every symbol's usr_id must exist in usrs."""
    rows = populated_conn.execute(
        "SELECT s.usr_id FROM symbols s LEFT JOIN usrs u ON u.id = s.usr_id WHERE u.id IS NULL"
    ).fetchall()
    assert rows == []


def test_no_orphan_symbol_usr_id_in_occurrences(populated_conn):
    rows = populated_conn.execute(
        "SELECT o.symbol_usr_id FROM occurrences o "
        "LEFT JOIN usrs u ON u.id = o.symbol_usr_id WHERE u.id IS NULL"
    ).fetchall()
    assert rows == []


def test_no_orphan_container_usr_id(populated_conn):
    rows = populated_conn.execute(
        "SELECT o.container_usr_id FROM occurrences o "
        "LEFT JOIN usrs u ON u.id = o.container_usr_id "
        "WHERE o.container_usr_id IS NOT NULL AND u.id IS NULL"
    ).fetchall()
    assert rows == []


def test_no_orphan_related_usr_id(populated_conn):
    rows = populated_conn.execute(
        "SELECT r.related_usr_id FROM relations r "
        "LEFT JOIN usrs u ON u.id = r.related_usr_id WHERE u.id IS NULL"
    ).fetchall()
    assert rows == []


# --- Migration trigger (v3 cache → re-bootstrap) ---------------------------

def test_v3_cache_triggers_schema_outdated(tmp_path):
    """A cache with schema_version=3 must be detected as outdated by the engine."""
    from xcindex import engine
    cache_path = tmp_path / "old_v3.sqlite"
    conn = sqlite3.connect(str(cache_path))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '3')")
    conn.commit()
    conn.close()
    assert engine._schema_outdated(cache_path) is True


def test_v4_cache_does_not_trigger_outdated(tmp_path):
    from xcindex import engine
    cache_path = tmp_path / "v4.sqlite"
    conn = sqlite3.connect(str(cache_path))
    schema.apply_schema(conn)
    schema.write_meta(conn, schema_version=4)
    conn.close()
    assert engine._schema_outdated(cache_path) is False


def test_no_meta_triggers_outdated(tmp_path):
    """Cache without a meta table is treated as outdated."""
    from xcindex import engine
    cache_path = tmp_path / "no_meta.sqlite"
    conn = sqlite3.connect(str(cache_path))
    conn.execute("CREATE TABLE foo(x INTEGER)")
    conn.commit()
    conn.close()
    assert engine._schema_outdated(cache_path) is True


# --- usr_id roundtrip via query layer ---------------------------------------

def test_query_resolves_usr_id_correctly(populated_conn):
    """Sanity: query helpers return the SAME USR text we inserted."""
    from xcindex import query
    canonical = query.query_symbol_by_usr(populated_conn, "foo")
    assert canonical["summary"]["found"]
    assert canonical["items"][0]["usr"] == "foo"
    assert canonical["items"][0]["name"] == "Foo"


def test_query_returns_usr_text_from_id(populated_conn):
    """Symbols returned by name lookup carry their full USR text."""
    from xcindex import query
    canonical = query.query_symbol_by_name(populated_conn, "Foo")
    assert any(it["usr"] == "foo" for it in canonical["items"])


# --- populated_conn (mirrors test_query.py fixture) ------------------------

@pytest.fixture
def populated_conn():
    """Mini v4 cache used for FK invariant + query roundtrip checks."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    cur = conn.cursor()
    USR_IDS = {"foo": 1, "bar": 2, "qux": 3, "subfoo": 4, "caller": 5, "run": 6}
    cur.executemany("INSERT INTO usrs(id, text) VALUES (?, ?)",
                    [(uid, text) for text, uid in USR_IDS.items()])
    cur.executemany(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (USR_IDS["foo"],    "Foo",    "class",           None, "swift", "Core",   "Core/Foo.swift",       1, 0, 0),
            (USR_IDS["bar"],    "bar()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       3, 0, 0),
            (USR_IDS["qux"],    "qux()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       7, 0, 0),
            (USR_IDS["subfoo"], "SubFoo", "class",           None, "swift", "Domain", "Domain/SubFoo.swift",  2, 0, 0),
            (USR_IDS["caller"], "Caller", "class",           None, "swift", "UI",     "UI/Caller.swift",      1, 0, 0),
            (USR_IDS["run"],    "run()",  "instance-method", None, "swift", "UI",     "UI/Caller.swift",      5, 0, 0),
        ],
    )
    cur.executemany(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, USR_IDS["foo"], "Core/Foo.swift",      1, 7,  2,  None, "u1"),
            (2, USR_IDS["bar"], "Core/Foo.swift",      3, 17, 2,  USR_IDS["foo"], "u1"),
            (3, USR_IDS["foo"], "Domain/SubFoo.swift", 2, 24, 4,  None, "u2"),
            (4, USR_IDS["bar"], "UI/Caller.swift",     42, 18, 32, USR_IDS["run"], "u3"),
            (5, USR_IDS["qux"], "Core/Foo.swift",      7, 17, 2,  USR_IDS["foo"], "u1"),
        ],
    )
    cur.executemany(
        "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (3, USR_IDS["subfoo"], "SubFoo", "baseOf",   0),
            (4, USR_IDS["run"],    "run()",  "calledBy", 0),
            (2, USR_IDS["foo"],    "Foo",    "childOf",  0),
            (5, USR_IDS["foo"],    "Foo",    "childOf",  0),
        ],
    )
    schema.apply_indexes(conn)
    conn.commit()
    yield conn
    conn.close()

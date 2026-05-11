from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA_VERSION = 4

CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS usrs (
      id   INTEGER PRIMARY KEY AUTOINCREMENT,
      text TEXT NOT NULL UNIQUE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS symbols (
      usr_id     INTEGER PRIMARY KEY,
      name       TEXT NOT NULL,
      kind       TEXT NOT NULL,
      sub_kind   TEXT,
      language   TEXT NOT NULL,
      module     TEXT,
      file       TEXT,
      line       INTEGER,
      is_system  INTEGER NOT NULL DEFAULT 0,
      properties INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE IF NOT EXISTS occurrences (
      id               INTEGER PRIMARY KEY,
      symbol_usr_id    INTEGER NOT NULL,
      file             TEXT NOT NULL,
      line             INTEGER NOT NULL,
      column           INTEGER NOT NULL,
      roles            INTEGER NOT NULL,
      container_usr_id INTEGER,
      unit_name        TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS relations (
      occurrence_id  INTEGER NOT NULL,
      related_usr_id INTEGER NOT NULL,
      related_name   TEXT,
      kind           TEXT NOT NULL,
      roles          INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS units (
      name       TEXT PRIMARY KEY,
      main_file  TEXT,
      module     TEXT,
      target     TEXT,
      provider   TEXT,
      mtime_ns   INTEGER NOT NULL DEFAULT 0,
      size_bytes INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS unit_files (
      unit_name TEXT NOT NULL,
      file      TEXT NOT NULL,
      PRIMARY KEY (unit_name, file)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
      path      TEXT PRIMARY KEY,
      mtime_ns  INTEGER
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
      key   TEXT PRIMARY KEY,
      value TEXT
    );
    """,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_sym_module       ON symbols(module);",
    "CREATE INDEX IF NOT EXISTS idx_sym_kind         ON symbols(kind);",
    "CREATE INDEX IF NOT EXISTS idx_sym_name_nocase  ON symbols(name COLLATE NOCASE);",
    "CREATE INDEX IF NOT EXISTS idx_sym_name         ON symbols(name);",
    "CREATE INDEX IF NOT EXISTS idx_sym_file         ON symbols(file);",
    "CREATE INDEX IF NOT EXISTS idx_occ_symbol       ON occurrences(symbol_usr_id);",
    "CREATE INDEX IF NOT EXISTS idx_occ_file_line    ON occurrences(file, line, column);",
    "CREATE INDEX IF NOT EXISTS idx_occ_container    ON occurrences(container_usr_id);",
    "CREATE INDEX IF NOT EXISTS idx_occ_unit         ON occurrences(unit_name);",
    "CREATE INDEX IF NOT EXISTS idx_rel_related_kind ON relations(related_usr_id, kind);",
    "CREATE INDEX IF NOT EXISTS idx_rel_occ          ON relations(occurrence_id);",
    "CREATE INDEX IF NOT EXISTS idx_unit_files_file  ON unit_files(file);",
    "CREATE INDEX IF NOT EXISTS idx_unit_files_unit  ON unit_files(unit_name);",
)


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the schema_version recorded in meta, or None if not present/legible."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        row = cursor.fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def apply_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    for stmt in CREATE_STATEMENTS:
        cursor.execute(stmt)
    conn.commit()


def apply_indexes(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    for stmt in INDEX_STATEMENTS:
        cursor.execute(stmt)
    conn.commit()


def write_meta(conn: sqlite3.Connection, **values: Any) -> None:
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [(key, str(value)) for key, value in values.items()],
    )
    conn.commit()


def configure_for_query(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA mmap_size = 268435456")
    cursor.execute("PRAGMA cache_size = -65536")
    cursor.execute("PRAGMA query_only = ON")

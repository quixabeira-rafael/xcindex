// Authoritative SQL schema for the xcindex SQLite cache.
//
// The helper owns writes; Python's `src/xcindex/schema.py` carries
// `SCHEMA_VERSION` for compatibility checks but no longer applies any of
// these statements itself.

import Foundation

enum Schema {
    static let version: Int = 3

    /// CREATE TABLE statements run on a fresh DB during bootstrap.
    /// Indexes are created separately at the end of bootstrap (idx).
    static let createStatements: [String] = [
        """
        CREATE TABLE symbols (
          usr        TEXT PRIMARY KEY,
          name       TEXT NOT NULL,
          kind       TEXT NOT NULL,
          sub_kind   TEXT,
          language   TEXT NOT NULL,
          module     TEXT,
          file       TEXT,
          line       INTEGER,
          is_system  INTEGER NOT NULL DEFAULT 0,
          properties INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE occurrences (
          id            INTEGER PRIMARY KEY,
          symbol_usr    TEXT NOT NULL,
          file          TEXT NOT NULL,
          line          INTEGER NOT NULL,
          column        INTEGER NOT NULL,
          roles         INTEGER NOT NULL,
          container_usr TEXT,
          unit_name     TEXT
        )
        """,
        """
        CREATE TABLE relations (
          occurrence_id INTEGER NOT NULL,
          related_usr   TEXT NOT NULL,
          related_name  TEXT,
          kind          TEXT NOT NULL,
          roles         INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE units (
          name       TEXT PRIMARY KEY,
          main_file  TEXT,
          module     TEXT,
          target     TEXT,
          provider   TEXT,
          mtime_ns   INTEGER NOT NULL DEFAULT 0,
          size_bytes INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE unit_files (
          unit_name TEXT NOT NULL,
          file      TEXT NOT NULL,
          PRIMARY KEY (unit_name, file)
        )
        """,
        """
        CREATE TABLE files (
          path     TEXT PRIMARY KEY,
          mtime_ns INTEGER
        )
        """,
        """
        CREATE TABLE meta (
          key   TEXT PRIMARY KEY,
          value TEXT
        )
        """,
    ]

    /// Index statements applied AFTER bulk inserts complete. Building B-trees
    /// from a populated table is dramatically faster than maintaining them
    /// during the inserts.
    static let indexStatements: [String] = [
        "CREATE INDEX idx_sym_module       ON symbols(module)",
        "CREATE INDEX idx_sym_kind         ON symbols(kind)",
        "CREATE INDEX idx_sym_name_nocase  ON symbols(name COLLATE NOCASE)",
        "CREATE INDEX idx_sym_file         ON symbols(file)",
        "CREATE INDEX idx_occ_symbol       ON occurrences(symbol_usr)",
        "CREATE INDEX idx_occ_file_line    ON occurrences(file, line, column)",
        "CREATE INDEX idx_occ_container    ON occurrences(container_usr)",
        "CREATE INDEX idx_occ_unit         ON occurrences(unit_name)",
        "CREATE INDEX idx_rel_related_kind ON relations(related_usr, kind)",
        "CREATE INDEX idx_rel_occ          ON relations(occurrence_id)",
        "CREATE INDEX idx_unit_files_file  ON unit_files(file)",
        "CREATE INDEX idx_unit_files_unit  ON unit_files(unit_name)",
    ]

    /// PRAGMAs applied right after opening the SQLite for a write session.
    /// These are unsafe-but-fast settings appropriate for a regenerable cache.
    static let writePragmas: [String] = [
        "PRAGMA synchronous = OFF",
        "PRAGMA journal_mode = MEMORY",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA cache_size = -65536",
    ]
}

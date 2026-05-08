"""Incremental cache updates: re-process only the units that changed.

The IndexStore is partitioned by units (one .o per source file in Xcode/SwiftPM).
A build incremental rewrites only units whose source changed; the rest stay byte-
identical on disk. The cache mirrors that partitioning via the `unit_files` table
so we can DELETE rows scoped to changed files and re-fetch them via `dump-files`,
avoiding the full ~3 min re-dump.

Lifecycle of a unit:

    on disk            cache snapshot (`units` table)        action
    ----------------   ----------------------------------    -----------------
    name + size + ts   <missing>                             added → fallback to
                                                              full dump (no main_file
                                                              mapping yet)
    name + size + ts   name + matching size + matching ts    no-op (cache hit)
    name + size + ts'  name + size + ts                      modified → DELETE
                                                              file rows, dump-files,
                                                              INSERT, refresh snapshot
    <missing>          name                                  removed → DELETE rows,
                                                              drop snapshot row
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from xcindex import dumper
from xcindex import helper as helper_module
from xcindex import schema as schema_module


@dataclass(frozen=True)
class UnitDelta:
    added: frozenset[str] = field(default_factory=frozenset)
    removed: frozenset[str] = field(default_factory=frozenset)
    modified: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    @property
    def needs_full_redump(self) -> bool:
        """Added units force a full re-dump because we can't map them to files yet."""
        return bool(self.added)


def compute_unit_delta(sqlite_path: Path, index_store: Path) -> UnitDelta:
    """Compare on-disk units vs the cached snapshot; return what differs."""
    units_dir = index_store / "v5" / "units"
    if not units_dir.exists():
        return UnitDelta()

    current: dict[str, tuple[int, int]] = {}
    for entry in units_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        current[entry.name] = (stat.st_size, stat.st_mtime_ns)

    cached: dict[str, tuple[int, int]] = {}
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        cursor = conn.execute("SELECT name, size_bytes, mtime_ns FROM units")
        for name, size, mtime in cursor.fetchall():
            cached[name] = (int(size), int(mtime))
    finally:
        conn.close()

    current_names = set(current)
    cached_names = set(cached)

    added = current_names - cached_names
    removed = cached_names - current_names
    modified = {
        name for name in current_names & cached_names
        if current[name] != cached[name]
    }

    return UnitDelta(
        added=frozenset(added),
        removed=frozenset(removed),
        modified=frozenset(modified),
    )


def refresh_units_snapshot(conn: sqlite3.Connection, index_store: Path) -> int:
    """Rewrite the `units` table so it matches the on-disk unit list (size, mtime).

    Idempotent: safe to call after every dump or incremental update.
    """
    units_dir = index_store / "v5" / "units"
    if not units_dir.exists():
        return 0

    current: list[tuple[str, int, int]] = []
    for entry in units_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        current.append((entry.name, stat.st_size, stat.st_mtime_ns))

    with conn:
        conn.execute("DELETE FROM units")
        conn.executemany(
            "INSERT INTO units(name, size_bytes, mtime_ns) VALUES (?, ?, ?)",
            current,
        )
    return len(current)


def apply_incremental_update(
    sqlite_path: Path,
    delta: UnitDelta,
    index_store: Path,
    helper_binary: Path,
    *,
    include_system: bool = False,
) -> dumper.DumpStats:
    """Apply a delta to the persistent SQLite cache.

    Caller must hold the project file lock for the duration. `delta.added` must be
    empty (otherwise caller should fall back to a full re-dump).
    """
    if delta.needs_full_redump:
        raise ValueError(
            "apply_incremental_update cannot handle added units; "
            "caller must fall back to full re-dump"
        )

    affected_units = delta.removed | delta.modified
    files_to_redump: set[str] = set()

    conn = sqlite3.connect(str(sqlite_path))
    try:
        if delta.modified:
            placeholders = ",".join("?" * len(delta.modified))
            cur = conn.execute(
                f"SELECT DISTINCT file FROM unit_files WHERE unit_name IN ({placeholders})",
                list(delta.modified),
            )
            files_to_redump = {row[0] for row in cur}

        # 1. Drop affected unit + unit_files rows. Drop occurrences/relations/
        #    symbol-definitions for the files that need re-dumping.
        with conn:
            if affected_units:
                up = ",".join("?" * len(affected_units))
                conn.execute(
                    f"DELETE FROM unit_files WHERE unit_name IN ({up})",
                    list(affected_units),
                )
                conn.execute(
                    f"DELETE FROM units WHERE name IN ({up})",
                    list(affected_units),
                )
            if files_to_redump:
                fp = ",".join("?" * len(files_to_redump))
                files_list = list(files_to_redump)
                conn.execute(
                    f"DELETE FROM relations WHERE occurrence_id IN "
                    f"(SELECT id FROM occurrences WHERE file IN ({fp}))",
                    files_list,
                )
                conn.execute(
                    f"DELETE FROM occurrences WHERE file IN ({fp})",
                    files_list,
                )
                conn.execute(
                    f"DELETE FROM symbols WHERE file IN ({fp})",
                    files_list,
                )

        # 2. Re-fetch via helper for the affected files.
        stats = dumper.DumpStats(symbols=0, occurrences=0, relations=0, units=0)
        if files_to_redump:
            records = helper_module.stream_dump_files(
                index_store,
                sorted(files_to_redump),
                include_system=include_system,
                helper_path=helper_binary,
            )
            stats = dumper._ingest(conn, records)

        # 3. Refresh the units snapshot for delta members. We resnap the entire
        #    table — cheap, ~12k INSERTs — to keep size_bytes/mtime_ns accurate.
        refresh_units_snapshot(conn, index_store)

        # 4. Update meta diagnostics.
        schema_module.write_meta(
            conn,
            last_incremental_files=len(files_to_redump),
            last_incremental_units=len(affected_units),
        )
    finally:
        conn.close()

    return stats

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from xcindex import __version__ as XCINDEX_VERSION
from xcindex import helper as helper_module
from xcindex import schema as schema_module

BATCH_SIZE = 10_000


@dataclass(frozen=True)
class DumpStats:
    symbols: int
    occurrences: int
    relations: int
    units: int


def dump_to_sqlite(
    sqlite_path: Path,
    records: Iterable[dict[str, Any]],
    *,
    index_hash: str,
    swift_version: str | None = None,
    helper_version: str | None = None,
) -> DumpStats:
    """Stream NDJSON-like dicts into a freshly-built SQLite at sqlite_path.

    The connection is closed before returning. The caller is responsible for atomic
    placement of the resulting file (typically: write to .tmp then rename).
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_path.exists():
        sqlite_path.unlink()
    conn = sqlite3.connect(str(sqlite_path))
    try:
        schema_module.apply_schema(conn)
        schema_module.configure_for_dump(conn)
        stats = _ingest(conn, records)
        schema_module.apply_indexes(conn)
        schema_module.write_meta(
            conn,
            schema_version=schema_module.SCHEMA_VERSION,
            xcindex_version=XCINDEX_VERSION,
            index_hash=index_hash,
            swift_version=swift_version or "",
            helper_version=helper_version or "",
            symbols_count=stats.symbols,
            occurrences_count=stats.occurrences,
            relations_count=stats.relations,
            units_count=stats.units,
        )
    finally:
        conn.close()
    return stats


def dump_from_helper(
    sqlite_path: Path,
    index_store_path: Path,
    *,
    index_hash: str,
    include_system: bool = False,
    swift_version: str | None = None,
    helper_version: str | None = None,
) -> DumpStats:
    """Convenience: spawn helper and stream its NDJSON into SQLite."""
    records = helper_module.stream_dump(
        index_store_path,
        include_system=include_system,
    )
    return dump_to_sqlite(
        sqlite_path,
        records,
        index_hash=index_hash,
        swift_version=swift_version,
        helper_version=helper_version,
    )


# --- Internal: batching --------------------------------------------------------

SYMBOL_COLS = (
    "usr", "name", "kind", "sub_kind", "language",
    "module", "file", "line", "is_system", "properties",
)
SYMBOL_INSERT = (
    "INSERT OR REPLACE INTO symbols("
    + ",".join(SYMBOL_COLS)
    + ") VALUES (" + ",".join(["?"] * len(SYMBOL_COLS)) + ")"
)

OCCURRENCE_COLS = (
    "id", "symbol_usr", "file", "line", "column",
    "roles", "container_usr", "unit_name",
)
OCCURRENCE_INSERT = (
    "INSERT INTO occurrences("
    + ",".join(OCCURRENCE_COLS)
    + ") VALUES (" + ",".join(["?"] * len(OCCURRENCE_COLS)) + ")"
)

RELATION_COLS = ("occurrence_id", "related_usr", "related_name", "kind", "roles")
RELATION_INSERT = (
    "INSERT INTO relations("
    + ",".join(RELATION_COLS)
    + ") VALUES (" + ",".join(["?"] * len(RELATION_COLS)) + ")"
)

UNIT_COLS = ("name", "main_file", "module", "target", "provider", "mtime_ns")
UNIT_INSERT = (
    "INSERT OR REPLACE INTO units("
    + ",".join(UNIT_COLS)
    + ") VALUES (" + ",".join(["?"] * len(UNIT_COLS)) + ")"
)


def _ingest(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> DumpStats:
    cursor = conn.cursor()
    cursor.execute("BEGIN")

    symbol_batch: list[tuple] = []
    occurrence_batch: list[tuple] = []
    relation_batch: list[tuple] = []
    unit_batch: list[tuple] = []

    counts = {"symbol": 0, "occurrence": 0, "relation": 0, "unit": 0}

    for record in records:
        record_type = record.get("type")
        if record_type == "symbol":
            symbol_batch.append(_symbol_row(record))
            counts["symbol"] += 1
            if len(symbol_batch) >= BATCH_SIZE:
                cursor.executemany(SYMBOL_INSERT, symbol_batch)
                symbol_batch.clear()
        elif record_type == "occurrence":
            occurrence_batch.append(_occurrence_row(record))
            counts["occurrence"] += 1
            if len(occurrence_batch) >= BATCH_SIZE:
                cursor.executemany(OCCURRENCE_INSERT, occurrence_batch)
                occurrence_batch.clear()
        elif record_type == "relation":
            relation_batch.append(_relation_row(record))
            counts["relation"] += 1
            if len(relation_batch) >= BATCH_SIZE:
                cursor.executemany(RELATION_INSERT, relation_batch)
                relation_batch.clear()
        elif record_type == "unit":
            unit_batch.append(_unit_row(record))
            counts["unit"] += 1
            if len(unit_batch) >= BATCH_SIZE:
                cursor.executemany(UNIT_INSERT, unit_batch)
                unit_batch.clear()
        else:
            continue

    if symbol_batch:
        cursor.executemany(SYMBOL_INSERT, symbol_batch)
    if occurrence_batch:
        cursor.executemany(OCCURRENCE_INSERT, occurrence_batch)
    if relation_batch:
        cursor.executemany(RELATION_INSERT, relation_batch)
    if unit_batch:
        cursor.executemany(UNIT_INSERT, unit_batch)

    conn.commit()
    return DumpStats(
        symbols=counts["symbol"],
        occurrences=counts["occurrence"],
        relations=counts["relation"],
        units=counts["unit"],
    )


# --- Internal: row builders ----------------------------------------------------

def _symbol_row(record: dict[str, Any]) -> tuple:
    return (
        record["usr"],
        record["name"],
        record["kind"],
        record.get("sub_kind"),
        record.get("language", "swift"),
        record.get("module"),
        record.get("file"),
        record.get("line"),
        1 if record.get("is_system") else 0,
        int(record.get("properties", 0)),
    )


def _occurrence_row(record: dict[str, Any]) -> tuple:
    return (
        record["id"],
        record["symbol_usr"],
        record["file"],
        record["line"],
        record["column"],
        _signed_64(int(record.get("roles", 0))),
        record.get("container_usr"),
        record.get("unit_name"),
    )


def _relation_row(record: dict[str, Any]) -> tuple:
    return (
        record["occurrence_id"],
        record["related_usr"],
        record.get("related_name"),
        record.get("kind", "other"),
        _signed_64(int(record.get("roles", 0))),
    )


def _signed_64(value: int) -> int:
    """Convert UInt64 (from JSON) to a signed 64-bit int that SQLite accepts."""
    if value >= (1 << 63):
        return value - (1 << 64)
    return value


def _unit_row(record: dict[str, Any]) -> tuple:
    return (
        record["name"],
        record.get("main_file"),
        record.get("module"),
        record.get("target"),
        record.get("provider"),
        int(record.get("mtime_ns", 0)),
    )

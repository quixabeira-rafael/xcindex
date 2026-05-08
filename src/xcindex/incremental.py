"""Delta detection between the on-disk IndexStore unit list and the cached
units snapshot. The actual DELETE+INSERT work runs in the Swift helper
(see `swift-helper/Sources/xcindex-helper/Incremental.swift`); this module
just decides what changed.

Unit lifecycle:

    on disk            cache snapshot (`units` table)        action
    ----------------   ----------------------------------    -----------------
    name + size + ts   <missing>                             added → fallback
                                                               to full bootstrap
                                                               (we don't know
                                                               its files yet)
    name + size + ts   matching size + matching ts            no-op (cache hit)
    name + size + ts'  name + size + ts                       modified → helper
                                                               re-walks
    <missing>          name                                   removed → helper
                                                               drops rows
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


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

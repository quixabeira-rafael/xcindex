#!/usr/bin/env python3
"""POC: re-materialize a v3 xcindex cache as v4 with USR interning.

Reads an existing `index.sqlite` (v3 schema, ~2GB on WW) and writes a sibling
`index_v4_poc.sqlite` with USRs normalized into a separate `usrs` table —
each USR appears exactly once globally; all references become INTEGER ids.

This script is a standalone experiment: it does NOT touch xcindex's
production materialization or query paths. The output is meant for size
and query-latency measurement only.

Usage:
  python scripts/usr_interning_poc.py <project_path>
    [--dst <output_path>]
    [--bench]      # also run query latency comparison (impact-style queries)

Examples:
  python scripts/usr_interning_poc.py /Users/rafael/Documents/Lumenalta/WW/ios-mobile
  python scripts/usr_interning_poc.py <path> --bench
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from xcindex import cache as cache_module  # noqa: E402
from xcindex import discovery  # noqa: E402


V4_SCHEMA = """
CREATE TABLE usrs (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE
);

CREATE TABLE symbols (
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

CREATE TABLE occurrences (
    id               INTEGER PRIMARY KEY,
    symbol_usr_id    INTEGER NOT NULL,
    file             TEXT NOT NULL,
    line             INTEGER NOT NULL,
    column           INTEGER NOT NULL,
    roles            INTEGER NOT NULL,
    container_usr_id INTEGER,
    unit_name        TEXT
);

CREATE TABLE relations (
    occurrence_id  INTEGER NOT NULL,
    related_usr_id INTEGER NOT NULL,
    related_name   TEXT,
    kind           TEXT NOT NULL,
    roles          INTEGER NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

V4_INDEXES = """
CREATE INDEX idx_sym_module       ON symbols(module);
CREATE INDEX idx_sym_kind         ON symbols(kind);
CREATE INDEX idx_sym_name_nocase  ON symbols(name COLLATE NOCASE);
CREATE INDEX idx_sym_file         ON symbols(file);
CREATE INDEX idx_occ_symbol       ON occurrences(symbol_usr_id);
CREATE INDEX idx_occ_file_line    ON occurrences(file, line, column);
CREATE INDEX idx_occ_container    ON occurrences(container_usr_id);
CREATE INDEX idx_occ_unit         ON occurrences(unit_name);
CREATE INDEX idx_rel_related_kind ON relations(related_usr_id, kind);
CREATE INDEX idx_rel_occ          ON relations(occurrence_id);
"""


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def materialize_v4(src_path: Path, dst_path: Path) -> dict:
    """Read v3 cache from src_path; write v4 cache (with USR interning) to dst_path.

    Returns a stats dict with timings, row counts, and intern stats.
    """
    if dst_path.exists():
        dst_path.unlink()

    t0 = time.monotonic()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.execute("PRAGMA mmap_size = 268435456")
    src.execute("PRAGMA cache_size = -65536")

    dst = sqlite3.connect(str(dst_path))
    dst.execute("PRAGMA journal_mode = WAL")
    dst.execute("PRAGMA synchronous = OFF")
    dst.execute("PRAGMA temp_store = MEMORY")
    dst.executescript(V4_SCHEMA)

    # === phase 1: collect distinct USRs from all sources ===
    print("phase 1/4: collecting distinct USRs...")
    t_phase = time.monotonic()
    usr_to_id: dict[str, int] = {}
    next_id = 1

    def intern(usr: str | None) -> int | None:
        nonlocal next_id
        if usr is None:
            return None
        existing = usr_to_id.get(usr)
        if existing is not None:
            return existing
        usr_to_id[usr] = next_id
        next_id += 1
        return next_id - 1

    cur = src.execute("SELECT usr FROM symbols")
    for (usr,) in cur:
        intern(usr)
    cur = src.execute("SELECT DISTINCT symbol_usr FROM occurrences")
    for (usr,) in cur:
        intern(usr)
    cur = src.execute("SELECT DISTINCT container_usr FROM occurrences WHERE container_usr IS NOT NULL")
    for (usr,) in cur:
        intern(usr)
    cur = src.execute("SELECT DISTINCT related_usr FROM relations")
    for (usr,) in cur:
        intern(usr)
    print(f"  collected {len(usr_to_id)} distinct USRs ({time.monotonic() - t_phase:.1f}s)")

    # === phase 2: bulk insert intern table ===
    print("phase 2/4: writing usrs table...")
    t_phase = time.monotonic()
    dst.executemany(
        "INSERT INTO usrs(id, text) VALUES (?, ?)",
        ((id_, text) for text, id_ in usr_to_id.items()),
    )
    dst.commit()
    print(f"  inserted {len(usr_to_id)} rows ({time.monotonic() - t_phase:.1f}s)")

    # === phase 3: copy symbols ===
    print("phase 3/4: copying symbols/occurrences/relations...")
    t_phase = time.monotonic()
    cur = src.execute(
        "SELECT usr, name, kind, sub_kind, language, module, file, line, is_system, properties FROM symbols"
    )
    sym_count = 0
    BATCH = 5000

    def gen_symbols():
        nonlocal sym_count
        batch = []
        for row in cur:
            usr, *rest = row
            batch.append((usr_to_id[usr], *rest))
            sym_count += 1
            if len(batch) >= BATCH:
                yield from batch
                batch = []
        yield from batch

    dst.executemany(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) VALUES (?,?,?,?,?,?,?,?,?,?)",
        gen_symbols(),
    )
    dst.commit()

    # occurrences
    cur = src.execute(
        "SELECT id, symbol_usr, file, line, column, roles, container_usr, unit_name FROM occurrences"
    )
    occ_count = 0

    def gen_occurrences():
        nonlocal occ_count
        for row in cur:
            id_, symbol_usr, file, line, col, roles, container_usr, unit_name = row
            occ_count += 1
            yield (
                id_,
                usr_to_id[symbol_usr],
                file, line, col, roles,
                usr_to_id[container_usr] if container_usr is not None else None,
                unit_name,
            )

    dst.executemany(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) VALUES (?,?,?,?,?,?,?,?)",
        gen_occurrences(),
    )
    dst.commit()

    # relations
    cur = src.execute(
        "SELECT occurrence_id, related_usr, related_name, kind, roles FROM relations"
    )
    rel_count = 0

    def gen_relations():
        nonlocal rel_count
        for row in cur:
            occ_id, related_usr, related_name, kind, roles = row
            rel_count += 1
            yield (occ_id, usr_to_id[related_usr], related_name, kind, roles)

    dst.executemany(
        "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) VALUES (?,?,?,?,?)",
        gen_relations(),
    )
    dst.commit()
    print(f"  copied {sym_count} symbols, {occ_count} occurrences, {rel_count} relations ({time.monotonic() - t_phase:.1f}s)")

    # === phase 4: indexes ===
    print("phase 4/4: building indexes...")
    t_phase = time.monotonic()
    dst.executescript(V4_INDEXES)
    dst.commit()
    print(f"  built indexes ({time.monotonic() - t_phase:.1f}s)")

    # final settle
    dst.execute("PRAGMA optimize")
    dst.commit()
    dst.close()
    src.close()

    return {
        "wall_seconds": time.monotonic() - t0,
        "usrs": len(usr_to_id),
        "symbols": sym_count,
        "occurrences": occ_count,
        "relations": rel_count,
    }


def measure_storage(src_path: Path, dst_path: Path) -> None:
    src_mb = file_size_mb(src_path)
    dst_mb = file_size_mb(dst_path)
    delta_mb = src_mb - dst_mb
    pct = (delta_mb / src_mb) * 100 if src_mb else 0.0
    print()
    print("=" * 70)
    print("STORAGE")
    print("=" * 70)
    print(f"  v3 (current):  {src_mb:>8.1f} MB  {src_path}")
    print(f"  v4 (interned): {dst_mb:>8.1f} MB  {dst_path}")
    print(f"  delta:         {delta_mb:>+8.1f} MB  ({pct:+.1f}%)")


def _pick_test_data(src: sqlite3.Connection) -> dict:
    """Pick representative parameters from the live cache for benchmarking."""
    data = {}
    # A non-system instance-method that has callers (good for reach/impact)
    row = src.execute(
        """
        SELECT s.usr, s.name, s.file, s.line FROM symbols s
        WHERE s.kind = 'instance-method' AND s.is_system = 0
          AND EXISTS (
            SELECT 1 FROM occurrences o JOIN relations r ON r.occurrence_id = o.id
            WHERE o.symbol_usr = s.usr AND r.kind = 'calledBy' LIMIT 1
          )
        LIMIT 1
        """
    ).fetchone()
    if row:
        data["method_usr"], data["method_name"], data["method_file"], data["method_line"] = row
    # A non-system class with subclasses or extensions (good for type queries)
    row = src.execute(
        """
        SELECT s.usr, s.name, s.file, s.line FROM symbols s
        WHERE s.kind = 'class' AND s.is_system = 0 AND s.module IS NOT NULL
        LIMIT 1
        """
    ).fetchone()
    if row:
        data["class_usr"], data["class_name"], data["class_file"], data["class_line"] = row
    # A non-system property
    row = src.execute(
        """
        SELECT s.usr, s.name, s.file, s.line FROM symbols s
        WHERE s.kind = 'instance-property' AND s.is_system = 0
        LIMIT 1
        """
    ).fetchone()
    if row:
        data["prop_usr"], data["prop_name"], data["prop_file"], data["prop_line"] = row
    return data


def _resolve_v4_id(dst: sqlite3.Connection, usr: str) -> int | None:
    row = dst.execute("SELECT id FROM usrs WHERE text = ?", (usr,)).fetchone()
    return row[0] if row else None


def _time_query(conn: sqlite3.Connection, sql: str, params: tuple, *, repeats: int) -> tuple[float, int]:
    """Run query `repeats` times, return (median_ms, row_count)."""
    last_count = 0
    times = []
    for _ in range(repeats):
        t = time.monotonic()
        rows = conn.execute(sql, params).fetchall()
        times.append((time.monotonic() - t) * 1000)
        last_count = len(rows)
    return statistics.median(times), last_count


def _bench_one(label: str, v3, v3p, v4, v4p, src, dst, repeats=3) -> dict:
    v3_ms, v3_rows = _time_query(src, v3, v3p, repeats=repeats)
    v4_ms, v4_rows = _time_query(dst, v4, v4p, repeats=repeats)
    return {
        "label": label,
        "v3_ms": v3_ms, "v4_ms": v4_ms,
        "v3_rows": v3_rows, "v4_rows": v4_rows,
        "delta_pct": ((v4_ms - v3_ms) / v3_ms * 100) if v3_ms > 0.001 else 0.0,
        "rows_match": v3_rows == v4_rows,
    }


def benchmark(src_path: Path, dst_path: Path, *, repeats: int = 5) -> None:
    print()
    print("=" * 95)
    print(f"COMPREHENSIVE QUERY BENCH (median of {repeats} runs)")
    print("=" * 95)

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(f"file:{dst_path}?mode=ro", uri=True)
    for c in (src, dst):
        c.execute("PRAGMA mmap_size = 268435456")
        c.execute("PRAGMA cache_size = -65536")
        # warm pages with a quick scan
        c.execute("SELECT COUNT(*) FROM symbols").fetchone()
        c.execute("SELECT COUNT(*) FROM occurrences").fetchone()
        c.execute("SELECT COUNT(*) FROM relations").fetchone()

    data = _pick_test_data(src)
    if "method_usr" not in data:
        print("  (no representative method found; aborting)")
        return

    method_usr = data["method_usr"]
    class_usr = data.get("class_usr", method_usr)
    prop_usr = data.get("prop_usr", method_usr)
    method_id = _resolve_v4_id(dst, method_usr)
    class_id = _resolve_v4_id(dst, class_usr)
    prop_id = _resolve_v4_id(dst, prop_usr)

    method_file = data["method_file"]
    method_line = data["method_line"]
    class_file = data.get("class_file", method_file)
    class_line = data.get("class_line", method_line)

    print(f"  method:   {data['method_name']} @ {method_file}:{method_line}")
    print(f"  class:    {data.get('class_name', 'n/a')} @ {class_file}")
    print(f"  property: {data.get('prop_name', 'n/a')}")
    print()

    benches: list[dict] = []

    # === Symbol lookups ===
    benches.append(_bench_one(
        "symbol by USR (PK lookup)",
        "SELECT * FROM symbols WHERE usr = ?", (method_usr,),
        "SELECT s.* FROM symbols s JOIN usrs u ON u.id = s.usr_id WHERE u.text = ?", (method_usr,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "symbol by name (exact)",
        "SELECT * FROM symbols WHERE name = ?", (data["method_name"],),
        "SELECT * FROM symbols WHERE name = ?", (data["method_name"],),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "search NOCASE substring",
        "SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE LIMIT 50",
        ("%manager%",),
        "SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE LIMIT 50",
        ("%manager%",),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "search filtered by kind",
        "SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE AND kind = ? LIMIT 50",
        ("%manager%", "class"),
        "SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE AND kind = ? LIMIT 50",
        ("%manager%", "class"),
        src, dst, repeats=repeats,
    ))

    # === at / containing ===
    benches.append(_bench_one(
        "at file:line",
        "SELECT * FROM occurrences WHERE file = ? AND line = ?",
        (method_file, method_line),
        "SELECT * FROM occurrences WHERE file = ? AND line = ?",
        (method_file, method_line),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "containing file:line (largest <=)",
        """SELECT s.usr, s.name, s.kind, s.file, s.line FROM symbols s
           WHERE s.file = ? AND s.line <= ? ORDER BY s.line DESC LIMIT 1""",
        (method_file, method_line + 5),
        """SELECT u.text, s.name, s.kind, s.file, s.line FROM symbols s
           JOIN usrs u ON u.id = s.usr_id
           WHERE s.file = ? AND s.line <= ? ORDER BY s.line DESC LIMIT 1""",
        (method_file, method_line + 5),
        src, dst, repeats=repeats,
    ))

    # === file (file definitions) ===
    benches.append(_bench_one(
        "file: top-level types",
        """SELECT * FROM symbols WHERE file = ?
           AND kind IN ('class','struct','enum','protocol') AND is_system = 0
           ORDER BY line LIMIT 50""",
        (method_file,),
        """SELECT * FROM symbols WHERE file = ?
           AND kind IN ('class','struct','enum','protocol') AND is_system = 0
           ORDER BY line LIMIT 50""",
        (method_file,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "file: --all (every definition)",
        "SELECT * FROM symbols WHERE file = ? AND is_system = 0 ORDER BY line LIMIT 200",
        (method_file,),
        "SELECT * FROM symbols WHERE file = ? AND is_system = 0 ORDER BY line LIMIT 200",
        (method_file,),
        src, dst, repeats=repeats,
    ))

    # === find files (LIKE on file path) ===
    benches.append(_bench_one(
        "find_files_in_index basename",
        "SELECT DISTINCT file FROM symbols WHERE file LIKE ? AND is_system = 0",
        (f"%/{Path(method_file).name}",),
        "SELECT DISTINCT file FROM symbols WHERE file LIKE ? AND is_system = 0",
        (f"%/{Path(method_file).name}",),
        src, dst, repeats=repeats,
    ))

    # === occurrences (all + role-filtered) ===
    benches.append(_bench_one(
        "occurrences (all)",
        """SELECT o.id, o.file, o.line, o.column, o.roles, o.container_usr
           FROM occurrences o WHERE o.symbol_usr = ? LIMIT 50""",
        (method_usr,),
        """SELECT o.id, o.file, o.line, o.column, o.roles, cu.text
           FROM occurrences o
           LEFT JOIN usrs cu ON cu.id = o.container_usr_id
           WHERE o.symbol_usr_id = ? LIMIT 50""",
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "occurrences --role call",
        """SELECT o.id, o.file, o.line FROM occurrences o
           WHERE o.symbol_usr = ? AND (o.roles & 32) != 0 LIMIT 50""",
        (method_usr,),
        """SELECT o.id, o.file, o.line FROM occurrences o
           WHERE o.symbol_usr_id = ? AND (o.roles & 32) != 0 LIMIT 50""",
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "occurrences --role read (property)",
        """SELECT o.id, o.file, o.line FROM occurrences o
           WHERE o.symbol_usr = ? AND (o.roles & 8) != 0 LIMIT 50""",
        (prop_usr,),
        """SELECT o.id, o.file, o.line FROM occurrences o
           WHERE o.symbol_usr_id = ? AND (o.roles & 8) != 0 LIMIT 50""",
        (prop_id,),
        src, dst, repeats=repeats,
    ))

    # === relations (in/out, multiple kinds) ===
    benches.append(_bench_one(
        "relations out --kind calledBy",
        """SELECT r.related_usr, r.kind FROM occurrences o
           JOIN relations r ON r.occurrence_id = o.id
           WHERE o.symbol_usr = ? AND r.kind = 'calledBy' LIMIT 50""",
        (method_usr,),
        """SELECT u.text, r.kind FROM occurrences o
           JOIN relations r ON r.occurrence_id = o.id
           JOIN usrs u ON u.id = r.related_usr_id
           WHERE o.symbol_usr_id = ? AND r.kind = 'calledBy' LIMIT 50""",
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "relations in --kind calledBy",
        """SELECT o.symbol_usr, r.kind FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           WHERE r.related_usr = ? AND r.kind = 'calledBy' LIMIT 50""",
        (method_usr,),
        """SELECT u.text, r.kind FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           JOIN usrs u ON u.id = o.symbol_usr_id
           WHERE r.related_usr_id = ? AND r.kind = 'calledBy' LIMIT 50""",
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "relations in --kind baseOf (subclasses)",
        """SELECT o.symbol_usr FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           WHERE r.related_usr = ? AND r.kind = 'baseOf' LIMIT 50""",
        (class_usr,),
        """SELECT u.text FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           JOIN usrs u ON u.id = o.symbol_usr_id
           WHERE r.related_usr_id = ? AND r.kind = 'baseOf' LIMIT 50""",
        (class_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "relations: containedBy (members of class)",
        """SELECT o.symbol_usr FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           WHERE r.related_usr = ? AND r.kind = 'containedBy' LIMIT 50""",
        (class_usr,),
        """SELECT u.text FROM relations r
           JOIN occurrences o ON o.id = r.occurrence_id
           JOIN usrs u ON u.id = o.symbol_usr_id
           WHERE r.related_usr_id = ? AND r.kind = 'containedBy' LIMIT 50""",
        (class_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "relations: extendedBy (class extensions)",
        """SELECT r.related_usr FROM occurrences o
           JOIN relations r ON r.occurrence_id = o.id
           WHERE o.symbol_usr = ? AND r.kind = 'extendedBy' LIMIT 50""",
        (class_usr,),
        """SELECT u.text FROM occurrences o
           JOIN relations r ON r.occurrence_id = o.id
           JOIN usrs u ON u.id = r.related_usr_id
           WHERE o.symbol_usr_id = ? AND r.kind = 'extendedBy' LIMIT 50""",
        (class_id,),
        src, dst, repeats=repeats,
    ))

    # === reach (recursive CTE, multiple depths) ===
    benches.append(_bench_one(
        "reach up depth=3",
        """
        WITH RECURSIVE reach(usr, depth) AS (
          SELECT ?, 0
          UNION
          SELECT r.related_usr, reach.depth + 1 FROM reach
          JOIN occurrences o ON o.symbol_usr = reach.usr
          JOIN relations r ON r.occurrence_id = o.id
          WHERE reach.depth < 3
            AND r.kind IN ('calledBy','containedBy','childOf','overrideOf','baseOf','specializationOf','extendedBy')
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr FROM reach)
        """,
        (method_usr,),
        """
        WITH RECURSIVE reach(usr_id, depth) AS (
          SELECT ?, 0
          UNION
          SELECT r.related_usr_id, reach.depth + 1 FROM reach
          JOIN occurrences o ON o.symbol_usr_id = reach.usr_id
          JOIN relations r ON r.occurrence_id = o.id
          WHERE reach.depth < 3
            AND r.kind IN ('calledBy','containedBy','childOf','overrideOf','baseOf','specializationOf','extendedBy')
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr_id FROM reach)
        """,
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "reach up depth=8 (deep)",
        """
        WITH RECURSIVE reach(usr, depth) AS (
          SELECT ?, 0
          UNION
          SELECT r.related_usr, reach.depth + 1 FROM reach
          JOIN occurrences o ON o.symbol_usr = reach.usr
          JOIN relations r ON r.occurrence_id = o.id
          WHERE reach.depth < 8 AND r.kind IN ('calledBy','overrideOf')
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr FROM reach)
        """,
        (method_usr,),
        """
        WITH RECURSIVE reach(usr_id, depth) AS (
          SELECT ?, 0
          UNION
          SELECT r.related_usr_id, reach.depth + 1 FROM reach
          JOIN occurrences o ON o.symbol_usr_id = reach.usr_id
          JOIN relations r ON r.occurrence_id = o.id
          WHERE reach.depth < 8 AND r.kind IN ('calledBy','overrideOf')
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr_id FROM reach)
        """,
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "reach down depth=3",
        """
        WITH RECURSIVE reach(usr, depth) AS (
          SELECT ?, 0
          UNION
          SELECT o.symbol_usr, reach.depth + 1 FROM reach
          JOIN relations r ON r.related_usr = reach.usr
          JOIN occurrences o ON o.id = r.occurrence_id
          WHERE reach.depth < 3 AND r.kind = 'calledBy'
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr FROM reach)
        """,
        (method_usr,),
        """
        WITH RECURSIVE reach(usr_id, depth) AS (
          SELECT ?, 0
          UNION
          SELECT o.symbol_usr_id, reach.depth + 1 FROM reach
          JOIN relations r ON r.related_usr_id = reach.usr_id
          JOIN occurrences o ON o.id = r.occurrence_id
          WHERE reach.depth < 3 AND r.kind = 'calledBy'
        )
        SELECT COUNT(*) FROM (SELECT DISTINCT usr_id FROM reach)
        """,
        (method_id,),
        src, dst, repeats=repeats,
    ))

    # === impact BFS layers (fetch_callers_layer / fetch_callees_layer style) ===
    benches.append(_bench_one(
        "impact fetch_callers_layer (1 frontier)",
        """SELECT o.symbol_usr AS callee, r.related_usr AS caller, r.kind, o.file, o.line
           FROM relations r JOIN occurrences o ON o.id = r.occurrence_id
           WHERE o.symbol_usr IN (?) AND r.kind IN ('calledBy','overrideOf')""",
        (method_usr,),
        """SELECT u_callee.text, u_caller.text, r.kind, o.file, o.line
           FROM relations r JOIN occurrences o ON o.id = r.occurrence_id
           JOIN usrs u_callee ON u_callee.id = o.symbol_usr_id
           JOIN usrs u_caller ON u_caller.id = r.related_usr_id
           WHERE o.symbol_usr_id IN (?) AND r.kind IN ('calledBy','overrideOf')""",
        (method_id,),
        src, dst, repeats=repeats,
    ))
    benches.append(_bench_one(
        "impact fetch_callees_layer (1 frontier)",
        """SELECT r.related_usr AS caller, o.symbol_usr AS callee, r.kind, o.file, o.line
           FROM relations r JOIN occurrences o ON o.id = r.occurrence_id
           WHERE r.related_usr IN (?) AND r.kind = 'calledBy'""",
        (method_usr,),
        """SELECT u_caller.text, u_callee.text, r.kind, o.file, o.line
           FROM relations r JOIN occurrences o ON o.id = r.occurrence_id
           JOIN usrs u_caller ON u_caller.id = r.related_usr_id
           JOIN usrs u_callee ON u_callee.id = o.symbol_usr_id
           WHERE r.related_usr_id IN (?) AND r.kind = 'calledBy'""",
        (method_id,),
        src, dst, repeats=repeats,
    ))

    # === type structure ===
    benches.append(_bench_one(
        "type ref containers (level-1 upstream)",
        """SELECT DISTINCT o.container_usr FROM occurrences o
           WHERE o.symbol_usr = ? AND o.container_usr IS NOT NULL
             AND o.container_usr != ? AND (o.roles & 4) != 0""",
        (class_usr, class_usr),
        """SELECT DISTINCT o.container_usr_id FROM occurrences o
           WHERE o.symbol_usr_id = ? AND o.container_usr_id IS NOT NULL
             AND o.container_usr_id != ? AND (o.roles & 4) != 0""",
        (class_id, class_id),
        src, dst, repeats=repeats,
    ))

    # === git-style (per-line containing × N) ===
    # Simulate 50 lines as the diff hunk sizes we saw on WW
    git_simulated_lines = [(method_file, method_line + i) for i in range(50)]

    def _bench_git_v3():
        for f, l in git_simulated_lines:
            src.execute(
                "SELECT s.usr, s.name FROM symbols s WHERE s.file = ? AND s.line <= ? "
                "ORDER BY s.line DESC LIMIT 1",
                (f, l),
            ).fetchall()

    def _bench_git_v4():
        for f, l in git_simulated_lines:
            dst.execute(
                "SELECT u.text, s.name FROM symbols s JOIN usrs u ON u.id = s.usr_id "
                "WHERE s.file = ? AND s.line <= ? ORDER BY s.line DESC LIMIT 1",
                (f, l),
            ).fetchall()

    v3_times = []
    v4_times = []
    for _ in range(repeats):
        t = time.monotonic(); _bench_git_v3(); v3_times.append((time.monotonic() - t) * 1000)
        t = time.monotonic(); _bench_git_v4(); v4_times.append((time.monotonic() - t) * 1000)
    v3_med = statistics.median(v3_times); v4_med = statistics.median(v4_times)
    benches.append({
        "label": "git: containing × 50 lines",
        "v3_ms": v3_med, "v4_ms": v4_med,
        "v3_rows": 50, "v4_rows": 50,
        "delta_pct": ((v4_med - v3_med) / v3_med * 100) if v3_med > 0 else 0,
        "rows_match": True,
    })

    # === Print results ===
    print(f"  {'QUERY':<48s}  {'v3 (ms)':>9s}  {'v4 (ms)':>9s}  {'delta':>7s}  {'rows':>10s}")
    print(f"  {'-' * 48}  {'-' * 9}  {'-' * 9}  {'-' * 7}  {'-' * 10}")
    for b in benches:
        rows_label = f"{b['v3_rows']}={b['v4_rows']}" if b["rows_match"] else f"{b['v3_rows']}!{b['v4_rows']}"
        delta_str = f"{b['delta_pct']:+.0f}%" if abs(b["delta_pct"]) >= 1 else "≈"
        marker = " ✗" if not b["rows_match"] else ""
        print(f"  {b['label']:<48s}  {b['v3_ms']:>9.2f}  {b['v4_ms']:>9.2f}  {delta_str:>7s}  {rows_label:>10s}{marker}")

    # Summary
    print()
    correct = sum(1 for b in benches if b["rows_match"])
    print(f"  correctness: {correct}/{len(benches)} queries return identical row counts")
    avg_delta = statistics.mean(b["delta_pct"] for b in benches if b["v3_ms"] > 0.1)
    print(f"  avg latency delta (queries >0.1ms): {avg_delta:+.1f}%")

    src.close()
    dst.close()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(f"file:{dst_path}?mode=ro", uri=True)
    for c in (src, dst):
        c.execute("PRAGMA mmap_size = 268435456")
        c.execute("PRAGMA cache_size = -65536")

    # Pick a representative USR from the v3 cache (first symbol with both
    # callers and callees, so impact-style traversal has data to walk).
    sample_usr = src.execute(
        """
        SELECT s.usr FROM symbols s
        WHERE s.kind = 'instance-method' AND s.is_system = 0
          AND EXISTS (
            SELECT 1 FROM occurrences o JOIN relations r ON r.occurrence_id = o.id
            WHERE o.symbol_usr = s.usr AND r.kind = 'calledBy' LIMIT 1
          )
        LIMIT 1
        """
    ).fetchone()
    if sample_usr is None:
        print("  (no representative USR found; skipping)")
        return
    sample_usr = sample_usr[0]
    print(f"  sample USR: {sample_usr}")
    print()

    sample_id_row = dst.execute("SELECT id FROM usrs WHERE text = ?", (sample_usr,)).fetchone()
    sample_id = sample_id_row[0] if sample_id_row else None

    queries = [
        (
            "lookup symbol by USR",
            ("SELECT * FROM symbols WHERE usr = ?", (sample_usr,)),
            (
                "SELECT s.* FROM symbols s "
                "JOIN usrs u ON u.id = s.usr_id "
                "WHERE u.text = ?",
                (sample_usr,),
            ),
        ),
        (
            "search by name (NOCASE substring)",
            ("SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE LIMIT 50", ("%manager%",)),
            ("SELECT * FROM symbols WHERE name LIKE ? COLLATE NOCASE LIMIT 50", ("%manager%",)),
        ),
        (
            "occurrences of a USR",
            (
                "SELECT id, file, line FROM occurrences WHERE symbol_usr = ? LIMIT 50",
                (sample_usr,),
            ),
            (
                "SELECT o.id, o.file, o.line FROM occurrences o "
                "WHERE o.symbol_usr_id = ? LIMIT 50",
                (sample_id,),
            ),
        ),
        (
            "1-hop relations (calledBy in)",
            (
                "SELECT r.related_usr, r.kind FROM relations r "
                "JOIN occurrences o ON o.id = r.occurrence_id "
                "WHERE o.symbol_usr = ? AND r.kind = 'calledBy' LIMIT 50",
                (sample_usr,),
            ),
            (
                "SELECT u.text, r.kind FROM relations r "
                "JOIN occurrences o ON o.id = r.occurrence_id "
                "JOIN usrs u ON u.id = r.related_usr_id "
                "WHERE o.symbol_usr_id = ? AND r.kind = 'calledBy' LIMIT 50",
                (sample_id,),
            ),
        ),
        (
            "reach up depth=3 (recursive CTE)",
            (
                """
                WITH RECURSIVE reach(usr, depth) AS (
                  SELECT ?, 0
                  UNION
                  SELECT r.related_usr, reach.depth + 1
                  FROM reach
                  JOIN occurrences o ON o.symbol_usr = reach.usr
                  JOIN relations r ON r.occurrence_id = o.id
                  WHERE reach.depth < 3 AND r.kind IN ('calledBy', 'overrideOf')
                )
                SELECT COUNT(*) FROM (SELECT DISTINCT usr FROM reach)
                """,
                (sample_usr,),
            ),
            (
                """
                WITH RECURSIVE reach(usr_id, depth) AS (
                  SELECT ?, 0
                  UNION
                  SELECT r.related_usr_id, reach.depth + 1
                  FROM reach
                  JOIN occurrences o ON o.symbol_usr_id = reach.usr_id
                  JOIN relations r ON r.occurrence_id = o.id
                  WHERE reach.depth < 3 AND r.kind IN ('calledBy', 'overrideOf')
                )
                SELECT COUNT(*) FROM (SELECT DISTINCT usr_id FROM reach)
                """,
                (sample_id,),
            ),
        ),
    ]

    print(f"  {'QUERY':<40s}  {'v3 (ms)':>10s}  {'v4 (ms)':>10s}  {'delta':>10s}")
    print(f"  {'-' * 40}  {'-' * 10}  {'-' * 10}  {'-' * 10}")
    for label, (sql_v3, params_v3), (sql_v4, params_v4) in queries:
        v3_times = []
        v4_times = []
        for _ in range(repeats):
            t = time.monotonic()
            src.execute(sql_v3, params_v3).fetchall()
            v3_times.append((time.monotonic() - t) * 1000)
            t = time.monotonic()
            dst.execute(sql_v4, params_v4).fetchall()
            v4_times.append((time.monotonic() - t) * 1000)
        v3_med = statistics.median(v3_times)
        v4_med = statistics.median(v4_times)
        delta = (v4_med - v3_med) / v3_med * 100 if v3_med > 0 else 0
        print(f"  {label:<40s}  {v3_med:>10.2f}  {v4_med:>10.2f}  {delta:>+9.1f}%")

    src.close()
    dst.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_path", type=Path, help="Path to project root.")
    parser.add_argument("--dst", type=Path, default=None,
                        help="Output path for v4 SQLite (default: sibling of v3).")
    parser.add_argument("--bench", action="store_true",
                        help="Run query latency comparison after materializing.")
    args = parser.parse_args()

    project_root = args.project_path.resolve()
    project = discovery.find_project(project_root if project_root.is_dir() else project_root.parent)
    sqlite_path = cache_module.canonical_sqlite_path(project.path)
    if not sqlite_path.exists():
        print(f"error: v3 cache not found at {sqlite_path}", file=sys.stderr)
        print("hint: run `xcindex prewarm` first to create the v3 cache", file=sys.stderr)
        return 1

    dst_path = args.dst or (sqlite_path.parent / "index_v4_poc.sqlite")
    print(f"src: {sqlite_path}")
    print(f"dst: {dst_path}")
    print()

    stats = materialize_v4(sqlite_path, dst_path)
    print()
    print(f"materialization: {stats['wall_seconds']:.1f}s "
          f"({stats['usrs']} USRs, {stats['symbols']} sym, "
          f"{stats['occurrences']} occ, {stats['relations']} rel)")

    measure_storage(sqlite_path, dst_path)
    if args.bench:
        benchmark(sqlite_path, dst_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

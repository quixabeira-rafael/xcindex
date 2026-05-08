from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from xcindex import schema as schema_module


def open_readonly(sqlite_path: Path) -> sqlite3.Connection:
    """Open a read-only SQLite connection tuned for queries."""
    if not sqlite_path.exists():
        raise FileNotFoundError(f"sqlite cache not found: {sqlite_path}")
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    schema_module.configure_for_query(conn)
    return conn


# --- Query helpers (return canonical dicts compatible with output engine) ---


def query_at(conn: sqlite3.Connection, file: str, line: int, column: int | None = None) -> dict[str, Any]:
    """Find occurrences at a given file:line[:column].

    Returns canonical shape with anchor (file/line) and items (one per occurrence).
    """
    cursor = conn.cursor()
    if column is not None:
        cursor.execute(
            """
            SELECT o.id, o.symbol_usr, o.file, o.line, o.column, o.roles, o.container_usr,
                   s.name, s.kind, s.module, s.language
            FROM occurrences o
            LEFT JOIN symbols s ON s.usr = o.symbol_usr
            WHERE o.file = ? AND o.line = ? AND o.column = ?
            ORDER BY o.column
            """,
            (file, line, column),
        )
    else:
        cursor.execute(
            """
            SELECT o.id, o.symbol_usr, o.file, o.line, o.column, o.roles, o.container_usr,
                   s.name, s.kind, s.module, s.language
            FROM occurrences o
            LEFT JOIN symbols s ON s.usr = o.symbol_usr
            WHERE o.file = ? AND o.line = ?
            ORDER BY o.column
            """,
            (file, line),
        )
    rows = cursor.fetchall()

    items = [_occurrence_item(row) for row in rows]
    return {
        "kind": "at",
        "anchor": {
            "file": file,
            "line": line,
            "column": column,
        },
        "summary": {
            "found": bool(items),
            "count": len(items),
            "files": 1 if items else 0,
        },
        "items": items,
    }


def query_containing(conn: sqlite3.Connection, file: str, line: int) -> dict[str, Any]:
    """Find the symbol(s) containing a given file:line position.

    The query: find any occurrence in the file with role=definition whose symbol's
    line is <= target line, then find the closest enclosing one whose container
    chain encloses the target. Simpler heuristic for v1: pick the symbol whose
    definition line is the largest line <= target and on the same file.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT s.usr, s.name, s.kind, s.module, s.language, s.file, s.line
        FROM symbols s
        WHERE s.file = ? AND s.line <= ?
        ORDER BY s.line DESC
        LIMIT 1
        """,
        (file, line),
    )
    row = cursor.fetchone()
    if row is None:
        return {
            "kind": "containing",
            "anchor": {"file": file, "line": line},
            "summary": {"found": False, "count": 0},
        }
    item = {
        "name": row["name"],
        "usr": row["usr"],
        "kind": row["kind"],
        "module": row["module"],
        "language": row["language"],
        "file": row["file"],
        "line": row["line"],
    }
    return {
        "kind": "containing",
        "anchor": {"file": file, "line": line},
        "summary": {"found": True, "count": 1},
        "items": [item],
    }


def query_symbol_by_usr(conn: sqlite3.Connection, usr: str) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT usr, name, kind, sub_kind, language, module, file, line, is_system, properties
        FROM symbols
        WHERE usr = ?
        """,
        (usr,),
    )
    row = cursor.fetchone()
    if row is None:
        return {
            "kind": "symbol",
            "anchor": {"usr": usr},
            "summary": {"found": False, "count": 0},
        }
    item = _symbol_item(row)
    return {
        "kind": "symbol",
        "anchor": {"usr": usr, "name": item["name"]},
        "summary": {"found": True, "count": 1},
        "items": [item],
    }


def query_symbol_by_name(conn: sqlite3.Connection, name: str, *, limit: int = 50) -> dict[str, Any]:
    """Match by exact (case-sensitive) name. Returns multiple matches for overloads."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT usr, name, kind, sub_kind, language, module, file, line, is_system, properties
        FROM symbols
        WHERE name = ?
        ORDER BY module, kind, file, line
        LIMIT ?
        """,
        (name, limit + 1),
    )
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [_symbol_item(row) for row in rows]
    files = {it["file"] for it in items if it.get("file")}
    return {
        "kind": "symbol",
        "anchor": {"name": name},
        "summary": {
            "found": bool(items),
            "count": len(items),
            "files": len(files),
        },
        "items": items,
        "truncated": truncated,
    }


def query_occurrences(
    conn: sqlite3.Connection,
    usr: str,
    *,
    role: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Find all occurrences of a symbol, optionally filtered by a single role."""
    cursor = conn.cursor()
    params: list[Any] = [usr]
    role_clause = ""
    if role:
        role_clause = "AND (o.roles & ?) != 0"
        params.append(role_bit(role))
    params.append(limit + 1)
    cursor.execute(
        f"""
        SELECT o.id, o.symbol_usr, o.file, o.line, o.column, o.roles, o.container_usr,
               s.name, s.kind, s.module, s.language
        FROM occurrences o
        LEFT JOIN symbols s ON s.usr = o.symbol_usr
        WHERE o.symbol_usr = ?
        {role_clause}
        ORDER BY o.file, o.line, o.column
        LIMIT ?
        """,
        params,
    )
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [_occurrence_item(row) for row in rows]
    files = {it["file"] for it in items if it.get("file")}
    by_role = _summarize_roles(items)
    return {
        "kind": "occurrences",
        "anchor": {"usr": usr, "name": items[0]["name"] if items else None, "role": role},
        "summary": {
            "found": bool(items),
            "count": len(items),
            "files": len(files),
            "by_role": by_role,
        },
        "items": items,
        "truncated": truncated,
    }


def query_relations(
    conn: sqlite3.Connection,
    usr: str,
    *,
    kind: str | None = None,
    direction: str = "out",
    limit: int = 50,
) -> dict[str, Any]:
    """Return relations involving the given symbol.

    direction='out': relations whose occurrences belong to `usr` and point at others.
    direction='in':  relations whose related_usr equals `usr` (others pointing at us).
    """
    if direction not in ("in", "out"):
        raise ValueError(f"unknown direction: {direction!r}")
    cursor = conn.cursor()
    params: list[Any] = [usr]
    kind_clause = ""
    if kind:
        kind_clause = "AND r.kind = ?"
        params.append(kind)
    params.append(limit + 1)

    if direction == "out":
        sql = f"""
            SELECT r.related_usr AS counterpart_usr,
                   COALESCE(s.name, r.related_name) AS counterpart_name,
                   s.kind AS counterpart_kind, s.module AS counterpart_module,
                   s.file AS counterpart_file, s.line AS counterpart_line,
                   r.kind AS rel_kind, r.roles AS rel_roles,
                   o.file AS site_file, o.line AS site_line, o.column AS site_column
            FROM occurrences o
            JOIN relations r ON r.occurrence_id = o.id
            LEFT JOIN symbols s ON s.usr = r.related_usr
            WHERE o.symbol_usr = ?
            {kind_clause}
            ORDER BY r.kind, counterpart_module, counterpart_name
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT o.symbol_usr AS counterpart_usr,
                   s.name AS counterpart_name,
                   s.kind AS counterpart_kind, s.module AS counterpart_module,
                   s.file AS counterpart_file, s.line AS counterpart_line,
                   r.kind AS rel_kind, r.roles AS rel_roles,
                   o.file AS site_file, o.line AS site_line, o.column AS site_column
            FROM relations r
            JOIN occurrences o ON o.id = r.occurrence_id
            LEFT JOIN symbols s ON s.usr = o.symbol_usr
            WHERE r.related_usr = ?
            {kind_clause}
            ORDER BY r.kind, counterpart_module, counterpart_name
            LIMIT ?
        """
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [_relation_item(row) for row in rows]
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["rel_kind"]] = by_kind.get(it["rel_kind"], 0) + 1
    return {
        "kind": "relations",
        "anchor": {"usr": usr, "direction": direction, "filter_kind": kind},
        "summary": {
            "found": bool(items),
            "count": len(items),
            "by_kind": by_kind,
        },
        "items": items,
        "truncated": truncated,
    }


def query_neighbors(
    conn: sqlite3.Connection,
    usr: str,
    *,
    direction: str = "both",
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """1-hop union of relations in either or both directions."""
    if direction == "both":
        out = query_relations(conn, usr, kind=kind, direction="out", limit=limit)
        inn = query_relations(conn, usr, kind=kind, direction="in", limit=limit)
        items = out["items"] + inn["items"]
        items.sort(key=lambda it: (it["rel_kind"], it.get("module") or "", it.get("name") or ""))
        items = items[:limit]
        truncated = out["truncated"] or inn["truncated"] or len(items) >= limit
        by_kind: dict[str, int] = {}
        for it in items:
            by_kind[it["rel_kind"]] = by_kind.get(it["rel_kind"], 0) + 1
        return {
            "kind": "neighbors",
            "anchor": {"usr": usr, "direction": "both", "filter_kind": kind},
            "summary": {"found": bool(items), "count": len(items), "by_kind": by_kind},
            "items": items,
            "truncated": truncated,
        }
    payload = query_relations(conn, usr, kind=kind, direction=direction, limit=limit)
    payload["kind"] = "neighbors"
    return payload


def query_reach(
    conn: sqlite3.Connection,
    usr: str,
    *,
    direction: str = "up",
    max_depth: int = 8,
    to_module: str | None = None,
    kinds: tuple[str, ...] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Transitive reachability via recursive CTE.

    direction='up'  : reverse closure — who transitively uses `usr`?
    direction='down': forward closure — what does `usr` transitively use?

    `kinds` filters which relation kinds to traverse (defaults to call/inheritance).
    `to_module` filters the OUTPUT (rows whose reached symbol's module matches).
    """
    if direction not in ("up", "down"):
        raise ValueError(f"unknown direction: {direction!r}")
    cursor = conn.cursor()
    travel_kinds = tuple(kinds) if kinds else DEFAULT_REACH_KINDS
    placeholders = ",".join(["?"] * len(travel_kinds))

    if direction == "up":
        # "Up" means: who transitively USES this symbol.
        # An occurrence with symbol_usr=X carries relations whose related_usr is the
        # entity that "uses" X (e.g. relation kind=calledBy → related_usr is the caller).
        recursive_step = f"""
            SELECT r.related_usr, reach.depth + 1
            FROM reach
            JOIN occurrences o ON o.symbol_usr = reach.usr
            JOIN relations r ON r.occurrence_id = o.id
            WHERE reach.depth < ?
              AND r.kind IN ({placeholders})
        """
    else:
        # "Down" means: what this symbol transitively USES.
        # When X calls Y, the occurrence is OF Y (symbol_usr=Y) with relation
        # (related_usr=X, kind=calledBy). To get what X uses we look for relations
        # where related_usr=X and follow back to o.symbol_usr (the callee).
        recursive_step = f"""
            SELECT o.symbol_usr, reach.depth + 1
            FROM reach
            JOIN relations r ON r.related_usr = reach.usr
            JOIN occurrences o ON o.id = r.occurrence_id
            WHERE reach.depth < ?
              AND r.kind IN ({placeholders})
        """

    module_clause = ""
    params: list[Any] = [usr, max_depth, *travel_kinds, usr]
    if to_module is not None:
        module_clause = " AND s.module = ?"
        params.append(to_module)
    params.append(limit + 1)

    cte_sql = f"""
        WITH RECURSIVE reach(usr, depth) AS (
            SELECT ?, 0
            UNION
            {recursive_step}
        )
        SELECT reach.usr, MIN(reach.depth) AS depth,
               s.name, s.kind, s.module, s.file, s.line
        FROM reach
        LEFT JOIN symbols s ON s.usr = reach.usr
        WHERE reach.usr != ?{module_clause}
        GROUP BY reach.usr
        ORDER BY depth, s.module, s.name
        LIMIT ?
    """

    cursor.execute(cte_sql, params)
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items: list[dict[str, Any]] = []
    by_module: dict[str, int] = {}
    by_depth: dict[int, int] = {}
    for row in rows:
        depth = int(row["depth"])
        module = row["module"]
        items.append({
            "usr": row["usr"],
            "name": row["name"],
            "kind": row["kind"],
            "module": module,
            "file": row["file"],
            "line": row["line"],
            "depth": depth,
        })
        if module:
            by_module[module] = by_module.get(module, 0) + 1
        by_depth[depth] = by_depth.get(depth, 0) + 1

    return {
        "kind": "reach",
        "anchor": {
            "usr": usr,
            "direction": direction,
            "max_depth": max_depth,
            "to_module": to_module,
        },
        "summary": {
            "found": bool(items),
            "count": len(items),
            "min_hops": min((it["depth"] for it in items), default=None),
            "max_hops": max((it["depth"] for it in items), default=None),
            "by_module": by_module,
            "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
        },
        "items": items,
        "truncated": truncated,
    }


def query_search(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    kind: str | None = None,
    module: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Substring match (case-insensitive) on symbol name."""
    cursor = conn.cursor()
    params: list[Any] = [f"%{pattern}%"]
    where = "WHERE name LIKE ? COLLATE NOCASE"
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if module:
        where += " AND module = ?"
        params.append(module)
    where += " AND is_system = 0"
    params.append(limit + 1)
    cursor.execute(
        f"""
        SELECT usr, name, kind, sub_kind, language, module, file, line, is_system, properties
        FROM symbols
        {where}
        ORDER BY name COLLATE NOCASE, module
        LIMIT ?
        """,
        params,
    )
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [_symbol_item(row) for row in rows]
    by_kind: dict[str, int] = {}
    by_module: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
        if it.get("module"):
            by_module[it["module"]] = by_module.get(it["module"], 0) + 1
    return {
        "kind": "search",
        "anchor": {"pattern": pattern, "filter_kind": kind, "filter_module": module},
        "summary": {
            "found": bool(items),
            "count": len(items),
            "by_kind": by_kind,
            "by_module": by_module,
        },
        "items": items,
        "truncated": truncated,
    }


# --- Internal helpers --------------------------------------------------------

_ROLE_BITS = (
    ("declaration", 1 << 0),
    ("definition", 1 << 1),
    ("reference", 1 << 2),
    ("read", 1 << 3),
    ("write", 1 << 4),
    ("call", 1 << 5),
    ("dynamic", 1 << 6),
    ("addressOf", 1 << 7),
    ("implicit", 1 << 8),
)
_ROLE_BIT_BY_NAME = dict(_ROLE_BITS)

# Relation kinds emitted by the helper (`primaryRelationKind` in main.swift).
RELATION_KINDS = (
    "childOf", "baseOf", "overrideOf", "receivedBy", "calledBy",
    "extendedBy", "accessorOf", "containedBy", "ibTypeOf", "specializationOf",
)
# Edges to follow during reach traversal. Both directions use the same set;
# semantics differ only in which side of the (occurrence, relation) join is fixed.
DEFAULT_REACH_KINDS = (
    "calledBy", "containedBy", "childOf", "overrideOf",
    "baseOf", "specializationOf", "extendedBy",
)


def role_bit(name: str) -> int:
    bit = _ROLE_BIT_BY_NAME.get(name)
    if bit is None:
        raise ValueError(f"unknown role: {name!r}")
    return _signed_64(bit)


def _signed_64(value: int) -> int:
    if value >= (1 << 63):
        return value - (1 << 64)
    return value


def decode_roles(roles: int) -> list[str]:
    """Decode the SymbolRole bitmask into a list of named primary roles."""
    if roles < 0:
        roles += 1 << 64
    return [name for name, bit in _ROLE_BITS if roles & bit]


def _occurrence_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "usr": row["symbol_usr"],
        "kind": row["kind"],
        "module": row["module"],
        "language": row["language"],
        "file": row["file"],
        "line": row["line"],
        "column": row["column"],
        "roles": decode_roles(int(row["roles"])),
        "container": row["container_usr"],
    }


def _relation_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["counterpart_name"],
        "usr": row["counterpart_usr"],
        "kind": row["counterpart_kind"],
        "module": row["counterpart_module"],
        "file": row["counterpart_file"],
        "line": row["counterpart_line"],
        "rel_kind": row["rel_kind"],
        "rel_roles": decode_roles(int(row["rel_roles"])),
        "site": {
            "file": row["site_file"],
            "line": row["site_line"],
            "column": row["site_column"],
        },
    }


def _summarize_roles(items: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for it in items:
        for role in it.get("roles") or []:
            summary[role] = summary.get(role, 0) + 1
    return summary


def _symbol_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "usr": row["usr"],
        "kind": row["kind"],
        "sub_kind": row["sub_kind"],
        "language": row["language"],
        "module": row["module"],
        "file": row["file"],
        "line": row["line"],
        "is_system": bool(row["is_system"]),
        "properties": int(row["properties"]),
    }

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from xcindex import schema as schema_module


class SymbolNotFoundError(Exception):
    """Raised when a USR or name argument cannot be resolved in the index."""


class AmbiguousNameError(Exception):
    """Raised when a name resolves to more than one symbol; carries candidates."""

    def __init__(self, name: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(f"name {name!r} matches {len(candidates)} symbols")
        self.name = name
        self.candidates = candidates


def open_readonly(sqlite_path: Path) -> sqlite3.Connection:
    """Open a read-only SQLite connection tuned for queries."""
    if not sqlite_path.exists():
        raise FileNotFoundError(f"sqlite cache not found: {sqlite_path}")
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    schema_module.configure_for_query(conn)
    return conn


# --- Query helpers (return canonical dicts compatible with output engine) ---


def _resolve_usr_id(conn: sqlite3.Connection, usr: str) -> int | None:
    """Look up the integer id for a USR text. Returns None when absent."""
    row = conn.execute("SELECT id FROM usrs WHERE text = ?", (usr,)).fetchone()
    return row[0] if row else None


def query_at(conn: sqlite3.Connection, file: str, line: int, column: int | None = None) -> dict[str, Any]:
    """Find occurrences at a given file:line[:column].

    Returns canonical shape with anchor (file/line) and items (one per occurrence).
    """
    cursor = conn.cursor()
    base_select = """
        SELECT o.id, u_sym.text AS symbol_usr, o.file, o.line, o.column, o.roles,
               u_ctr.text AS container_usr,
               s.name, s.kind, s.module, s.language
        FROM occurrences o
        LEFT JOIN symbols s ON s.usr_id = o.symbol_usr_id
        LEFT JOIN usrs u_sym ON u_sym.id = o.symbol_usr_id
        LEFT JOIN usrs u_ctr ON u_ctr.id = o.container_usr_id
    """
    if column is not None:
        cursor.execute(
            base_select + " WHERE o.file = ? AND o.line = ? AND o.column = ? ORDER BY o.column",
            (file, line, column),
        )
    else:
        cursor.execute(
            base_select + " WHERE o.file = ? AND o.line = ? ORDER BY o.column",
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

    Heuristic: pick the symbol whose definition line is the largest value
    less than or equal to the target line in the same file. Doesn't fully
    walk the container chain — sufficient for "what method/class is this
    line inside?" in practice.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.text AS usr, s.name, s.kind, s.module, s.language, s.file, s.line
        FROM symbols s
        JOIN usrs u ON u.id = s.usr_id
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
        SELECT u.text AS usr, s.name, s.kind, s.sub_kind, s.language, s.module,
               s.file, s.line, s.is_system, s.properties
        FROM symbols s
        JOIN usrs u ON u.id = s.usr_id
        WHERE u.text = ?
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
        SELECT u.text AS usr, s.name, s.kind, s.sub_kind, s.language, s.module,
               s.file, s.line, s.is_system, s.properties
        FROM symbols s
        JOIN usrs u ON u.id = s.usr_id
        WHERE s.name = ?
        ORDER BY s.module, s.kind, s.file, s.line
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
    usr_id = _resolve_usr_id(conn, usr)
    if usr_id is None:
        return {
            "kind": "occurrences",
            "anchor": {"usr": usr, "name": None, "role": role},
            "summary": {"found": False, "count": 0, "files": 0, "by_role": {}},
            "items": [],
            "truncated": False,
        }
    params: list[Any] = [usr_id]
    role_clause = ""
    if role:
        role_clause = "AND (o.roles & ?) != 0"
        params.append(role_bit(role))
    params.append(limit + 1)
    cursor.execute(
        f"""
        SELECT o.id, u_sym.text AS symbol_usr, o.file, o.line, o.column, o.roles,
               u_ctr.text AS container_usr,
               s.name, s.kind, s.module, s.language
        FROM occurrences o
        LEFT JOIN symbols s ON s.usr_id = o.symbol_usr_id
        LEFT JOIN usrs u_sym ON u_sym.id = o.symbol_usr_id
        LEFT JOIN usrs u_ctr ON u_ctr.id = o.container_usr_id
        WHERE o.symbol_usr_id = ?
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
    usr_id = _resolve_usr_id(conn, usr)
    if usr_id is None:
        return {
            "kind": "relations",
            "anchor": {"usr": usr, "direction": direction, "filter_kind": kind},
            "summary": {"found": False, "count": 0, "by_kind": {}},
            "items": [],
            "truncated": False,
        }
    params: list[Any] = [usr_id]
    kind_clause = ""
    if kind:
        kind_clause = "AND r.kind = ?"
        params.append(kind)
    params.append(limit + 1)

    if direction == "out":
        sql = f"""
            SELECT u.text AS counterpart_usr,
                   COALESCE(s.name, r.related_name) AS counterpart_name,
                   s.kind AS counterpart_kind, s.module AS counterpart_module,
                   s.file AS counterpart_file, s.line AS counterpart_line,
                   r.kind AS rel_kind, r.roles AS rel_roles,
                   o.file AS site_file, o.line AS site_line, o.column AS site_column
            FROM occurrences o
            JOIN relations r ON r.occurrence_id = o.id
            LEFT JOIN symbols s ON s.usr_id = r.related_usr_id
            JOIN usrs u ON u.id = r.related_usr_id
            WHERE o.symbol_usr_id = ?
            {kind_clause}
            ORDER BY r.kind, counterpart_module, counterpart_name
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT u.text AS counterpart_usr,
                   s.name AS counterpart_name,
                   s.kind AS counterpart_kind, s.module AS counterpart_module,
                   s.file AS counterpart_file, s.line AS counterpart_line,
                   r.kind AS rel_kind, r.roles AS rel_roles,
                   o.file AS site_file, o.line AS site_line, o.column AS site_column
            FROM relations r
            JOIN occurrences o ON o.id = r.occurrence_id
            LEFT JOIN symbols s ON s.usr_id = o.symbol_usr_id
            JOIN usrs u ON u.id = o.symbol_usr_id
            WHERE r.related_usr_id = ?
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
    usr_id = _resolve_usr_id(conn, usr)
    if usr_id is None:
        return {
            "kind": "reach",
            "anchor": {"usr": usr, "direction": direction, "max_depth": max_depth, "to_module": to_module},
            "summary": {"found": False, "count": 0, "min_hops": None, "max_hops": None,
                         "by_module": {}, "by_depth": {}},
            "items": [],
            "truncated": False,
        }
    travel_kinds = tuple(kinds) if kinds else DEFAULT_REACH_KINDS
    placeholders = ",".join(["?"] * len(travel_kinds))

    if direction == "up":
        recursive_step = f"""
            SELECT r.related_usr_id, reach.depth + 1
            FROM reach
            JOIN occurrences o ON o.symbol_usr_id = reach.usr_id
            JOIN relations r ON r.occurrence_id = o.id
            WHERE reach.depth < ?
              AND r.kind IN ({placeholders})
        """
    else:
        recursive_step = f"""
            SELECT o.symbol_usr_id, reach.depth + 1
            FROM reach
            JOIN relations r ON r.related_usr_id = reach.usr_id
            JOIN occurrences o ON o.id = r.occurrence_id
            WHERE reach.depth < ?
              AND r.kind IN ({placeholders})
        """

    module_clause = ""
    params: list[Any] = [usr_id, max_depth, *travel_kinds, usr_id]
    if to_module is not None:
        module_clause = " AND s.module = ?"
        params.append(to_module)
    params.append(limit + 1)

    cte_sql = f"""
        WITH RECURSIVE reach(usr_id, depth) AS (
            SELECT ?, 0
            UNION
            {recursive_step}
        )
        SELECT u.text AS usr, MIN(reach.depth) AS depth,
               s.name, s.kind, s.module, s.file, s.line
        FROM reach
        LEFT JOIN symbols s ON s.usr_id = reach.usr_id
        JOIN usrs u ON u.id = reach.usr_id
        WHERE reach.usr_id != ?{module_clause}
        GROUP BY reach.usr_id
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


def find_files_in_index(conn: sqlite3.Connection, input_str: str) -> list[str]:
    """Resolve a user-supplied file argument to indexed file paths.

    The input may be:
      - an absolute or relative path (matched exactly after resolution),
      - a filename with extension (matched as basename suffix),
      - a bare filename (matched against any extension).
    """
    cursor = conn.cursor()
    p = Path(input_str)

    if "/" in input_str or input_str.startswith("~"):
        expanded = p.expanduser()
        try:
            resolved = str(expanded.resolve())
        except OSError:
            resolved = str(expanded)
        cursor.execute(
            "SELECT DISTINCT file FROM symbols WHERE file = ?",
            (resolved,),
        )
        row = cursor.fetchone()
        if row is not None and row[0]:
            return [row[0]]

    name = p.name or input_str
    if "." in name:
        like = f"%/{name}"
    else:
        like = f"%/{name}.%"
    cursor.execute(
        "SELECT DISTINCT file FROM symbols WHERE file LIKE ? AND is_system = 0 ORDER BY file",
        (like,),
    )
    return [row[0] for row in cursor.fetchall() if row[0]]


def query_file_definitions(
    conn: sqlite3.Connection,
    file: str,
    *,
    kinds: tuple[str, ...] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List symbols defined in a given file, optionally filtered by kind."""
    cursor = conn.cursor()
    params: list[Any] = [file]
    where_kind = ""
    if kinds:
        placeholders = ",".join(["?"] * len(kinds))
        where_kind = f"AND s.kind IN ({placeholders})"
        params.extend(kinds)
    params.append(limit + 1)
    cursor.execute(
        f"""
        SELECT u.text AS usr, s.name, s.kind, s.sub_kind, s.language, s.module,
               s.file, s.line, s.is_system, s.properties
        FROM symbols s
        JOIN usrs u ON u.id = s.usr_id
        WHERE s.file = ? {where_kind} AND s.is_system = 0
        ORDER BY s.line, s.name
        LIMIT ?
        """,
        params,
    )
    rows = cursor.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [_symbol_item(row) for row in rows]
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    return {
        "kind": "file",
        "anchor": {"file": file, "filter_kinds": list(kinds) if kinds else None},
        "summary": {
            "found": bool(items),
            "count": len(items),
            "by_kind": by_kind,
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
    where = "WHERE s.name LIKE ? COLLATE NOCASE"
    if kind:
        where += " AND s.kind = ?"
        params.append(kind)
    if module:
        where += " AND s.module = ?"
        params.append(module)
    where += " AND s.is_system = 0"
    params.append(limit + 1)
    cursor.execute(
        f"""
        SELECT u.text AS usr, s.name, s.kind, s.sub_kind, s.language, s.module,
               s.file, s.line, s.is_system, s.properties
        FROM symbols s
        JOIN usrs u ON u.id = s.usr_id
        {where}
        ORDER BY s.name COLLATE NOCASE, s.module
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


# --- Helpers for impact analysis (BFS over relations + input resolution) -----


def resolve_input_to_usr(conn: sqlite3.Connection, input_str: str) -> dict[str, Any]:
    """Resolve a CLI input (USR / `<file>:<line>` / name) to a single symbol.

    Returns the symbol row dict (with at least `usr`, `name`, `kind`).
    Raises:
      - `SymbolNotFoundError` when nothing matches.
      - `AmbiguousNameError` when a name matches more than one symbol.
      - `ValueError` when the file:line form has a malformed line component.
    """
    text = input_str.strip()
    if text.startswith(("s:", "c:")):
        canonical = query_symbol_by_usr(conn, text)
        if not canonical["summary"]["found"]:
            raise SymbolNotFoundError(f"USR not found in index: {text}")
        return canonical["items"][0]

    if ":" in text:
        head, _, tail = text.rpartition(":")
        if head and tail.isdigit():
            file_path = head
            line = int(tail)
            expanded = Path(file_path).expanduser()
            resolved = str(expanded.resolve()) if expanded.exists() else str(expanded)
            canonical = query_containing(conn, resolved, line)
            if not canonical["summary"]["found"]:
                raise SymbolNotFoundError(
                    f"no symbol found at {resolved}:{line} (file may not be indexed)"
                )
            return canonical["items"][0]

    canonical = query_symbol_by_name(conn, text, limit=20)
    items = canonical["items"]
    if not items:
        raise SymbolNotFoundError(f"name not found in index: {text!r}")
    if len(items) > 1:
        raise AmbiguousNameError(text, items)
    return items[0]


def _resolve_usr_ids(conn: sqlite3.Connection, usrs: list[str]) -> list[int]:
    """Bulk USR text → id resolution. USRs not present in the index are dropped."""
    if not usrs:
        return []
    placeholders = ",".join(["?"] * len(usrs))
    rows = conn.execute(
        f"SELECT id FROM usrs WHERE text IN ({placeholders})",
        usrs,
    ).fetchall()
    return [row[0] for row in rows]


def fetch_callers_layer(
    conn: sqlite3.Connection,
    frontier_usrs: list[str],
    *,
    kinds: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return one BFS layer of upstream callers for the given frontier.

    Each row carries (callee, caller, edge_kind, site_file, site_line, caller_name,
    caller_kind, caller_module, caller_file, caller_line). Caller fields come from
    the symbols table when known.
    """
    if not frontier_usrs or not kinds:
        return []
    frontier_ids = _resolve_usr_ids(conn, frontier_usrs)
    if not frontier_ids:
        return []
    frontier_placeholders = ",".join(["?"] * len(frontier_ids))
    kind_placeholders = ",".join(["?"] * len(kinds))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT u_callee.text AS callee,
               u_caller.text AS caller,
               r.kind         AS edge_kind,
               o.file         AS site_file,
               o.line         AS site_line,
               s.name         AS caller_name,
               s.kind         AS caller_kind,
               s.module       AS caller_module,
               s.file         AS caller_file,
               s.line         AS caller_line
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        JOIN usrs u_callee ON u_callee.id = o.symbol_usr_id
        JOIN usrs u_caller ON u_caller.id = r.related_usr_id
        LEFT JOIN symbols s ON s.usr_id = r.related_usr_id
        WHERE o.symbol_usr_id IN ({frontier_placeholders})
          AND r.kind IN ({kind_placeholders})
        """,
        (*frontier_ids, *kinds),
    )
    return [dict(row) for row in cursor.fetchall()]


def fetch_callees_layer(
    conn: sqlite3.Connection,
    frontier_usrs: list[str],
    *,
    kinds: tuple[str, ...] = ("calledBy",),
) -> list[dict[str, Any]]:
    """Return one BFS layer of downstream callees for the given frontier.

    Inverts the relation direction: `r.related_usr=current` and `o.symbol_usr` is
    the callee. Site fields come from the call-site occurrence.
    """
    if not frontier_usrs or not kinds:
        return []
    frontier_ids = _resolve_usr_ids(conn, frontier_usrs)
    if not frontier_ids:
        return []
    frontier_placeholders = ",".join(["?"] * len(frontier_ids))
    kind_placeholders = ",".join(["?"] * len(kinds))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT u_caller.text AS caller,
               u_callee.text AS callee,
               r.kind         AS edge_kind,
               o.file         AS site_file,
               o.line         AS site_line,
               s.name         AS callee_name,
               s.kind         AS callee_kind,
               s.module       AS callee_module,
               s.file         AS callee_file,
               s.line         AS callee_line
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        JOIN usrs u_caller ON u_caller.id = r.related_usr_id
        JOIN usrs u_callee ON u_callee.id = o.symbol_usr_id
        LEFT JOIN symbols s ON s.usr_id = o.symbol_usr_id
        WHERE r.related_usr_id IN ({frontier_placeholders})
          AND r.kind IN ({kind_placeholders})
        """,
        (*frontier_ids, *kinds),
    )
    return [dict(row) for row in cursor.fetchall()]


def fetch_type_reference_containers(
    conn: sqlite3.Connection,
    type_usr: str,
) -> list[dict[str, Any]]:
    """Return distinct container symbols whose code references the given type.

    A 'container' is the enclosing symbol (method/function) that hosts a
    reference to the type. Used as the level-1 upstream layer for a type target.
    """
    type_id = _resolve_usr_id(conn, type_usr)
    if type_id is None:
        return []
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.text  AS container_usr,
               s.name  AS container_name,
               s.kind  AS container_kind,
               s.module AS container_module,
               s.file  AS container_file,
               s.line  AS container_line,
               MIN(o.file) AS site_file,
               MIN(o.line) AS site_line
        FROM occurrences o
        JOIN usrs u ON u.id = o.container_usr_id
        LEFT JOIN symbols s ON s.usr_id = o.container_usr_id
        WHERE o.symbol_usr_id = ?
          AND o.container_usr_id IS NOT NULL
          AND o.container_usr_id != ?
          AND (o.roles & 4) != 0
        GROUP BY o.container_usr_id
        """,
        (type_id, type_id),
    )
    return [dict(row) for row in cursor.fetchall()]


def fetch_type_structure(
    conn: sqlite3.Connection,
    type_usr: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return members, subclasses/conformers, and extensions of a type.

    Direction notes (matching `query_relations` semantics):
      - members: member's occurrence carries a `childOf`/`containedBy` relation
        pointing to the type (`r.related_usr = T`); member's USR is `o.symbol_usr`.
      - subclasses/conformers: type's occurrence (in the subclass def file) carries
        a `baseOf` relation pointing to the subclass (`r.related_usr` is the subclass).
      - extensions: type's occurrence (in the extension's site) carries an
        `extendedBy` relation; the extension's USR is `r.related_usr`.
    """
    type_id = _resolve_usr_id(conn, type_usr)
    if type_id is None:
        return {"members": [], "subclasses": [], "extensions": []}
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT u.text AS usr, s.name, s.kind, s.module, s.file, s.line
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        JOIN symbols s ON s.usr_id = o.symbol_usr_id
        JOIN usrs u ON u.id = o.symbol_usr_id
        WHERE r.related_usr_id = ?
          AND r.kind IN ('childOf', 'containedBy')
        ORDER BY s.line, s.name
        """,
        (type_id,),
    )
    members = [dict(row) for row in cursor.fetchall() if row["usr"]]

    cursor.execute(
        """
        SELECT DISTINCT u.text AS usr, s.name, s.kind, s.module, s.file, s.line
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        JOIN symbols s ON s.usr_id = r.related_usr_id
        JOIN usrs u ON u.id = r.related_usr_id
        WHERE o.symbol_usr_id = ?
          AND r.kind = 'baseOf'
        ORDER BY s.module, s.name
        """,
        (type_id,),
    )
    subclasses = [dict(row) for row in cursor.fetchall() if row["usr"]]

    cursor.execute(
        """
        SELECT DISTINCT u.text AS usr, s.name, s.kind, s.module, s.file, s.line
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        JOIN symbols s ON s.usr_id = r.related_usr_id
        JOIN usrs u ON u.id = r.related_usr_id
        WHERE o.symbol_usr_id = ?
          AND r.kind = 'extendedBy'
        ORDER BY s.module, s.name
        """,
        (type_id,),
    )
    extensions = [dict(row) for row in cursor.fetchall() if row["usr"]]

    return {"members": members, "subclasses": subclasses, "extensions": extensions}


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

# Relation kinds emitted by the helper (`Mappings.primaryRelationKind`).
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

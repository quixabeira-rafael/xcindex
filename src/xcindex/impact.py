"""Bidirectional impact analysis for `xcindex impact`.

Builds stack-frame-style call/usage chains for a target symbol by walking the
relations graph layer-by-layer in Python. Three modes:

  * `call_stack`   — callable target (method/function/constructor/...).
                     Upstream via calledBy + overrideOf, downstream via callees.
  * `usage_chain`  — type target (class/struct/enum/protocol).
                     Upstream level 1 = reference containers, then BFS via callers.
                     Downstream is structural (members/subclasses/extensions).
  * `hint_only`    — kinds that don't fit either model (property/extension/
                     typealias/parameter/etc.); emits next-step suggestions.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from xcindex import query as query_module


CALLABLE_KINDS = frozenset({
    "instance-method", "class-method", "static-method",
    "function", "constructor", "destructor", "conversion-function",
})
TYPE_KINDS = frozenset({"class", "struct", "enum", "protocol"})

DEFAULT_UPSTREAM_KINDS: tuple[str, ...] = ("calledBy", "overrideOf")
DEFAULT_DOWNSTREAM_KINDS: tuple[str, ...] = ("calledBy",)


# --- Mode classification -----------------------------------------------------


def classify_kind(kind: str | None) -> str:
    """Return one of 'call_stack', 'usage_chain', 'hint_only'."""
    if kind in CALLABLE_KINDS:
        return "call_stack"
    if kind in TYPE_KINDS:
        return "usage_chain"
    return "hint_only"


# --- BFS primitives ----------------------------------------------------------


def _bfs_upstream(
    conn: sqlite3.Connection,
    target_usr: str,
    *,
    max_depth: int,
    kinds: tuple[str, ...],
    seed_layer: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """BFS upstream via fetch_callers_layer; returns (depth-by-usr, parent-info).

    `seed_layer`, when provided, replaces the level-1 query: each row must
    carry the standard caller_* fields. Used for type targets to inject
    reference containers as the first layer.
    """
    visited: dict[str, int] = {target_usr: 0}
    parents: dict[str, dict[str, Any]] = {}
    frontier = [target_usr]

    for depth in range(1, max_depth + 1):
        if not frontier:
            break

        if depth == 1 and seed_layer is not None:
            rows = seed_layer
        else:
            rows = query_module.fetch_callers_layer(conn, frontier, kinds=kinds)

        next_frontier: list[str] = []
        for row in rows:
            caller = row.get("caller")
            if not caller or caller in visited:
                continue
            visited[caller] = depth
            parents[caller] = {
                "callee": row.get("callee"),
                "edge_kind": row.get("edge_kind"),
                "site_file": row.get("site_file"),
                "site_line": row.get("site_line"),
                "name": row.get("caller_name"),
                "kind": row.get("caller_kind"),
                "module": row.get("caller_module"),
                "file": row.get("caller_file"),
                "line": row.get("caller_line"),
            }
            next_frontier.append(caller)
        frontier = next_frontier

    return visited, parents


def _bfs_downstream(
    conn: sqlite3.Connection,
    target_usr: str,
    *,
    max_depth: int,
    kinds: tuple[str, ...] = DEFAULT_DOWNSTREAM_KINDS,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """BFS downstream via fetch_callees_layer."""
    visited: dict[str, int] = {target_usr: 0}
    parents: dict[str, dict[str, Any]] = {}
    frontier = [target_usr]

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        rows = query_module.fetch_callees_layer(conn, frontier, kinds=kinds)
        next_frontier: list[str] = []
        for row in rows:
            callee = row.get("callee")
            if not callee or callee in visited:
                continue
            visited[callee] = depth
            parents[callee] = {
                "caller": row.get("caller"),
                "edge_kind": row.get("edge_kind"),
                "site_file": row.get("site_file"),
                "site_line": row.get("site_line"),
                "name": row.get("callee_name"),
                "kind": row.get("callee_kind"),
                "module": row.get("callee_module"),
                "file": row.get("callee_file"),
                "line": row.get("callee_line"),
            }
            next_frontier.append(callee)
        frontier = next_frontier

    return visited, parents


# --- Leaf detection ----------------------------------------------------------


def _find_terminal_callers(
    conn: sqlite3.Connection,
    candidate_usrs: list[str],
    *,
    kinds: tuple[str, ...],
) -> set[str]:
    """Return the subset of candidates that have NO further upstream callers."""
    if not candidate_usrs:
        return set()
    placeholders = ",".join(["?"] * len(candidate_usrs))
    kind_placeholders = ",".join(["?"] * len(kinds))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT DISTINCT o.symbol_usr
        FROM relations r
        JOIN occurrences o ON o.id = r.occurrence_id
        WHERE o.symbol_usr IN ({placeholders})
          AND r.kind IN ({kind_placeholders})
        """,
        (*candidate_usrs, *kinds),
    )
    has_more = {row[0] for row in cursor.fetchall()}
    return set(candidate_usrs) - has_more


def _find_terminal_callees(
    conn: sqlite3.Connection,
    candidate_usrs: list[str],
    *,
    kinds: tuple[str, ...] = DEFAULT_DOWNSTREAM_KINDS,
) -> set[str]:
    """Return the subset of candidates that don't call anything further."""
    if not candidate_usrs:
        return set()
    placeholders = ",".join(["?"] * len(candidate_usrs))
    kind_placeholders = ",".join(["?"] * len(kinds))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT DISTINCT r.related_usr
        FROM relations r
        WHERE r.related_usr IN ({placeholders})
          AND r.kind IN ({kind_placeholders})
        """,
        (*candidate_usrs, *kinds),
    )
    has_more = {row[0] for row in cursor.fetchall()}
    return set(candidate_usrs) - has_more


# --- Stack reconstruction ----------------------------------------------------


def _reconstruct_upstream_stack(
    leaf_usr: str,
    target_usr: str,
    target_info: dict[str, Any],
    parents: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk parent pointers from leaf back to target. Frame 0 = leaf, last = target."""
    chain: list[dict[str, Any]] = []
    current = leaf_usr
    safety = 0
    while True:
        safety += 1
        if safety > 1000:
            break
        if current == target_usr:
            chain.append({"usr": target_usr, **target_info, "is_target": True})
            break
        info = parents.get(current)
        if info is None:
            break
        chain.append({
            "usr": current,
            "name": info.get("name"),
            "kind": info.get("kind"),
            "module": info.get("module"),
            "file": info.get("file"),
            "line": info.get("line"),
            "edge_kind": info.get("edge_kind"),
            "site_file": info.get("site_file"),
            "site_line": info.get("site_line"),
            "is_target": False,
        })
        current = info.get("callee")
        if current is None:
            break
    return chain


def _reconstruct_downstream_stack(
    leaf_usr: str,
    target_usr: str,
    target_info: dict[str, Any],
    parents: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk back from leaf to target, then reverse so frame 0 = target."""
    upchain = _reconstruct_upstream_stack(leaf_usr, target_usr, target_info, _flip_downstream_parents(parents))
    return list(reversed(upchain))


def _flip_downstream_parents(parents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Adapter: downstream parents store `caller` as parent; upstream walker reads `callee`."""
    flipped: dict[str, dict[str, Any]] = {}
    for usr, info in parents.items():
        flipped[usr] = {**info, "callee": info.get("caller")}
    return flipped


def _prune_subsumed(stacks: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Drop stack S when its USR sequence is a prefix of another kept stack."""
    by_seq = sorted(
        ((tuple(f["usr"] for f in s), s) for s in stacks),
        key=lambda x: -len(x[0]),
    )
    kept: list[list[dict[str, Any]]] = []
    kept_seqs: list[tuple[str, ...]] = []
    for seq, stack in by_seq:
        if any(other[: len(seq)] == seq and len(other) > len(seq) for other in kept_seqs):
            continue
        kept.append(stack)
        kept_seqs.append(seq)
    return kept


# --- Canonical builders ------------------------------------------------------


def _summarize_stacks(stacks: list[list[dict[str, Any]]], *, target_usr: str) -> dict[str, Any]:
    transitive: set[str] = set()
    by_module: dict[str, int] = {}
    by_depth: dict[int, int] = {}
    by_edge: dict[str, int] = {}
    for stack in stacks:
        for frame in stack:
            usr = frame.get("usr")
            if usr and usr != target_usr:
                transitive.add(usr)
            module = frame.get("module")
            if module:
                by_module[module] = by_module.get(module, 0) + 1
            edge = frame.get("edge_kind")
            if edge:
                by_edge[edge] = by_edge.get(edge, 0) + 1
        depth = max(0, len(stack) - 1)
        by_depth[depth] = by_depth.get(depth, 0) + 1
    return {
        "transitive_count": len(transitive),
        "module_count": len(by_module),
        "by_module": by_module,
        "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
        "by_edge_kind": by_edge,
    }


def build_call_stacks(
    conn: sqlite3.Connection,
    target: dict[str, Any],
    *,
    max_depth: int,
    max_stacks: int,
    upstream_kinds: tuple[str, ...],
    direction: str,
    to_module: str | None,
) -> dict[str, Any]:
    """Build canonical for callable target (mode=call_stack)."""
    target_usr = target["usr"]
    target_info = {
        "name": target.get("name"),
        "kind": target.get("kind"),
        "module": target.get("module"),
        "file": target.get("file"),
        "line": target.get("line"),
        "edge_kind": None,
        "site_file": None,
        "site_line": None,
    }

    upstream_stacks: list[list[dict[str, Any]]] = []
    upstream_truncated = False
    if direction in ("both", "up"):
        visited, parents = _bfs_upstream(
            conn, target_usr, max_depth=max_depth, kinds=upstream_kinds,
        )
        non_target = [u for u in visited if u != target_usr]
        terminals_at_depth = {u for u in non_target if visited[u] == max_depth}
        true_terminals = _find_terminal_callers(conn, non_target, kinds=upstream_kinds)
        leaves = sorted(true_terminals | terminals_at_depth, key=lambda u: -visited[u])
        for leaf in leaves:
            stack = _reconstruct_upstream_stack(leaf, target_usr, target_info, parents)
            if not stack or stack[-1]["usr"] != target_usr:
                continue
            if to_module and (stack[0].get("module") or "") != to_module:
                continue
            upstream_stacks.append(stack)
        upstream_stacks = _prune_subsumed(upstream_stacks)
        if len(upstream_stacks) > max_stacks:
            upstream_truncated = True
            upstream_stacks = upstream_stacks[:max_stacks]

    downstream_stacks: list[list[dict[str, Any]]] = []
    downstream_truncated = False
    if direction in ("both", "down"):
        visited_d, parents_d = _bfs_downstream(conn, target_usr, max_depth=max_depth)
        non_target = [u for u in visited_d if u != target_usr]
        terminals_at_depth = {u for u in non_target if visited_d[u] == max_depth}
        true_terminals = _find_terminal_callees(conn, non_target)
        leaves = sorted(true_terminals | terminals_at_depth, key=lambda u: -visited_d[u])
        for leaf in leaves:
            stack = _reconstruct_downstream_stack(leaf, target_usr, target_info, parents_d)
            if not stack or stack[0]["usr"] != target_usr:
                continue
            if to_module and (stack[-1].get("module") or "") != to_module:
                continue
            downstream_stacks.append(stack)
        downstream_stacks = _prune_subsumed_downstream(downstream_stacks)
        if len(downstream_stacks) > max_stacks:
            downstream_truncated = True
            downstream_stacks = downstream_stacks[:max_stacks]

    summary_up = _summarize_stacks(upstream_stacks, target_usr=target_usr)
    summary_down = _summarize_stacks(downstream_stacks, target_usr=target_usr)

    return {
        "kind": "impact",
        "mode": "call_stack",
        "anchor": {
            "usr": target_usr,
            "name": target.get("name"),
            "kind": target.get("kind"),
            "module": target.get("module"),
            "file": target.get("file"),
            "line": target.get("line"),
        },
        "summary": {
            "found": bool(upstream_stacks or downstream_stacks),
            "count": len(upstream_stacks) + len(downstream_stacks),
            "upstream": {
                "stacks": len(upstream_stacks),
                **summary_up,
            },
            "downstream": {
                "stacks": len(downstream_stacks),
                **summary_down,
            },
        },
        "stacks": {
            "upstream": upstream_stacks,
            "downstream": downstream_stacks,
        },
        "structure": None,
        "truncated": upstream_truncated or downstream_truncated,
    }


def _prune_subsumed_downstream(stacks: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Downstream stacks share the target prefix; subsumption is on the suffix."""
    by_seq = sorted(
        ((tuple(f["usr"] for f in s), s) for s in stacks),
        key=lambda x: -len(x[0]),
    )
    kept: list[list[dict[str, Any]]] = []
    kept_seqs: list[tuple[str, ...]] = []
    for seq, stack in by_seq:
        if any(other[: len(seq)] == seq and len(other) > len(seq) for other in kept_seqs):
            continue
        kept.append(stack)
        kept_seqs.append(seq)
    return kept


def build_usage_chain(
    conn: sqlite3.Connection,
    target: dict[str, Any],
    *,
    max_depth: int,
    max_stacks: int,
    direction: str,
    to_module: str | None,
) -> dict[str, Any]:
    """Build canonical for type target (mode=usage_chain)."""
    target_usr = target["usr"]
    target_info = {
        "name": target.get("name"),
        "kind": target.get("kind"),
        "module": target.get("module"),
        "file": target.get("file"),
        "line": target.get("line"),
        "edge_kind": None,
        "site_file": None,
        "site_line": None,
    }

    upstream_stacks: list[list[dict[str, Any]]] = []
    upstream_truncated = False
    if direction in ("both", "up"):
        seed_rows = query_module.fetch_type_reference_containers(conn, target_usr)
        seed_layer = [
            {
                "callee": target_usr,
                "caller": row.get("container_usr"),
                "edge_kind": "references",
                "site_file": row.get("site_file"),
                "site_line": row.get("site_line"),
                "caller_name": row.get("container_name"),
                "caller_kind": row.get("container_kind"),
                "caller_module": row.get("container_module"),
                "caller_file": row.get("container_file"),
                "caller_line": row.get("container_line"),
            }
            for row in seed_rows
            if row.get("container_usr")
        ]
        visited, parents = _bfs_upstream(
            conn, target_usr,
            max_depth=max_depth,
            kinds=("calledBy", "overrideOf", "baseOf"),
            seed_layer=seed_layer,
        )
        non_target = [u for u in visited if u != target_usr]
        terminals_at_depth = {u for u in non_target if visited[u] == max_depth}
        true_terminals = _find_terminal_callers(
            conn, non_target, kinds=("calledBy", "overrideOf", "baseOf"),
        )
        leaves = sorted(true_terminals | terminals_at_depth, key=lambda u: -visited[u])
        for leaf in leaves:
            stack = _reconstruct_upstream_stack(leaf, target_usr, target_info, parents)
            if not stack or stack[-1]["usr"] != target_usr:
                continue
            if to_module and (stack[0].get("module") or "") != to_module:
                continue
            upstream_stacks.append(stack)
        upstream_stacks = _prune_subsumed(upstream_stacks)
        if len(upstream_stacks) > max_stacks:
            upstream_truncated = True
            upstream_stacks = upstream_stacks[:max_stacks]

    structure = query_module.fetch_type_structure(conn, target_usr)

    summary_up = _summarize_stacks(upstream_stacks, target_usr=target_usr)

    return {
        "kind": "impact",
        "mode": "usage_chain",
        "anchor": {
            "usr": target_usr,
            "name": target.get("name"),
            "kind": target.get("kind"),
            "module": target.get("module"),
            "file": target.get("file"),
            "line": target.get("line"),
        },
        "summary": {
            "found": bool(upstream_stacks or any(structure.values())),
            "count": len(upstream_stacks),
            "upstream": {
                "stacks": len(upstream_stacks),
                **summary_up,
            },
            "structure_counts": {
                "members": len(structure["members"]),
                "subclasses": len(structure["subclasses"]),
                "extensions": len(structure["extensions"]),
            },
        },
        "stacks": {
            "upstream": upstream_stacks,
            "downstream": [],
        },
        "structure": structure,
        "truncated": upstream_truncated,
    }


def build_hint_only(target: dict[str, Any]) -> dict[str, Any]:
    """Build canonical for kinds that don't have a stack semantic (mode=hint_only)."""
    return {
        "kind": "impact",
        "mode": "hint_only",
        "anchor": {
            "usr": target["usr"],
            "name": target.get("name"),
            "kind": target.get("kind"),
            "module": target.get("module"),
            "file": target.get("file"),
            "line": target.get("line"),
        },
        "summary": {
            "found": True,
            "count": 0,
            "reason": f"kind {target.get('kind')!r} has no call/usage stack semantic",
        },
        "stacks": {"upstream": [], "downstream": []},
        "structure": None,
        "truncated": False,
    }


# --- Public entry point ------------------------------------------------------


def build_impact(
    conn: sqlite3.Connection,
    target: dict[str, Any],
    *,
    max_depth: int = 8,
    max_stacks: int = 10,
    direction: str = "both",
    no_overrides: bool = False,
    to_module: str | None = None,
) -> dict[str, Any]:
    """Dispatch on target kind and return the canonical impact dict."""
    mode = classify_kind(target.get("kind"))
    if mode == "call_stack":
        kinds = (
            ("calledBy",) if no_overrides
            else DEFAULT_UPSTREAM_KINDS
        )
        return build_call_stacks(
            conn, target,
            max_depth=max_depth,
            max_stacks=max_stacks,
            upstream_kinds=kinds,
            direction=direction,
            to_module=to_module,
        )
    if mode == "usage_chain":
        return build_usage_chain(
            conn, target,
            max_depth=max_depth,
            max_stacks=max_stacks,
            direction=direction,
            to_module=to_module,
        )
    return build_hint_only(target)

"""Unit tests for the impact module: BFS, stack reconstruction, classification."""
from __future__ import annotations

import sqlite3

import pytest

from xcindex import impact as impact_module
from xcindex import schema


def _build_call_graph(edges: list[tuple[str, str, str, str | None, int | None]]) -> sqlite3.Connection:
    """Build an in-memory cache from a list of (caller, callee, edge_kind, file, line) edges.

    Each tuple becomes:
      - symbols rows (caller and callee, deduped)
      - one occurrence of `callee` in `file`:`line` with container=caller
      - one relation pointing to caller with kind=edge_kind on that occurrence
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    cur = conn.cursor()

    usr_id_map: dict[str, int] = {}

    def intern(usr: str) -> int:
        if usr in usr_id_map:
            return usr_id_map[usr]
        cur.execute("INSERT INTO usrs(text) VALUES (?)", (usr,))
        usr_id_map[usr] = cur.lastrowid
        return usr_id_map[usr]

    seen_symbols: set[str] = set()
    occ_id = 0
    for caller, callee, _kind, file, line in edges:
        for usr in (caller, callee):
            if usr in seen_symbols or usr is None:
                continue
            seen_symbols.add(usr)
            cur.execute(
                "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
                "VALUES (?, ?, 'instance-method', NULL, 'swift', 'Mod', ?, ?, 0, 0)",
                (intern(usr), usr.upper(), file or f"{usr}.swift", line or 1),
            )
    for caller, callee, kind, file, line in edges:
        occ_id += 1
        cur.execute(
            "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
            "VALUES (?, ?, ?, ?, 1, 32, ?, 'u1')",
            (occ_id, intern(callee), file or f"{caller}.swift", line or 1, intern(caller)),
        )
        cur.execute(
            "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) VALUES (?, ?, ?, ?, 0)",
            (occ_id, intern(caller), caller.upper(), kind),
        )
    schema.apply_indexes(conn)
    conn.commit()
    return conn


def _target(usr: str, kind: str = "instance-method", name: str | None = None) -> dict:
    return {
        "usr": usr,
        "name": name or usr.upper(),
        "kind": kind,
        "module": "Mod",
        "file": f"{usr}.swift",
        "line": 1,
    }


# --- Mode classification ----------------------------------------------------

def test_classify_kind_callable():
    for k in ("instance-method", "class-method", "static-method", "function",
              "constructor", "destructor", "conversion-function"):
        assert impact_module.classify_kind(k) == "call_stack"


def test_classify_kind_type():
    for k in ("class", "struct", "enum", "protocol"):
        assert impact_module.classify_kind(k) == "usage_chain"


def test_classify_kind_property_returns_hint_only():
    for k in ("instance-property", "class-property", "static-property",
              "field", "variable", "extension", "typealias", "parameter",
              "enum-case", "macro", "namespace"):
        assert impact_module.classify_kind(k) == "hint_only"


def test_classify_kind_unknown_or_none_returns_hint_only():
    assert impact_module.classify_kind(None) == "hint_only"
    assert impact_module.classify_kind("nonsense") == "hint_only"


# --- Upstream BFS — simple chains -------------------------------------------

def test_upstream_simple_chain():
    # A → B → T (A calls B, B calls T)
    conn = _build_call_graph([
        ("A", "B", "calledBy", "A.swift", 1),
        ("B", "T", "calledBy", "B.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    assert len(stacks) == 1
    sequence = [f["usr"] for f in stacks[0]]
    assert sequence == ["A", "B", "T"]
    assert stacks[0][-1]["is_target"] is True


def test_upstream_two_roots_same_intermediate():
    # A → B → T, D → B → T → expect 2 stacks
    conn = _build_call_graph([
        ("A", "B", "calledBy", "A.swift", 1),
        ("D", "B", "calledBy", "D.swift", 1),
        ("B", "T", "calledBy", "B.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    assert len(stacks) == 2
    roots = {s[0]["usr"] for s in stacks}
    assert roots == {"A", "D"}


def test_upstream_branching_two_direct_roots():
    # A → T, B → T → 2 stacks of depth 1 each
    conn = _build_call_graph([
        ("A", "T", "calledBy", "A.swift", 1),
        ("B", "T", "calledBy", "B.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    assert len(stacks) == 2
    for s in stacks:
        assert len(s) == 2
        assert s[1]["usr"] == "T"


# --- Cycles -----------------------------------------------------------------

def test_upstream_self_recursion_terminates():
    # T calls T (self), and A calls T → no infinite loop
    conn = _build_call_graph([
        ("T", "T", "calledBy", "T.swift", 5),
        ("A", "T", "calledBy", "A.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    roots = {s[0]["usr"] for s in stacks}
    assert "A" in roots


def test_upstream_mutual_recursion_terminates():
    # A → B → A → T → cycle short-circuits, A appears once
    conn = _build_call_graph([
        ("A", "B", "calledBy", "A.swift", 1),
        ("B", "A", "calledBy", "B.swift", 1),
        ("A", "T", "calledBy", "A.swift", 5),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    for s in stacks:
        usrs = [f["usr"] for f in s]
        assert len(usrs) == len(set(usrs)), "cycle leaked into a stack"


# --- Depth limit ------------------------------------------------------------

def test_upstream_depth_limit_truncates_chain():
    # Z → Y → X → W → T  ; depth=2 should stop at X
    conn = _build_call_graph([
        ("Z", "Y", "calledBy", "Z.swift", 1),
        ("Y", "X", "calledBy", "Y.swift", 1),
        ("X", "W", "calledBy", "X.swift", 1),
        ("W", "T", "calledBy", "W.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=2, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    for s in stacks:
        # Frame depth = len(stack) - 1; never exceeds 2
        assert len(s) - 1 <= 2


# --- overrideOf inclusion / no-overrides flag -------------------------------

def test_upstream_includes_overrideOf_when_kind_provided():
    # M overrides T: relation kind=overrideOf with related_usr=M, occurrence of T
    conn = _build_call_graph([
        ("M", "T", "overrideOf", "M.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy", "overrideOf"), direction="up", to_module=None,
    )
    roots = {s[0]["usr"] for s in canonical["stacks"]["upstream"]}
    assert "M" in roots


def test_upstream_no_overrides_excludes_overrideOf():
    conn = _build_call_graph([
        ("M", "T", "overrideOf", "M.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    assert canonical["stacks"]["upstream"] == []


# --- Module filter ----------------------------------------------------------

def test_upstream_to_module_filters_root():
    # Multi-module fixture: two roots in different modules
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    cur = conn.cursor()
    USR_IDS = {"T": 1, "A": 2, "B": 3}
    cur.executemany("INSERT INTO usrs(id, text) VALUES (?, ?)",
                    [(uid, text) for text, uid in USR_IDS.items()])
    cur.executemany(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, 'instance-method', NULL, 'swift', ?, ?, 1, 0, 0)",
        [
            (USR_IDS["T"], "T", "Core",   "Core/T.swift"),
            (USR_IDS["A"], "A", "UI",     "UI/A.swift"),
            (USR_IDS["B"], "B", "Domain", "Domain/B.swift"),
        ],
    )
    cur.executemany(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
        "VALUES (?, ?, ?, ?, 1, 32, ?, 'u1')",
        [
            (1, USR_IDS["T"], "UI/A.swift",     5, USR_IDS["A"]),
            (2, USR_IDS["T"], "Domain/B.swift", 5, USR_IDS["B"]),
        ],
    )
    cur.executemany(
        "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) "
        "VALUES (?, ?, ?, 'calledBy', 0)",
        [(1, USR_IDS["A"], "A"), (2, USR_IDS["B"], "B")],
    )
    schema.apply_indexes(conn)
    conn.commit()

    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module="UI",
    )
    stacks = canonical["stacks"]["upstream"]
    assert len(stacks) == 1
    assert stacks[0][0]["module"] == "UI"


# --- Downstream BFS ---------------------------------------------------------

def test_downstream_simple_chain():
    # T calls X calls Y → 1 downstream stack [T, X, Y]
    conn = _build_call_graph([
        ("T", "X", "calledBy", "T.swift", 1),
        ("X", "Y", "calledBy", "X.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="down", to_module=None,
    )
    stacks = canonical["stacks"]["downstream"]
    assert len(stacks) == 1
    sequence = [f["usr"] for f in stacks[0]]
    assert sequence == ["T", "X", "Y"]
    assert stacks[0][0]["is_target"] is True


def test_downstream_multiple_callees_branches():
    conn = _build_call_graph([
        ("T", "X", "calledBy", "T.swift", 1),
        ("T", "Y", "calledBy", "T.swift", 2),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="down", to_module=None,
    )
    stacks = canonical["stacks"]["downstream"]
    assert len(stacks) == 2
    leaves = {s[-1]["usr"] for s in stacks}
    assert leaves == {"X", "Y"}


# --- Path subsumption -------------------------------------------------------

def test_path_subsumption_drops_prefix():
    # Two stacks: [A, B, T] and [B, T] — the second is prefix-subsumed by the first
    conn = _build_call_graph([
        ("A", "B", "calledBy", "A.swift", 1),
        ("B", "T", "calledBy", "B.swift", 1),
    ])
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    stacks = canonical["stacks"]["upstream"]
    # Only [A, B, T] should be kept; B alone has further callers (A) so it's not a true terminal
    assert any([f["usr"] for f in s] == ["A", "B", "T"] for s in stacks)


# --- Truncation -------------------------------------------------------------

def test_max_stacks_truncation_marks_truncated():
    edges = [(f"R{i}", "T", "calledBy", f"R{i}.swift", 1) for i in range(20)]
    conn = _build_call_graph(edges)
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=5,
        upstream_kinds=("calledBy",), direction="up", to_module=None,
    )
    assert canonical["truncated"] is True
    assert len(canonical["stacks"]["upstream"]) == 5


# --- Empty result -----------------------------------------------------------

def test_no_callers_no_callees_empty_canonical():
    # Isolated symbol with no relations
    conn = _build_call_graph([])
    cur = conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('T')")
    t_id = cur.lastrowid
    cur.execute(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, 'T', 'instance-method', NULL, 'swift', 'Mod', 'T.swift', 1, 0, 0)",
        (t_id,),
    )
    conn.commit()
    canonical = impact_module.build_call_stacks(
        conn, _target("T"), max_depth=8, max_stacks=10,
        upstream_kinds=("calledBy",), direction="both", to_module=None,
    )
    assert canonical["stacks"]["upstream"] == []
    assert canonical["stacks"]["downstream"] == []
    assert canonical["summary"]["found"] is False


# --- Type target ------------------------------------------------------------

def test_type_canonical_includes_structure_block():
    # type Foo with member bar() and subclass SubFoo
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    cur = conn.cursor()
    USR_IDS = {"foo": 1, "bar": 2, "subfoo": 3}
    cur.executemany("INSERT INTO usrs(id, text) VALUES (?, ?)",
                    [(uid, text) for text, uid in USR_IDS.items()])
    cur.executemany(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, ?, NULL, 'swift', 'Core', ?, ?, 0, 0)",
        [
            (USR_IDS["foo"],    "Foo",    "class",           "Core/Foo.swift",      1),
            (USR_IDS["bar"],    "bar()",  "instance-method", "Core/Foo.swift",      3),
            (USR_IDS["subfoo"], "SubFoo", "class",           "Domain/SubFoo.swift", 1),
        ],
    )
    cur.executemany(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, 'u1')",
        [
            (1, USR_IDS["bar"], "Core/Foo.swift",      3, 2, USR_IDS["foo"]),
            (2, USR_IDS["foo"], "Domain/SubFoo.swift", 1, 4, None),
        ],
    )
    cur.executemany(
        "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) VALUES (?, ?, ?, ?, 0)",
        [
            (1, USR_IDS["foo"],    "Foo",    "childOf"),
            (2, USR_IDS["subfoo"], "SubFoo", "baseOf"),
        ],
    )
    schema.apply_indexes(conn)
    conn.commit()

    canonical = impact_module.build_usage_chain(
        conn,
        {"usr": "foo", "name": "Foo", "kind": "class", "module": "Core",
         "file": "Core/Foo.swift", "line": 1},
        max_depth=8, max_stacks=10, direction="both", to_module=None,
    )
    assert canonical["mode"] == "usage_chain"
    structure = canonical["structure"]
    assert any(m["name"] == "bar()" for m in structure["members"])
    assert any(s["name"] == "SubFoo" for s in structure["subclasses"])


# --- Hint-only mode ---------------------------------------------------------

def test_hint_only_mode_returns_no_stacks():
    canonical = impact_module.build_hint_only(
        {"usr": "s:prop", "name": "token", "kind": "instance-property",
         "module": "Mod", "file": "f.swift", "line": 1},
    )
    assert canonical["mode"] == "hint_only"
    assert canonical["stacks"]["upstream"] == []
    assert canonical["stacks"]["downstream"] == []
    assert canonical["structure"] is None
    assert canonical["summary"]["found"] is True


# --- Public dispatch --------------------------------------------------------

def test_build_impact_dispatches_to_call_stack_for_method():
    conn = _build_call_graph([("A", "T", "calledBy", "A.swift", 1)])
    canonical = impact_module.build_impact(
        conn, _target("T", kind="instance-method"),
    )
    assert canonical["mode"] == "call_stack"


def test_build_impact_dispatches_to_usage_chain_for_class():
    conn = _build_call_graph([])
    cur = conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('T')")
    t_id = cur.lastrowid
    cur.execute(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, 'T', 'class', NULL, 'swift', 'Mod', 'T.swift', 1, 0, 0)",
        (t_id,),
    )
    conn.commit()
    canonical = impact_module.build_impact(
        conn, _target("T", kind="class"),
    )
    assert canonical["mode"] == "usage_chain"


def test_build_impact_dispatches_to_hint_only_for_property():
    conn = _build_call_graph([])
    canonical = impact_module.build_impact(
        conn, _target("T", kind="instance-property"),
    )
    assert canonical["mode"] == "hint_only"


def test_build_impact_no_overrides_drops_overrideOf_kind():
    # Method M overrides T → with default kinds, T should see M upstream
    conn = _build_call_graph([("M", "T", "overrideOf", "M.swift", 1)])
    default = impact_module.build_impact(
        conn, _target("T"), no_overrides=False,
    )
    strict = impact_module.build_impact(
        conn, _target("T"), no_overrides=True,
    )
    assert any(s[0]["usr"] == "M" for s in default["stacks"]["upstream"])
    assert strict["stacks"]["upstream"] == []

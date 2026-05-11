"""Unit tests for query.py against an in-memory SQLite populated with fixture data.

These tests do NOT touch the Swift helper. They validate the SQL queries directly,
which keeps them fast (<10ms) and isolates query correctness from helper behavior.
"""
from __future__ import annotations

import sqlite3

import pytest

from xcindex import query as query_module
from xcindex import schema


@pytest.fixture
def populated_conn():
    """Build a small in-memory store mirroring a Core/Domain/UI layered app.

    Layout:
      class Foo (Core)               usr=foo
        method bar()                  usr=bar
        method qux()                  usr=qux
      class SubFoo : Foo (Domain)    usr=subfoo
      class Caller (UI)              usr=caller
        method run() calls bar()     usr=run, occurrence at L42
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema.apply_schema(conn)
    cur = conn.cursor()

    # v4 schema: USRs interned. Pre-populate with known ids 1..6.
    USR_IDS = {
        "foo": 1, "bar": 2, "qux": 3,
        "subfoo": 4, "caller": 5, "run": 6,
    }
    cur.executemany(
        "INSERT INTO usrs(id, text) VALUES (?, ?)",
        [(uid, text) for text, uid in USR_IDS.items()],
    )
    cur.executemany(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (USR_IDS["foo"],    "Foo",    "class",           None, "swift", "Core",   "Core/Foo.swift",       1, 0, 0),
            (USR_IDS["bar"],    "bar()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       3, 0, 0),
            (USR_IDS["qux"],    "qux()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       7, 0, 0),
            (USR_IDS["subfoo"], "SubFoo", "class",           None, "swift", "Domain", "Domain/SubFoo.swift",  2, 0, 0),
            (USR_IDS["caller"], "Caller", "class",           None, "swift", "UI",     "UI/Caller.swift",      1, 0, 0),
            (USR_IDS["run"],    "run()",  "instance-method", None, "swift", "UI",     "UI/Caller.swift",      5, 0, 0),
        ],
    )

    # Occurrences:
    # 1: Foo definition at Core/Foo.swift:1
    # 2: bar() definition at Core/Foo.swift:3
    # 3: Foo as base in Domain/SubFoo.swift:2 (relation: SubFoo baseOf Foo)
    # 4: bar() called inside run() at UI/Caller.swift:42 (relation: run calledBy bar)
    # 5: bar() childOf Foo (definition relation)
    cur.executemany(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, USR_IDS["foo"], "Core/Foo.swift",      1, 7,  2,  None, "u1"),
            (2, USR_IDS["bar"], "Core/Foo.swift",      3, 17, 2,  USR_IDS["foo"], "u1"),
            (3, USR_IDS["foo"], "Domain/SubFoo.swift", 2, 24, 4,  None, "u2"),
            (4, USR_IDS["bar"], "UI/Caller.swift",     42, 18, 32, USR_IDS["run"], "u3"),
            (5, USR_IDS["qux"], "Core/Foo.swift",      7, 17, 2,  USR_IDS["foo"], "u1"),
        ],
    )
    cur.executemany(
        "INSERT INTO relations(occurrence_id, related_usr_id, related_name, kind, roles) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (3, USR_IDS["subfoo"], "SubFoo", "baseOf",     0),
            (4, USR_IDS["run"],    "run()",  "calledBy",   0),
            (2, USR_IDS["foo"],    "Foo",    "childOf",    0),
            (5, USR_IDS["foo"],    "Foo",    "childOf",    0),
        ],
    )
    schema.apply_indexes(conn)
    conn.commit()
    yield conn
    conn.close()


# --- query_at ---------------------------------------------------------------

def test_query_at_returns_occurrences(populated_conn):
    out = query_module.query_at(populated_conn, "Core/Foo.swift", 3)
    assert out["summary"]["found"]
    assert any(item["name"] == "bar()" for item in out["items"])


def test_query_at_with_column(populated_conn):
    out = query_module.query_at(populated_conn, "Core/Foo.swift", 3, column=17)
    assert out["summary"]["count"] == 1


def test_query_at_no_match(populated_conn):
    out = query_module.query_at(populated_conn, "Core/Foo.swift", 999)
    assert out["summary"]["found"] is False


# --- query_containing -------------------------------------------------------

def test_query_containing_finds_method(populated_conn):
    out = query_module.query_containing(populated_conn, "Core/Foo.swift", 4)
    assert out["summary"]["found"]
    assert out["items"][0]["name"] == "bar()"


def test_query_containing_picks_nearest(populated_conn):
    out = query_module.query_containing(populated_conn, "Core/Foo.swift", 8)
    assert out["items"][0]["name"] == "qux()"


# --- query_symbol_by_usr / by_name ------------------------------------------

def test_query_symbol_by_usr(populated_conn):
    out = query_module.query_symbol_by_usr(populated_conn, "foo")
    assert out["summary"]["found"]
    assert out["items"][0]["kind"] == "class"


def test_query_symbol_by_name(populated_conn):
    out = query_module.query_symbol_by_name(populated_conn, "Foo")
    assert out["summary"]["found"]
    assert any(it["usr"] == "foo" for it in out["items"])


# --- query_occurrences ------------------------------------------------------

def test_query_occurrences_unfiltered(populated_conn):
    out = query_module.query_occurrences(populated_conn, "foo")
    assert out["summary"]["count"] == 2


def test_query_occurrences_filtered_by_role(populated_conn):
    out = query_module.query_occurrences(populated_conn, "bar", role="call")
    assert out["summary"]["count"] == 1
    assert out["items"][0]["file"] == "UI/Caller.swift"


# --- query_relations --------------------------------------------------------

def test_query_relations_out_finds_subclasses(populated_conn):
    out = query_module.query_relations(populated_conn, "foo", direction="out", kind="baseOf")
    assert out["summary"]["count"] == 1
    assert out["items"][0]["name"] == "SubFoo"


def test_query_relations_in_finds_caller(populated_conn):
    out = query_module.query_relations(populated_conn, "run", direction="in", kind="calledBy")
    assert out["summary"]["count"] == 1
    assert out["items"][0]["name"] == "bar()"


def test_query_relations_invalid_direction_raises(populated_conn):
    with pytest.raises(ValueError):
        query_module.query_relations(populated_conn, "foo", direction="sideways")


# --- query_neighbors --------------------------------------------------------

def test_query_neighbors_both_directions(populated_conn):
    out = query_module.query_neighbors(populated_conn, "foo", direction="both")
    assert out["summary"]["found"]
    rel_kinds = {it["rel_kind"] for it in out["items"]}
    assert "baseOf" in rel_kinds or "childOf" in rel_kinds


# --- query_reach ------------------------------------------------------------

def test_query_reach_up_from_bar_reaches_run(populated_conn):
    out = query_module.query_reach(populated_conn, "bar", direction="up", max_depth=4)
    usrs = {it["usr"] for it in out["items"]}
    assert "run" in usrs


def test_query_reach_to_module_filter(populated_conn):
    out = query_module.query_reach(populated_conn, "bar", direction="up", max_depth=4, to_module="UI")
    assert all(it["module"] == "UI" for it in out["items"])
    assert any(it["usr"] == "run" for it in out["items"])


def test_query_reach_respects_max_depth(populated_conn):
    shallow = query_module.query_reach(populated_conn, "bar", direction="up", max_depth=0)
    assert shallow["summary"]["count"] == 0


def test_query_reach_invalid_direction_raises(populated_conn):
    with pytest.raises(ValueError):
        query_module.query_reach(populated_conn, "foo", direction="sideways")


def test_query_reach_dedupe_keeps_min_depth(populated_conn):
    out = query_module.query_reach(populated_conn, "bar", direction="up", max_depth=4)
    seen = {}
    for it in out["items"]:
        assert it["usr"] not in seen, "USR appears more than once"
        seen[it["usr"]] = it["depth"]


# --- query_search -----------------------------------------------------------

def test_query_search_substring(populated_conn):
    out = query_module.query_search(populated_conn, "oo")
    assert out["summary"]["found"]
    names = {it["name"] for it in out["items"]}
    assert "Foo" in names
    assert "SubFoo" in names


def test_query_search_filter_by_kind(populated_conn):
    out = query_module.query_search(populated_conn, "oo", kind="class")
    for it in out["items"]:
        assert it["kind"] == "class"


def test_query_search_filter_by_module(populated_conn):
    out = query_module.query_search(populated_conn, "Foo", module="Domain")
    assert out["summary"]["count"] == 1
    assert out["items"][0]["name"] == "SubFoo"


# --- find_files_in_index / query_file_definitions --------------------------

def test_find_files_by_basename(populated_conn):
    matches = query_module.find_files_in_index(populated_conn, "Foo.swift")
    assert matches == ["Core/Foo.swift"]


def test_find_files_by_stem_no_extension(populated_conn):
    matches = query_module.find_files_in_index(populated_conn, "Caller")
    assert matches == ["UI/Caller.swift"]


def test_find_files_by_full_path_exact_match(populated_conn):
    matches = query_module.find_files_in_index(populated_conn, "Core/Foo.swift")
    assert matches == ["Core/Foo.swift"]


def test_find_files_returns_empty_when_missing(populated_conn):
    assert query_module.find_files_in_index(populated_conn, "Nope.swift") == []


def test_find_files_returns_all_basename_collisions(populated_conn):
    cur = populated_conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('dupfoo')")
    dupfoo_id = cur.lastrowid
    cur.execute(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dupfoo_id, "DupFoo", "class", None, "swift", "Other", "Other/Foo.swift", 1, 0, 0),
    )
    populated_conn.commit()
    matches = query_module.find_files_in_index(populated_conn, "Foo.swift")
    assert set(matches) == {"Core/Foo.swift", "Other/Foo.swift"}


def test_query_file_definitions_default_kinds(populated_conn):
    out = query_module.query_file_definitions(
        populated_conn, "Core/Foo.swift", kinds=("class", "struct", "enum", "protocol"),
    )
    assert out["summary"]["count"] == 1
    assert out["items"][0]["name"] == "Foo"
    assert out["items"][0]["usr"] == "foo"
    assert out["items"][0]["kind"] == "class"


def test_query_file_definitions_all_kinds(populated_conn):
    out = query_module.query_file_definitions(populated_conn, "Core/Foo.swift")
    names = [it["name"] for it in out["items"]]
    assert "Foo" in names and "bar()" in names and "qux()" in names
    assert out["summary"]["by_kind"]["class"] == 1
    assert out["summary"]["by_kind"]["instance-method"] == 2


def test_query_file_definitions_no_match(populated_conn):
    out = query_module.query_file_definitions(populated_conn, "Nope.swift")
    assert out["summary"]["found"] is False
    assert out["items"] == []


# --- decode_roles -----------------------------------------------------------

def test_decode_roles_definition_bit():
    assert "definition" in query_module.decode_roles(2)


def test_decode_roles_handles_signed_overflow():
    # SymbolRole.all wraps to negative when stored as int64; decode should still work
    assert query_module.decode_roles(-1) != []


def test_role_bit_unknown_raises():
    with pytest.raises(ValueError):
        query_module.role_bit("nonsense")


# --- resolve_input_to_usr ---------------------------------------------------

def test_resolve_input_passthrough_swift_usr_keeps_existing(populated_conn):
    cur = populated_conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('s:Real')")
    real_id = cur.lastrowid
    cur.execute(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, 'Real', 'class', NULL, 'swift', 'Core', 'Core/Real.swift', 1, 0, 0)",
        (real_id,),
    )
    populated_conn.commit()
    target = query_module.resolve_input_to_usr(populated_conn, "s:Real")
    assert target["usr"] == "s:Real"
    assert target["kind"] == "class"


def test_resolve_input_passthrough_unknown_usr_raises(populated_conn):
    with pytest.raises(query_module.SymbolNotFoundError):
        query_module.resolve_input_to_usr(populated_conn, "s:nonexistent")


def test_resolve_input_name_unique_match(populated_conn):
    target = query_module.resolve_input_to_usr(populated_conn, "Caller")
    assert target["usr"] == "caller"
    assert target["kind"] == "class"


def test_resolve_input_name_ambiguous_raises_with_candidates(populated_conn):
    cur = populated_conn.cursor()
    cur.execute("INSERT INTO usrs(text) VALUES ('caller2')")
    caller2_id = cur.lastrowid
    cur.execute(
        "INSERT INTO symbols(usr_id, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, 'Caller', 'class', NULL, 'swift', 'Other', 'Other/Caller.swift', 1, 0, 0)",
        (caller2_id,),
    )
    populated_conn.commit()
    with pytest.raises(query_module.AmbiguousNameError) as exc_info:
        query_module.resolve_input_to_usr(populated_conn, "Caller")
    assert exc_info.value.name == "Caller"
    assert len(exc_info.value.candidates) == 2


def test_resolve_input_name_not_found_raises(populated_conn):
    with pytest.raises(query_module.SymbolNotFoundError):
        query_module.resolve_input_to_usr(populated_conn, "Nonexistent")


def test_resolve_input_file_line_picks_containing_symbol(populated_conn):
    target = query_module.resolve_input_to_usr(populated_conn, "Core/Foo.swift:4")
    assert target["usr"] == "bar"


def test_resolve_input_file_line_no_match_raises(populated_conn):
    with pytest.raises(query_module.SymbolNotFoundError):
        query_module.resolve_input_to_usr(populated_conn, "Nope.swift:42")


# --- fetch_callers_layer / fetch_callees_layer ------------------------------

def test_fetch_callers_layer_simple(populated_conn):
    rows = query_module.fetch_callers_layer(populated_conn, ["bar"], kinds=("calledBy",))
    assert len(rows) == 1
    assert rows[0]["caller"] == "run"
    assert rows[0]["edge_kind"] == "calledBy"


def test_fetch_callers_layer_filters_by_kind(populated_conn):
    rows = query_module.fetch_callers_layer(populated_conn, ["bar"], kinds=("overrideOf",))
    assert rows == []


def test_fetch_callers_layer_includes_site(populated_conn):
    rows = query_module.fetch_callers_layer(populated_conn, ["bar"], kinds=("calledBy",))
    assert rows[0]["site_file"] == "UI/Caller.swift"
    assert rows[0]["site_line"] == 42


def test_fetch_callers_layer_empty_frontier_returns_empty(populated_conn):
    assert query_module.fetch_callers_layer(populated_conn, [], kinds=("calledBy",)) == []


def test_fetch_callees_layer_inverts_direction(populated_conn):
    rows = query_module.fetch_callees_layer(populated_conn, ["run"], kinds=("calledBy",))
    callees = {row["callee"] for row in rows}
    assert "bar" in callees


# --- fetch_type_reference_containers ---------------------------------------

def test_fetch_type_reference_containers_returns_distinct(populated_conn):
    cur = populated_conn.cursor()
    # Add a reference of Foo inside run() (container=run). Reuse the seed ids 1-6.
    cur.execute(
        "INSERT INTO occurrences(id, symbol_usr_id, file, line, column, roles, container_usr_id, unit_name) "
        "VALUES (10, 1, 'UI/Caller.swift', 6, 10, 4, 6, 'u3')"
    )
    populated_conn.commit()
    rows = query_module.fetch_type_reference_containers(populated_conn, "foo")
    containers = {row["container_usr"] for row in rows}
    assert "run" in containers


def test_fetch_type_reference_containers_skips_target_self(populated_conn):
    rows = query_module.fetch_type_reference_containers(populated_conn, "foo")
    for row in rows:
        assert row["container_usr"] != "foo"


# --- fetch_type_structure ---------------------------------------------------

def test_fetch_type_structure_lists_members(populated_conn):
    structure = query_module.fetch_type_structure(populated_conn, "foo")
    member_names = {m["name"] for m in structure["members"]}
    assert "bar()" in member_names
    assert "qux()" in member_names


def test_fetch_type_structure_lists_subclasses(populated_conn):
    structure = query_module.fetch_type_structure(populated_conn, "foo")
    sub_names = {s["name"] for s in structure["subclasses"]}
    assert "SubFoo" in sub_names


def test_fetch_type_structure_returns_empty_lists_for_unknown_type(populated_conn):
    structure = query_module.fetch_type_structure(populated_conn, "nonexistent")
    assert structure == {"members": [], "subclasses": [], "extensions": []}

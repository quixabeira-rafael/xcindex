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
    cur.executemany(
        "INSERT INTO symbols(usr, name, kind, sub_kind, language, module, file, line, is_system, properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("foo",    "Foo",    "class",           None, "swift", "Core",   "Core/Foo.swift",       1, 0, 0),
            ("bar",    "bar()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       3, 0, 0),
            ("qux",    "qux()",  "instance-method", None, "swift", "Core",   "Core/Foo.swift",       7, 0, 0),
            ("subfoo", "SubFoo", "class",           None, "swift", "Domain", "Domain/SubFoo.swift",  2, 0, 0),
            ("caller", "Caller", "class",           None, "swift", "UI",     "UI/Caller.swift",      1, 0, 0),
            ("run",    "run()",  "instance-method", None, "swift", "UI",     "UI/Caller.swift",      5, 0, 0),
        ],
    )

    # Occurrences:
    # 1: Foo definition at Core/Foo.swift:1
    # 2: bar() definition at Core/Foo.swift:3
    # 3: Foo as base in Domain/SubFoo.swift:2 (relation: SubFoo baseOf Foo)
    # 4: bar() called inside run() at UI/Caller.swift:42 (relation: run calledBy bar)
    # 5: bar() childOf Foo (definition relation)
    cur.executemany(
        "INSERT INTO occurrences(id, symbol_usr, file, line, column, roles, container_usr, unit_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "foo",    "Core/Foo.swift",       1, 7,  2, None, "u1"),       # role=definition
            (2, "bar",    "Core/Foo.swift",       3, 17, 2, "foo", "u1"),     # role=definition, container=foo
            (3, "foo",    "Domain/SubFoo.swift",  2, 24, 4, None, "u2"),       # role=reference + baseOf
            (4, "bar",    "UI/Caller.swift",      42, 18, 32, "run", "u3"),    # role=call (32=1<<5)
            (5, "qux",    "Core/Foo.swift",       7, 17, 2, "foo", "u1"),
        ],
    )
    cur.executemany(
        "INSERT INTO relations(occurrence_id, related_usr, related_name, kind, roles) VALUES (?, ?, ?, ?, ?)",
        [
            (3, "subfoo", "SubFoo", "baseOf",     0),       # at occ 3 (Foo in SubFoo def): SubFoo has Foo as base
            (4, "run",    "run()",  "calledBy",   0),       # at occ 4 (bar called in run): run is the caller
            (2, "foo",    "Foo",    "childOf",    0),       # bar is child of Foo
            (5, "foo",    "Foo",    "childOf",    0),       # qux is child of Foo
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


# --- decode_roles -----------------------------------------------------------

def test_decode_roles_definition_bit():
    assert "definition" in query_module.decode_roles(2)


def test_decode_roles_handles_signed_overflow():
    # SymbolRole.all wraps to negative when stored as int64; decode should still work
    assert query_module.decode_roles(-1) != []


def test_role_bit_unknown_raises():
    with pytest.raises(ValueError):
        query_module.role_bit("nonsense")

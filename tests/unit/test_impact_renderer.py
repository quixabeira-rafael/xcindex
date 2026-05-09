"""Unit tests for the impact renderer (agent/json formats, hints, structure)."""
from __future__ import annotations

import json

from xcindex.output import project, render


def _frame(usr, name, file=None, line=None, module=None, edge_kind=None, is_target=False, kind=None):
    return {
        "usr": usr, "name": name, "file": file, "line": line, "module": module,
        "edge_kind": edge_kind, "site_file": None, "site_line": None,
        "is_target": is_target, "kind": kind,
    }


def _call_stack_canonical(*, upstream=None, downstream=None, truncated=False):
    target_anchor = {
        "usr": "s:T", "name": "compute(_:)", "kind": "instance-method",
        "module": "Core", "file": "Core/Calc.swift", "line": 100,
    }
    return {
        "kind": "impact",
        "mode": "call_stack",
        "anchor": target_anchor,
        "summary": {
            "found": bool(upstream or downstream),
            "count": (len(upstream or []) + len(downstream or [])),
            "upstream": {
                "stacks": len(upstream or []),
                "transitive_count": 3,
                "module_count": 1,
                "by_module": {"UI": 1, "Domain": 1, "Core": 1},
                "by_depth": {"3": 1},
                "by_edge_kind": {"calledBy": 3},
            },
            "downstream": {
                "stacks": len(downstream or []),
                "transitive_count": 0,
                "module_count": 0,
                "by_module": {},
                "by_depth": {},
                "by_edge_kind": {},
            },
        },
        "stacks": {
            "upstream": upstream or [],
            "downstream": downstream or [],
        },
        "structure": None,
        "truncated": truncated,
        "warnings": [],
    }


def _usage_chain_canonical(structure=None):
    return {
        "kind": "impact",
        "mode": "usage_chain",
        "anchor": {
            "usr": "s:Foo", "name": "Foo", "kind": "class",
            "module": "Core", "file": "Core/Foo.swift", "line": 1,
        },
        "summary": {
            "found": True,
            "count": 1,
            "upstream": {
                "stacks": 1,
                "transitive_count": 2,
                "module_count": 2,
                "by_module": {"UI": 1, "Domain": 1},
                "by_depth": {"2": 1},
                "by_edge_kind": {"references": 1, "calledBy": 1},
            },
            "structure_counts": {"members": 2, "subclasses": 1, "extensions": 0},
        },
        "stacks": {"upstream": [], "downstream": []},
        "structure": structure or {
            "members": [
                {"usr": "s:bar", "name": "bar()", "kind": "instance-method",
                 "module": "Core", "file": "Core/Foo.swift", "line": 3},
            ],
            "subclasses": [
                {"usr": "s:Sub", "name": "SubFoo", "kind": "class",
                 "module": "Domain", "file": "Domain/Sub.swift", "line": 1},
            ],
            "extensions": [],
        },
        "truncated": False,
        "warnings": [],
    }


def _hint_only_canonical(kind="instance-property", usr="s:tok", name="token"):
    return {
        "kind": "impact",
        "mode": "hint_only",
        "anchor": {"usr": usr, "name": name, "kind": kind,
                   "module": "Mod", "file": "f.swift", "line": 1},
        "summary": {
            "found": True, "count": 0,
            "reason": f"kind {kind!r} has no call/usage stack semantic",
        },
        "stacks": {"upstream": [], "downstream": []},
        "structure": None,
        "truncated": False,
        "warnings": [],
    }


# --- Call-stack agent rendering --------------------------------------------

def test_callable_impact_agent_renders_stack_frames():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:A", "start()", "UI/A.swift", 88, "UI"),
        _frame("s:T", "compute(_:)", "Core/Calc.swift", 100, "Core", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "agent")
    assert "[upstream stack 1]" in text
    assert "#0" in text and "#1" in text
    assert "start()" in text
    assert "compute(_:)" in text


def test_callable_impact_marks_target_with_arrow():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:A", "caller()", "f.swift", 1, "M"),
        _frame("s:T", "compute(_:)", "f.swift", 100, "Core", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "agent")
    assert "← target" in text


def test_callable_impact_shows_edge_kind_when_not_calledBy():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:M", "test()", "Tests/M.swift", 1, "Tests", edge_kind="overrideOf"),
        _frame("s:T", "compute(_:)", "Core/T.swift", 100, "Core", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "agent")
    assert "(overrideOf)" in text


def test_callable_impact_summary_block_includes_module_and_depth():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:A", "caller()", "f.swift", 1, "M"),
        _frame("s:T", "compute(_:)", "f.swift", 100, "Core", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "agent")
    assert "**summary**" in text
    assert "by_module:" in text
    assert "by_depth:" in text


def test_callable_impact_truncated_marker_shown():
    canonical = _call_stack_canonical(
        upstream=[[_frame("s:T", "compute(_:)", "f.swift", 100, "Core", is_target=True)]],
        truncated=True,
    )
    text = render(project(canonical, "detailed"), "agent")
    assert "truncated" in text.lower()


def test_callable_impact_empty_renders_no_impact_message():
    canonical = _call_stack_canonical(upstream=[], downstream=[])
    text = render(project(canonical, "detailed"), "agent")
    assert "no transitive callers/callees found" in text


# --- Usage-chain rendering --------------------------------------------------

def test_type_impact_renders_structure_block():
    canonical = _usage_chain_canonical()
    text = render(project(canonical, "detailed"), "agent")
    assert "**structure**" in text
    assert "members" in text and "bar()" in text
    assert "subclasses" in text and "SubFoo" in text


def test_type_impact_includes_structure_counts_in_headline():
    canonical = _usage_chain_canonical()
    text = render(project(canonical, "detailed"), "agent")
    assert "type" in text
    assert "members" in text


# --- Hint-only rendering ----------------------------------------------------

def test_hint_only_no_stacks_no_structure_block():
    text = render(project(_hint_only_canonical(), "detailed"), "agent")
    assert "[upstream stack" not in text
    assert "**structure**" not in text


def test_hint_only_property_renders_read_write_suggestions():
    text = render(project(_hint_only_canonical(kind="instance-property"), "detailed"), "agent")
    assert "read sites" in text
    assert "write sites" in text
    assert "containing type" in text


def test_hint_only_extension_renders_member_and_extended_type():
    text = render(project(_hint_only_canonical(kind="extension"), "detailed"), "agent")
    assert "members:" in text
    assert "extended type" in text


def test_hint_only_typealias_renders_references():
    text = render(project(_hint_only_canonical(kind="typealias"), "detailed"), "agent")
    assert "references" in text


def test_hint_only_enum_case_renders_containing_enum():
    text = render(project(_hint_only_canonical(kind="enum-case"), "detailed"), "agent")
    assert "containing enum" in text


def test_hint_only_parameter_falls_back_to_references():
    text = render(project(_hint_only_canonical(kind="parameter"), "detailed"), "agent")
    assert "references" in text


# --- Shell quoting in hints -------------------------------------------------

def test_impact_quotes_objc_usr_with_parens_in_hints():
    canonical = _hint_only_canonical(
        kind="instance-property",
        usr="c:@M@WWMobile@objc(cs)Foo(py)bar",
        name="bar",
    )
    text = render(project(canonical, "detailed"), "agent")
    assert "'c:@M@WWMobile@objc(cs)Foo(py)bar'" in text


def test_impact_leaves_clean_swift_usr_unquoted_in_hints():
    canonical = _hint_only_canonical(
        kind="instance-property",
        usr="s:5MyApp4FooC3barSdvp",
    )
    text = render(project(canonical, "detailed"), "agent")
    assert "xcindex occurrences s:5MyApp4FooC3barSdvp --role read" in text
    assert "'s:5MyApp4FooC3barSdvp'" not in text


# --- JSON / format invariants ----------------------------------------------

def test_impact_json_format_emits_canonical_shape():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:A", "a()", "A.swift", 1, "M"),
        _frame("s:T", "t()", "T.swift", 1, "M", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "json")
    parsed = json.loads(text)
    assert parsed["kind"] == "impact"
    assert parsed["mode"] == "call_stack"
    assert "stacks" in parsed
    assert isinstance(parsed["stacks"]["upstream"], list)


def test_impact_json_does_not_include_next_steps_text():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:T", "t()", "T.swift", 1, "M", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "json")
    assert "next steps" not in text


def test_impact_warnings_propagated_in_agent_output():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:T", "t()", "T.swift", 1, "M", is_target=True),
    ]])
    canonical["warnings"] = ["index store is older than source files"]
    text = render(project(canonical, "detailed"), "agent")
    assert "warnings:" in text
    assert "older than source" in text


def test_impact_external_frame_renders_external_marker():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:Ext", None, file=None, line=None, module=None),
        _frame("s:T", "t()", "T.swift", 1, "M", is_target=True),
    ]])
    text = render(project(canonical, "detailed"), "agent")
    assert "(external)" in text


# --- summary level omits stacks --------------------------------------------

def test_impact_summary_level_omits_stacks_block():
    canonical = _call_stack_canonical(upstream=[[
        _frame("s:A", "a()", "A.swift", 1, "M"),
        _frame("s:T", "t()", "T.swift", 1, "M", is_target=True),
    ]])
    projected = project(canonical, "summary")
    assert "stacks" not in projected

from __future__ import annotations

import json

import pytest

from xcindex.output import project, render


def _canonical():
    return {
        "kind": "occurrences",
        "anchor": {"name": "compute(_:)"},
        "summary": {"found": True, "count": 2, "files": 2},
        "items": [
            {"name": "compute(_:)", "file": "Foo.swift", "line": 12, "container": "Foo.bar()", "kind": "instance-method"},
            {"name": "compute(_:)", "file": "Bar.swift", "line": 44, "container": "Bar.run()", "kind": "instance-method"},
        ],
        "truncated": False,
        "warnings": [],
    }


def test_json_format_returns_valid_json():
    projected = project(_canonical(), "detailed")
    text = render(projected, "json")
    parsed = json.loads(text)
    assert parsed["kind"] == "occurrences"
    assert len(parsed["items"]) == 2


def test_jsonl_emits_header_and_one_line_per_item():
    projected = project(_canonical(), "detailed")
    text = render(projected, "jsonl")
    lines = text.split("\n")
    assert len(lines) == 3  # header + 2 items
    head = json.loads(lines[0])
    assert "items" not in head
    assert head["kind"] == "occurrences"
    items = [json.loads(line) for line in lines[1:]]
    assert all("file" in item for item in items)


def test_compact_emits_tsv_for_items():
    projected = project(_canonical(), "detailed")
    text = render(projected, "compact")
    rows = text.split("\n")
    assert len(rows) == 2
    cols = rows[0].split("\t")
    assert "Foo.swift" in cols


def test_compact_falls_back_when_no_items():
    canonical = _canonical()
    canonical.pop("items")
    projected = project(canonical, "summary")
    text = render(projected, "compact")
    assert "found=" in text or "count=" in text


def test_agent_format_has_headline_and_locations():
    projected = project(_canonical(), "detailed")
    text = render(projected, "agent")
    assert text.startswith("## occurrences")
    assert "compute(_:)" in text
    assert "**locations**" in text
    assert "Foo.swift" in text


def test_agent_format_includes_truncated_marker():
    canonical = _canonical()
    canonical["truncated"] = True
    projected = project(canonical, "detailed")
    text = render(projected, "agent")
    assert "truncated" in text


def test_agent_format_includes_warnings():
    canonical = _canonical()
    canonical["warnings"] = ["index store is older than source files"]
    projected = project(canonical, "detailed")
    text = render(projected, "agent")
    assert "warnings:" in text
    assert "older than source" in text


def test_unknown_format_raises():
    projected = project(_canonical(), "summary")
    with pytest.raises(ValueError):
        render(projected, "yaml")


def _file_canonical(items, file="/abs/path/User.swift"):
    return {
        "kind": "file",
        "anchor": {"file": file},
        "summary": {"found": True, "count": len(items)},
        "items": items,
        "truncated": False,
        "warnings": [],
    }


def test_agent_file_table_columns_are_kind_name_usr():
    canonical = _file_canonical([
        {"kind": "class", "name": "User", "usr": "s:U", "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "agent")
    assert "kind" in text and "name" in text and "usr" in text
    assert "class" in text and "User" in text and "s:U" in text


def test_agent_file_hints_class_includes_subclasses_and_extensions():
    canonical = _file_canonical([
        {"kind": "class", "name": "User", "usr": "s:U", "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "agent")
    assert "**next steps**" in text
    assert "subclasses/conformers:" in text
    assert "extensions:" in text
    assert "members:" in text
    assert "callers" not in text
    assert "reads" not in text


def test_agent_file_hints_struct_omits_subclasses():
    canonical = _file_canonical([
        {"kind": "struct", "name": "Money", "usr": "s:M", "file": "/abs/path/Money.swift"},
    ], file="/abs/path/Money.swift")
    text = render(project(canonical, "detailed"), "agent")
    assert "subclasses/conformers" not in text
    assert "extensions:" in text


def test_agent_file_hints_target_matches_file_stem():
    canonical = _file_canonical([
        {"kind": "enum", "name": "CodingKeys", "usr": "s:CK", "file": "/abs/path/User.swift"},
        {"kind": "class", "name": "User", "usr": "s:U", "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "agent")
    assert "inspect User:" in text
    assert "xcindex symbol s:U\n" in text + "\n"


def test_agent_file_hints_with_method_and_property_extends_block():
    canonical = _file_canonical([
        {"kind": "class", "name": "User", "usr": "s:U", "file": "/abs/path/User.swift"},
        {"kind": "instance-method", "name": "doIt()", "usr": "s:M", "file": "/abs/path/User.swift"},
        {"kind": "instance-property", "name": "token", "usr": "s:P", "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "agent")
    assert "callers of doIt():" in text
    assert "override chain of doIt():" in text
    assert "reads of token:" in text
    assert "writes of token:" in text


def test_agent_file_hints_absent_for_non_file_kinds():
    canonical = _canonical()
    text = render(project(canonical, "detailed"), "agent")
    assert "**next steps**" not in text


def test_file_hints_absent_in_json():
    canonical = _file_canonical([
        {"kind": "class", "name": "User", "usr": "s:U", "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "json")
    assert "next steps" not in text


def test_file_hints_quote_objc_usr_with_parens():
    canonical = _file_canonical([
        {"kind": "class", "name": "AppDelegate",
         "usr": "c:@M@WWMobile@objc(cs)AppDelegate",
         "file": "/abs/path/AppDelegate.swift"},
    ], file="/abs/path/AppDelegate.swift")
    text = render(project(canonical, "detailed"), "agent")
    assert "xcindex symbol 'c:@M@WWMobile@objc(cs)AppDelegate'" in text


def test_file_hints_leave_swift_usr_unquoted():
    canonical = _file_canonical([
        {"kind": "class", "name": "User", "usr": "s:5MyApp4UserC",
         "file": "/abs/path/User.swift"},
    ])
    text = render(project(canonical, "detailed"), "agent")
    assert "xcindex symbol s:5MyApp4UserC" in text
    assert "'s:5MyApp4UserC'" not in text

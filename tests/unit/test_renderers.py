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

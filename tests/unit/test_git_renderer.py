"""Renderer tests for the `git` canonical (agent + json formats)."""
from __future__ import annotations

import json

from xcindex.output import project, render


def _git_canonical(files=None, base="origin/main", staged=False):
    files = files or []
    sym_count = sum(len(f.get("symbols") or []) for f in files)
    by_status: dict[str, int] = {}
    for f in files:
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1
    return {
        "kind": "git",
        "anchor": {
            "base": base,
            "head": "HEAD",
            "staged": staged,
            "label": "staged changes" if staged else f"{base} → HEAD",
        },
        "summary": {
            "found": bool(files),
            "count": sym_count,
            "files": len(files),
            "by_status": by_status,
            "modified_symbols": sym_count,
        },
        "files": files,
        "truncated": False,
        "warnings": [],
    }


def _file_entry(path, status="modified", symbols=None, note=None, old_path=None):
    return {
        "path": path,
        "absolute_path": f"/abs/{path}",
        "status": status,
        "old_path": old_path,
        "symbols": symbols or [],
        "note": note,
    }


def _symbol(name, usr, kind="instance-method", line_range=(10, 15)):
    return {
        "name": name, "usr": usr, "kind": kind,
        "module": "Mod", "file": "F.swift", "line": line_range[0],
        "modified_range": list(line_range),
    }


# --- Headline + summary -----------------------------------------------------

def test_headline_includes_label_and_counts():
    canonical = _git_canonical(files=[
        _file_entry("Foo.swift", symbols=[_symbol("bar()", "s:bar")]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "git changes" in text
    assert "origin/main → HEAD" in text
    assert "1 file" in text
    assert "1 modified symbol" in text


def test_headline_shows_staged_label_when_staged():
    canonical = _git_canonical(files=[], staged=True)
    text = render(project(canonical, "locations"), "agent")
    assert "staged changes" in text


def test_summary_lists_by_status():
    canonical = _git_canonical(files=[
        _file_entry("A.swift", status="modified"),
        _file_entry("B.swift", status="added"),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "by_status" in text


def test_empty_changes_renders_no_changes_message():
    canonical = _git_canonical(files=[])
    text = render(project(canonical, "locations"), "agent")
    assert "no indexable file changes" in text


# --- Per-file blocks --------------------------------------------------------

def test_per_file_block_lists_symbols_with_range_and_usr():
    canonical = _git_canonical(files=[
        _file_entry("Foo.swift", symbols=[
            _symbol("bar()", "s:bar", line_range=(42, 58)),
            _symbol("qux()", "s:qux", line_range=(88, 88)),
        ]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "L42-58" in text
    assert "L88" in text and "L88-88" not in text  # single-line collapses
    assert "bar()" in text and "qux()" in text
    assert "s:bar" in text and "s:qux" in text


def test_added_file_emits_warning_note():
    canonical = _git_canonical(files=[
        _file_entry("New.swift", status="added", note="new file — not yet in the IndexStore; rebuild to index"),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "[added]" in text
    assert "new file" in text


def test_renamed_file_shows_old_path():
    canonical = _git_canonical(files=[
        _file_entry("New.swift", status="renamed", old_path="Old.swift"),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "renamed from Old.swift" in text


def test_no_resolved_symbols_shows_explicit_note():
    canonical = _git_canonical(files=[
        _file_entry("F.swift", symbols=[]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "no enclosing symbols resolved" in text


# --- Suggestions block ------------------------------------------------------

def test_next_steps_emits_file_and_impact_commands():
    canonical = _git_canonical(files=[
        _file_entry("Foo.swift", symbols=[
            _symbol("bar()", "s:bar"),
        ]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "**next steps**" in text
    assert "xcindex file 'Foo.swift'" in text or "xcindex file Foo.swift" in text
    assert "xcindex impact s:bar" in text


def test_next_steps_quotes_objc_usr_with_parens():
    canonical = _git_canonical(files=[
        _file_entry("App.swift", symbols=[
            _symbol("init", "c:@M@WWMobile@objc(cs)AppDelegate(im)init"),
        ]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert "'c:@M@WWMobile@objc(cs)AppDelegate(im)init'" in text


def test_next_steps_dedupes_repeated_usrs_across_files():
    canonical = _git_canonical(files=[
        _file_entry("A.swift", symbols=[_symbol("foo()", "s:foo")]),
        _file_entry("B.swift", symbols=[_symbol("foo()", "s:foo")]),
    ])
    text = render(project(canonical, "locations"), "agent")
    assert text.count("xcindex impact s:foo") == 1


def test_next_steps_omitted_when_no_symbols():
    canonical = _git_canonical(files=[
        _file_entry("Empty.swift", symbols=[]),
    ])
    text = render(project(canonical, "locations"), "agent")
    # File suggestion still emitted (path is known) — but no impact section.
    assert "blast radius" not in text


# --- JSON shape -------------------------------------------------------------

def test_json_shape_is_stable_at_locations_level():
    canonical = _git_canonical(files=[
        _file_entry("Foo.swift", symbols=[_symbol("bar()", "s:bar")]),
    ])
    text = render(project(canonical, "locations"), "json")
    parsed = json.loads(text)
    assert parsed["kind"] == "git"
    assert parsed["anchor"]["base"] == "origin/main"
    assert isinstance(parsed["files"], list)
    assert parsed["files"][0]["symbols"][0]["usr"] == "s:bar"


def test_json_summary_level_omits_files():
    canonical = _git_canonical(files=[
        _file_entry("Foo.swift", symbols=[_symbol("bar()", "s:bar")]),
    ])
    projected = project(canonical, "summary")
    assert "files" not in projected

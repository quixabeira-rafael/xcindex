from __future__ import annotations

import pytest

from xcindex.output import project


def _canonical():
    return {
        "kind": "occurrences",
        "anchor": {"name": "compute(_:)", "usr": "s:abc"},
        "summary": {
            "found": True,
            "count": 3,
            "files": 2,
            "by_role": {"call": 2, "definition": 1},
        },
        "items": [
            {
                "name": "compute(_:)",
                "file": "Core/Foo.swift",
                "line": 12,
                "column": 5,
                "container": "Foo.calculate(_:)",
                "kind": "instance-method",
                "module": "Core",
                "roles": ["definition"],
                "depth": 0,
                "usr": "s:abc",
            },
            {
                "name": "compute(_:)",
                "file": "Domain/Bar.swift",
                "line": 88,
                "column": 22,
                "container": "Bar.run()",
                "kind": "instance-method",
                "module": "Domain",
                "roles": ["call", "reference"],
                "depth": 1,
            },
            {
                "name": "compute(_:)",
                "file": "UI/View.swift",
                "line": 5,
                "container": "View.body",
                "kind": "instance-method",
                "module": "UI",
                "roles": ["call"],
                "depth": 2,
            },
        ],
        "warnings": [],
        "truncated": False,
    }


def test_count_level_drops_items_and_keeps_only_scalar_summary():
    out = project(_canonical(), "count")
    assert "items" not in out
    assert out["summary"] == {"found": True, "count": 3, "files": 2}
    # by_role is a dict so it gets dropped at L0
    assert "by_role" not in out["summary"]


def test_summary_level_keeps_summary_dict_drops_items():
    out = project(_canonical(), "summary")
    assert "items" not in out
    assert out["summary"]["by_role"] == {"call": 2, "definition": 1}


def test_locations_level_keeps_only_location_fields_in_items():
    out = project(_canonical(), "locations")
    assert len(out["items"]) == 3
    item = out["items"][0]
    allowed = {"name", "kind", "module", "file", "line", "column",
               "container", "container_kind", "depth", "roles", "rel_kind"}
    assert set(item.keys()).issubset(allowed)
    assert "usr" not in item
    assert "language" not in item


def test_detailed_level_includes_extra_fields():
    out = project(_canonical(), "detailed")
    item = out["items"][0]
    assert item["kind"] == "instance-method"
    assert item["module"] == "Core"
    assert item["depth"] == 0
    assert item["usr"] == "s:abc"


def test_detailed_includes_raw_when_present():
    canonical = _canonical()
    canonical["raw"] = {"foo": "bar"}
    out = project(canonical, "detailed")
    assert out["raw"] == {"foo": "bar"}


def test_each_level_is_strict_superset_of_previous():
    canonical = _canonical()
    levels = ["count", "summary", "locations", "detailed"]
    projections = [project(canonical, level) for level in levels]
    # summary keys present at L1 should be a superset of those at L0
    assert set(projections[0]["summary"]).issubset(set(projections[1]["summary"]))
    # items absent at L0/L1, present at L2 onward, and L3 items are supersets of L2
    assert "items" not in projections[0] and "items" not in projections[1]
    for l2_item, l3_item in zip(projections[2]["items"], projections[3]["items"]):
        assert set(l2_item.keys()).issubset(set(l3_item.keys()))


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        project(_canonical(), "verbose")


def test_strip_empty_drops_empty_warnings_and_items():
    canonical = _canonical()
    canonical["warnings"] = []
    out = project(canonical, "summary")
    assert "warnings" not in out

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from xcindex import discovery


def test_finds_xcodeproj_in_cwd(tmp_repo: Path, make_xcodeproj):
    proj = make_xcodeproj("Sample")
    info = discovery.find_project(tmp_repo)
    assert info.kind == "xcodeproj"
    assert info.path == proj
    assert info.name == "Sample"
    assert info.root == tmp_repo


def test_walks_up_to_find_xcodeproj(tmp_repo: Path, make_xcodeproj):
    make_xcodeproj("Sample")
    nested = tmp_repo / "deep" / "nested" / "dir"
    nested.mkdir(parents=True)
    info = discovery.find_project(nested)
    assert info.kind == "xcodeproj"


def test_prefers_workspace_over_project(tmp_repo: Path, make_xcodeproj):
    make_xcodeproj("Sample")
    workspace = tmp_repo / "Sample.xcworkspace"
    workspace.mkdir()
    info = discovery.find_project(tmp_repo)
    assert info.kind == "xcworkspace"


def test_swiftpm_package(tmp_repo: Path):
    (tmp_repo / "Package.swift").write_text("// swift-tools-version:5.9")
    info = discovery.find_project(tmp_repo)
    assert info.kind == "swiftpm"
    assert info.name == tmp_repo.name


def test_stops_at_git_boundary(tmp_path: Path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").mkdir()
    with pytest.raises(discovery.DiscoveryError):
        discovery.find_project(inner)


def test_find_index_store_swiftpm(tmp_repo: Path):
    (tmp_repo / "Package.swift").write_text("// stub")
    units = tmp_repo / ".build" / "debug" / "index" / "store" / "v5" / "units"
    units.mkdir(parents=True)
    (units / "unit-1").write_bytes(b"")
    info = discovery.find_project(tmp_repo)
    store = discovery.find_index_store(info)
    assert store == tmp_repo / ".build" / "debug" / "index" / "store"


def test_find_index_store_via_override(tmp_repo: Path, make_xcodeproj, tmp_path: Path):
    make_xcodeproj("Sample")
    info = discovery.find_project(tmp_repo)
    store = tmp_path / "explicit_store"
    (store / "v5" / "units").mkdir(parents=True)
    result = discovery.find_index_store(info, index_store_override=store)
    assert result == store


def test_find_index_store_missing_units_dir(tmp_repo: Path, make_xcodeproj, tmp_path: Path):
    make_xcodeproj("Sample")
    info = discovery.find_project(tmp_repo)
    bad_store = tmp_path / "empty"
    bad_store.mkdir()
    with pytest.raises(discovery.DiscoveryError):
        discovery.find_index_store(info, index_store_override=bad_store)


def test_find_index_store_xcode_via_derived_data(tmp_repo: Path, make_xcodeproj, tmp_path: Path):
    proj = make_xcodeproj("MyApp")
    derived = tmp_path / "DerivedData"
    entry = derived / "MyApp-abc123"
    units = entry / "Index.noindex" / "DataStore" / "v5" / "units"
    units.mkdir(parents=True)
    (units / "unit-1").write_bytes(b"")
    info = discovery.find_project(tmp_repo)
    store = discovery.find_index_store(info, derived_data_override=derived)
    assert store == entry / "Index.noindex" / "DataStore"


def test_find_index_store_picks_matching_workspace(tmp_repo: Path, make_xcodeproj, tmp_path: Path):
    proj = make_xcodeproj("MyApp")
    derived = tmp_path / "DerivedData"
    correct_entry = derived / "MyApp-aaa"
    wrong_entry = derived / "MyApp-bbb"
    for e in (correct_entry, wrong_entry):
        units = e / "Index.noindex" / "DataStore" / "v5" / "units"
        units.mkdir(parents=True)
        (units / "u").write_bytes(b"")
    plistlib.dump(
        {"WorkspacePath": str(proj)},
        (correct_entry / "info.plist").open("wb"),
    )
    plistlib.dump(
        {"WorkspacePath": "/some/other/Path.xcodeproj"},
        (wrong_entry / "info.plist").open("wb"),
    )
    info = discovery.find_project(tmp_repo)
    store = discovery.find_index_store(info, derived_data_override=derived)
    assert correct_entry in store.parents


def test_env_var_overrides_index_store(monkeypatch, tmp_repo: Path, make_xcodeproj, tmp_path: Path):
    make_xcodeproj("Sample")
    info = discovery.find_project(tmp_repo)
    store = tmp_path / "env_store"
    (store / "v5" / "units").mkdir(parents=True)
    monkeypatch.setenv(discovery.ENV_INDEX_STORE, str(store))
    result = discovery.find_index_store(info)
    assert result == store

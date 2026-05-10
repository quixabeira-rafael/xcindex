"""Unit tests for engine.materialize() — the public dispatch."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from xcindex import cache as cache_module
from xcindex import discovery
from xcindex import engine
from xcindex import helper as helper_module


def _args(**overrides) -> argparse.Namespace:
    base = {
        "project": None,
        "index_store": None,
        "derived_data": None,
        "include_system": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def fixture_project_info(tmp_path: Path) -> discovery.ProjectInfo:
    """Build a minimal SwiftPM-style project info pointing at tmp_path."""
    pkg = tmp_path / "Package.swift"
    pkg.write_text("// stub\n")
    return discovery.ProjectInfo(
        path=pkg,
        name="StubApp",
        kind="swiftpm",
        root=tmp_path,
    )


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch) -> Path:
    cache_dir = tmp_path / "xcache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    return cache_dir


def _stub_helper_version() -> helper_module.HelperVersion:
    return helper_module.HelperVersion(
        helper_version="test-0",
        schema_version=3,
        swift_version="test-swift",
        binary_path=Path("/fake/helper"),
    )


def _stub_helper_run_result(symbols=10, occurrences=20, relations=30, wall=0.05) -> helper_module.HelperRunResult:
    return helper_module.HelperRunResult(
        wall_seconds=wall,
        symbols=symbols,
        occurrences=occurrences,
        relations=relations,
    )


def _patch_engine_for_materialize(
    monkeypatch,
    project_info,
    index_store_path: Path,
    *,
    bootstrap_result=None,
    incremental_result=None,
    delta=None,
    schema_outdated=False,
):
    monkeypatch.setattr(engine, "resolve_project", lambda args: project_info)
    monkeypatch.setattr(engine, "resolve_index_store", lambda args, project: index_store_path)
    monkeypatch.setattr(helper_module, "ensure_helper", lambda allow_build=True: Path("/fake/helper"))
    monkeypatch.setattr(helper_module, "get_version", lambda binary: _stub_helper_version())
    monkeypatch.setattr(cache_module, "compute_index_hash",
                        lambda store, **kw: "deadbeef")
    monkeypatch.setattr(cache_module, "migrate_v1_caches", lambda root: 0)
    monkeypatch.setattr(cache_module, "gc_caches", lambda root: None)
    monkeypatch.setattr(cache_module, "write_meta", lambda root, **kw: None)
    monkeypatch.setattr(engine, "_schema_outdated", lambda path: schema_outdated)

    def _bootstrap(**kw):
        # Touch the sqlite path to simulate the helper writing.
        kw["sqlite_path"].parent.mkdir(parents=True, exist_ok=True)
        kw["sqlite_path"].write_bytes(b"sqlite-stub")
        return bootstrap_result or _stub_helper_run_result()

    def _incremental(**kw):
        return incremental_result or _stub_helper_run_result(symbols=2, occurrences=5, relations=3)

    monkeypatch.setattr(engine, "_materialize", lambda **kw: _bootstrap(**kw))
    monkeypatch.setattr(helper_module, "run_incremental", lambda **kw: _incremental(**kw))

    if delta is not None:
        from xcindex import incremental as incremental_module
        monkeypatch.setattr(incremental_module, "compute_unit_delta",
                            lambda sqlite, store: delta)


# --- Smoke tests on the dataclass ------------------------------------------

def test_materialization_result_carries_all_stats():
    project = discovery.ProjectInfo(
        path=Path("/p"), name="P", kind="swiftpm", root=Path("/")
    )
    result = engine.MaterializationResult(
        mode="cold", project=project, index_store=Path("/idx"),
        sqlite_path=Path("/cache.sqlite"), index_hash="abc",
        wall_seconds=1.5, symbols_added=10, occurrences_added=20,
        relations_added=30,
    )
    assert result.mode == "cold"
    assert result.symbols_added == 10
    assert result.units_modified == 0  # default


# --- materialize() dispatch -------------------------------------------------

def test_materialize_cold_when_cache_missing(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()
    _patch_engine_for_materialize(monkeypatch, fixture_project_info, index_store)

    result = engine.materialize(_args())
    assert result.mode == "cold"
    assert result.symbols_added == 10
    assert result.occurrences_added == 20
    assert result.relations_added == 30
    assert result.index_hash == "deadbeef"
    assert result.sqlite_path.exists()


def test_materialize_returns_noop_when_delta_empty(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()

    class _EmptyDelta:
        modified: set[str] = set()
        removed: set[str] = set()
        added: set[str] = set()
        is_empty = True
        needs_full_redump = False

    _patch_engine_for_materialize(
        monkeypatch, fixture_project_info, index_store,
        delta=_EmptyDelta(),
    )

    # First call: cold dump (creates the sqlite stub)
    first = engine.materialize(_args())
    assert first.mode == "cold"

    # Second call: delta empty → noop
    second = engine.materialize(_args())
    assert second.mode == "noop"
    assert second.symbols_added == 0


def test_materialize_incremental_when_units_modified(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()

    class _Delta:
        modified = {"u1.unit", "u2.unit"}
        removed: set[str] = set()
        added: set[str] = set()
        is_empty = False
        needs_full_redump = False

    _patch_engine_for_materialize(
        monkeypatch, fixture_project_info, index_store,
        delta=_Delta(),
    )

    # First: cold
    engine.materialize(_args())

    # Second with modified units → incremental
    second = engine.materialize(_args())
    assert second.mode == "incremental"
    assert second.units_modified == 2
    assert second.units_removed == 0
    assert second.symbols_added == 2
    assert second.occurrences_added == 5
    assert second.relations_added == 3


def test_materialize_cold_when_units_added(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    """Tuist regen / new source files force a full re-dump."""
    index_store = tmp_path / "store"
    index_store.mkdir()

    class _Delta:
        modified: set[str] = set()
        removed: set[str] = set()
        added = {"new1.unit", "new2.unit"}
        is_empty = False
        needs_full_redump = True

    _patch_engine_for_materialize(
        monkeypatch, fixture_project_info, index_store,
        delta=_Delta(),
    )

    engine.materialize(_args())          # initial cold
    second = engine.materialize(_args()) # added units → cold again
    assert second.mode == "cold"
    assert second.units_added == 2


def test_materialize_schema_upgrade_when_outdated(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()
    sqlite_path = cache_module.canonical_sqlite_path(fixture_project_info.path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_bytes(b"old-stub")

    _patch_engine_for_materialize(
        monkeypatch, fixture_project_info, index_store,
        schema_outdated=True,
    )

    result = engine.materialize(_args())
    assert result.mode == "schema_upgrade"
    assert result.symbols_added == 10  # bootstrap was re-run


def test_materialize_raises_on_no_project(monkeypatch, isolated_cache):
    def _fail(args):
        raise discovery.DiscoveryError("no project")
    monkeypatch.setattr(engine, "resolve_project", _fail)
    with pytest.raises(engine.EngineError) as exc_info:
        engine.materialize(_args())
    assert "could not discover project" in str(exc_info.value)


def test_materialize_raises_on_no_index_store(monkeypatch, fixture_project_info, isolated_cache):
    monkeypatch.setattr(engine, "resolve_project", lambda args: fixture_project_info)
    def _fail(args, project):
        raise discovery.DiscoveryError("no index store")
    monkeypatch.setattr(engine, "resolve_index_store", _fail)
    with pytest.raises(engine.EngineError) as exc_info:
        engine.materialize(_args())
    assert "could not discover index store" in str(exc_info.value)


def test_materialize_propagates_include_system_flag(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()

    captured = {}
    def _capture_bootstrap(**kw):
        captured["include_system"] = kw["include_system"]
        kw["sqlite_path"].parent.mkdir(parents=True, exist_ok=True)
        kw["sqlite_path"].write_bytes(b"stub")
        return _stub_helper_run_result()

    monkeypatch.setattr(engine, "resolve_project", lambda args: fixture_project_info)
    monkeypatch.setattr(engine, "resolve_index_store", lambda args, project: index_store)
    monkeypatch.setattr(helper_module, "ensure_helper", lambda allow_build=True: Path("/fake"))
    monkeypatch.setattr(helper_module, "get_version", lambda binary: _stub_helper_version())
    monkeypatch.setattr(cache_module, "compute_index_hash", lambda store, **kw: "h")
    monkeypatch.setattr(cache_module, "migrate_v1_caches", lambda root: 0)
    monkeypatch.setattr(cache_module, "gc_caches", lambda root: None)
    monkeypatch.setattr(cache_module, "write_meta", lambda root, **kw: None)
    monkeypatch.setattr(engine, "_schema_outdated", lambda path: False)
    monkeypatch.setattr(engine, "_materialize", _capture_bootstrap)

    engine.materialize(_args(include_system=True))
    assert captured["include_system"] is True


def test_materialize_records_wall_seconds(monkeypatch, fixture_project_info, isolated_cache, tmp_path):
    index_store = tmp_path / "store"
    index_store.mkdir()
    _patch_engine_for_materialize(monkeypatch, fixture_project_info, index_store)
    result = engine.materialize(_args())
    assert result.wall_seconds >= 0.0
    assert result.wall_seconds < 5.0  # mocked path should be near-instant


# --- add_project_arguments freshness flags ----------------------------------

def test_add_project_arguments_default_includes_freshness_flags():
    parser = argparse.ArgumentParser()
    engine.add_project_arguments(parser)
    args = parser.parse_args(["--check-fresh"])
    assert args.check_fresh is True


def test_add_project_arguments_can_omit_freshness_flags():
    parser = argparse.ArgumentParser()
    engine.add_project_arguments(parser, include_freshness_flags=False)
    with pytest.raises(SystemExit):
        parser.parse_args(["--check-fresh"])

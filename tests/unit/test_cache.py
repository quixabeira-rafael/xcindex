from __future__ import annotations

from pathlib import Path

from xcindex import cache as cache_module


def test_fingerprint_stable_across_calls(tmp_path: Path):
    project = tmp_path / "MyApp.xcodeproj"
    project.mkdir()
    a = cache_module.project_fingerprint(project)
    b = cache_module.project_fingerprint(project)
    assert a == b
    assert len(a) == 16


def test_fingerprint_differs_for_different_paths(tmp_path: Path):
    a = tmp_path / "A.xcodeproj"
    b = tmp_path / "B.xcodeproj"
    a.mkdir()
    b.mkdir()
    assert cache_module.project_fingerprint(a) != cache_module.project_fingerprint(b)


def test_project_cache_dir_under_root(tmp_path: Path):
    project = tmp_path / "X.xcodeproj"
    project.mkdir()
    directory = cache_module.project_cache_dir(project)
    assert directory.parent == cache_module.cache_root()
    assert directory.name == cache_module.project_fingerprint(project)


def test_sqlite_path_for_returns_named_file(tmp_path: Path):
    project = tmp_path / "X.xcodeproj"
    project.mkdir()
    sqlite = cache_module.sqlite_path_for(project, "deadbeef")
    assert sqlite.name == "deadbeef.sqlite"
    assert sqlite.parent == cache_module.project_cache_dir(project)


def test_list_caches_empty_returns_empty_list(tmp_path: Path):
    project = tmp_path / "X.xcodeproj"
    project.mkdir()
    assert cache_module.list_caches(project) == []


def test_list_caches_finds_sqlite_files(tmp_path: Path):
    project = tmp_path / "MyApp.xcodeproj"
    project.mkdir()
    directory = cache_module.ensure_cache_dir(project)
    (directory / "abc.sqlite").write_bytes(b"x" * 100)
    (directory / "def.sqlite").write_bytes(b"y" * 200)
    cache_module.write_meta(project, latest_hash="abc")
    entries = cache_module.list_caches(project)
    hashes = sorted(e.index_hash for e in entries)
    assert hashes == ["abc", "def"]
    assert all(e.size_bytes > 0 for e in entries)


def test_clear_caches_for_project(tmp_path: Path):
    project = tmp_path / "MyApp.xcodeproj"
    project.mkdir()
    directory = cache_module.ensure_cache_dir(project)
    (directory / "abc.sqlite").write_bytes(b"x")
    (directory / "def.sqlite").write_bytes(b"x")
    removed = cache_module.clear_caches(project)
    assert removed == 2
    assert not directory.exists()


def test_clear_caches_all_projects(tmp_path: Path):
    project_a = tmp_path / "A.xcodeproj"
    project_b = tmp_path / "B.xcodeproj"
    for p in (project_a, project_b):
        p.mkdir()
        directory = cache_module.ensure_cache_dir(p)
        (directory / "h.sqlite").write_bytes(b"x")
    removed = cache_module.clear_caches(all_projects=True)
    assert removed == 2
    assert not cache_module.cache_root().exists()


def test_list_caches_across_all_projects(tmp_path: Path):
    project_a = tmp_path / "A.xcodeproj"
    project_b = tmp_path / "B.xcodeproj"
    for p in (project_a, project_b):
        p.mkdir()
        directory = cache_module.ensure_cache_dir(p)
        (directory / "h.sqlite").write_bytes(b"x")
    entries = cache_module.list_caches()
    fingerprints = {e.project_fingerprint for e in entries}
    assert fingerprints == {
        cache_module.project_fingerprint(project_a),
        cache_module.project_fingerprint(project_b),
    }

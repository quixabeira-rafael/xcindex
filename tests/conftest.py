from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Simulate a project root with a `.git` boundary marker."""
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def make_xcodeproj(tmp_repo: Path):
    """Factory: create an empty .xcodeproj inside tmp_repo and return ProjectInfo path."""
    def _make(name: str = "Sample") -> Path:
        proj = tmp_repo / f"{name}.xcodeproj"
        proj.mkdir()
        (proj / "project.pbxproj").write_text("// stub")
        return proj
    return _make


@pytest.fixture
def make_index_store(tmp_path: Path):
    """Factory: create a directory mimicking Index.noindex/DataStore layout."""
    def _make(unit_filenames: list[str] | None = None) -> Path:
        store = tmp_path / "DataStore"
        units = store / "v5" / "units"
        units.mkdir(parents=True)
        for name in unit_filenames or []:
            (units / name).write_bytes(b"\x00")
        return store
    return _make


@pytest.fixture
def in_memory_db():
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path, monkeypatch):
    """Redirect HOME and XDG_CACHE_HOME to a tmp dir to avoid touching real user state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(fake_home / ".cache"))
    import xcindex.cache as cache_module
    monkeypatch.setattr(cache_module, "CACHE_ROOT", fake_home / ".cache" / "xcindex")
    yield

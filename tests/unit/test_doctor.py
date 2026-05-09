from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from xcindex import doctor


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_python_version_ok():
    result = doctor.check_python_version()
    if sys.version_info >= (3, 11):
        assert result.status == doctor.STATUS_OK
    else:
        assert result.status == doctor.STATUS_ERROR


def test_macos_version_non_darwin():
    with patch("platform.system", return_value="Linux"):
        result = doctor.check_macos_version()
    assert result.status == doctor.STATUS_ERROR


def test_macos_version_ok():
    with patch("platform.system", return_value="Darwin"), \
         patch.object(doctor, "_run", return_value=_proc(0, "14.5\n")):
        result = doctor.check_macos_version()
    assert result.status == doctor.STATUS_OK
    assert "14.5" in result.detail


def test_xcrun_missing():
    with patch("shutil.which", return_value=None):
        result = doctor.check_xcrun()
    assert result.status == doctor.STATUS_ERROR
    assert "xcode-select" in (result.fix or "")


def test_xcrun_present():
    with patch("shutil.which", return_value="/usr/bin/xcrun"), \
         patch.object(doctor, "_run", return_value=_proc(0, "/Applications/Xcode.app/Contents/Developer\n")):
        result = doctor.check_xcrun()
    assert result.status == doctor.STATUS_OK
    assert "Xcode.app" in result.detail


def test_swift_toolchain_missing():
    with patch.object(doctor, "_run", return_value=_proc(1, "", "swift: not found")):
        result = doctor.check_swift_toolchain()
    assert result.status == doctor.STATUS_ERROR


def test_swift_toolchain_ok():
    def fake_run(cmd):
        if cmd[:2] == ["xcrun", "--find"]:
            return _proc(0, "/usr/bin/swift\n")
        if cmd[:1] == ["swift"]:
            return _proc(0, "swift-driver version: 1.90 Apple Swift version 5.10\n")
        return _proc(1)
    with patch.object(doctor, "_run", side_effect=fake_run):
        result = doctor.check_swift_toolchain()
    assert result.status == doctor.STATUS_OK
    assert "Swift version" in result.detail or "swift" in result.detail.lower()


def test_check_project_when_no_project(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = doctor.check_project(tmp_path)
    assert result.status == doctor.STATUS_INFO


def test_check_project_finds_project(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "MyApp.xcodeproj").mkdir()
    result = doctor.check_project(tmp_path)
    assert result.status == doctor.STATUS_OK
    assert "MyApp" in result.detail


def test_check_index_store_skipped_when_no_project(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    result = doctor.check_index_store(tmp_path)
    assert result.status == doctor.STATUS_INFO


def test_check_index_store_ok(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "MyApp.xcodeproj").mkdir()
    store = tmp_path / "store"
    units = store / "v5" / "units"
    units.mkdir(parents=True)
    (units / "u1").write_bytes(b"")
    result = doctor.check_index_store(tmp_path, index_store_override=store)
    assert result.status in (doctor.STATUS_OK, doctor.STATUS_WARN)
    assert "1 unit" in result.detail


def test_check_index_store_empty_warns(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "MyApp.xcodeproj").mkdir()
    store = tmp_path / "store"
    (store / "v5" / "units").mkdir(parents=True)
    result = doctor.check_index_store(tmp_path, index_store_override=store)
    assert result.status == doctor.STATUS_WARN


def test_check_cache_dir_writable(tmp_path: Path, monkeypatch):
    import xcindex.cache as cache_module
    target = tmp_path / "xcache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", target)
    result = doctor.check_cache_dir()
    assert result.status == doctor.STATUS_OK
    assert target.exists()


def test_overall_status_picks_worst_severity():
    results = [
        doctor.CheckResult(name="a", status=doctor.STATUS_OK, detail=""),
        doctor.CheckResult(name="b", status=doctor.STATUS_WARN, detail=""),
    ]
    assert doctor.overall_status(results) == doctor.STATUS_WARN
    results.append(doctor.CheckResult(name="c", status=doctor.STATUS_ERROR, detail=""))
    assert doctor.overall_status(results) == doctor.STATUS_ERROR


def test_run_all_checks_returns_list(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    results = doctor.run_all_checks(tmp_path)
    assert len(results) >= 6
    names = {r.name for r in results}
    assert {"macOS", "Python", "xcrun", "swift", "cache", "project", "index-store"}.issubset(names)


# --- check_git_repo ---------------------------------------------------------

def test_check_git_repo_no_cli_returns_info():
    with patch("shutil.which", return_value=None):
        result = doctor.check_git_repo()
    assert result.status == doctor.STATUS_INFO
    assert "git CLI not on PATH" in result.detail


def test_check_git_repo_outside_worktree_returns_info(tmp_path: Path):
    result = doctor.check_git_repo(tmp_path)
    assert result.status == doctor.STATUS_INFO
    assert "not inside a git working tree" in result.detail


def test_check_git_repo_inside_worktree_returns_ok(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = doctor.check_git_repo(tmp_path)
    assert result.status == doctor.STATUS_OK
    assert "branch=main" in result.detail
    assert "base=" in result.detail


def test_run_all_checks_includes_git_check(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    results = doctor.run_all_checks(tmp_path)
    names = {r.name for r in results}
    assert "git" in names

"""End-to-end integration tests for `xcindex git` against a fresh fixture repo."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from xcindex import cache as cache_module

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "SampleApp"
HELPER_BINARY = REPO_ROOT / "swift-helper" / ".build" / "release" / "xcindex-helper"


@pytest.fixture(scope="module")
def built_helper() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    if not HELPER_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(REPO_ROOT / "swift-helper"),
            check=True, timeout=600,
        )
    return HELPER_BINARY


@pytest.fixture
def git_fixture_project(tmp_path):
    """Copy SampleApp into a fresh git repo, build the index, and make one
    branch commit that edits a method body."""
    project = tmp_path / "SampleApp"
    shutil.copytree(FIXTURE_ROOT, project)
    # Strip pre-existing build artifacts so swift build is deterministic.
    build_dir = project / ".build"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    subprocess.run(["git", "checkout", "-b", "feature"], cwd=project, check=True, capture_output=True)
    target_file = project / "Sources" / "Core" / "PriceCalculator.swift"
    text = target_file.read_text()
    # Edit `compute()` body — line 13 in original. Keep line layout to preserve indexed positions.
    edited = text.replace("return 100.0", "return 200.0")
    target_file.write_text(edited)
    subprocess.run(["git", "add", "Sources/Core/PriceCalculator.swift"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "tweak compute"], cwd=project, check=True, capture_output=True)

    subprocess.run(["swift", "build"], cwd=project, check=True, timeout=300)
    return project


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "xcindex-cache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    yield cache_dir


def _xcindex(args, cwd, env_overrides=None, fmt="json"):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    extra = ["--format", fmt] if fmt else []
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
            *args, *extra,
        ],
        cwd=str(cwd), capture_output=True, text=True, env=env,
    )


def _env(helper):
    return {"XCINDEX_HELPER": str(helper)}


def test_git_resolves_modified_method_to_enclosing_symbol(
    git_fixture_project, built_helper, isolated_cache,
):
    proc = _xcindex(["git", "main"], git_fixture_project, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["kind"] == "git"
    assert payload["summary"]["files"] == 1
    file_entry = payload["files"][0]
    assert file_entry["path"] == "Sources/Core/PriceCalculator.swift"
    sym_names = {s["name"] for s in file_entry["symbols"]}
    assert "compute()" in sym_names


def test_git_emits_impact_command_for_modified_symbol(
    git_fixture_project, built_helper, isolated_cache,
):
    proc = _xcindex(["git", "main"], git_fixture_project, _env(built_helper), fmt=None)
    assert proc.returncode == 0, proc.stderr
    assert "xcindex impact" in proc.stdout
    assert "compute" in proc.stdout or "PriceCalculator" in proc.stdout


def test_git_detects_default_base_when_omitted(
    git_fixture_project, built_helper, isolated_cache,
):
    proc = _xcindex(["git"], git_fixture_project, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    # Default base falls back to `main` since there's no origin/main in fixture.
    assert payload["anchor"]["base"] == "main"


def test_git_unknown_ref_returns_invalid_state(
    git_fixture_project, built_helper, isolated_cache,
):
    proc = _xcindex(["git", "totally-not-a-ref"],
                    git_fixture_project, _env(built_helper), fmt=None)
    assert proc.returncode != 0
    assert "git_ref_not_found" in proc.stderr or "not found" in proc.stderr


def test_git_added_file_emits_rebuild_warning(
    git_fixture_project, built_helper, isolated_cache,
):
    new_file = git_fixture_project / "Sources" / "Core" / "Newcomer.swift"
    new_file.write_text("public class Newcomer {}\n")
    subprocess.run(["git", "add", "Sources/Core/Newcomer.swift"],
                   cwd=git_fixture_project, check=True)
    subprocess.run(["git", "commit", "-m", "add file"],
                   cwd=git_fixture_project, check=True, capture_output=True)
    proc = _xcindex(["git", "main"], git_fixture_project, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    new_entries = [f for f in payload["files"] if f["path"].endswith("Newcomer.swift")]
    assert new_entries, "expected the new file to appear in changes"
    assert new_entries[0]["status"] == "added"
    assert new_entries[0].get("note")


def test_git_skips_non_indexable_files(
    git_fixture_project, built_helper, isolated_cache,
):
    txt = git_fixture_project / "README.txt"
    txt.write_text("hello\n")
    subprocess.run(["git", "add", "README.txt"], cwd=git_fixture_project, check=True)
    subprocess.run(["git", "commit", "-m", "doc"], cwd=git_fixture_project, check=True, capture_output=True)
    proc = _xcindex(["git", "main"], git_fixture_project, _env(built_helper))
    payload = json.loads(proc.stdout)
    paths = {f["path"] for f in payload["files"]}
    assert "README.txt" not in paths


def test_git_canonical_json_shape_is_stable(
    git_fixture_project, built_helper, isolated_cache,
):
    proc = _xcindex(["git", "main"], git_fixture_project, _env(built_helper))
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) >= {"kind", "anchor", "summary", "files"}
    assert "base" in payload["anchor"]
    assert "by_status" in payload["summary"]

"""Unit tests for git_diff helpers."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from xcindex import git_diff as git_diff_module


# --- pure parsers / classifiers --------------------------------------------

def test_is_indexable_recognizes_swift_and_objc():
    assert git_diff_module.is_indexable("Sources/Foo.swift")
    assert git_diff_module.is_indexable("ios/Bar.m")
    assert git_diff_module.is_indexable("ios/Baz.mm")
    assert git_diff_module.is_indexable("ios/Qux.h")


def test_is_indexable_skips_non_indexable_files():
    assert not git_diff_module.is_indexable("Podfile")
    assert not git_diff_module.is_indexable("Gemfile")
    assert not git_diff_module.is_indexable("README.md")
    assert not git_diff_module.is_indexable("ci.yml")


def test_short_describe_formats_diff_label(tmp_path):
    label = git_diff_module.short_describe("origin/main", tmp_path)
    assert "origin/main" in label
    assert "HEAD" in label


# --- git CLI integration with a real fixture repo --------------------------

@pytest.fixture
def fixture_repo(tmp_path):
    """Build a self-contained git repo with one commit on `main` and an extra
    commit on a branch that adds, modifies, and renames Swift files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "Foo.swift").write_text(
        "// header\n"
        "class Foo {\n"
        "    func bar() -> Int { return 1 }\n"
        "    func qux() -> Int { return 2 }\n"
        "}\n"
    )
    (repo / "Static.txt").write_text("not a swift file\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
    # Modify Foo.swift: change bar() body
    (repo / "Foo.swift").write_text(
        "// header\n"
        "class Foo {\n"
        "    func bar() -> Int { return 42 }\n"
        "    func qux() -> Int { return 2 }\n"
        "}\n"
    )
    # Add new file
    (repo / "Baz.swift").write_text("class Baz {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature work"], cwd=repo, check=True, capture_output=True)
    return repo


def test_is_git_repo_true_inside_worktree(fixture_repo):
    assert git_diff_module.is_git_repo(fixture_repo) is True


def test_is_git_repo_false_outside(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert git_diff_module.is_git_repo(bare) is False


def test_ref_exists_true_for_main(fixture_repo):
    assert git_diff_module.ref_exists("main", fixture_repo) is True


def test_ref_exists_false_for_unknown(fixture_repo):
    assert git_diff_module.ref_exists("does-not-exist", fixture_repo) is False


def test_detect_default_base_picks_main_when_no_origin(fixture_repo):
    base = git_diff_module.detect_default_base(fixture_repo)
    assert base == "main"


def test_list_changed_files_classifies_modified_and_added(fixture_repo):
    changed = git_diff_module.list_changed_files("main", fixture_repo)
    by_path = {cf.path: cf for cf in changed}
    assert "Foo.swift" in by_path
    assert by_path["Foo.swift"].status == "M"
    assert "Baz.swift" in by_path
    assert by_path["Baz.swift"].status == "A"


def test_list_modified_line_ranges_returns_new_side_lines(fixture_repo):
    ranges = git_diff_module.list_modified_line_ranges(
        "main", fixture_repo, "Foo.swift",
    )
    assert ranges, "expected at least one hunk"
    # Modified line is bar() body on line 3 (post-change file is identical layout)
    flat = [n for start, end in ranges for n in range(start, end + 1)]
    assert 3 in flat


def test_list_modified_line_ranges_skips_pure_deletion_hunks(tmp_path):
    repo = tmp_path / "deletion-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "F.swift").write_text("a\nb\nc\nd\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feat"], cwd=repo, check=True, capture_output=True)
    (repo / "F.swift").write_text("a\nd\n")  # delete b and c
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "rm"], cwd=repo, check=True, capture_output=True)
    ranges = git_diff_module.list_modified_line_ranges("main", repo, "F.swift")
    # Pure deletion (NEW count = 0) is skipped → empty list expected
    assert ranges == []


def test_list_changed_files_handles_rename(tmp_path):
    repo = tmp_path / "rename-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "Old.swift").write_text("class Old {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feat"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "mv", "Old.swift", "New.swift"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "rename"], cwd=repo, check=True, capture_output=True)
    changed = git_diff_module.list_changed_files("main", repo)
    by_status = {cf.status: cf for cf in changed}
    # Git emits R (with similarity score) — our wrapper strips to first char
    assert "R" in by_status
    rename_entry = by_status["R"]
    assert rename_entry.path == "New.swift"
    assert rename_entry.old_path == "Old.swift"


def test_git_error_raised_when_not_a_repo(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(git_diff_module.GitError):
        git_diff_module.list_changed_files("main", bare)


def test_hunk_header_regex_handles_single_line_hunk(fixture_repo):
    """The +N (no comma) form means a 1-line hunk; ensure parser handles it."""
    repo = fixture_repo
    (repo / "OneLine.swift").write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add file"], cwd=repo, check=True, capture_output=True)
    (repo / "OneLine.swift").write_text("line1\nlineCHANGED\nline3\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], cwd=repo, check=True, capture_output=True)
    ranges = git_diff_module.list_modified_line_ranges("HEAD~1", repo, "OneLine.swift")
    assert ranges == [(2, 2)]

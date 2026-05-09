"""Helpers around `git diff` used by the `xcindex git` command.

Pure subprocess wrappers — no IndexStore knowledge here. Returns Python
data the command layer can pair with `query_containing` to map line ranges
to symbols.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_INDEXABLE_SUFFIXES = {".swift", ".m", ".mm", ".h", ".hpp", ".c", ".cc", ".cpp"}


class GitError(Exception):
    """Raised when the git CLI is unavailable, the cwd isn't a repo, or a ref is invalid."""


@dataclass(frozen=True)
class ChangedFile:
    """A file affected by a diff range."""
    status: str            # 'M' modified, 'A' added, 'D' deleted, 'R' renamed, 'C' copied
    path: str              # current/new path (post-change)
    old_path: str | None   # source path on rename/copy, else None


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def is_git_repo(cwd: Path) -> bool:
    try:
        _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
        return True
    except GitError:
        return False


def ref_exists(ref: str, cwd: Path) -> bool:
    try:
        _run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
        return True
    except GitError:
        return False


def detect_default_base(cwd: Path) -> str:
    """Pick the most useful base ref to diff against, falling back gracefully.

    Order: `origin/main` → `origin/master` → `main` → `master` → `HEAD~1`.
    """
    for ref in ("origin/main", "origin/master", "main", "master"):
        if ref_exists(ref, cwd):
            return ref
    return "HEAD~1"


def list_changed_files(
    base: str,
    cwd: Path,
    *,
    staged: bool = False,
) -> list[ChangedFile]:
    """Enumerate files changed between `base` and HEAD (or staged)."""
    if staged:
        args = ["diff", "--name-status", "--cached"]
    else:
        args = ["diff", "--name-status", f"{base}...HEAD"]
    output = _run_git(args, cwd)
    files: list[ChangedFile] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status_token = parts[0]
        status = status_token[0]
        if status in ("R", "C") and len(parts) >= 3:
            files.append(ChangedFile(status=status, path=parts[2], old_path=parts[1]))
        elif len(parts) >= 2:
            files.append(ChangedFile(status=status, path=parts[1], old_path=None))
    return files


def list_modified_line_ranges(
    base: str,
    cwd: Path,
    file: str,
    *,
    staged: bool = False,
) -> list[tuple[int, int]]:
    """Return inclusive (start, end) ranges of NEW-side lines touched in `file`.

    Pure deletion hunks (NEW count = 0) are skipped — there's no current line
    to resolve a containing symbol for.
    """
    if staged:
        args = ["diff", "-U0", "--cached", "--", file]
    else:
        args = ["diff", "-U0", f"{base}...HEAD", "--", file]
    output = _run_git(args, cwd)
    ranges: list[tuple[int, int]] = []
    for line in output.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            continue
        new_start = int(match.group(1))
        new_count = int(match.group(2)) if match.group(2) is not None else 1
        if new_count == 0:
            continue
        ranges.append((new_start, new_start + new_count - 1))
    return ranges


def is_indexable(path: str) -> bool:
    """True if the file's suffix matches a language we have IndexStore data for."""
    return Path(path).suffix.lower() in _INDEXABLE_SUFFIXES


def short_describe(base: str, cwd: Path) -> str:
    """Return a human-readable label for the diff range, e.g. 'origin/main → HEAD'."""
    return f"{base} → HEAD"

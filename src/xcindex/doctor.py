from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from xcindex import cache as cache_module
from xcindex import discovery
from xcindex import git_diff as git_diff_module
from xcindex import helper as helper_module

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_INFO = "info"

GROUP_SYSTEM = "system"
GROUP_TOOLCHAIN = "toolchain"
GROUP_PROJECT = "project"
GROUP_CACHE = "cache"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str | None = None
    group: str = GROUP_SYSTEM

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "group": self.group,
        }


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)


# --- Individual checks --------------------------------------------------------

def check_macos_version() -> CheckResult:
    if platform.system() != "Darwin":
        return CheckResult(
            name="macOS",
            status=STATUS_ERROR,
            detail=f"running on {platform.system()}; xcindex requires macOS",
            fix="run on a Mac with Xcode installed",
        )
    try:
        proc = _run(["sw_vers", "-productVersion"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult(
            name="macOS",
            status=STATUS_WARN,
            detail="could not detect macOS version (sw_vers failed)",
        )
    version = proc.stdout.strip() if proc.returncode == 0 else ""
    if not version:
        return CheckResult(
            name="macOS",
            status=STATUS_WARN,
            detail="could not detect macOS version",
        )
    return CheckResult(
        name="macOS",
        status=STATUS_OK,
        detail=version,
    )


def check_python_version() -> CheckResult:
    version = sys.version.split()[0]
    if sys.version_info >= (3, 11):
        return CheckResult(
            name="Python",
            status=STATUS_OK,
            detail=version,
        )
    return CheckResult(
        name="Python",
        status=STATUS_ERROR,
        detail=f"{version} — xcindex requires Python 3.11+",
        fix="install Python 3.11 or newer (e.g., via pyenv or python.org)",
    )


def check_xcrun() -> CheckResult:
    if shutil.which("xcrun") is None:
        return CheckResult(
            name="xcrun",
            status=STATUS_ERROR,
            detail="xcrun not found on PATH",
            fix="install Xcode command line tools: xcode-select --install",
            group=GROUP_TOOLCHAIN,
        )
    proc = _run(["xcode-select", "-p"])
    xcode_path = proc.stdout.strip() if proc.returncode == 0 else ""
    if not xcode_path:
        return CheckResult(
            name="xcrun",
            status=STATUS_WARN,
            detail="xcrun present but xcode-select returned no path",
            fix="run: sudo xcode-select -s /Applications/Xcode.app",
            group=GROUP_TOOLCHAIN,
        )
    return CheckResult(
        name="xcrun",
        status=STATUS_OK,
        detail=xcode_path,
        group=GROUP_TOOLCHAIN,
    )


def check_swift_toolchain() -> CheckResult:
    proc = _run(["xcrun", "--find", "swift"])
    if proc.returncode != 0:
        return CheckResult(
            name="swift",
            status=STATUS_ERROR,
            detail="swift toolchain not found via xcrun",
            fix="ensure Xcode is installed and selected (xcode-select -p)",
            group=GROUP_TOOLCHAIN,
        )
    swift_path = proc.stdout.strip()
    version_proc = _run(["swift", "--version"])
    version_line = version_proc.stdout.splitlines()[0] if version_proc.stdout else ""
    detail = swift_path + (f" — {version_line}" if version_line else "")
    return CheckResult(
        name="swift",
        status=STATUS_OK,
        detail=detail,
        group=GROUP_TOOLCHAIN,
    )


def check_project(cwd: Path | None = None) -> CheckResult:
    try:
        project = discovery.find_project(cwd)
    except discovery.DiscoveryError as exc:
        return CheckResult(
            name="project",
            status=STATUS_INFO,
            detail=str(exc),
            fix="run xcindex from inside an Xcode/SwiftPM project (or pass --project)",
            group=GROUP_PROJECT,
        )
    return CheckResult(
        name="project",
        status=STATUS_OK,
        detail=f"{project.kind}: {project.path}",
        group=GROUP_PROJECT,
    )


def check_index_store(
    cwd: Path | None = None,
    *,
    index_store_override: Path | None = None,
    derived_data_override: Path | None = None,
) -> CheckResult:
    try:
        project = discovery.find_project(cwd)
    except discovery.DiscoveryError:
        return CheckResult(
            name="index-store",
            status=STATUS_INFO,
            detail="skipped — no project discovered",
            group=GROUP_PROJECT,
        )
    try:
        path = discovery.find_index_store(
            project,
            index_store_override=index_store_override,
            derived_data_override=derived_data_override,
        )
    except discovery.DiscoveryError as exc:
        return CheckResult(
            name="index-store",
            status=STATUS_ERROR,
            detail=str(exc),
            fix="build the project in Xcode (or `xcodebuild build`) to populate the IndexStore",
            group=GROUP_PROJECT,
        )

    units_dir = path / "v5" / "units"
    units_count = sum(1 for _ in units_dir.iterdir()) if units_dir.exists() else 0
    if units_count == 0:
        return CheckResult(
            name="index-store",
            status=STATUS_WARN,
            detail=f"{path} — empty (0 units)",
            fix="build the project to populate the IndexStore",
            group=GROUP_PROJECT,
        )

    freshness = _index_store_freshness(project, units_dir)
    if freshness is not None:
        return CheckResult(
            name="index-store",
            status=STATUS_WARN,
            detail=f"{path} — {units_count} units; {freshness}",
            fix="rebuild the project to refresh the IndexStore",
            group=GROUP_PROJECT,
        )
    return CheckResult(
        name="index-store",
        status=STATUS_OK,
        detail=f"{path} — {units_count} units",
        group=GROUP_PROJECT,
    )


def check_cache_dir() -> CheckResult:
    root = cache_module.cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="cache",
            status=STATUS_ERROR,
            detail=f"cannot create cache dir at {root}: {exc}",
            fix=f"check permissions on {root.parent}",
            group=GROUP_CACHE,
        )
    if not os.access(root, os.W_OK):
        return CheckResult(
            name="cache",
            status=STATUS_ERROR,
            detail=f"cache dir not writable: {root}",
            group=GROUP_CACHE,
        )
    return CheckResult(
        name="cache",
        status=STATUS_OK,
        detail=str(root),
        group=GROUP_CACHE,
    )


def check_helper_present() -> CheckResult:
    binary = helper_module.locate_helper()
    if binary is None:
        try:
            helper_module.helper_source_dir()
            return CheckResult(
                name="helper",
                status=STATUS_INFO,
                detail="binary not built — will build on first query (~60s)",
                fix="run `xcindex setup install` to build now",
                group=GROUP_TOOLCHAIN,
            )
        except helper_module.HelperError as exc:
            return CheckResult(
                name="helper",
                status=STATUS_ERROR,
                detail=f"binary not found and source unavailable: {exc}",
                fix="reinstall xcindex (pipx install) or set XCINDEX_HELPER_SOURCE",
                group=GROUP_TOOLCHAIN,
            )
    return CheckResult(
        name="helper",
        status=STATUS_OK,
        detail=str(binary),
        group=GROUP_TOOLCHAIN,
    )


def check_helper_version() -> CheckResult:
    binary = helper_module.locate_helper()
    if binary is None:
        return CheckResult(
            name="helper-version",
            status=STATUS_INFO,
            detail="skipped — binary not built",
            group=GROUP_TOOLCHAIN,
        )
    try:
        info = helper_module.get_version(binary)
    except helper_module.HelperError as exc:
        return CheckResult(
            name="helper-version",
            status=STATUS_ERROR,
            detail=f"helper failed: {exc}",
            fix="rebuild via `xcindex setup install`",
            group=GROUP_TOOLCHAIN,
        )
    if info.schema_version != helper_module.EXPECTED_SCHEMA_VERSION:
        return CheckResult(
            name="helper-version",
            status=STATUS_WARN,
            detail=f"schema {info.schema_version} (expected {helper_module.EXPECTED_SCHEMA_VERSION})",
            fix="rebuild via `xcindex setup install`",
            group=GROUP_TOOLCHAIN,
        )
    return CheckResult(
        name="helper-version",
        status=STATUS_OK,
        detail=f"helper={info.helper_version} schema={info.schema_version} swift={info.swift_version}",
        group=GROUP_TOOLCHAIN,
    )


def check_git_repo(cwd: Path | None = None) -> CheckResult:
    """Detect whether the working directory is inside a git repo and
    whether the `git` CLI is reachable. Required only by `xcindex git`."""
    if shutil.which("git") is None:
        return CheckResult(
            name="git",
            status=STATUS_INFO,
            detail="git CLI not on PATH (xcindex git unavailable)",
            fix="install git via Xcode CLT or `brew install git`",
            group=GROUP_PROJECT,
        )
    target = Path(cwd) if cwd is not None else Path.cwd()
    if not git_diff_module.is_git_repo(target):
        return CheckResult(
            name="git",
            status=STATUS_INFO,
            detail=f"not inside a git working tree: {target}",
            fix="run `git init` or invoke xcindex from inside a checkout to enable `xcindex git`",
            group=GROUP_PROJECT,
        )
    try:
        base = git_diff_module.detect_default_base(target)
    except git_diff_module.GitError as exc:
        return CheckResult(
            name="git",
            status=STATUS_WARN,
            detail=f"git repo detected but base ref check failed: {exc}",
            group=GROUP_PROJECT,
        )
    branch_proc = _run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "(detached)"
    return CheckResult(
        name="git",
        status=STATUS_OK,
        detail=f"branch={branch} base={base}",
        group=GROUP_PROJECT,
    )


def check_pipx() -> CheckResult:
    if shutil.which("pipx") is None:
        return CheckResult(
            name="pipx",
            status=STATUS_INFO,
            detail="pipx not found (only required if installing via pipx)",
            fix="brew install pipx",
            group=GROUP_TOOLCHAIN,
        )
    return CheckResult(
        name="pipx",
        status=STATUS_OK,
        detail=shutil.which("pipx") or "found",
        group=GROUP_TOOLCHAIN,
    )


# --- Aggregate ---------------------------------------------------------------

def run_all_checks(
    cwd: Path | None = None,
    *,
    index_store_override: Path | None = None,
    derived_data_override: Path | None = None,
) -> list[CheckResult]:
    return [
        check_macos_version(),
        check_python_version(),
        check_xcrun(),
        check_swift_toolchain(),
        check_pipx(),
        check_cache_dir(),
        check_helper_present(),
        check_helper_version(),
        check_project(cwd),
        check_index_store(
            cwd,
            index_store_override=index_store_override,
            derived_data_override=derived_data_override,
        ),
        check_git_repo(cwd),
    ]


def overall_status(results: list[CheckResult]) -> str:
    if any(r.status == STATUS_ERROR for r in results):
        return STATUS_ERROR
    if any(r.status == STATUS_WARN for r in results):
        return STATUS_WARN
    return STATUS_OK


# --- Internal helpers --------------------------------------------------------

def _index_store_freshness(project: discovery.ProjectInfo, units_dir: Path) -> str | None:
    try:
        unit_mtimes = [u.stat().st_mtime_ns for u in units_dir.iterdir()]
    except OSError:
        return None
    if not unit_mtimes:
        return None
    latest_unit = max(unit_mtimes)

    source_extensions = {".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp"}
    latest_source = 0
    for path in project.root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in source_extensions:
            continue
        if any(part.startswith(".") for part in path.relative_to(project.root).parts):
            continue
        try:
            latest_source = max(latest_source, path.stat().st_mtime_ns)
        except OSError:
            continue
    if latest_source > latest_unit:
        return "source files modified after last build"
    return None

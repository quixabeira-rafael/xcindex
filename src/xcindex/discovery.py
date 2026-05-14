from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ENV_INDEX_STORE = "XCINDEX_INDEX_STORE"
ENV_DERIVED_DATA = "XCINDEX_DERIVED_DATA"

DEFAULT_DERIVED_DATA = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"


class DiscoveryError(Exception):
    """Raised when discovery cannot find required artifacts."""


@dataclass(frozen=True)
class ProjectInfo:
    path: Path                  # absolute path to .xcworkspace, .xcodeproj, or Package.swift
    name: str                   # base name without extension
    kind: str                   # "xcworkspace" | "xcodeproj" | "swiftpm"
    root: Path                  # directory enclosing the project artifact

    @property
    def is_xcode(self) -> bool:
        return self.kind in ("xcworkspace", "xcodeproj")


def find_project(cwd: Path | None = None) -> ProjectInfo:
    """Walk up from cwd looking for an Xcode/SwiftPM project artifact.

    Search order at each directory level: .xcworkspace, .xcodeproj, Package.swift.
    Walk stops at a `.git` directory boundary or filesystem root.
    """
    start = (cwd or Path.cwd()).resolve()
    current = start
    while True:
        match = _scan_directory(current)
        if match is not None:
            return match
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise DiscoveryError(
        f"no .xcworkspace/.xcodeproj/Package.swift found walking up from {start}"
    )


def find_index_store(
    project: ProjectInfo,
    *,
    index_store_override: Path | None = None,
    derived_data_override: Path | None = None,
) -> Path:
    """Locate the IndexStore DataStore directory for a project.

    Resolution order:
        1. explicit `index_store_override`
        2. XCINDEX_INDEX_STORE env var
        3. SwiftPM project: <root>/.build/debug/index/store
        4. Xcode project: DerivedData (override / env var / default), accepting either
           a root directory to scan for `<project.name>-*`, or a project-specific
           entry that already contains `Index.noindex/DataStore` (used by worktrees
           with custom DerivedData locations).
    """
    if index_store_override is not None:
        return _validate_index_store(index_store_override.expanduser().resolve())

    env_override = os.environ.get(ENV_INDEX_STORE)
    if env_override:
        return _validate_index_store(Path(env_override).expanduser().resolve())

    if project.kind == "swiftpm":
        candidate = project.root / ".build" / "debug" / "index" / "store"
        return _validate_index_store(candidate)

    derived_data = _resolve_derived_data(derived_data_override)
    direct_store = derived_data / "Index.noindex" / "DataStore"
    if direct_store.is_dir():
        return _validate_index_store(direct_store)
    project_dir = _find_derived_data_for_project(project, derived_data)
    return _validate_index_store(project_dir / "Index.noindex" / "DataStore")


# --- Internal: project scanning -----------------------------------------------------

def _scan_directory(directory: Path) -> ProjectInfo | None:
    workspace = _first_match(directory.glob("*.xcworkspace"))
    if workspace is not None:
        return ProjectInfo(
            path=workspace,
            name=workspace.stem,
            kind="xcworkspace",
            root=directory,
        )
    project = _first_match(directory.glob("*.xcodeproj"))
    if project is not None:
        return ProjectInfo(
            path=project,
            name=project.stem,
            kind="xcodeproj",
            root=directory,
        )
    package = directory / "Package.swift"
    if package.is_file():
        return ProjectInfo(
            path=package,
            name=directory.name,
            kind="swiftpm",
            root=directory,
        )
    return None


def _first_match(matches: Iterable[Path]) -> Path | None:
    candidates = sorted(matches)
    return candidates[0] if candidates else None


# --- Internal: derived data ---------------------------------------------------------

def _resolve_derived_data(override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    env = os.environ.get(ENV_DERIVED_DATA)
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DERIVED_DATA


def _find_derived_data_for_project(project: ProjectInfo, derived_data: Path) -> Path:
    if not derived_data.exists():
        raise DiscoveryError(f"DerivedData directory not found: {derived_data}")

    candidates = sorted(derived_data.glob(f"{project.name}-*"))
    if not candidates:
        raise DiscoveryError(
            f"no DerivedData entry matching '{project.name}-*' under {derived_data}"
        )

    if len(candidates) == 1:
        return candidates[0]

    target_path = str(project.path)
    matching_by_workspace: list[Path] = []
    for entry in candidates:
        info_plist = entry / "info.plist"
        workspace_path = _read_workspace_path(info_plist)
        if workspace_path is not None and workspace_path == target_path:
            matching_by_workspace.append(entry)

    if len(matching_by_workspace) == 1:
        return matching_by_workspace[0]
    if len(matching_by_workspace) > 1:
        return max(matching_by_workspace, key=lambda p: p.stat().st_mtime)

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_workspace_path(info_plist: Path) -> str | None:
    if not info_plist.is_file():
        return None
    try:
        with info_plist.open("rb") as f:
            data = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError):
        return None
    value = data.get("WorkspacePath")
    return value if isinstance(value, str) else None


def _validate_index_store(path: Path) -> Path:
    if not path.exists():
        raise DiscoveryError(f"index store not found at: {path}")
    if not path.is_dir():
        raise DiscoveryError(f"index store path is not a directory: {path}")
    units_dir = path / "v5" / "units"
    if not units_dir.exists():
        raise DiscoveryError(
            f"index store at {path} appears empty or unsupported (missing v5/units)"
        )
    return path

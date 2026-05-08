from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from xcindex import cache as cache_module
from xcindex import discovery
from xcindex import dumper
from xcindex import helper as helper_module
from xcindex import query as query_module


@dataclass(frozen=True)
class ProjectContext:
    project: discovery.ProjectInfo
    index_store: Path
    sqlite_path: Path
    index_hash: str
    warnings: tuple[str, ...] = ()
    is_stale: bool = False


class EngineError(Exception):
    """Raised when the engine cannot prepare a usable SQLite cache."""


class StaleIndexError(EngineError):
    """Raised when --require-fresh detects the IndexStore is older than source files."""


def add_project_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the standard project/index/derived-data overrides to a subcommand."""
    parser.add_argument("--project", type=Path, default=None,
                        help="Path to .xcodeproj/.xcworkspace/Package.swift (overrides discovery).")
    parser.add_argument("--index-store", type=Path, default=None,
                        help="Path to the IndexStore DataStore directory (overrides discovery).")
    parser.add_argument("--derived-data", type=Path, default=None,
                        help="Path to DerivedData root (overrides default).")
    parser.add_argument("--include-system", action="store_true",
                        help="Include SDK / system symbols in the dump (default: false).")
    parser.add_argument("--check-fresh", action="store_true",
                        help="Walk the project tree to detect source files newer than the index "
                             "(emits a warning if stale; default: skipped on large projects).")
    parser.add_argument("--require-fresh", action="store_true",
                        help="Like --check-fresh, but fails with EXIT_STALE_INDEX instead of warning.")


def resolve_project(args: argparse.Namespace) -> discovery.ProjectInfo:
    if args.project is not None:
        path = args.project.expanduser().resolve()
        return discovery.find_project(path if path.is_dir() else path.parent)
    return discovery.find_project()


def resolve_index_store(
    args: argparse.Namespace,
    project: discovery.ProjectInfo,
) -> Path:
    return discovery.find_index_store(
        project,
        index_store_override=args.index_store,
        derived_data_override=args.derived_data,
    )


@contextlib.contextmanager
def open_context(
    args: argparse.Namespace,
    *,
    allow_build: bool = True,
) -> Iterator[tuple[ProjectContext, sqlite3.Connection]]:
    """Resolve project, ensure cache is fresh, yield (context, sqlite connection).

    On cache miss this spawns the helper to materialize the SQLite. On cache hit
    it just opens the existing file.
    """
    try:
        project = resolve_project(args)
    except discovery.DiscoveryError as exc:
        raise EngineError(f"could not discover project: {exc}") from exc

    try:
        index_store = resolve_index_store(args, project)
    except discovery.DiscoveryError as exc:
        raise EngineError(f"could not discover index store: {exc}") from exc

    helper_binary = helper_module.ensure_helper(allow_build=allow_build)
    helper_info = helper_module.get_version(helper_binary)

    index_hash = cache_module.compute_index_hash(
        index_store,
        swift_version=helper_info.swift_version,
        helper_version=helper_info.helper_version,
    )
    sqlite_path = cache_module.sqlite_path_for(project.path, index_hash)
    cache_module.ensure_cache_dir(project.path)

    if not sqlite_path.exists():
        with cache_module.acquire_lock(project.path):
            if not sqlite_path.exists():
                _materialize(
                    project=project,
                    index_store=index_store,
                    sqlite_path=sqlite_path,
                    index_hash=index_hash,
                    helper_info=helper_info,
                    helper_binary=helper_binary,
                    include_system=getattr(args, "include_system", False),
                )
                cache_module.gc_caches(project.path)
                cache_module.write_meta(project.path, latest_hash=index_hash)

    warnings: list[str] = []
    is_stale = False
    require_fresh = getattr(args, "require_fresh", False)
    check_fresh = getattr(args, "check_fresh", False)
    if require_fresh or check_fresh:
        staleness = _detect_staleness(project, index_store)
        if staleness is not None:
            is_stale = True
            warnings.append(staleness)
            if require_fresh:
                raise StaleIndexError(staleness)

    context = ProjectContext(
        project=project,
        index_store=index_store,
        sqlite_path=sqlite_path,
        index_hash=index_hash,
        warnings=tuple(warnings),
        is_stale=is_stale,
    )
    conn = query_module.open_readonly(sqlite_path)
    try:
        yield context, conn
    finally:
        conn.close()


_SOURCE_EXTENSIONS = (".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp")


def _detect_staleness(project: discovery.ProjectInfo, index_store: Path) -> str | None:
    """Return a warning string if any project source file is newer than the latest unit, else None."""
    units_dir = index_store / "v5" / "units"
    if not units_dir.exists():
        return None
    try:
        latest_unit = max(
            (entry.stat().st_mtime_ns for entry in units_dir.iterdir() if entry.is_file()),
            default=None,
        )
    except OSError:
        return None
    if latest_unit is None:
        return None

    latest_source = 0
    latest_path: Path | None = None
    for path in project.root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _SOURCE_EXTENSIONS:
            continue
        try:
            rel_parts = path.relative_to(project.root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if mtime > latest_source:
            latest_source = mtime
            latest_path = path

    if latest_path is None or latest_source <= latest_unit:
        return None
    rel = latest_path.relative_to(project.root)
    return f"index store is older than source file {rel} (rebuild to refresh)"


def _materialize(
    *,
    project: discovery.ProjectInfo,
    index_store: Path,
    sqlite_path: Path,
    index_hash: str,
    helper_info: helper_module.HelperVersion,
    helper_binary: Path,
    include_system: bool,
) -> dumper.DumpStats:
    sys.stderr.write(
        f"materializing cache for {project.name} (hash={index_hash})... "
    )
    sys.stderr.flush()
    with cache_module.staged_write(sqlite_path) as temp_path:
        records = helper_module.stream_dump(
            index_store,
            include_system=include_system,
            helper_path=helper_binary,
        )
        stats = dumper.dump_to_sqlite(
            temp_path,
            records,
            index_hash=index_hash,
            swift_version=helper_info.swift_version,
            helper_version=helper_info.helper_version,
        )
    sys.stderr.write(
        f"{stats.symbols} symbols, {stats.occurrences} occurrences, "
        f"{stats.relations} relations.\n"
    )
    return stats

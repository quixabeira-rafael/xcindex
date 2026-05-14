from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from xcindex import cache as cache_module
from xcindex import discovery
from xcindex import helper as helper_module
from xcindex import query as query_module
from xcindex import schema as schema_module


@dataclass(frozen=True)
class ProjectContext:
    project: discovery.ProjectInfo
    index_store: Path
    sqlite_path: Path
    index_hash: str
    warnings: tuple[str, ...] = ()
    is_stale: bool = False


@dataclass(frozen=True)
class MaterializationResult:
    """Outcome of a single materialize() call.

    `mode` is one of:
      - "cold"           — no cache existed (or new units detected); ran a full bootstrap.
      - "schema_upgrade" — cache existed but schema was outdated; ran a full re-bootstrap.
      - "incremental"    — cache existed; processed delta of modified/removed units.
      - "noop"           — cache up to date; no work performed.
    """
    mode: str
    project: discovery.ProjectInfo
    index_store: Path
    sqlite_path: Path
    index_hash: str
    wall_seconds: float
    symbols_added: int = 0
    occurrences_added: int = 0
    relations_added: int = 0
    units_modified: int = 0
    units_removed: int = 0
    units_added: int = 0


class EngineError(Exception):
    """Raised when the engine cannot prepare a usable SQLite cache."""


class StaleIndexError(EngineError):
    """Raised when --require-fresh detects the IndexStore is older than source files."""


def add_project_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_freshness_flags: bool = True,
) -> None:
    """Attach the standard project/index/derived-data overrides to a subcommand.

    `include_freshness_flags=False` skips `--check-fresh` and `--require-fresh`.
    Use this for commands like `prewarm` where freshness checks don't apply
    (since the command itself IS the freshness operation).
    """
    parser.add_argument("--project", type=Path, default=None,
                        help="Path to .xcodeproj/.xcworkspace/Package.swift (overrides discovery).")
    parser.add_argument("--index-store", type=Path, default=None,
                        help="Path to the IndexStore DataStore directory (overrides discovery).")
    parser.add_argument("--derived-data", type=Path, default=None,
                        help="Path to DerivedData root, or directly to a project-specific "
                             "DerivedData entry (containing Index.noindex/DataStore). "
                             "Overrides XCINDEX_DERIVED_DATA and the default.")
    parser.add_argument("--include-system", action="store_true",
                        help="Include SDK / system symbols in the dump (default: false).")
    if include_freshness_flags:
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


def materialize(
    args: argparse.Namespace,
    *,
    allow_build: bool = True,
) -> MaterializationResult:
    """Resolve project + IndexStore, ensure SQLite cache is up-to-date, return stats.

    Does NOT open a query connection. Idempotent: a second consecutive call with
    no changes returns mode="noop". Concurrency-safe via `cache_module.acquire_lock`.

    `allow_build=False` skips the helper rebuild step (raises HelperError if the
    helper binary is missing). Useful in hot paths where the caller doesn't want
    to pay the ~60s build cost.
    """
    start = time.monotonic()

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

    cache_module.ensure_cache_dir(project.path)
    sqlite_path = cache_module.canonical_sqlite_path(project.path)

    index_hash = cache_module.compute_index_hash(
        index_store,
        swift_version=helper_info.swift_version,
        helper_version=helper_info.helper_version,
    )
    include_system = getattr(args, "include_system", False)

    mode = "noop"
    symbols_added = 0
    occurrences_added = 0
    relations_added = 0
    units_modified = 0
    units_removed = 0
    units_added = 0

    with cache_module.acquire_lock(project.path):
        # Rename any pre-canonical-name caches to `legacy_*.sqlite` before
        # deciding bootstrap vs reuse, so they're preserved for forensics
        # but don't pollute the live cache file path.
        renamed = cache_module.migrate_v1_caches(project.path)
        if renamed:
            sys.stderr.write(
                f"xcindex: schema upgraded to v{schema_module.SCHEMA_VERSION}; "
                f"preserved {renamed} legacy snapshot(s); see `xcindex cache list`.\n"
            )

        from xcindex import incremental as incremental_module

        cache_existed_before = sqlite_path.exists()
        needs_bootstrap = not cache_existed_before or _schema_outdated(sqlite_path)
        if needs_bootstrap:
            mode = "schema_upgrade" if cache_existed_before else "cold"
            if cache_existed_before:
                sqlite_path.unlink()
            result = _materialize(
                project=project,
                index_store=index_store,
                sqlite_path=sqlite_path,
                index_hash=index_hash,
                helper_info=helper_info,
                helper_binary=helper_binary,
                include_system=include_system,
            )
            symbols_added = result.symbols
            occurrences_added = result.occurrences
            relations_added = result.relations
            cache_module.gc_caches(project.path)
            cache_module.write_meta(project.path, latest_hash=index_hash)
        else:
            delta = incremental_module.compute_unit_delta(sqlite_path, index_store)
            if delta.needs_full_redump:
                # New units (added source files) — fall back to full re-dump.
                mode = "cold"
                units_added = len(delta.added)
                sys.stderr.write(
                    f"xcindex: {units_added} new unit(s) detected; "
                    "running full re-dump (incremental cannot infer their files yet).\n"
                )
                sqlite_path.unlink()
                result = _materialize(
                    project=project,
                    index_store=index_store,
                    sqlite_path=sqlite_path,
                    index_hash=index_hash,
                    helper_info=helper_info,
                    helper_binary=helper_binary,
                    include_system=include_system,
                )
                symbols_added = result.symbols
                occurrences_added = result.occurrences
                relations_added = result.relations
                cache_module.gc_caches(project.path)
                cache_module.write_meta(project.path, latest_hash=index_hash)
            elif not delta.is_empty:
                units_modified = len(delta.modified)
                units_removed = len(delta.removed)
                try:
                    stats = helper_module.run_incremental(
                        index_store_path=index_store,
                        sqlite_path=sqlite_path,
                        modified_units=sorted(delta.modified),
                        removed_units=sorted(delta.removed),
                        include_system=include_system,
                        helper_path=helper_binary,
                    )
                except helper_module.StaleSchemaError:
                    # Cache schema lags behind the helper — tear down and bootstrap fresh.
                    mode = "schema_upgrade"
                    units_modified = 0
                    units_removed = 0
                    sys.stderr.write(
                        "xcindex: cache schema mismatch; running full re-bootstrap.\n"
                    )
                    sqlite_path.unlink()
                    result = _materialize(
                        project=project,
                        index_store=index_store,
                        sqlite_path=sqlite_path,
                        index_hash=index_hash,
                        helper_info=helper_info,
                        helper_binary=helper_binary,
                        include_system=include_system,
                    )
                    symbols_added = result.symbols
                    occurrences_added = result.occurrences
                    relations_added = result.relations
                    cache_module.gc_caches(project.path)
                    cache_module.write_meta(project.path, latest_hash=index_hash)
                else:
                    mode = "incremental"
                    symbols_added = stats.symbols
                    occurrences_added = stats.occurrences
                    relations_added = stats.relations
                    sys.stderr.write(
                        f"xcindex: incremental update — "
                        f"modified {units_modified}, removed {units_removed} unit(s); "
                        f"+{stats.symbols} symbols, +{stats.occurrences} occurrences, "
                        f"+{stats.relations} relations ({stats.wall_seconds:.1f}s).\n"
                    )
                    cache_module.write_meta(project.path, latest_hash=index_hash)
            # else: cache hit, mode stays "noop"

    return MaterializationResult(
        mode=mode,
        project=project,
        index_store=index_store,
        sqlite_path=sqlite_path,
        index_hash=index_hash,
        wall_seconds=time.monotonic() - start,
        symbols_added=symbols_added,
        occurrences_added=occurrences_added,
        relations_added=relations_added,
        units_modified=units_modified,
        units_removed=units_removed,
        units_added=units_added,
    )


@contextlib.contextmanager
def open_context(
    args: argparse.Namespace,
    *,
    allow_build: bool = True,
) -> Iterator[tuple[ProjectContext, sqlite3.Connection]]:
    """Resolve project, ensure cache is fresh, yield (context, sqlite connection).

    Materialization is delegated to `materialize()`; this wrapper adds the
    staleness check (when --check-fresh / --require-fresh are set) and opens
    a read-only SQLite connection for queries.
    """
    result = materialize(args, allow_build=allow_build)

    warnings: list[str] = []
    is_stale = False
    require_fresh = getattr(args, "require_fresh", False)
    check_fresh = getattr(args, "check_fresh", False)
    if require_fresh or check_fresh:
        staleness = _detect_staleness(result.project, result.index_store)
        if staleness is not None:
            is_stale = True
            warnings.append(staleness)
            if require_fresh:
                raise StaleIndexError(staleness)

    context = ProjectContext(
        project=result.project,
        index_store=result.index_store,
        sqlite_path=result.sqlite_path,
        index_hash=result.index_hash,
        warnings=tuple(warnings),
        is_stale=is_stale,
    )
    conn = query_module.open_readonly(result.sqlite_path)
    try:
        yield context, conn
    finally:
        conn.close()


_SOURCE_EXTENSIONS = (".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp")


def _schema_outdated(sqlite_path: Path) -> bool:
    """Return True if the cache at sqlite_path was written with a stale schema."""
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return True
    try:
        version = schema_module.read_schema_version(conn)
    finally:
        conn.close()
    if version is None:
        return True
    return version != schema_module.SCHEMA_VERSION


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
) -> helper_module.HelperRunResult:
    """Run a fresh bootstrap: the helper writes a new SQLite at `sqlite_path`.

    The helper handles the staged-write + atomic-rename pattern itself.
    """
    sys.stderr.write(
        f"materializing cache for {project.name} (hash={index_hash})... "
    )
    sys.stderr.flush()
    result = helper_module.run_bootstrap(
        index_store_path=index_store,
        output_path=sqlite_path,
        include_system=include_system,
        helper_path=helper_binary,
    )
    # Stamp the meta table with the index_hash so `xcindex cache list` and
    # diagnostics keep working. Helper already wrote schema_version, helper_version,
    # dumped_at, and per-table counts.
    conn = sqlite3.connect(str(sqlite_path))
    try:
        schema_module.write_meta(conn, index_hash=index_hash,
                                  swift_version=helper_info.swift_version)
    finally:
        conn.close()

    sys.stderr.write(
        f"{result.symbols} symbols, {result.occurrences} occurrences, "
        f"{result.relations} relations ({result.wall_seconds:.1f}s).\n"
    )
    return result

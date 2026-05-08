from __future__ import annotations

import argparse
from pathlib import Path

from xcindex import cache as cache_module
from xcindex import discovery
from xcindex.output import (
    EXIT_INVALID_STATE,
    EXIT_OK,
    EXIT_USAGE,
    emit_json,
    emit_text,
    render_table,
)


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "cache",
        help="Inspect and manage the SQLite cache.",
        description="List or clear cached SQLite databases derived from IndexStores.",
    )
    sub = parser.add_subparsers(dest="cache_command", metavar="SUBCOMMAND")

    list_parser = sub.add_parser("list", help="List cached SQLite files.")
    list_parser.add_argument("--json", dest="json_mode", action="store_true",
                             help="Emit results as JSON.")
    list_parser.add_argument("--project", type=Path, default=None,
                             help="Limit to caches for a specific project (default: all projects).")
    list_parser.set_defaults(func=cmd_cache_list)

    clear_parser = sub.add_parser("clear", help="Remove cached SQLite files.")
    clear_parser.add_argument("--json", dest="json_mode", action="store_true",
                              help="Emit results as JSON.")
    clear_parser.add_argument("--all", dest="all_projects", action="store_true",
                              help="Clear caches across all projects.")
    clear_parser.add_argument("--project", type=Path, default=None,
                              help="Project to clear (default: discovered project).")
    clear_parser.set_defaults(func=cmd_cache_clear)

    parser.set_defaults(func=lambda args: _print_help(parser))


def _print_help(parser) -> int:
    parser.print_help()
    return EXIT_USAGE


def cmd_cache_list(args: argparse.Namespace) -> int:
    project_path = _resolve_project_path(args.project)
    entries = cache_module.list_caches(project_path)

    if args.json_mode:
        emit_json({
            "count": len(entries),
            "entries": [
                {
                    "project_fingerprint": e.project_fingerprint,
                    "project_path": str(e.project_path),
                    "index_hash": e.index_hash,
                    "sqlite_path": str(e.sqlite_path),
                    "size_bytes": e.size_bytes,
                    "mtime_ns": e.mtime_ns,
                }
                for e in entries
            ],
        })
        return EXIT_OK

    if not entries:
        emit_text("no cache entries.")
        return EXIT_OK

    rows = [
        {
            "fingerprint": e.project_fingerprint,
            "project": str(e.project_path),
            "hash": e.index_hash,
            "size": _format_size(e.size_bytes),
        }
        for e in entries
    ]
    emit_text(render_table(
        rows,
        columns=[
            ("fingerprint", "FINGERPRINT"),
            ("hash", "INDEX HASH"),
            ("size", "SIZE"),
            ("project", "PROJECT"),
        ],
    ))
    return EXIT_OK


def cmd_cache_clear(args: argparse.Namespace) -> int:
    if args.all_projects:
        removed = cache_module.clear_caches(all_projects=True)
        _emit_clear_result(args, removed=removed, scope="all projects")
        return EXIT_OK

    project_path = _resolve_project_path(args.project)
    if project_path is None:
        if args.json_mode:
            emit_json({"error": "no_project", "message": "no project discovered; pass --project or --all"})
        else:
            emit_text("error: no project discovered; pass --project or --all")
        return EXIT_INVALID_STATE
    removed = cache_module.clear_caches(project_path)
    _emit_clear_result(args, removed=removed, scope=str(project_path))
    return EXIT_OK


def _emit_clear_result(args: argparse.Namespace, *, removed: int, scope: str) -> None:
    if args.json_mode:
        emit_json({"removed": removed, "scope": scope})
    else:
        emit_text(f"removed {removed} cache entr{'y' if removed == 1 else 'ies'} ({scope})")


def _resolve_project_path(override: Path | None) -> Path | None:
    if override is not None:
        return override.expanduser().resolve()
    try:
        return discovery.find_project().path
    except discovery.DiscoveryError:
        return None


def _format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes //= 1024
    return f"{num_bytes} TB"

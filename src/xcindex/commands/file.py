from __future__ import annotations

import argparse
import shlex

from xcindex import engine
from xcindex import query as query_module
from xcindex.commands._common import (
    add_output_arguments,
    annotate_with_context,
    handle_engine_error,
)
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, emit_error, emit_result

DEFAULT_TYPE_KINDS: tuple[str, ...] = ("class", "struct", "enum", "protocol")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "file",
        help="List type definitions in a source file (also reachable as `xcindex <file>`).",
        description=(
            "Inspect a Swift/ObjC source file and list the top-level types it "
            "defines (class, struct, enum, protocol). Use --all to include every "
            "definition (extensions, methods, properties, etc.). The argument can "
            "be a full path, a filename with extension, or a bare filename."
        ),
    )
    parser.add_argument("file", type=str,
                        help="File path, filename, or filename without extension.")
    parser.add_argument("--all", action="store_true", dest="show_all",
                        help="Include every definition, not only top-level types.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_file, json_mode=False)
    # Items are the whole point of this command — bump the default level so
    # the table shows up without the caller having to pass --level.
    parser.set_defaults(level="detailed")


def cmd_file(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            matches = query_module.find_files_in_index(conn, args.file)
            if not matches:
                return emit_error(
                    "file_not_indexed",
                    f"file not found in index: {args.file!r} "
                    "(rebuild the project to refresh)",
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )
            if len(matches) > 1:
                listing = "\n".join(f"  xcindex {shlex.quote(m)}" for m in matches)
                return emit_error(
                    "ambiguous_file",
                    f"multiple files match {args.file!r}; pick one:\n{listing}",
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )
            file_path = matches[0]
            kinds = None if args.show_all else DEFAULT_TYPE_KINDS
            canonical = query_module.query_file_definitions(
                conn, file_path, kinds=kinds, limit=args.limit,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)

    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

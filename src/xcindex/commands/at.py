from __future__ import annotations

import argparse

from xcindex import engine
from xcindex import query as query_module
from xcindex.commands._common import (
    add_output_arguments,
    annotate_with_context,
    handle_engine_error,
    parse_position,
    truncate_items,
)
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, emit_error, emit_result


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "at",
        help="List the symbols/occurrences at a given file:line[:column].",
        description="Resolve a source position to its overlapping symbols and roles.",
    )
    parser.add_argument("position", type=str,
                        help="Position as <file>:<line> or <file>:<line>:<column>.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_at, json_mode=False)


def cmd_at(args: argparse.Namespace) -> int:
    try:
        file, line, column = parse_position(args.position)
    except argparse.ArgumentTypeError as exc:
        return emit_error(
            "invalid_position", str(exc),
            json_mode=False, exit_code=EXIT_INVALID_STATE,
        )

    expanded = file.expanduser()
    file_path = str(expanded.resolve()) if expanded.exists() else str(expanded)
    file_warning: str | None = None
    if not expanded.exists():
        file_warning = (
            f"file does not exist on disk: {file_path}. "
            f"may be generated (SwiftGen, R.swift) or moved since last build."
        )

    try:
        with engine.open_context(args) as (ctx, conn):
            canonical = query_module.query_at(conn, file_path, line, column)
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)

    if file_warning:
        canonical.setdefault("warnings", []).append(file_warning)
    truncate_items(canonical, args.limit)
    if args.include_raw:
        canonical["raw"] = {"sqlite_path": str(ctx.sqlite_path)}
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

from __future__ import annotations

import argparse

from xcindex import engine
from xcindex import query as query_module
from xcindex.commands._common import (
    add_output_arguments,
    annotate_with_context,
    handle_engine_error,
)
from xcindex.output import EXIT_OK, emit_result


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "occurrences",
        help="List all occurrences of a symbol (definitions, references, calls, etc.).",
    )
    parser.add_argument("usr", type=str, help="The symbol's USR (e.g. 's:...').")
    parser.add_argument("--role", type=str, default=None,
                        choices=[name for name, _ in query_module._ROLE_BITS],
                        help="Filter by a single role (default: all roles).")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_occurrences, json_mode=False)


def cmd_occurrences(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            canonical = query_module.query_occurrences(
                conn, args.usr, role=args.role, limit=args.limit,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

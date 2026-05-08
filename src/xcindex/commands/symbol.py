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
        "symbol",
        help="Look up a symbol by USR or by name.",
        description="If the input starts with 's:' it is treated as a USR; otherwise as a name.",
    )
    parser.add_argument("identifier", type=str, help="USR (`s:...`) or symbol name.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_symbol, json_mode=False)


def cmd_symbol(args: argparse.Namespace) -> int:
    identifier = args.identifier
    try:
        with engine.open_context(args) as (ctx, conn):
            if identifier.startswith("s:") or identifier.startswith("c:"):
                canonical = query_module.query_symbol_by_usr(conn, identifier)
            else:
                canonical = query_module.query_symbol_by_name(conn, identifier, limit=args.limit)
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

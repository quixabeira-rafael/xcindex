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
        "search",
        help="Search symbols by name (substring, case-insensitive).",
    )
    parser.add_argument("pattern", type=str, help="Substring to match against symbol names.")
    parser.add_argument("--kind", type=str, default=None,
                        help="Filter by symbol kind (e.g. class, struct, instance-method).")
    parser.add_argument("--module", type=str, default=None,
                        help="Filter by module name.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_search, json_mode=False)


def cmd_search(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            canonical = query_module.query_search(
                conn, args.pattern,
                kind=args.kind,
                module=args.module,
                limit=args.limit if args.limit else 20,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

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
        "relations",
        help="List relations for a symbol (baseOf, overrideOf, calledBy, etc.).",
    )
    parser.add_argument("usr", type=str, help="The symbol's USR.")
    parser.add_argument("--kind", type=str, default=None,
                        choices=list(query_module.RELATION_KINDS),
                        help="Filter by relation kind.")
    parser.add_argument("--direction", choices=("in", "out"), default="out",
                        help="Direction: 'out' = relations originating from this symbol; "
                             "'in' = relations targeting this symbol (default: out).")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_relations, json_mode=False)


def cmd_relations(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            canonical = query_module.query_relations(
                conn, args.usr,
                kind=args.kind,
                direction=args.direction,
                limit=args.limit,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

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
        "reach",
        help="Transitive reachability from a symbol via relations (call/inheritance).",
        description="Walk the relation graph up (who uses) or down (what is used) up to N hops.",
    )
    parser.add_argument("usr", type=str, help="Starting symbol's USR.")
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--up", dest="direction", action="store_const", const="up",
                           help="Reverse closure: who transitively uses this symbol (default).")
    direction.add_argument("--down", dest="direction", action="store_const", const="down",
                           help="Forward closure: what this symbol transitively uses.")
    parser.add_argument("--depth", dest="max_depth", type=int, default=8,
                        help="Maximum hops (default: 8).")
    parser.add_argument("--to-module", type=str, default=None,
                        help="Restrict output to symbols whose module matches.")
    parser.add_argument("--kind", action="append", default=None,
                        choices=list(query_module.RELATION_KINDS),
                        help="Relation kinds to traverse (repeatable). Default: call+inheritance set.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_reach, json_mode=False, direction="up")


def cmd_reach(args: argparse.Namespace) -> int:
    kinds = tuple(args.kind) if args.kind else None
    try:
        with engine.open_context(args) as (ctx, conn):
            canonical = query_module.query_reach(
                conn, args.usr,
                direction=args.direction,
                max_depth=args.max_depth,
                to_module=args.to_module,
                kinds=kinds,
                limit=args.limit,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)
    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

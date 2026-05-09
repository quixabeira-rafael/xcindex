from __future__ import annotations

import argparse
import shlex

from xcindex import engine
from xcindex import impact as impact_module
from xcindex import query as query_module
from xcindex.commands._common import (
    add_output_arguments,
    annotate_with_context,
    handle_engine_error,
)
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, emit_error, emit_result


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "impact",
        help="Bidirectional impact analysis for a method or type (call/usage stacks).",
        description=(
            "Resolve the input (file:line, name, or USR) to a symbol and produce "
            "stack-frame-style call chains: upstream (who transitively uses it) "
            "and downstream (what it transitively uses). For non-callable, "
            "non-type kinds, emits suggested follow-up commands instead."
        ),
    )
    parser.add_argument("target", type=str,
                        help="File:line, symbol name, or USR (s:.../c:...).")
    parser.add_argument("--depth", dest="max_depth", type=int, default=8,
                        help="Maximum BFS depth in each direction (default: 8).")
    parser.add_argument("--max-stacks", dest="max_stacks", type=int, default=10,
                        help="Maximum stacks shown per direction (default: 10).")
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--up-only", dest="direction", action="store_const", const="up",
                           help="Show only upstream stacks (callers).")
    direction.add_argument("--down-only", dest="direction", action="store_const", const="down",
                           help="Show only downstream stacks (callees).")
    parser.add_argument("--no-overrides", action="store_true",
                        help="Skip overrideOf edges (strict call-only traversal).")
    parser.add_argument("--to-module", type=str, default=None,
                        help="Restrict stacks whose terminal symbol's module matches.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_impact, json_mode=False, level="locations", direction="both")


def cmd_impact(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            try:
                target = query_module.resolve_input_to_usr(conn, args.target)
            except query_module.AmbiguousNameError as exc:
                listing = "\n".join(
                    f"  xcindex impact {shlex.quote(c['usr'])}  # {c.get('name')} ({c.get('kind')}) "
                    f"{c.get('module') or '?'}:{c.get('file') or '?'}"
                    for c in exc.candidates
                )
                return emit_error(
                    "ambiguous_name",
                    f"name {exc.name!r} matches {len(exc.candidates)} symbols; pick one:\n{listing}",
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )
            except query_module.SymbolNotFoundError as exc:
                return emit_error(
                    "target_not_found", str(exc),
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )

            canonical = impact_module.build_impact(
                conn, target,
                max_depth=args.max_depth,
                max_stacks=args.max_stacks,
                direction=args.direction,
                no_overrides=args.no_overrides,
                to_module=args.to_module,
            )
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)

    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK

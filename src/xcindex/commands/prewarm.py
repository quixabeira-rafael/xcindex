from __future__ import annotations

import argparse

from xcindex import engine
from xcindex.commands._common import (
    add_output_arguments,
    handle_engine_error,
)
from xcindex.output import EXIT_OK, emit_result, emit_text


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "prewarm",
        help="Materialize the SQLite cache from the IndexStore (cold/incremental/noop).",
        description=(
            "Run the cold-or-incremental dispatch that normally happens lazily on the "
            "first query, but as a standalone one-shot command. Idempotent: a second "
            "consecutive call with no IndexStore changes is a no-op. Designed to be "
            "wired into a build hook so query-time latency stays warm-cache fast."
        ),
    )
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress output when the cache is already up to date "
                             "(no-op). Cold and incremental updates still print.")
    parser.add_argument("--no-build-helper", dest="allow_build", action="store_false",
                        help="Fail fast if the helper binary is missing instead of "
                             "rebuilding it (~60s). Useful in build hooks where the "
                             "rebuild cost is unwanted.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser, include_freshness_flags=False)
    parser.set_defaults(func=cmd_prewarm, json_mode=False, allow_build=True)


def cmd_prewarm(args: argparse.Namespace) -> int:
    try:
        result = engine.materialize(args, allow_build=args.allow_build)
    except engine.EngineError as exc:
        return handle_engine_error(exc)

    canonical = _build_canonical(result)

    if args.output_format == "agent":
        # Default agent format: emit a single human-readable line on stdout.
        # `--quiet` suppresses the no-op message.
        if result.mode == "noop" and args.quiet:
            return EXIT_OK
        emit_text(_format_text(result))
        return EXIT_OK

    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK


def _build_canonical(result: engine.MaterializationResult) -> dict:
    return {
        "kind": "prewarm",
        "anchor": {
            "project": str(result.project.path),
            "sqlite": str(result.sqlite_path),
            "index_hash": result.index_hash,
        },
        "summary": {
            "found": True,
            "mode": result.mode,
            "wall_seconds": round(result.wall_seconds, 3),
            "symbols_added": result.symbols_added,
            "occurrences_added": result.occurrences_added,
            "relations_added": result.relations_added,
            "units_modified": result.units_modified,
            "units_removed": result.units_removed,
            "units_added": result.units_added,
        },
        "warnings": [],
        "truncated": False,
    }


def _format_text(result: engine.MaterializationResult) -> str:
    seconds = f"{result.wall_seconds:.1f}s"
    if result.mode == "cold":
        prefix = "bootstrapped"
        if result.units_added:
            prefix = f"bootstrapped ({result.units_added} new unit(s) detected)"
        return (
            f"{prefix}: {result.symbols_added} symbols, "
            f"{result.occurrences_added} occurrences, "
            f"{result.relations_added} relations ({seconds})"
        )
    if result.mode == "schema_upgrade":
        return (
            f"schema upgraded; full re-bootstrap: {result.symbols_added} symbols, "
            f"{result.occurrences_added} occurrences, "
            f"{result.relations_added} relations ({seconds})"
        )
    if result.mode == "incremental":
        return (
            f"incremental: {result.units_modified} modified, "
            f"{result.units_removed} removed (+{result.symbols_added} symbols, "
            f"+{result.occurrences_added} occurrences, "
            f"+{result.relations_added} relations) ({seconds})"
        )
    return f"cache up to date ({seconds})"

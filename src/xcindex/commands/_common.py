from __future__ import annotations

import argparse
from pathlib import Path

from xcindex import engine
from xcindex.output import (
    DEFAULT_FORMAT,
    DEFAULT_LEVEL,
    EXIT_INVALID_STATE,
    EXIT_STALE_INDEX,
    FORMATS,
    LEVELS,
    emit_error,
)


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--level", choices=list(LEVELS), default=DEFAULT_LEVEL,
                        help=f"Detail level (default: {DEFAULT_LEVEL}).")
    parser.add_argument("--format", choices=list(FORMATS), default=DEFAULT_FORMAT,
                        dest="output_format",
                        help=f"Output format (default: {DEFAULT_FORMAT}).")
    parser.add_argument("--limit", type=int, default=50,
                        help="Maximum number of items to return (default: 50).")
    parser.add_argument("--include-raw", action="store_true",
                        help="Include raw underlying records in detailed output.")


def parse_position(value: str) -> tuple[Path, int, int | None]:
    """Parse `<file>:<line>[:<column>]` into (path, line, column).

    Raises argparse.ArgumentTypeError on malformed input.
    """
    parts = value.rsplit(":", 2)
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "expected <file>:<line> or <file>:<line>:<column>"
        )
    file_part = parts[0]
    try:
        line = int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid line number: {parts[1]!r}")
    column: int | None = None
    if len(parts) == 3:
        try:
            column = int(parts[2])
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid column number: {parts[2]!r}")
    return Path(file_part), line, column


def truncate_items(canonical: dict, limit: int) -> dict:
    items = canonical.get("items")
    if items is None or len(items) <= limit:
        return canonical
    canonical["items"] = items[:limit]
    canonical["truncated"] = True
    return canonical


def annotate_with_context(canonical: dict, ctx: "engine.ProjectContext") -> dict:
    """Merge engine-side warnings (e.g. staleness) into the canonical result."""
    if ctx.warnings:
        warnings = list(canonical.get("warnings") or [])
        warnings.extend(ctx.warnings)
        canonical["warnings"] = warnings
    return canonical


def handle_engine_error(exc: Exception, *, json_mode: bool = False) -> int:
    """Map engine exceptions to exit codes + structured error output."""
    if isinstance(exc, engine.StaleIndexError):
        return emit_error(
            "stale_index", str(exc),
            json_mode=json_mode, exit_code=EXIT_STALE_INDEX,
        )
    return emit_error(
        "engine_error", str(exc),
        json_mode=json_mode, exit_code=EXIT_INVALID_STATE,
    )

from __future__ import annotations

import argparse
import errno
import sys

from xcindex import __version__
from xcindex.commands import at as at_commands
from xcindex.commands import cache as cache_commands
from xcindex.commands import containing as containing_commands
from xcindex.commands import doctor as doctor_commands
from xcindex.commands import file as file_commands
from xcindex.commands import neighbors as neighbors_commands
from xcindex.commands import occurrences as occurrences_commands
from xcindex.commands import reach as reach_commands
from xcindex.commands import relations as relations_commands
from xcindex.commands import search as search_commands
from xcindex.commands import setup as setup_commands
from xcindex.commands import skill as skill_commands
from xcindex.commands import symbol as symbol_commands
from xcindex.output import EXIT_SYSTEM, EXIT_USAGE, emit_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xcindex",
        description="Blast-radius CLI for Xcode projects: query the IndexStore for symbols, references, and reachability.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    setup_commands.register(subparsers)
    skill_commands.register(subparsers)
    doctor_commands.register(subparsers)
    cache_commands.register(subparsers)
    at_commands.register(subparsers)
    containing_commands.register(subparsers)
    file_commands.register(subparsers)
    symbol_commands.register(subparsers)
    occurrences_commands.register(subparsers)
    relations_commands.register(subparsers)
    neighbors_commands.register(subparsers)
    reach_commands.register(subparsers)
    search_commands.register(subparsers)

    return parser


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json_mode", False))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = _expand_file_shorthand(parser, list(argv))
    args = parser.parse_args(argv)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        return int(func(args))
    except PermissionError as exc:
        return emit_error(
            "permission_denied",
            _format_os_error("permission denied", exc),
            json_mode=_json_mode(args),
            exit_code=EXIT_SYSTEM,
        )
    except FileNotFoundError as exc:
        return emit_error(
            "path_not_found",
            _format_os_error("path not found", exc),
            json_mode=_json_mode(args),
            exit_code=EXIT_SYSTEM,
        )
    except IsADirectoryError as exc:
        return emit_error(
            "filesystem_error",
            _format_os_error("expected a file but found a directory", exc),
            json_mode=_json_mode(args),
            exit_code=EXIT_SYSTEM,
        )
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code in {errno.ENOSPC, errno.EDQUOT}:
            return emit_error(
                "no_space",
                _format_os_error("no space left on device", exc),
                json_mode=_json_mode(args),
                exit_code=EXIT_SYSTEM,
            )
        return emit_error(
            "filesystem_error",
            _format_os_error("filesystem error", exc),
            json_mode=_json_mode(args),
            exit_code=EXIT_SYSTEM,
        )


def _expand_file_shorthand(parser: argparse.ArgumentParser, argv: list[str]) -> list[str]:
    """Treat `xcindex <file>` as `xcindex file <file>` when the first positional
    isn't a known subcommand. Leaves explicit subcommands and `-`-prefixed flags
    untouched.
    """
    if not argv or argv[0].startswith("-"):
        return argv
    subparsers_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if subparsers_action is None:
        return argv
    if argv[0] in subparsers_action.choices:
        return argv
    return ["file", *argv]


def _format_os_error(prefix: str, exc: OSError) -> str:
    parts = [prefix]
    detail = str(exc) or exc.__class__.__name__
    parts.append(detail)
    return ": ".join(parts)

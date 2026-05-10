from __future__ import annotations

import argparse

from xcindex import discovery
from xcindex import engine
from xcindex import watch as watch_module
from xcindex.commands._common import handle_engine_error
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, emit_error


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "watch",
        help="Watch the IndexStore for changes and run prewarm automatically (foreground process).",
        description=(
            "Listen for filesystem events on the IndexStore's `units/` directory "
            "and dispatch `xcindex prewarm --quiet` whenever a build settles. "
            "Foreground process — Ctrl+C to stop. Single-instance per project; "
            "use `xcindex doctor` to inspect watcher state. The watcher keeps "
            "running even if a prewarm subprocess fails (errors are logged + "
            "counted in the watch state file)."
        ),
    )
    parser.add_argument("--debounce", type=int, default=500,
                        help="Milliseconds to wait after the last filesystem event "
                             "before triggering prewarm (default: 500).")
    engine.add_project_arguments(parser, include_freshness_flags=False)
    parser.set_defaults(func=cmd_watch)


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        project = engine.resolve_project(args)
    except discovery.DiscoveryError as exc:
        return emit_error(
            "engine_error",
            f"could not discover project: {exc}",
            json_mode=False, exit_code=EXIT_INVALID_STATE,
        )
    try:
        index_store = engine.resolve_index_store(args, project)
    except discovery.DiscoveryError as exc:
        return emit_error(
            "engine_error",
            f"could not discover index store: {exc}",
            json_mode=False, exit_code=EXIT_INVALID_STATE,
        )

    try:
        watch_module.acquire_watch_lock(project.path)
    except watch_module.WatchError as exc:
        return emit_error(
            "watcher_already_running", str(exc),
            json_mode=False, exit_code=EXIT_INVALID_STATE,
        )

    try:
        return watch_module.run_watch_loop(
            project.path,
            index_store,
            debounce_seconds=args.debounce / 1000.0,
        )
    except watch_module.WatchError as exc:
        return emit_error(
            "watcher_error", str(exc),
            json_mode=False, exit_code=EXIT_INVALID_STATE,
        )

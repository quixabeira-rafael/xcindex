from __future__ import annotations

import argparse
from pathlib import Path

from xcindex import doctor as doctor_module
from xcindex.output import (
    EXIT_INVALID_STATE,
    EXIT_OK,
    emit_json,
    emit_text,
    render_table,
)


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="Validate environment, toolchain, IndexStore, cache, watcher, and git state.",
        description=(
            "Run a series of checks against the local environment "
            "(macOS / Python / Xcode toolchain / pipx / cache dir / helper / project / "
            "IndexStore / cache freshness vs IndexStore / `xcindex watch` status / "
            "git working tree) and report status."
        ),
    )
    parser.add_argument("--json", dest="json_mode", action="store_true",
                        help="Emit results as JSON (suitable for agent consumption).")
    parser.add_argument("--project", type=Path, default=None,
                        help="Path to .xcodeproj/.xcworkspace/Package.swift (overrides discovery).")
    parser.add_argument("--index-store", type=Path, default=None,
                        help="Path to the IndexStore DataStore directory (overrides discovery).")
    parser.add_argument("--derived-data", type=Path, default=None,
                        help="Path to DerivedData root (overrides ~/Library/Developer/Xcode/DerivedData).")
    parser.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    cwd = args.project.parent if args.project else None
    results = doctor_module.run_all_checks(
        cwd=cwd,
        index_store_override=args.index_store,
        derived_data_override=args.derived_data,
    )
    overall = doctor_module.overall_status(results)

    if args.json_mode:
        emit_json({
            "overall": overall,
            "checks": [r.to_dict() for r in results],
        })
    else:
        emit_text(_format_human(results, overall))

    if overall == doctor_module.STATUS_ERROR:
        return EXIT_INVALID_STATE
    return EXIT_OK


def _format_human(results, overall: str) -> str:
    rows = []
    for result in results:
        rows.append({
            "status": _status_glyph(result.status) + " " + result.status,
            "name": result.name,
            "group": result.group,
            "detail": result.detail,
        })
    table = render_table(
        rows,
        columns=[
            ("status", "STATUS"),
            ("name", "CHECK"),
            ("group", "GROUP"),
            ("detail", "DETAIL"),
        ],
    )
    fixes = [
        f"  {r.name}: {r.fix}"
        for r in results
        if r.status in (doctor_module.STATUS_WARN, doctor_module.STATUS_ERROR) and r.fix
    ]
    parts = [table, "", f"overall: {overall}"]
    if fixes:
        parts.extend(["", "fix hints:", *fixes])
    return "\n".join(parts)


def _status_glyph(status: str) -> str:
    return {
        doctor_module.STATUS_OK: "[OK]",
        doctor_module.STATUS_WARN: "[!!]",
        doctor_module.STATUS_ERROR: "[XX]",
        doctor_module.STATUS_INFO: "[--]",
    }.get(status, "[??]")

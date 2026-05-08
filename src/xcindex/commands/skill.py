from __future__ import annotations

import argparse

from xcindex import claude_skill
from xcindex.output import (
    EXIT_OK,
    EXIT_SYSTEM,
    emit_error,
    emit_json,
    emit_text,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "skill",
        help="Install/uninstall the Claude Code skill at user level (~/.claude/skills/).",
    )
    sub = parser.add_subparsers(dest="skill_action", metavar="ACTION")
    sub.required = True

    install_p = sub.add_parser(
        "install",
        help="Symlink the repo's SKILL.md into ~/.claude/skills/xcindex/.",
    )
    install_p.add_argument("--json", action="store_true", dest="json_mode")
    install_p.set_defaults(func=cmd_install)

    uninstall_p = sub.add_parser(
        "uninstall",
        help="Remove the Claude Code skill (only the symlink we manage).",
    )
    uninstall_p.add_argument("--json", action="store_true", dest="json_mode")
    uninstall_p.set_defaults(func=cmd_uninstall)

    status_p = sub.add_parser(
        "status",
        help="Show the Claude Code skill install state.",
    )
    status_p.add_argument("--json", action="store_true", dest="json_mode")
    status_p.set_defaults(func=cmd_status)


def cmd_install(args: argparse.Namespace) -> int:
    result = claude_skill.install()
    if not result.installed and result.skipped_reason:
        return emit_error(
            "skill_install_skipped",
            result.skipped_reason,
            json_mode=args.json_mode,
            exit_code=EXIT_SYSTEM,
        )
    if args.json_mode:
        emit_json(result.to_dict())
    else:
        verb = "replaced" if result.replaced_existing else "installed"
        emit_text(
            f"skill {verb}: {result.skill_file} -> {result.source_file}"
        )
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    result = claude_skill.uninstall()
    if args.json_mode:
        emit_json(result.to_dict())
    else:
        if result.removed:
            emit_text(f"skill removed: {result.skill_file}")
        elif result.skipped_unmanaged:
            emit_text(
                f"skill at {result.skill_file} is not the symlink we created; left untouched.\n"
                "Remove manually if you want a clean state."
            )
        else:
            emit_text("skill not installed; nothing to do")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    result = claude_skill.status()
    if args.json_mode:
        emit_json(result.to_dict())
    else:
        emit_text(
            f"skill_file:        {result.skill_file}\n"
            f"installed:         {result.installed}\n"
            f"is_symlink:        {result.is_symlink}\n"
            f"is_managed_symlink:{result.is_managed_symlink}\n"
            f"points_to:         {result.points_to if result.points_to else '-'}\n"
            f"source_file:       {result.source_file if result.source_file else '-'}"
        )
    return EXIT_OK

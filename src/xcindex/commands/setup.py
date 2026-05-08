from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from xcindex import cache as cache_module
from xcindex import claude_skill
from xcindex import doctor as doctor_module
from xcindex import helper as helper_module
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, EXIT_USAGE, emit_json, emit_text

XCINDEX_HOME = Path.home() / ".local" / "share" / "xcindex"
HELPER_BIN_DIR = XCINDEX_HOME / "bin"


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "setup",
        help="Install and uninstall the xcindex helper binary and caches.",
        description="Manage the xcindex helper installation.",
    )
    sub = parser.add_subparsers(dest="setup_command", metavar="SUBCOMMAND")

    install = sub.add_parser("install", help="Prepare directories and validate toolchain.")
    install.add_argument("--json", dest="json_mode", action="store_true",
                         help="Emit results as JSON.")
    install.add_argument("--skip-skill", action="store_true",
                         help="Don't prompt to install the Claude Code skill at user level.")
    install.add_argument("--with-skill", action="store_true",
                         help="Install the Claude Code skill without prompting (non-interactive).")
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="Remove caches and helper binary.")
    uninstall.add_argument("--json", dest="json_mode", action="store_true",
                           help="Emit results as JSON.")
    uninstall.set_defaults(func=cmd_uninstall)

    parser.set_defaults(func=lambda args: _print_help(parser))


def _print_help(parser) -> int:
    parser.print_help()
    return EXIT_USAGE


def cmd_install(args: argparse.Namespace) -> int:
    HELPER_BIN_DIR.mkdir(parents=True, exist_ok=True)
    cache_module.cache_root().mkdir(parents=True, exist_ok=True)

    swift_check = doctor_module.check_swift_toolchain()
    cache_check = doctor_module.check_cache_dir()

    helper_status = "ok"
    helper_detail: str | None = None
    helper_path: str | None = None
    if swift_check.status != doctor_module.STATUS_ERROR:
        try:
            binary = helper_module.ensure_helper(allow_build=True)
            info = helper_module.get_version(binary)
            helper_path = str(binary)
            helper_detail = (
                f"helper={info.helper_version} schema={info.schema_version} swift={info.swift_version}"
            )
        except helper_module.HelperError as exc:
            helper_status = "error"
            helper_detail = str(exc)
    else:
        helper_status = "skipped"
        helper_detail = "swift toolchain unavailable"

    skill_payload: dict | None = None
    if _resolve_skill_choice(args):
        skill_result = claude_skill.install()
        skill_payload = skill_result.to_dict()

    payload = {
        "helper_bin_dir": str(HELPER_BIN_DIR),
        "cache_root": str(cache_module.cache_root()),
        "helper": {
            "status": helper_status,
            "detail": helper_detail,
            "path": helper_path,
        },
        "checks": [swift_check.to_dict(), cache_check.to_dict()],
        "skill": skill_payload,
        "next_steps": [
            "run `xcindex doctor` to verify environment",
            "build your Xcode project to populate the IndexStore",
        ],
    }

    if args.json_mode:
        emit_json(payload)
    else:
        emit_text(f"created {HELPER_BIN_DIR}")
        emit_text(f"created {cache_module.cache_root()}")
        emit_text(f"  swift toolchain: {swift_check.status} — {swift_check.detail}")
        emit_text(f"  cache dir:       {cache_check.status} — {cache_check.detail}")
        emit_text(f"  helper:          {helper_status} — {helper_detail}")
        if skill_payload is not None:
            if skill_payload.get("installed"):
                verb = "replaced" if skill_payload.get("replaced_existing") else "installed"
                emit_text(f"  claude skill:    {verb} ({skill_payload['skill_file']})")
            elif skill_payload.get("skipped_reason"):
                emit_text(f"  claude skill:    skipped — {skill_payload['skipped_reason']}")
        emit_text("\nnext steps:")
        for step in payload["next_steps"]:
            emit_text(f"  - {step}")

    if swift_check.status == doctor_module.STATUS_ERROR or helper_status == "error":
        return EXIT_INVALID_STATE
    return EXIT_OK


def _resolve_skill_choice(args: argparse.Namespace) -> bool:
    if args.skip_skill:
        return False
    if not claude_skill.claude_code_present():
        return False
    if args.with_skill:
        return True
    if args.json_mode:
        return False
    if not sys.stdin.isatty():
        return False
    sys.stderr.write(
        "\nClaude Code detected. Install the xcindex skill at user level "
        "(~/.claude/skills/xcindex)? [Y/n] "
    )
    sys.stderr.flush()
    try:
        response = input().strip().lower()
    except EOFError:
        return False
    return response in ("", "y", "yes")


def cmd_uninstall(args: argparse.Namespace) -> int:
    removed: list[str] = []
    if XCINDEX_HOME.exists():
        shutil.rmtree(XCINDEX_HOME)
        removed.append(str(XCINDEX_HOME))
    if cache_module.cache_root().exists():
        shutil.rmtree(cache_module.cache_root())
        removed.append(str(cache_module.cache_root()))

    skill_result = claude_skill.uninstall()

    payload = {
        "removed": removed,
        "skill": skill_result.to_dict(),
        "note": "to uninstall the Python package itself, run: pipx uninstall xcindex",
    }
    if args.json_mode:
        emit_json(payload)
    else:
        if not removed:
            emit_text("nothing to remove.")
        else:
            for path in removed:
                emit_text(f"removed {path}")
        if skill_result.removed:
            emit_text(f"removed {skill_result.skill_file}")
        elif skill_result.skipped_unmanaged:
            emit_text(
                f"left untouched: {skill_result.skill_file} (not the symlink we created)"
            )
        emit_text(payload["note"])
    return EXIT_OK

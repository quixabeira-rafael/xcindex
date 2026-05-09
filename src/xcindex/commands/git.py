from __future__ import annotations

import argparse
from pathlib import Path

from xcindex import engine
from xcindex import git_diff as git_diff_module
from xcindex import query as query_module
from xcindex.commands._common import (
    add_output_arguments,
    annotate_with_context,
    handle_engine_error,
)
from xcindex.output import EXIT_INVALID_STATE, EXIT_OK, emit_error, emit_result


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "git",
        help="List symbols touched in the current branch and emit ready-to-paste impact/file commands.",
        description=(
            "Resolve files changed in `git diff <base>...HEAD` (or --staged) to "
            "the enclosing symbols at each modified line range, then emit "
            "shell-safe `xcindex impact` / `xcindex file` commands the agent "
            "can run to assess blast radius. Pairs git review workflows with "
            "the IndexStore."
        ),
    )
    parser.add_argument("base", nargs="?", default=None,
                        help="Git ref to diff against (default: origin/main → main → HEAD~1).")
    parser.add_argument("--staged", action="store_true",
                        help="Diff staged changes (`git diff --cached`) instead of branch-vs-base.")
    add_output_arguments(parser)
    engine.add_project_arguments(parser)
    parser.set_defaults(func=cmd_git, json_mode=False, level="locations")


def cmd_git(args: argparse.Namespace) -> int:
    try:
        with engine.open_context(args) as (ctx, conn):
            project_root: Path = ctx.project.root
            if not git_diff_module.is_git_repo(project_root):
                return emit_error(
                    "not_a_git_repo",
                    f"not inside a git working tree: {project_root}",
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )
            try:
                base = args.base or git_diff_module.detect_default_base(project_root)
                if not args.staged and not git_diff_module.ref_exists(base, project_root):
                    return emit_error(
                        "git_ref_not_found",
                        f"git ref not found: {base!r}",
                        json_mode=False, exit_code=EXIT_INVALID_STATE,
                    )
                changed = git_diff_module.list_changed_files(
                    base, project_root, staged=args.staged,
                )
            except git_diff_module.GitError as exc:
                return emit_error(
                    "git_error", str(exc),
                    json_mode=False, exit_code=EXIT_INVALID_STATE,
                )

            files_payload = []
            total_symbols = 0
            for cf in changed:
                if not git_diff_module.is_indexable(cf.path):
                    continue
                file_entry = _resolve_file_entry(
                    conn, base, project_root, cf, staged=args.staged,
                )
                files_payload.append(file_entry)
                total_symbols += len(file_entry["symbols"])

            by_status: dict[str, int] = {}
            for entry in files_payload:
                by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1

            canonical = {
                "kind": "git",
                "anchor": {
                    "base": base,
                    "head": "HEAD",
                    "staged": bool(args.staged),
                    "label": "staged changes" if args.staged
                              else git_diff_module.short_describe(base, project_root),
                },
                "summary": {
                    "found": bool(files_payload),
                    "count": total_symbols,
                    "files": len(files_payload),
                    "by_status": by_status,
                    "modified_symbols": total_symbols,
                },
                "files": files_payload,
                "truncated": False,
            }
            annotate_with_context(canonical, ctx)
    except engine.EngineError as exc:
        return handle_engine_error(exc)

    emit_result(canonical, level=args.level, fmt=args.output_format)
    return EXIT_OK


def _resolve_file_entry(
    conn,
    base: str,
    project_root: Path,
    cf: git_diff_module.ChangedFile,
    *,
    staged: bool,
) -> dict:
    """Build the per-file payload, resolving touched line ranges to symbols."""
    abs_path = (project_root / cf.path).resolve()
    entry = {
        "path": cf.path,
        "absolute_path": str(abs_path),
        "status": _status_label(cf.status),
        "old_path": cf.old_path,
        "symbols": [],
        "note": None,
    }
    if cf.status == "A":
        entry["note"] = "new file — not yet in the IndexStore; rebuild to index"
        return entry
    if cf.status == "D":
        entry["note"] = "file deleted — symbols may still exist in the cached index"
        return entry

    try:
        ranges = git_diff_module.list_modified_line_ranges(
            base, project_root, cf.path, staged=staged,
        )
    except git_diff_module.GitError:
        entry["note"] = "could not read diff for this file"
        return entry

    seen_usrs: set[str] = set()
    file_str = str(abs_path)
    for start, end in ranges:
        for line in range(start, end + 1):
            try:
                canonical = query_module.query_containing(conn, file_str, line)
            except Exception:
                continue
            if not canonical["summary"]["found"]:
                continue
            item = canonical["items"][0]
            usr = item.get("usr")
            if not usr or usr in seen_usrs:
                continue
            seen_usrs.add(usr)
            entry["symbols"].append({
                **item,
                "modified_range": [start, end],
            })
    return entry


def _status_label(status: str) -> str:
    return {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
    }.get(status, status)

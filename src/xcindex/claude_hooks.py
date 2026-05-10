"""Install/remove xcindex auto-hooks in ~/.claude/settings.json.

Three hooks coordinate the watch + GC lifecycle:

  SessionStart  → spawns `xcindex watch` in background (covers IDE + CLI builds)
  SessionEnd    → kills the watcher and runs `cache gc` (cleans worktree leaks)
  SubagentStop  → runs `cache gc` (catches worktree-derived caches early)

The installer is idempotent: each managed hook entry carries a literal
sentinel (`XCINDEX_MANAGED_HOOK`) inside its `command` so we can detect
existing entries on re-install and remove them on uninstall WITHOUT
clobbering the user's other hooks.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
SENTINEL = "XCINDEX_MANAGED_HOOK"


def _watch_command() -> str:
    return (
        "(nohup xcindex watch >/tmp/xcindex-watch.log 2>&1 &) "
        ">/dev/null 2>&1 || true  "
        f"# {SENTINEL}"
    )


def _session_end_command() -> str:
    return (
        "(for f in $HOME/.cache/xcindex/*/watch.json; do "
        "[ -f \"$f\" ] && kill $(python3 -c \"import json; print(json.load(open('$f'))['pid'])\" 2>/dev/null) 2>/dev/null; "
        "done; xcindex cache gc >/dev/null 2>&1) || true  "
        f"# {SENTINEL}"
    )


def _subagent_stop_command() -> str:
    return (
        "xcindex cache gc >/dev/null 2>&1 || true  "
        f"# {SENTINEL}"
    )


_HOOK_PLAN = (
    ("SessionStart", _watch_command),
    ("SessionEnd", _session_end_command),
    ("SubagentStop", _subagent_stop_command),
)


@dataclass(frozen=True)
class HooksStatus:
    """State of xcindex-managed entries in ~/.claude/settings.json."""
    settings_path: Path
    settings_exists: bool
    installed: dict[str, bool]   # event_name → True if our managed entry is present


@dataclass(frozen=True)
class InstallResult:
    settings_path: Path
    added: list[str]
    refreshed: list[str]
    skipped_no_claude_dir: bool


@dataclass(frozen=True)
class UninstallResult:
    settings_path: Path
    removed: list[str]
    not_found: list[str]


# --- Reading / writing the settings file ----------------------------------


def _read_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        text = SETTINGS_PATH.read_text()
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"~/.claude/settings.json is not valid JSON ({exc}); "
            "fix or remove it before running `xcindex setup hooks install`"
        )


def _write_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _entry_is_managed(entry: dict) -> bool:
    """An entry is xcindex-managed if any of its hooks contain the sentinel."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "") if isinstance(hook, dict) else ""
        if SENTINEL in cmd:
            return True
    return False


# --- Public API -------------------------------------------------------------


def claude_dir_present() -> bool:
    return (Path.home() / ".claude").is_dir()


def status() -> HooksStatus:
    settings = _read_settings() if SETTINGS_PATH.exists() else {}
    hooks_section = settings.get("hooks", {}) or {}
    installed: dict[str, bool] = {}
    for event, _ in _HOOK_PLAN:
        entries = hooks_section.get(event, []) or []
        installed[event] = any(_entry_is_managed(e) for e in entries if isinstance(e, dict))
    return HooksStatus(
        settings_path=SETTINGS_PATH,
        settings_exists=SETTINGS_PATH.exists(),
        installed=installed,
    )


def install() -> InstallResult:
    if not claude_dir_present():
        return InstallResult(
            settings_path=SETTINGS_PATH,
            added=[], refreshed=[],
            skipped_no_claude_dir=True,
        )
    settings = _read_settings()
    hooks_section = settings.setdefault("hooks", {})
    added: list[str] = []
    refreshed: list[str] = []

    for event, command_factory in _HOOK_PLAN:
        entries = hooks_section.setdefault(event, [])
        # Drop any pre-existing managed entry — guarantees idempotency.
        before = len(entries)
        entries[:] = [e for e in entries if not (isinstance(e, dict) and _entry_is_managed(e))]
        was_present = before != len(entries)

        new_entry = {
            "hooks": [{
                "type": "command",
                "command": command_factory(),
            }],
        }
        entries.append(new_entry)
        if was_present:
            refreshed.append(event)
        else:
            added.append(event)

    _write_settings(settings)
    return InstallResult(
        settings_path=SETTINGS_PATH,
        added=added, refreshed=refreshed,
        skipped_no_claude_dir=False,
    )


def uninstall() -> UninstallResult:
    if not SETTINGS_PATH.exists():
        return UninstallResult(
            settings_path=SETTINGS_PATH,
            removed=[],
            not_found=[event for event, _ in _HOOK_PLAN],
        )
    settings = _read_settings()
    hooks_section = settings.get("hooks", {}) or {}
    removed: list[str] = []
    not_found: list[str] = []

    for event, _ in _HOOK_PLAN:
        entries = hooks_section.get(event, []) or []
        before = len(entries)
        kept = [e for e in entries if not (isinstance(e, dict) and _entry_is_managed(e))]
        if len(kept) != before:
            removed.append(event)
            if kept:
                hooks_section[event] = kept
            else:
                hooks_section.pop(event, None)
        else:
            not_found.append(event)

    if hooks_section is settings.get("hooks") and not hooks_section:
        settings.pop("hooks", None)
    _write_settings(settings)
    return UninstallResult(
        settings_path=SETTINGS_PATH,
        removed=removed,
        not_found=not_found,
    )

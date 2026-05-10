"""Unit tests for the Claude Code hooks installer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xcindex import claude_hooks


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Pin SETTINGS_PATH to a tmp ~/.claude/settings.json."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    settings = claude_dir / "settings.json"
    monkeypatch.setattr(claude_hooks, "SETTINGS_PATH", settings)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _write_settings(home: Path, payload: dict) -> None:
    (home / ".claude" / "settings.json").write_text(json.dumps(payload, indent=2))


def _read_settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text())


# --- claude_dir_present -----------------------------------------------------

def test_claude_dir_present_true(fake_home):
    assert claude_hooks.claude_dir_present() is True


def test_claude_dir_present_false(tmp_path: Path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(Path, "home", lambda: bare)
    assert claude_hooks.claude_dir_present() is False


# --- status -----------------------------------------------------------------

def test_status_when_no_settings_file(fake_home):
    state = claude_hooks.status()
    assert state.settings_exists is False
    assert all(not v for v in state.installed.values())


def test_status_when_no_managed_entries(fake_home):
    _write_settings(fake_home, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}]}})
    state = claude_hooks.status()
    assert state.settings_exists is True
    assert state.installed["SessionStart"] is False


def test_status_detects_managed_entries(fake_home):
    _write_settings(fake_home, {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": f"echo nada # {claude_hooks.SENTINEL}"}]},
            ],
        }
    })
    state = claude_hooks.status()
    assert state.installed["SessionStart"] is True
    assert state.installed["SessionEnd"] is False


# --- install ----------------------------------------------------------------

def test_install_creates_settings_with_three_hooks(fake_home):
    result = claude_hooks.install()
    assert result.skipped_no_claude_dir is False
    assert set(result.added) == {"SessionStart", "SessionEnd", "SubagentStop"}
    assert not result.refreshed

    settings = _read_settings(fake_home)
    for event in ("SessionStart", "SessionEnd", "SubagentStop"):
        entries = settings["hooks"][event]
        assert any(claude_hooks.SENTINEL in h["command"]
                   for entry in entries for h in entry["hooks"])


def test_install_idempotent(fake_home):
    claude_hooks.install()
    result2 = claude_hooks.install()
    assert set(result2.refreshed) == {"SessionStart", "SessionEnd", "SubagentStop"}
    assert not result2.added

    # No duplicate managed entries
    settings = _read_settings(fake_home)
    for event in ("SessionStart", "SessionEnd", "SubagentStop"):
        entries = settings["hooks"][event]
        managed_count = sum(
            1 for entry in entries for h in entry["hooks"]
            if claude_hooks.SENTINEL in h.get("command", "")
        )
        assert managed_count == 1


def test_install_preserves_user_hooks(fake_home):
    user_entry = {
        "hooks": [{"type": "command", "command": "echo user-script"}],
    }
    _write_settings(fake_home, {"hooks": {"SessionStart": [user_entry]}})

    claude_hooks.install()
    settings = _read_settings(fake_home)
    user_commands = [
        h["command"]
        for entry in settings["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    assert "echo user-script" in user_commands
    assert any(claude_hooks.SENTINEL in c for c in user_commands)


def test_install_skipped_when_no_claude_dir(tmp_path: Path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(Path, "home", lambda: bare)
    monkeypatch.setattr(claude_hooks, "SETTINGS_PATH", bare / ".claude" / "settings.json")
    result = claude_hooks.install()
    assert result.skipped_no_claude_dir is True
    assert result.added == []


def test_install_raises_on_invalid_json(fake_home):
    (fake_home / ".claude" / "settings.json").write_text("{ broken")
    with pytest.raises(RuntimeError) as exc_info:
        claude_hooks.install()
    assert "valid JSON" in str(exc_info.value)


# --- uninstall --------------------------------------------------------------

def test_uninstall_removes_managed_entries(fake_home):
    claude_hooks.install()
    result = claude_hooks.uninstall()
    assert set(result.removed) == {"SessionStart", "SessionEnd", "SubagentStop"}
    assert not result.not_found

    settings = _read_settings(fake_home)
    # hooks key may have been removed (empty)
    hooks = settings.get("hooks", {})
    for event in ("SessionStart", "SessionEnd", "SubagentStop"):
        entries = hooks.get(event, [])
        for entry in entries:
            for h in entry.get("hooks", []):
                assert claude_hooks.SENTINEL not in h.get("command", "")


def test_uninstall_preserves_user_hooks(fake_home):
    user_entry = {
        "hooks": [{"type": "command", "command": "echo user-script"}],
    }
    _write_settings(fake_home, {"hooks": {"SessionStart": [user_entry]}})
    claude_hooks.install()
    claude_hooks.uninstall()

    settings = _read_settings(fake_home)
    entries = settings["hooks"]["SessionStart"]
    user_commands = [h["command"] for e in entries for h in e["hooks"]]
    assert "echo user-script" in user_commands
    assert not any(claude_hooks.SENTINEL in c for c in user_commands)


def test_uninstall_when_settings_absent(fake_home):
    """No settings.json — nothing to remove."""
    result = claude_hooks.uninstall()
    assert result.removed == []
    assert set(result.not_found) == {"SessionStart", "SessionEnd", "SubagentStop"}


def test_uninstall_when_no_managed_entries(fake_home):
    _write_settings(fake_home, {"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo other"}]}
    ]}})
    result = claude_hooks.uninstall()
    assert result.removed == []
    assert "SessionStart" in result.not_found


# --- Round-trip integration -------------------------------------------------

def test_install_uninstall_round_trip_leaves_user_settings_unchanged(fake_home):
    user_data = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo A"}]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo B"}]}],
        },
        "otherKey": "preserved",
    }
    _write_settings(fake_home, user_data)
    claude_hooks.install()
    claude_hooks.uninstall()

    final = _read_settings(fake_home)
    # User entries preserved
    assert final["otherKey"] == "preserved"
    assert final["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo B"
    # SessionStart still has user's entry
    session_start_commands = [
        h["command"] for entry in final["hooks"]["SessionStart"] for h in entry["hooks"]
    ]
    assert "echo A" in session_start_commands
    assert not any(claude_hooks.SENTINEL in c for c in session_start_commands)

"""Unit tests for the prewarm subcommand wiring."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from xcindex import discovery
from xcindex import engine
from xcindex.commands import prewarm as prewarm_command


def _xcindex(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         *args],
        capture_output=True,
        text=True,
    )


def _stub_result(mode="cold", **overrides) -> engine.MaterializationResult:
    project = discovery.ProjectInfo(
        path=Path("/abs/Package.swift"), name="Stub",
        kind="swiftpm", root=Path("/abs"),
    )
    base = {
        "mode": mode,
        "project": project,
        "index_store": Path("/abs/store"),
        "sqlite_path": Path("/cache/index.sqlite"),
        "index_hash": "deadbeef",
        "wall_seconds": 1.234,
        "symbols_added": 100,
        "occurrences_added": 200,
        "relations_added": 300,
        "units_modified": 0,
        "units_removed": 0,
        "units_added": 0,
    }
    base.update(overrides)
    return engine.MaterializationResult(**base)


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "project": None,
        "index_store": None,
        "derived_data": None,
        "include_system": False,
        "quiet": False,
        "allow_build": True,
        "level": "summary",
        "output_format": "agent",
        "limit": 50,
        "include_raw": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- Help / wiring ----------------------------------------------------------

def test_prewarm_subcommand_help_works():
    proc = _xcindex("prewarm", "--help")
    assert proc.returncode == 0
    assert "prewarm" in proc.stdout.lower()
    assert "--quiet" in proc.stdout
    assert "--no-build-helper" in proc.stdout


def test_prewarm_does_not_register_check_fresh():
    """Freshness flags don't apply — argparse should reject them."""
    proc = _xcindex("prewarm", "--check-fresh")
    assert proc.returncode != 0
    proc = _xcindex("prewarm", "--require-fresh")
    assert proc.returncode != 0


def test_prewarm_no_project_returns_invalid_state(tmp_path: Path):
    """Outside a project, prewarm should fail with engine error → invalid state."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "could not discover project" in proc.stderr or "engine_error" in proc.stderr


# --- Output formats with mocked materialize() ------------------------------

def test_prewarm_default_text_for_cold():
    args = _make_args()
    with patch.object(engine, "materialize", return_value=_stub_result(mode="cold")):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    text = emit_mock.call_args[0][0]
    assert text.startswith("bootstrapped")
    assert "100 symbols" in text
    assert "(1.2s)" in text


def test_prewarm_default_text_for_noop():
    args = _make_args()
    with patch.object(engine, "materialize", return_value=_stub_result(mode="noop", symbols_added=0, occurrences_added=0, relations_added=0, wall_seconds=0.05)):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    text = emit_mock.call_args[0][0]
    assert "cache up to date" in text


def test_prewarm_quiet_silences_noop():
    args = _make_args(quiet=True)
    with patch.object(engine, "materialize", return_value=_stub_result(mode="noop")):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    emit_mock.assert_not_called()


def test_prewarm_quiet_still_prints_on_cold():
    args = _make_args(quiet=True)
    with patch.object(engine, "materialize", return_value=_stub_result(mode="cold")):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    emit_mock.assert_called_once()


def test_prewarm_text_for_incremental():
    args = _make_args()
    result = _stub_result(
        mode="incremental",
        symbols_added=5, occurrences_added=12, relations_added=7,
        units_modified=3, units_removed=1, wall_seconds=0.8,
    )
    with patch.object(engine, "materialize", return_value=result):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    text = emit_mock.call_args[0][0]
    assert "incremental" in text
    assert "3 modified" in text
    assert "1 removed" in text
    assert "+5 symbols" in text


def test_prewarm_text_for_schema_upgrade():
    args = _make_args()
    result = _stub_result(mode="schema_upgrade", wall_seconds=12.3)
    with patch.object(engine, "materialize", return_value=result):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    text = emit_mock.call_args[0][0]
    assert "schema upgraded" in text
    assert "(12.3s)" in text


def test_prewarm_text_marks_units_added():
    args = _make_args()
    result = _stub_result(mode="cold", units_added=5)
    with patch.object(engine, "materialize", return_value=result):
        with patch("xcindex.commands.prewarm.emit_text") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    text = emit_mock.call_args[0][0]
    assert "5 new unit" in text


# --- JSON shape -------------------------------------------------------------

def test_prewarm_json_shape_is_stable():
    args = _make_args(output_format="json")
    result = _stub_result(mode="cold")
    with patch.object(engine, "materialize", return_value=result):
        with patch("xcindex.commands.prewarm.emit_result") as emit_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 0
    canonical = emit_mock.call_args[0][0]
    assert canonical["kind"] == "prewarm"
    assert canonical["summary"]["mode"] == "cold"
    assert canonical["summary"]["symbols_added"] == 100
    assert canonical["summary"]["wall_seconds"] == 1.234
    assert canonical["anchor"]["index_hash"] == "deadbeef"


def test_prewarm_canonical_contains_anchor_paths():
    result = _stub_result()
    canonical = prewarm_command._build_canonical(result)
    assert "project" in canonical["anchor"]
    assert "sqlite" in canonical["anchor"]


# --- allow_build wiring -----------------------------------------------------

def test_prewarm_passes_allow_build_false_when_no_build_helper():
    args = _make_args(allow_build=False)
    captured = {}
    def _fake(args, *, allow_build=True):
        captured["allow_build"] = allow_build
        return _stub_result()
    with patch.object(engine, "materialize", side_effect=_fake):
        with patch("xcindex.commands.prewarm.emit_text"):
            prewarm_command.cmd_prewarm(args)
    assert captured["allow_build"] is False


# --- engine error handling --------------------------------------------------

def test_prewarm_engine_error_returns_invalid_state():
    args = _make_args()
    with patch.object(engine, "materialize", side_effect=engine.EngineError("boom")):
        with patch("xcindex.commands.prewarm.handle_engine_error", return_value=2) as handler_mock:
            rc = prewarm_command.cmd_prewarm(args)
    assert rc == 2
    handler_mock.assert_called_once()

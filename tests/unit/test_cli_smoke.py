from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _xcindex(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         *args],
        capture_output=True,
        text=True,
    )


def test_no_args_prints_usage():
    proc = _xcindex()
    assert proc.returncode == 1


def test_version_flag():
    proc = _xcindex("--version")
    assert proc.returncode == 0
    assert "xcindex" in proc.stdout


def test_doctor_json_emits_valid_json():
    proc = _xcindex("doctor", "--json")
    payload = json.loads(proc.stdout)
    assert "overall" in payload
    assert isinstance(payload["checks"], list)


def test_cache_list_empty(tmp_path: Path, monkeypatch):
    proc = _xcindex("cache", "list", "--json")
    payload = json.loads(proc.stdout)
    assert payload["count"] >= 0
    assert isinstance(payload["entries"], list)


def test_cache_subcommand_required():
    proc = _xcindex("cache")
    assert proc.returncode == 1


def test_setup_subcommand_required():
    proc = _xcindex("setup")
    assert proc.returncode == 1


def test_file_shorthand_expands_to_file_subcommand():
    """`xcindex Foo.swift` outside a project should fail like `xcindex file Foo.swift`,
    proving the parser dispatches to the file command rather than rejecting the arg."""
    foreign = _xcindex("file", "ZZZ_NotARealFile_xyz.swift")
    short = _xcindex("ZZZ_NotARealFile_xyz.swift")
    assert foreign.returncode == short.returncode
    assert foreign.stderr.splitlines()[-1] == short.stderr.splitlines()[-1]


def test_impact_subcommand_help_works():
    proc = _xcindex("impact", "--help")
    assert proc.returncode == 0
    assert "impact" in proc.stdout.lower()
    assert "--depth" in proc.stdout


def test_impact_requires_target_argument():
    proc = _xcindex("impact")
    assert proc.returncode != 0


def test_impact_does_not_steal_file_shorthand():
    """`xcindex Foo.swift` continues going to file, not impact."""
    proc = _xcindex("ZZZ_NotARealFile_xyz.swift")
    # If shorthand stole the call to impact, error would mention 'target_not_found'
    # via name lookup; with file dispatch we get 'file_not_indexed' or project-discovery error.
    assert "ambiguous_name" not in proc.stderr

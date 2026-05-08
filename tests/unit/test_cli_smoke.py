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

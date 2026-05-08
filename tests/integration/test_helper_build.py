from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from xcindex import helper

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_SOURCE = REPO_ROOT / "swift-helper"
PREBUILT_BINARY = HELPER_SOURCE / ".build" / "release" / "xcindex-helper"


@pytest.fixture(scope="session")
def built_helper() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    if not PREBUILT_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(HELPER_SOURCE),
            check=True,
            timeout=600,
        )
    assert PREBUILT_BINARY.is_file(), "helper binary was not produced"
    return PREBUILT_BINARY


def test_helper_version_emits_valid_json(built_helper: Path):
    result = subprocess.run(
        [str(built_helper), "version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["helper_version"] == "0.1.0"
    assert payload["schema_version"] == helper.EXPECTED_SCHEMA_VERSION
    assert "swift" in payload["swift_version"].lower() or payload["swift_version"]


def test_get_version_via_python_api(built_helper: Path):
    info = helper.get_version(built_helper)
    assert info.helper_version == "0.1.0"
    assert info.schema_version == helper.EXPECTED_SCHEMA_VERSION
    assert info.binary_path == built_helper


def test_helper_dump_rejects_missing_args(built_helper: Path):
    result = subprocess.run(
        [str(built_helper), "dump"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    payload = json.loads(result.stderr)
    assert payload["error"] == "usage"


def test_helper_unknown_command_returns_error(built_helper: Path):
    result = subprocess.run(
        [str(built_helper), "bogus"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    payload = json.loads(result.stderr)
    assert payload["error"] == "usage"

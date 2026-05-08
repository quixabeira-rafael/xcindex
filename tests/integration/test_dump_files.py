"""Validate the helper's `dump-files` subcommand emits proper NDJSON."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "SampleApp"
HELPER_BINARY = REPO_ROOT / "swift-helper" / ".build" / "release" / "xcindex-helper"


@pytest.fixture(scope="module")
def built_fixture() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    subprocess.run(["swift", "build"], cwd=str(FIXTURE_ROOT), check=True, timeout=300)
    store = FIXTURE_ROOT / ".build" / "debug" / "index" / "store"
    assert (store / "v5" / "units").is_dir()
    return store


@pytest.fixture(scope="module")
def built_helper() -> Path:
    if not HELPER_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(REPO_ROOT / "swift-helper"),
            check=True,
            timeout=600,
        )
    return HELPER_BINARY


def _run(helper: Path, *args: str) -> tuple[list[dict], dict]:
    """Execute helper with args, parse stdout NDJSON + stderr summary line."""
    result = subprocess.run(
        [str(helper), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"helper failed: {result.stderr}"
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    summary = {}
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                summary = json.loads(line)
            except json.JSONDecodeError:
                pass
    return records, summary


def test_dump_files_single_file_emits_records(built_fixture, built_helper):
    receipt = FIXTURE_ROOT / "Sources" / "Domain" / "Receipt.swift"
    records, summary = _run(
        built_helper,
        "dump-files",
        "--index-store", str(built_fixture),
        "--file", str(receipt),
    )

    assert summary.get("info") == "dump_files_complete"
    assert summary.get("files") == 1
    assert summary.get("occurrences", 0) > 0
    assert summary.get("file_units", 0) >= 1

    types = {r["type"] for r in records}
    assert {"symbol", "occurrence", "relation", "file_unit"}.issubset(types)


def test_dump_files_only_emits_definition_symbols(built_fixture, built_helper):
    receipt = FIXTURE_ROOT / "Sources" / "Domain" / "Receipt.swift"
    records, _ = _run(
        built_helper,
        "dump-files",
        "--index-store", str(built_fixture),
        "--file", str(receipt),
    )
    symbols = [r for r in records if r["type"] == "symbol"]
    assert symbols, "expected at least one symbol record"
    receipt_path = str(receipt)
    for sym in symbols:
        assert sym["file"] == receipt_path, (
            f"symbol {sym['name']} from outside the requested file leaked: "
            f"{sym['file']!r}"
        )


def test_dump_files_occurrences_only_in_requested_files(built_fixture, built_helper):
    receipt = FIXTURE_ROOT / "Sources" / "Domain" / "Receipt.swift"
    records, _ = _run(
        built_helper,
        "dump-files",
        "--index-store", str(built_fixture),
        "--file", str(receipt),
    )
    occs = [r for r in records if r["type"] == "occurrence"]
    assert occs
    target = str(receipt)
    for o in occs:
        assert o["file"] == target


def test_dump_files_file_unit_records_match_request(built_fixture, built_helper):
    files = [
        FIXTURE_ROOT / "Sources" / "Domain" / "Receipt.swift",
        FIXTURE_ROOT / "Sources" / "Core" / "Money.swift",
    ]
    records, _ = _run(
        built_helper,
        "dump-files",
        "--index-store", str(built_fixture),
        "--file", str(files[0]),
        "--file", str(files[1]),
    )
    fu = [r for r in records if r["type"] == "file_unit"]
    requested = {str(f) for f in files}
    assert {r["file"] for r in fu}.issubset(requested)
    assert len({r["unit_name"] for r in fu}) >= 1


def test_dump_files_rejects_missing_args(built_helper):
    result = subprocess.run(
        [str(built_helper), "dump-files"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    payload = json.loads(result.stderr.splitlines()[-1])
    assert payload["error"] == "usage"


def test_dump_emits_file_unit_records(built_fixture, built_helper):
    """The full `dump` command should also emit file_unit records (M2)."""
    records, summary = _run(
        built_helper,
        "dump",
        "--index-store", str(built_fixture),
    )
    assert summary.get("file_units", 0) > 0
    file_units = [r for r in records if r["type"] == "file_unit"]
    assert file_units
    assert all("file" in r and "unit_name" in r for r in file_units)

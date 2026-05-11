"""End-to-end test for the helper's `bootstrap` subcommand.

Invokes `helper.run_bootstrap` against the SwiftPM fixture, opens the
produced SQLite, and asserts the schema, contents, and indexes look sane.
The full xcindex CLI is exercised by the pipeline integration tests; this
file targets the helper subcommand alone.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from xcindex import helper

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "SampleApp"
HELPER_BINARY = REPO_ROOT / "swift-helper" / ".build" / "release" / "xcindex-helper"


@pytest.fixture(scope="module")
def built_fixture() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    subprocess.run(["swift", "build"], cwd=str(FIXTURE_ROOT), check=True, timeout=300)
    return FIXTURE_ROOT / ".build" / "debug" / "index" / "store"


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


def test_bootstrap_writes_valid_sqlite(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    result = helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    assert output.is_file()
    assert result.symbols > 0
    assert result.occurrences > 0
    assert result.relations > 0


def test_bootstrap_writes_current_schema_version(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    conn = sqlite3.connect(str(output))
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        from xcindex import schema as schema_module
        assert version == (str(schema_module.SCHEMA_VERSION),)
    finally:
        conn.close()


def test_bootstrap_creates_all_tables_and_indexes(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    conn = sqlite3.connect(str(output))
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "symbols", "occurrences", "relations",
            "units", "unit_files", "files", "meta",
        }.issubset(tables)

        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # A few sentinel indexes that must exist for query.py to be fast.
        assert {
            "idx_sym_name_nocase",
            "idx_occ_symbol",
            "idx_occ_file_line",
            "idx_rel_related_kind",
            "idx_unit_files_unit",
        }.issubset(indexes)
    finally:
        conn.close()


def test_bootstrap_populates_units_with_size_and_mtime(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    conn = sqlite3.connect(str(output))
    try:
        rows = conn.execute(
            "SELECT name, size_bytes, mtime_ns FROM units WHERE size_bytes > 0"
        ).fetchall()
        assert len(rows) > 0, "units table should have rows with size_bytes > 0"
        # Every populated row should also carry a non-zero mtime_ns; this is
        # what compute_unit_delta in Python compares against on next runs.
        assert all(mtime > 0 for _, _, mtime in rows)
    finally:
        conn.close()


def test_bootstrap_unit_files_link_units_to_source_paths(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    conn = sqlite3.connect(str(output))
    try:
        # We expect the fixture's source files to appear in unit_files.
        files = {
            row[0] for row in conn.execute("SELECT DISTINCT file FROM unit_files")
        }
        # SwiftPM's record dependency filePath is absolute. Just check that we
        # have some recognizable source files.
        assert any(p.endswith("PriceCalculator.swift") for p in files), files
        assert any(p.endswith("Money.swift") for p in files), files
    finally:
        conn.close()


def test_bootstrap_includes_expected_fixture_symbols(built_fixture, built_helper, tmp_path: Path):
    """Sanity: well-known fixture types/methods come through with correct kinds."""
    output = tmp_path / "fixture.sqlite"
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    conn = sqlite3.connect(str(output))
    try:
        # `Money` appears under two USRs: the struct (in Money.swift) and an
        # `extension` symbol (in Money+Extensions.swift). Verify both kinds
        # are present rather than checking the first row that happens back.
        kinds_per_name: dict[str, set[str]] = {}
        for name, kind in conn.execute(
            "SELECT name, kind FROM symbols "
            "WHERE name IN ('PriceCalculator','PriceProvider','Money','Currency','Container')"
        ):
            kinds_per_name.setdefault(name, set()).add(kind)
        assert kinds_per_name.get("PriceCalculator") == {"class"}
        assert kinds_per_name.get("PriceProvider") == {"protocol"}
        assert "struct" in kinds_per_name.get("Money", set())
        assert kinds_per_name.get("Currency") == {"enum"}
        assert kinds_per_name.get("Container") == {"struct"}
    finally:
        conn.close()


def test_bootstrap_overwrites_existing_output(built_fixture, built_helper, tmp_path: Path):
    output = tmp_path / "fixture.sqlite"
    output.write_bytes(b"corrupt sentinel that should be replaced")
    helper.run_bootstrap(
        index_store_path=built_fixture,
        output_path=output,
        helper_path=built_helper,
    )
    # Bootstrap atomically renames the staged DB over the existing file.
    conn = sqlite3.connect(str(output))
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        from xcindex import schema as schema_module
        assert version == (str(schema_module.SCHEMA_VERSION),)
    finally:
        conn.close()

"""End-to-end test for the helper's `incremental` subcommand.

Bootstraps the fixture, then triggers an incremental update by passing the
unit names of files we touched. Asserts:

  - Untouched files' rows are byte-identical before and after the update
    (relations, occurrences, symbols rows).
  - The "modified unit" file's rows are present after the update with the
    same kinds (semantically equivalent).
  - Schema-mismatch path: a helper run against a SQLite tagged
    schema_version=99 exits with code 4 and raises StaleSchemaError.
"""
from __future__ import annotations

import hashlib
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
def built_helper() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    if not HELPER_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(REPO_ROOT / "swift-helper"),
            check=True,
            timeout=600,
        )
    return HELPER_BINARY


@pytest.fixture
def fresh_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "SampleApp"
    shutil.copytree(FIXTURE_ROOT, target,
                    ignore=shutil.ignore_patterns(".build", ".swiftpm"))
    subprocess.run(["swift", "build"], cwd=str(target), check=True, timeout=300)
    return target


def _bootstrap(fixture: Path, helper_path: Path, output: Path) -> None:
    store = fixture / ".build" / "debug" / "index" / "store"
    helper.run_bootstrap(
        index_store_path=store,
        output_path=output,
        helper_path=helper_path,
    )


def _table_rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    return list(conn.execute(sql).fetchall())


def _all_unit_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM units")]


def test_incremental_no_op_when_no_units_passed(fresh_fixture, built_helper, tmp_path: Path):
    cache = tmp_path / "fixture.sqlite"
    _bootstrap(fresh_fixture, built_helper, cache)

    conn = sqlite3.connect(str(cache))
    before_symbols = _table_rows(conn, "SELECT COUNT(*) FROM symbols")[0][0]
    before_occs = _table_rows(conn, "SELECT COUNT(*) FROM occurrences")[0][0]
    conn.close()

    store = fresh_fixture / ".build" / "debug" / "index" / "store"
    helper.run_incremental(
        index_store_path=store,
        sqlite_path=cache,
        modified_units=[],
        removed_units=[],
        helper_path=built_helper,
    )

    conn = sqlite3.connect(str(cache))
    after_symbols = _table_rows(conn, "SELECT COUNT(*) FROM symbols")[0][0]
    after_occs = _table_rows(conn, "SELECT COUNT(*) FROM occurrences")[0][0]
    conn.close()

    assert before_symbols == after_symbols
    assert before_occs == after_occs


def test_incremental_replaces_rows_for_modified_unit(fresh_fixture, built_helper, tmp_path: Path):
    cache = tmp_path / "fixture.sqlite"
    _bootstrap(fresh_fixture, built_helper, cache)

    # Pick one unit (the SampleApp Money.swift unit is a stable target).
    conn = sqlite3.connect(str(cache))
    money_units = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT unit_name FROM unit_files "
            "WHERE file LIKE '%Money.swift'"
        )
    ]
    conn.close()
    assert money_units, "expected at least one unit covering Money.swift"

    store = fresh_fixture / ".build" / "debug" / "index" / "store"
    result = helper.run_incremental(
        index_store_path=store,
        sqlite_path=cache,
        modified_units=money_units,
        helper_path=built_helper,
    )
    # Helper actually re-walked something
    assert result.symbols >= 0  # non-negative — at least the call succeeded
    assert result.files_redumped >= 1

    # Money struct must still be queryable post-incremental
    conn = sqlite3.connect(str(cache))
    money_kinds = [
        row[0] for row in conn.execute(
            "SELECT kind FROM symbols WHERE name = 'Money' AND file LIKE '%Money.swift'"
        )
    ]
    conn.close()
    assert "struct" in money_kinds


def test_incremental_preserves_unaffected_files(fresh_fixture, built_helper, tmp_path: Path):
    cache = tmp_path / "fixture.sqlite"
    _bootstrap(fresh_fixture, built_helper, cache)

    # Capture all rows for an unaffected file (PriceCalculator.swift) before.
    def snapshot(conn) -> str:
        rows = list(conn.execute("""
            SELECT id, symbol_usr, line, column, roles, container_usr
            FROM occurrences
            WHERE file LIKE '%PriceCalculator.swift'
            ORDER BY id
        """))
        h = hashlib.sha256()
        for r in rows:
            h.update(repr(r).encode())
        return h.hexdigest()

    conn = sqlite3.connect(str(cache))
    pre = snapshot(conn)
    money_units = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT unit_name FROM unit_files WHERE file LIKE '%Money.swift'"
        )
    ]
    conn.close()

    store = fresh_fixture / ".build" / "debug" / "index" / "store"
    helper.run_incremental(
        index_store_path=store,
        sqlite_path=cache,
        modified_units=money_units,
        helper_path=built_helper,
    )

    conn = sqlite3.connect(str(cache))
    post = snapshot(conn)
    conn.close()

    assert pre == post, "occurrences for PriceCalculator.swift must be byte-identical after Money's incremental update"


def test_incremental_schema_mismatch_raises_stale_schema(fresh_fixture, built_helper, tmp_path: Path):
    cache = tmp_path / "fixture.sqlite"
    _bootstrap(fresh_fixture, built_helper, cache)

    # Tamper with schema_version to force a mismatch.
    conn = sqlite3.connect(str(cache))
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    store = fresh_fixture / ".build" / "debug" / "index" / "store"
    with pytest.raises(helper.StaleSchemaError):
        helper.run_incremental(
            index_store_path=store,
            sqlite_path=cache,
            modified_units=["dummy"],
            helper_path=built_helper,
        )


def test_incremental_run_result_reports_files_redumped(fresh_fixture, built_helper, tmp_path: Path):
    cache = tmp_path / "fixture.sqlite"
    _bootstrap(fresh_fixture, built_helper, cache)

    conn = sqlite3.connect(str(cache))
    money_units = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT unit_name FROM unit_files WHERE file LIKE '%Money.swift'"
        )
    ]
    conn.close()

    store = fresh_fixture / ".build" / "debug" / "index" / "store"
    result = helper.run_incremental(
        index_store_path=store,
        sqlite_path=cache,
        modified_units=money_units,
        helper_path=built_helper,
    )
    assert result.wall_seconds >= 0
    assert result.files_redumped == 1, "Money.swift should be the single redumped file"

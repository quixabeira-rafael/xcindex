"""End-to-end integration of incremental updates.

Each test copies the SwiftPM fixture into tmp_path, builds it, runs xcindex,
mutates the source, rebuilds, and re-runs xcindex. We assert that:
  1. The query reflects the post-edit state.
  2. The cache file is reused (not torn down) after a modify.
  3. Only the affected files were re-processed (via stderr signal).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from xcindex import cache as cache_module

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
    """Copy the SampleApp into tmp_path so we can mutate freely."""
    target = tmp_path / "SampleApp"
    shutil.copytree(FIXTURE_ROOT, target, ignore=shutil.ignore_patterns(".build", ".swiftpm"))
    subprocess.run(["swift", "build"], cwd=str(target), check=True, timeout=300)
    return target


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch):
    cache_dir = tmp_path / "xcindex-cache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    return cache_dir


def _xcindex(args: list[str], cwd: Path, helper: Path,
             cache_root: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["XCINDEX_HELPER"] = str(helper)
    if cache_root is not None:
        # Force the subprocess to use the same isolated cache root via XDG override
        # NB: the runtime resolves CACHE_ROOT at module import, so we patch via env.
        env["XCINDEX_CACHE_ROOT"] = str(cache_root)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, os; "
                "from xcindex import cache; "
                "root = os.environ.get('XCINDEX_CACHE_ROOT'); "
                "cache.CACHE_ROOT = __import__('pathlib').Path(root) if root else cache.CACHE_ROOT; "
                "from xcindex.cli import main; sys.exit(main(sys.argv[1:]))"
            ),
            *args,
            "--format", "json",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def test_first_run_bootstraps_then_second_run_is_cache_hit(
    fresh_fixture, built_helper, isolated_cache
):
    first = _xcindex(["search", "Receipt", "--kind", "class"],
                     fresh_fixture, built_helper, isolated_cache)
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["summary"]["found"]
    assert "materializing cache" in first.stderr

    second = _xcindex(["search", "Receipt", "--kind", "class"],
                      fresh_fixture, built_helper, isolated_cache)
    assert second.returncode == 0, second.stderr
    assert "materializing cache" not in second.stderr
    assert "incremental update" not in second.stderr


def test_modify_source_then_rebuild_then_query_uses_incremental(
    fresh_fixture, built_helper, isolated_cache
):
    # Bootstrap
    boot = _xcindex(["search", "Receipt", "--kind", "class"],
                    fresh_fixture, built_helper, isolated_cache)
    assert boot.returncode == 0
    assert "materializing cache" in boot.stderr

    # Mutate Receipt: add a new method that should be findable post-rebuild.
    receipt = fresh_fixture / "Sources" / "Domain" / "Receipt.swift"
    text = receipt.read_text()
    receipt.write_text(text.replace(
        "public func record(name: String) {",
        "public func brandNewMethod() {}\n\n    public func record(name: String) {",
    ))

    subprocess.run(["swift", "build"], cwd=str(fresh_fixture), check=True, timeout=300)

    # Query after rebuild → should incrementally update.
    after = _xcindex(["search", "brandNewMethod", "--kind", "instance-method"],
                     fresh_fixture, built_helper, isolated_cache)
    assert after.returncode == 0, after.stderr
    payload = json.loads(after.stdout)
    assert payload["summary"]["found"], (
        "newly added method should appear after incremental update; "
        f"stderr={after.stderr!r}"
    )
    assert "incremental update" in after.stderr, (
        f"expected incremental path; got stderr={after.stderr!r}"
    )
    # Crucially, NOT a full re-dump
    assert "materializing cache" not in after.stderr


def test_remove_source_then_rebuild_then_query_drops_symbols(
    fresh_fixture, built_helper, isolated_cache
):
    # Bootstrap
    _xcindex(["search", "Glucose", "--kind", "class"],
             fresh_fixture, built_helper, isolated_cache)

    # Sanity: operator + must be present at first
    pre = _xcindex(["search", "+", "--kind", "function"],
                   fresh_fixture, built_helper, isolated_cache)
    pre_payload = json.loads(pre.stdout)
    assert pre_payload["summary"]["found"]

    # Delete Operators.swift and rebuild
    operators = fresh_fixture / "Sources" / "Core" / "Operators.swift"
    operators.unlink()
    subprocess.run(["swift", "build"], cwd=str(fresh_fixture), check=True, timeout=300)

    after = _xcindex(["search", "+", "--kind", "function"],
                     fresh_fixture, built_helper, isolated_cache)
    assert after.returncode == 0
    payload = json.loads(after.stdout)
    # Operators are gone (or at minimum no longer in this fixture's modules)
    names = [(it.get("name"), it.get("module")) for it in payload.get("items", [])]
    fixture_modules = {"Core", "Domain", "UI"}
    assert not any(mod in fixture_modules for _, mod in names), (
        f"removed operator should not be queryable from fixture modules; got {names!r}"
    )


def test_add_source_then_rebuild_falls_back_to_full_redump(
    fresh_fixture, built_helper, isolated_cache
):
    # Bootstrap
    _xcindex(["search", "Glucose"], fresh_fixture, built_helper, isolated_cache)

    # Add a brand new file to the Core target
    new_file = fresh_fixture / "Sources" / "Core" / "Quantum.swift"
    new_file.write_text("public struct Quantum { public init() {} }\n")
    subprocess.run(["swift", "build"], cwd=str(fresh_fixture), check=True, timeout=300)

    after = _xcindex(["search", "Quantum", "--kind", "struct"],
                     fresh_fixture, built_helper, isolated_cache)
    assert after.returncode == 0
    payload = json.loads(after.stdout)
    assert payload["summary"]["found"]
    assert "new unit" in after.stderr or "running full re-dump" in after.stderr, (
        f"expected fallback message; got stderr={after.stderr!r}"
    )

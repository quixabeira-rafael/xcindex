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


@pytest.fixture(scope="session")
def built_fixture() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    subprocess.run(
        ["swift", "build"],
        cwd=str(FIXTURE_ROOT),
        check=True,
        timeout=300,
    )
    store = FIXTURE_ROOT / ".build" / "debug" / "index" / "store"
    assert (store / "v5" / "units").is_dir()
    return store


@pytest.fixture(scope="session")
def built_helper() -> Path:
    if not HELPER_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(REPO_ROOT / "swift-helper"),
            check=True,
            timeout=600,
        )
    assert HELPER_BINARY.is_file()
    return HELPER_BINARY


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch):
    cache_dir = tmp_path / "xcindex-cache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    yield cache_dir


def _xcindex(args: list[str], cwd: Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def test_symbol_by_name(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    result = _xcindex(
        ["symbol", "PriceCalculator", "--format", "json", "--level", "detailed"],
        cwd=FIXTURE_ROOT,
        env_overrides={"XCINDEX_HELPER": str(built_helper)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["kind"] == "symbol"
    assert payload["summary"]["found"] is True
    items = payload["items"]
    names = {item["name"] for item in items}
    assert "PriceCalculator" in names
    assert any(item["kind"] == "class" for item in items)


def test_symbol_by_usr(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    result = _xcindex(
        ["symbol", "s:4Core15PriceCalculatorC", "--format", "json"],
        cwd=FIXTURE_ROOT,
        env_overrides={"XCINDEX_HELPER": str(built_helper)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["found"] is True


def test_at_resolves_position(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    target = "Sources/Core/PriceCalculator.swift:5"
    result = _xcindex(
        ["at", target, "--format", "json", "--level", "locations"],
        cwd=FIXTURE_ROOT,
        env_overrides={"XCINDEX_HELPER": str(built_helper)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["found"] is True
    names = {item.get("name") for item in payload["items"]}
    assert "PriceCalculator" in names
    assert "PriceProvider" in names


def test_containing_finds_enclosing_method(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    target = "Sources/Core/PriceCalculator.swift:13"
    result = _xcindex(
        ["containing", target, "--format", "json", "--level", "detailed"],
        cwd=FIXTURE_ROOT,
        env_overrides={"XCINDEX_HELPER": str(built_helper)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["found"] is True
    item = payload["items"][0]
    assert item["name"] == "compute()"
    assert item["kind"] == "instance-method"


def test_cache_hit_skips_helper(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    """Second invocation should not re-dump (helper not invoked)."""
    args = ["symbol", "Money", "--format", "json"]
    first = _xcindex(args, cwd=FIXTURE_ROOT, env_overrides={"XCINDEX_HELPER": str(built_helper)})
    assert first.returncode == 0
    assert "materializing cache" in first.stderr

    second = _xcindex(args, cwd=FIXTURE_ROOT, env_overrides={"XCINDEX_HELPER": str(built_helper)})
    assert second.returncode == 0
    assert "materializing cache" not in second.stderr


def test_cache_invalidates_on_swift_version_change(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    """Different swift_version → different hash → cache miss."""
    from xcindex import cache as cm

    h1 = cm.compute_index_hash(built_fixture, swift_version="6.2", helper_version="0.1.0")
    h2 = cm.compute_index_hash(built_fixture, swift_version="6.3", helper_version="0.1.0")
    assert h1 != h2


def test_cross_module_relations_present(built_fixture: Path, built_helper: Path, isolated_cache: Path):
    """OrderProcessor.charge() should appear with PriceCalculator-related occurrences."""
    result = _xcindex(
        ["symbol", "OrderProcessor", "--format", "json", "--level", "detailed"],
        cwd=FIXTURE_ROOT,
        env_overrides={"XCINDEX_HELPER": str(built_helper)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    items = payload["items"]
    assert any(item["module"] == "Domain" for item in items)

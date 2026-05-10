"""End-to-end integration tests for `xcindex prewarm` against the SampleApp fixture."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from xcindex import cache as cache_module

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "SampleApp"
HELPER_BINARY = REPO_ROOT / "swift-helper" / ".build" / "release" / "xcindex-helper"


@pytest.fixture(scope="module")
def built_fixture() -> Path:
    if shutil.which("swift") is None:
        pytest.skip("swift toolchain not available")
    subprocess.run(
        ["swift", "build"],
        cwd=str(FIXTURE_ROOT),
        check=True, timeout=300,
    )
    store = FIXTURE_ROOT / ".build" / "debug" / "index" / "store"
    assert (store / "v5" / "units").is_dir()
    return store


@pytest.fixture(scope="module")
def built_helper() -> Path:
    if not HELPER_BINARY.is_file():
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(REPO_ROOT / "swift-helper"),
            check=True, timeout=600,
        )
    return HELPER_BINARY


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch):
    cache_dir = tmp_path / "xcindex-cache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    yield cache_dir


def _xcindex(args, cwd, env_overrides=None):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         *args],
        cwd=str(cwd), capture_output=True, text=True, env=env,
    )


def _env(helper):
    return {
        "XCINDEX_HELPER": str(helper),
        "XCINDEX_CACHE_ROOT": "",  # rely on monkeypatch via subprocess env
    }


@pytest.fixture
def isolated_cache_env(tmp_path: Path):
    cache_dir = tmp_path / "xcindex-cache"
    return cache_dir, {"XDG_CACHE_HOME": str(tmp_path)}


# --- Cold then noop ---------------------------------------------------------

def test_prewarm_cold_then_noop(built_fixture, built_helper, tmp_path):
    """First call materializes; second is a noop."""
    env = {**os.environ, "XCINDEX_HELPER": str(built_helper),
           "HOME": str(tmp_path)}  # isolate cache

    first = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--format", "json"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["kind"] == "prewarm"
    assert payload["summary"]["mode"] in ("cold", "schema_upgrade")
    assert payload["summary"]["symbols_added"] > 0

    second = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--format", "json"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )
    assert second.returncode == 0, second.stderr
    payload2 = json.loads(second.stdout)
    assert payload2["summary"]["mode"] == "noop"
    assert payload2["summary"]["symbols_added"] == 0


# --- Incremental after edit -------------------------------------------------

def test_prewarm_after_edit_rebuild_is_incremental(built_fixture, built_helper, tmp_path):
    """Edit one .swift file, rebuild, then prewarm runs incremental."""
    # Copy fixture to tmp_path to avoid mutating the shared fixture
    project = tmp_path / "SampleApp"
    shutil.copytree(FIXTURE_ROOT, project)
    if (project / ".build").exists():
        shutil.rmtree(project / ".build")
    subprocess.run(["swift", "build"], cwd=project, check=True, timeout=300)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "XCINDEX_HELPER": str(built_helper),
           "HOME": str(home)}

    # Initial prewarm: cold
    first = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--format", "json"],
        cwd=str(project), capture_output=True, text=True, env=env,
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["summary"]["mode"] == "cold"

    # Edit a file (preserve line layout to avoid invalidating containing positions)
    target = project / "Sources" / "Core" / "PriceCalculator.swift"
    text = target.read_text().replace("return 100.0", "return 200.0")
    target.write_text(text)

    # Rebuild — IndexStore unit for Core gets a fresh mtime
    subprocess.run(["swift", "build"], cwd=project, check=True, timeout=300)

    # Second prewarm: incremental
    second = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--format", "json"],
        cwd=str(project), capture_output=True, text=True, env=env,
    )
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["summary"]["mode"] == "incremental"
    assert payload["summary"]["units_modified"] >= 1


# --- Quiet --------------------------------------------------------------------

def test_prewarm_quiet_silences_noop(built_fixture, built_helper, tmp_path):
    env = {**os.environ, "XCINDEX_HELPER": str(built_helper),
           "HOME": str(tmp_path)}

    # Warm the cache
    subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )

    quiet = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--quiet"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout.strip() == ""


# --- JSON shape stability ---------------------------------------------------

def test_prewarm_json_shape_is_stable(built_fixture, built_helper, tmp_path):
    env = {**os.environ, "XCINDEX_HELPER": str(built_helper),
           "HOME": str(tmp_path)}

    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm", "--format", "json"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) >= {"kind", "anchor", "summary"}
    assert set(payload["anchor"].keys()) == {"project", "sqlite", "index_hash"}
    assert "mode" in payload["summary"]
    assert "wall_seconds" in payload["summary"]


# --- Doctor reflects warm state --------------------------------------------

def test_doctor_cache_warm_check_after_prewarm(built_fixture, built_helper, tmp_path):
    env = {**os.environ, "XCINDEX_HELPER": str(built_helper),
           "HOME": str(tmp_path)}

    subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "prewarm"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )

    doc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
         "doctor", "--json"],
        cwd=str(FIXTURE_ROOT), capture_output=True, text=True, env=env,
    )
    assert doc.returncode == 0, doc.stderr
    payload = json.loads(doc.stdout)
    cache_warm = next(c for c in payload["checks"] if c["name"] == "cache-warm")
    assert cache_warm["status"] == "ok"
    assert "in sync" in cache_warm["detail"]

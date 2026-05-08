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


@pytest.fixture(scope="module")
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


def _xcindex(args: list[str], cwd: Path, env_overrides: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
            "--format", "json",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"args={args!r}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


PRICE_CALC_USR = "s:4Core15PriceCalculatorC"
PRICE_PROVIDER_USR = "s:4Core13PriceProviderP"
DISCOUNTED_CALC_USR = "s:4Core25DiscountedPriceCalculatorC"
COMPUTE_USR = "s:4Core15PriceCalculatorC7computeSdyF"
CHECKOUT_VIEW_USR = "s:2UI12CheckoutViewV"


def _env(helper: Path) -> dict[str, str]:
    return {"XCINDEX_HELPER": str(helper)}


def test_search_finds_multiple_matches(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(["search", "Price", "--level", "summary"], FIXTURE_ROOT, _env(built_helper))
    assert payload["summary"]["found"] is True
    assert payload["summary"]["count"] >= 3
    assert payload["summary"]["by_module"].get("Core", 0) >= 3


def test_occurrences_filtered_by_role(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["occurrences", PRICE_CALC_USR, "--role", "definition", "--level", "locations"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    assert all("definition" in (it.get("roles") or []) for it in payload["items"])


def test_relations_out_finds_subclasses(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["relations", PRICE_CALC_USR, "--direction", "out", "--kind", "baseOf",
         "--level", "locations"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    names = {it["name"] for it in payload["items"]}
    assert "DiscountedPriceCalculator" in names


def test_relations_out_finds_protocol_conformers(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["relations", PRICE_PROVIDER_USR, "--direction", "out", "--kind", "baseOf",
         "--level", "locations"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    names = {it["name"] for it in payload["items"]}
    assert "PriceCalculator" in names


def test_relations_in_finds_method_overriders(built_fixture, built_helper, isolated_cache):
    """Who overrides PriceCalculator.compute? → DiscountedPriceCalculator.compute.

    `--direction in` returns occurrences of OTHER symbols whose relations point at
    the anchor; combined with `--kind overrideOf` this answers "who overrides me".
    """
    payload = _xcindex(
        ["relations", COMPUTE_USR, "--direction", "in", "--kind", "overrideOf",
         "--level", "locations"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"], (
        "expected DiscountedPriceCalculator.compute to register as an overrider"
    )
    names = {it["name"] for it in payload["items"]}
    assert any("compute" in (n or "") for n in names)


def test_neighbors_combines_directions(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["neighbors", PRICE_CALC_USR, "--direction", "both", "--level", "summary"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    assert payload["summary"]["count"] > 0


def test_reach_up_from_compute_reaches_ui(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["reach", COMPUTE_USR, "--up", "--depth", "8", "--level", "detailed"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    by_module = payload["summary"]["by_module"]
    assert "UI" in by_module
    assert "Domain" in by_module


def test_reach_to_module_filter(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["reach", COMPUTE_USR, "--up", "--depth", "8", "--to-module", "UI",
         "--level", "detailed"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    for item in payload["items"]:
        assert item["module"] == "UI"


def test_reach_down_from_checkout_reaches_core(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["reach", CHECKOUT_VIEW_USR, "--down", "--depth", "8", "--level", "detailed"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    by_module = payload["summary"]["by_module"]
    assert by_module.get("Core", 0) > 0
    assert by_module.get("Domain", 0) > 0


def test_reach_respects_max_depth(built_fixture, built_helper, isolated_cache):
    shallow = _xcindex(
        ["reach", COMPUTE_USR, "--up", "--depth", "1", "--level", "summary"],
        FIXTURE_ROOT, _env(built_helper),
    )
    deep = _xcindex(
        ["reach", COMPUTE_USR, "--up", "--depth", "8", "--level", "summary"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert shallow["summary"]["count"] <= deep["summary"]["count"]
    assert shallow["summary"]["max_hops"] is None or shallow["summary"]["max_hops"] <= 1


def test_search_filter_by_kind(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "Calculator", "--kind", "class", "--level", "locations"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert payload["summary"]["found"] is True
    for item in payload["items"]:
        assert item["kind"] == "class"

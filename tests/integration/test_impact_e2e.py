"""End-to-end integration tests for `xcindex impact` against the SampleApp fixture."""
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

PRICE_CALC_USR = "s:4Core15PriceCalculatorC"
PRICE_PROVIDER_USR = "s:4Core13PriceProviderP"
COMPUTE_USR = "s:4Core15PriceCalculatorC7computeSdyF"


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


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "xcindex-cache"
    monkeypatch.setattr(cache_module, "CACHE_ROOT", cache_dir)
    yield cache_dir


def _xcindex(args, cwd, env_overrides=None, fmt="json"):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    extra = ["--format", fmt] if fmt else []
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
            *args, *extra,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def _env(helper):
    return {"XCINDEX_HELPER": str(helper)}


# --- Callable target -------------------------------------------------------

def test_impact_callable_returns_call_stack(built_fixture, built_helper, isolated_cache):
    proc = _xcindex(["impact", COMPUTE_USR], FIXTURE_ROOT, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["kind"] == "impact"
    assert payload["mode"] == "call_stack"
    assert "stacks" in payload
    upstream = payload["stacks"]["upstream"]
    # compute() is called from CheckoutView (UI) via Domain.OrderProcessor.charge()
    leaf_modules = {s[0].get("module") for s in upstream if s}
    assert "UI" in leaf_modules or "Domain" in leaf_modules


def test_impact_callable_via_file_line_resolves_same_target(
    built_fixture, built_helper, isolated_cache,
):
    direct = _xcindex(["impact", COMPUTE_USR], FIXTURE_ROOT, _env(built_helper))
    via_pos = _xcindex(
        ["impact", "Sources/Core/PriceCalculator.swift:13"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert direct.returncode == 0
    assert via_pos.returncode == 0
    a = json.loads(direct.stdout)
    b = json.loads(via_pos.stdout)
    assert a["anchor"]["usr"] == b["anchor"]["usr"]


def test_impact_callable_via_unique_name(built_fixture, built_helper, isolated_cache):
    # SampleApp has only one definition of `compute()` — but DiscountedPriceCalculator overrides.
    # Both share the same name, so we expect ambiguous error with two candidates.
    proc = _xcindex(["impact", "compute()"], FIXTURE_ROOT, _env(built_helper), fmt=None)
    assert proc.returncode != 0
    assert "ambiguous_name" in proc.stderr or "matches" in proc.stderr


# --- Type target -----------------------------------------------------------

def test_impact_class_returns_usage_chain(built_fixture, built_helper, isolated_cache):
    proc = _xcindex(["impact", PRICE_CALC_USR], FIXTURE_ROOT, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "usage_chain"
    assert payload["structure"] is not None
    member_names = {m["name"] for m in payload["structure"]["members"]}
    assert any("compute" in n for n in member_names)
    sub_names = {s["name"] for s in payload["structure"]["subclasses"]}
    assert "DiscountedPriceCalculator" in sub_names


def test_impact_protocol_lists_conformers_in_structure(
    built_fixture, built_helper, isolated_cache,
):
    proc = _xcindex(["impact", PRICE_PROVIDER_USR], FIXTURE_ROOT, _env(built_helper))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "usage_chain"
    sub_names = {s["name"] for s in payload["structure"]["subclasses"]}
    assert "PriceCalculator" in sub_names


# --- Module filter ----------------------------------------------------------

def test_impact_to_module_restricts_stacks(built_fixture, built_helper, isolated_cache):
    proc = _xcindex(
        ["impact", COMPUTE_USR, "--to-module", "UI"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    upstream = payload["stacks"]["upstream"]
    for stack in upstream:
        # Root frame's module must be UI when --to-module=UI applies upstream.
        assert (stack[0].get("module") or "") == "UI"


# --- Hint-only mode --------------------------------------------------------

def test_impact_property_target_returns_hint_only_mode(
    built_fixture, built_helper, isolated_cache,
):
    # Money.amount is an instance-property in the fixture
    proc = _xcindex(
        ["impact", "s:4Core5MoneyV6amountSdvp"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "hint_only"
    assert payload["stacks"]["upstream"] == []
    assert payload["stacks"]["downstream"] == []


# --- Direction restrictions ------------------------------------------------

def test_impact_up_only_omits_downstream_stacks(
    built_fixture, built_helper, isolated_cache,
):
    proc = _xcindex(
        ["impact", COMPUTE_USR, "--up-only"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["stacks"]["downstream"] == []


def test_impact_down_only_omits_upstream_stacks(
    built_fixture, built_helper, isolated_cache,
):
    proc = _xcindex(
        ["impact", COMPUTE_USR, "--down-only"],
        FIXTURE_ROOT, _env(built_helper),
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["stacks"]["upstream"] == []


# --- Error handling --------------------------------------------------------

def test_impact_unknown_target_exits_with_invalid_state(
    built_fixture, built_helper, isolated_cache,
):
    proc = _xcindex(
        ["impact", "s:NonexistentUSR"],
        FIXTURE_ROOT, _env(built_helper), fmt=None,
    )
    assert proc.returncode != 0
    assert "USR not found" in proc.stderr or "target_not_found" in proc.stderr


# --- JSON schema stability -------------------------------------------------

def test_impact_canonical_json_shape_is_stable(
    built_fixture, built_helper, isolated_cache,
):
    proc = _xcindex(["impact", COMPUTE_USR], FIXTURE_ROOT, _env(built_helper))
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) >= {"kind", "mode", "anchor", "summary", "stacks"}
    assert set(payload["stacks"].keys()) == {"upstream", "downstream"}
    assert payload["mode"] in {"call_stack", "usage_chain", "hint_only"}

"""End-to-end coverage of every IndexStore primitive xcindex models.

The fixture under tests/fixtures/SampleApp/ is intentionally crafted to exercise
the full data model — every kind, sub_kind, role and relation kind that we promise
to expose. These tests assert that each primitive survives the round-trip through
the Swift helper, the SQLite cache, and the xcindex CLI.

If a primitive is missing here, it is unverified. Add a fixture if needed.
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


def _xcindex(args: list[str], helper: Path) -> dict:
    env = os.environ.copy()
    env["XCINDEX_HELPER"] = str(helper)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
            "--format", "json",
        ],
        cwd=str(FIXTURE_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"args={args!r}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


# --- Symbol kinds -----------------------------------------------------------
#
# Each test exercises a single symbol kind through `xcindex search`. The
# fixture is laid out so each kind is uniquely identifiable by name.

@pytest.mark.parametrize(
    "name,expected_kind",
    [
        ("Money",                   "struct"),
        ("PriceCalculator",         "class"),
        ("PriceProvider",           "protocol"),
        ("Currency",                "enum"),
        ("MoneyBox",                "typealias"),
        ("Container",               "struct"),
        ("PositiveAmountValidator", "struct"),
        ("Validator",               "protocol"),
        ("Receipt",                 "class"),
    ],
)
def test_symbol_kind_resolves(built_fixture, built_helper, isolated_cache,
                              name, expected_kind):
    payload = _xcindex(["search", name, "--level", "locations"], built_helper)
    assert payload["summary"]["found"], f"{name} not indexed"
    matches = [it for it in payload["items"] if it["name"] == name and it["kind"] == expected_kind]
    assert matches, (
        f"expected to find {name} with kind={expected_kind}; got "
        f"{[(it['name'], it['kind']) for it in payload['items']]}"
    )


def test_enum_cases_have_kind_enum_case(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "usd", "--kind", "enum-case", "--level", "locations"],
        built_helper,
    )
    assert payload["summary"]["found"]
    assert any(it["name"] == "usd" for it in payload["items"])


def test_destructor_kind_emitted(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "deinit", "--kind", "destructor", "--level", "locations"],
        built_helper,
    )
    assert payload["summary"]["found"]


def test_static_property_kind(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "identifier", "--kind", "static-property", "--level", "locations"],
        built_helper,
    )
    assert payload["summary"]["found"]
    assert any(it["module"] == "Domain" for it in payload["items"])


def test_static_method_kind(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "zero", "--kind", "static-method", "--level", "locations"],
        built_helper,
    )
    assert payload["summary"]["found"]


def test_constructor_kind(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(
        ["search", "init", "--kind", "constructor", "--level", "summary"],
        built_helper,
    )
    assert payload["summary"]["found"]
    assert payload["summary"]["count"] >= 4


def test_function_kind_for_operators(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(["search", "+", "--kind", "function", "--level", "detailed"], built_helper)
    assert payload["summary"]["found"], "operator + should be indexed as function"


# --- Sub-kinds (rendered as `sub_kind` at level=detailed) -------------------

def _detailed(name: str, helper: Path) -> list[dict]:
    payload = _xcindex(["search", name, "--level", "detailed"], helper)
    return payload["items"]


def test_subscript_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("subscript", built_helper)
    assert any(it.get("sub_kind") == "swift-subscript" for it in items)


def test_infix_operator_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("+", built_helper)
    assert any(it.get("sub_kind") == "swift-infix-operator" for it in items)


def test_prefix_operator_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("-", built_helper)
    assert any(it.get("sub_kind") == "swift-prefix-operator" for it in items)


def test_associated_type_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("Input", built_helper)
    assert any(it.get("sub_kind") == "swift-associated-type" for it in items)


def test_generic_type_param_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("Element", built_helper)
    assert any(it.get("sub_kind") == "swift-generic-type-param" for it in items)


def test_didset_observer_subkind(built_fixture, built_helper, isolated_cache):
    items = _detailed("total", built_helper)
    assert any(it.get("sub_kind") == "swift-accessor-didset" for it in items)


def test_accessor_getter_subkind(built_fixture, built_helper, isolated_cache):
    # `symbol` property on Currency has a custom getter
    items = _detailed("symbol", built_helper)
    assert any(it.get("sub_kind") == "accessor-getter" for it in items)


def test_accessor_setter_subkind(built_fixture, built_helper, isolated_cache):
    # `items` setter on Receipt
    items = _detailed("items", built_helper)
    assert any(it.get("sub_kind") == "accessor-setter" for it in items)


# --- Roles ------------------------------------------------------------------

POSITIVE_AMOUNT_USR = None  # resolved at runtime in fixtures below


def _usr_for(name: str, helper: Path, kind: str | None = None) -> str:
    """Return the USR of the first symbol whose name contains `name` substring.

    Swift method names are emitted with their argument labels and parens (e.g. the
    method `compute` shows up as `compute()`, `validateAll` as `validateAll(_:)`).
    Substring matching keeps the test ergonomic without making callers spell out
    the full mangled name.
    """
    args = ["search", name, "--level", "detailed"]
    if kind:
        args += ["--kind", kind]
    payload = _xcindex(args, helper)
    items = [it for it in payload["items"] if name in (it.get("name") or "")]
    if kind:
        items = [it for it in items if it["kind"] == kind]
    assert items, (
        f"could not find symbol named {name!r} (kind={kind!r}). "
        f"raw matches: {[(it.get('name'), it.get('kind')) for it in payload['items']]}"
    )
    # Prefer the symbol whose name equals `name` exactly or `name()` (Swift method
    # names carry the parameter list). This disambiguates between e.g.
    # PriceCalculator and DiscountedPriceCalculator when searching "PriceCalculator".
    exact = [it for it in items if it["name"] in (name, f"{name}()", f"{name}(_:)")]
    if exact:
        return exact[0]["usr"]
    # Otherwise prefer the shortest name (closest to the query).
    items.sort(key=lambda it: len(it["name"]))
    return items[0]["usr"]


def test_role_definition(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("compute", built_helper, kind="instance-method")
    payload = _xcindex(["occurrences", usr, "--role", "definition", "--level", "locations"],
                      built_helper)
    assert payload["summary"]["found"]
    for item in payload["items"]:
        assert "definition" in (item.get("roles") or [])


def test_role_call(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("compute", built_helper, kind="instance-method")
    payload = _xcindex(["occurrences", usr, "--role", "call", "--level", "locations"],
                      built_helper)
    assert payload["summary"]["found"]
    for item in payload["items"]:
        assert "call" in (item.get("roles") or [])


def test_role_read(built_fixture, built_helper, isolated_cache):
    # Money.amount is read inside `+` operator
    usr = _usr_for("amount", built_helper, kind="instance-property")
    payload = _xcindex(["occurrences", usr, "--role", "read", "--level", "locations"],
                      built_helper)
    assert payload["summary"]["found"]


def test_role_write(built_fixture, built_helper, isolated_cache):
    # Receipt.items is written inside record(name:); Receipt.total via setter side-effect
    usr = _usr_for("items", built_helper, kind="instance-property")
    payload = _xcindex(["occurrences", usr, "--role", "write", "--level", "locations"],
                      built_helper)
    assert payload["summary"]["found"], "expected at least one write occurrence of items"
    for item in payload["items"]:
        assert "write" in (item.get("roles") or [])


def test_role_dynamic_on_protocol_call(built_fixture, built_helper, isolated_cache):
    # OrderProcessor.charge() calls calculator.price() which is a protocol method →
    # the call site is dynamic
    usr = _usr_for("price", built_helper, kind="instance-method")
    payload = _xcindex(["occurrences", usr, "--role", "call", "--level", "detailed"],
                      built_helper)
    assert any("dynamic" in (it.get("roles") or []) for it in payload["items"]), (
        "expected at least one dynamic call to price()"
    )


# --- Relation kinds ---------------------------------------------------------

def test_relation_baseOf_inheritance(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("PriceCalculator", built_helper, kind="class")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "baseOf",
                       "--level", "locations"], built_helper)
    names = {it["name"] for it in payload["items"]}
    assert "DiscountedPriceCalculator" in names


def test_relation_baseOf_conformance(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("PriceProvider", built_helper, kind="protocol")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "baseOf",
                       "--level", "locations"], built_helper)
    names = {it["name"] for it in payload["items"]}
    assert "PriceCalculator" in names


def test_relation_overrideOf(built_fixture, built_helper, isolated_cache):
    payload = _xcindex(["search", "compute", "--kind", "instance-method", "--level", "detailed"],
                      built_helper)
    overriding = [it for it in payload["items"]
                  if it["module"] == "Core" and it["file"].endswith("PriceCalculator.swift")
                  and it["line"] >= 17]  # DiscountedPriceCalculator.compute is later
    assert overriding, "expected to find DiscountedPriceCalculator.compute"
    usr = overriding[0]["usr"]
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "overrideOf",
                       "--level", "locations"], built_helper)
    assert payload["summary"]["found"]
    assert any(it["name"] == "compute()" for it in payload["items"])


def test_relation_calledBy(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("compute", built_helper, kind="instance-method")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "calledBy",
                       "--level", "locations"], built_helper)
    assert payload["summary"]["found"]


def test_relation_childOf(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("Money", built_helper, kind="struct")
    payload = _xcindex(["relations", usr, "--direction", "in", "--kind", "childOf",
                       "--level", "summary"], built_helper)
    assert payload["summary"]["found"]
    assert payload["summary"]["count"] >= 3  # amount, currency, init, formatted, etc.


def test_relation_containedBy(built_fixture, built_helper, isolated_cache):
    # Money's amount property is contained by Money struct
    usr = _usr_for("amount", built_helper, kind="instance-property")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "containedBy",
                       "--level", "locations"], built_helper)
    assert payload["summary"]["found"]


def test_relation_extendedBy(built_fixture, built_helper, isolated_cache):
    # Money.swift has an extension in Money+Extensions.swift → extendedBy relation
    usr = _usr_for("Money", built_helper, kind="struct")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "extendedBy",
                       "--level", "locations"], built_helper)
    assert payload["summary"]["found"], "expected an extendedBy relation for Money"


def test_relation_receivedBy(built_fixture, built_helper, isolated_cache):
    # OrderProcessor.charge() calls calculator.price() — calculator is the receiver
    usr = _usr_for("price", built_helper, kind="instance-method")
    payload = _xcindex(["relations", usr, "--direction", "out", "--kind", "receivedBy",
                       "--level", "locations"], built_helper)
    assert payload["summary"]["found"], (
        "expected at least one receivedBy relation for price() (dynamic dispatch)"
    )


# --- End-to-end blast radius across the expanded graph ----------------------

def test_reach_up_from_compute_traverses_all_layers(built_fixture, built_helper, isolated_cache):
    usr = _usr_for("compute", built_helper, kind="instance-method")
    payload = _xcindex(["reach", usr, "--up", "--depth", "8", "--level", "summary"],
                      built_helper)
    by_module = payload["summary"]["by_module"]
    assert {"Core", "Domain", "UI"}.issubset(by_module.keys())


def test_reach_down_from_validator_extension(built_fixture, built_helper, isolated_cache):
    # validateAll() is a default impl on the Validator protocol extension; reaching
    # down should hit `validate(_:)` (the abstract requirement it calls).
    usr = _usr_for("validateAll", built_helper, kind="instance-method")
    payload = _xcindex(["reach", usr, "--down", "--depth", "4", "--level", "detailed"],
                      built_helper)
    assert payload["summary"]["found"]
    names = {it["name"] for it in payload["items"]}
    assert "validate(_:)" in names


# --- Coverage budget: any new kind/relation should also have a test ----------

EXPECTED_KINDS = {
    "class", "struct", "protocol", "enum", "enum-case", "typealias",
    "function", "destructor", "constructor",
    "instance-method", "static-method",
    "instance-property", "static-property",
    "parameter",
}

EXPECTED_SUBKINDS = {
    "accessor-getter", "accessor-setter",
    "swift-infix-operator", "swift-prefix-operator",
    "swift-generic-type-param", "swift-associated-type",
    "swift-accessor-didset", "swift-subscript",
}

EXPECTED_RELATION_KINDS = {
    "childOf", "containedBy", "calledBy", "receivedBy",
    "overrideOf", "baseOf", "extendedBy",
}


def test_fixture_emits_full_kind_coverage(built_fixture, built_helper, isolated_cache):
    seen = set()
    for kind in EXPECTED_KINDS:
        payload = _xcindex(["search", "", "--kind", kind, "--limit", "1", "--level", "summary"],
                          built_helper)
        if payload["summary"]["found"]:
            seen.add(kind)
    missing = EXPECTED_KINDS - seen
    assert not missing, f"fixture is missing symbol kinds: {sorted(missing)}"

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from xcindex import helper


@pytest.fixture
def fake_helper_binary(tmp_path: Path) -> Path:
    """Create a stub executable that responds to common helper subcommands."""
    binary = tmp_path / "xcindex-helper"
    binary.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "version" ]; then\n'
        '  echo \'{"helper_version":"0.1.0","schema_version":1,"swift_version":"swift-test"}\'\n'
        '  exit 0\n'
        'fi\n'
        'exit 99\n'
    )
    binary.chmod(0o755)
    return binary


def test_locate_via_env_var(monkeypatch, fake_helper_binary: Path):
    monkeypatch.setenv(helper.ENV_HELPER, str(fake_helper_binary))
    found = helper.locate_helper()
    assert found == fake_helper_binary


def test_locate_returns_none_when_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(helper.ENV_HELPER, raising=False)
    monkeypatch.setattr(helper, "INSTALLED_HELPER", tmp_path / "nope")
    monkeypatch.setattr(helper, "_dev_helper_path", lambda: None)
    assert helper.locate_helper() is None


def test_locate_prefers_env_over_installed(monkeypatch, tmp_path: Path, fake_helper_binary: Path):
    installed = tmp_path / "installed"
    installed.write_text("#!/bin/bash\nexit 0\n")
    installed.chmod(0o755)
    monkeypatch.setattr(helper, "INSTALLED_HELPER", installed)
    monkeypatch.setenv(helper.ENV_HELPER, str(fake_helper_binary))
    assert helper.locate_helper() == fake_helper_binary


def test_get_version_parses_json(fake_helper_binary: Path):
    info = helper.get_version(fake_helper_binary)
    assert info.helper_version == "0.1.0"
    assert info.schema_version == 1
    assert info.swift_version == "swift-test"
    assert info.binary_path == fake_helper_binary


def test_get_version_raises_on_non_zero_exit(tmp_path: Path):
    binary = tmp_path / "broken"
    binary.write_text("#!/bin/bash\nexit 1\n")
    binary.chmod(0o755)
    with pytest.raises(helper.HelperError):
        helper.get_version(binary)


def test_get_version_raises_on_invalid_json(tmp_path: Path):
    binary = tmp_path / "bad-json"
    binary.write_text("#!/bin/bash\necho 'not json'\nexit 0\n")
    binary.chmod(0o755)
    with pytest.raises(helper.HelperError):
        helper.get_version(binary)


def test_ensure_helper_existing_skips_build(monkeypatch, fake_helper_binary: Path):
    monkeypatch.setenv(helper.ENV_HELPER, str(fake_helper_binary))
    monkeypatch.setattr(helper, "build_helper", lambda: pytest.fail("should not build"))
    assert helper.ensure_helper(allow_build=False) == fake_helper_binary


def test_ensure_helper_raises_when_disallowed(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(helper.ENV_HELPER, raising=False)
    monkeypatch.setattr(helper, "INSTALLED_HELPER", tmp_path / "nope")
    monkeypatch.setattr(helper, "_dev_helper_path", lambda: None)
    with pytest.raises(helper.HelperError, match="not found"):
        helper.ensure_helper(allow_build=False)


def test_stream_dump_yields_parsed_records(monkeypatch, tmp_path: Path):
    binary = tmp_path / "fake-dump"
    binary.write_text(
        "#!/bin/bash\n"
        'echo \'{"type":"symbol","usr":"s:1","name":"foo"}\'\n'
        'echo \'{"type":"occurrence","id":1,"symbol_usr":"s:1"}\'\n'
        'exit 0\n'
    )
    binary.chmod(0o755)
    store = tmp_path / "store"
    (store / "v5" / "units").mkdir(parents=True)
    monkeypatch.setenv(helper.ENV_HELPER, str(binary))
    records = list(helper.stream_dump(store))
    assert len(records) == 2
    assert records[0]["type"] == "symbol"
    assert records[1]["type"] == "occurrence"


def test_stream_dump_raises_on_invalid_json(monkeypatch, tmp_path: Path):
    binary = tmp_path / "bad"
    binary.write_text("#!/bin/bash\necho 'not json'\nexit 0\n")
    binary.chmod(0o755)
    store = tmp_path / "store"
    (store / "v5" / "units").mkdir(parents=True)
    monkeypatch.setenv(helper.ENV_HELPER, str(binary))
    with pytest.raises(helper.HelperError, match="non-JSON"):
        list(helper.stream_dump(store))


def test_stream_dump_raises_on_helper_failure(monkeypatch, tmp_path: Path):
    binary = tmp_path / "fail"
    binary.write_text(
        "#!/bin/bash\n"
        'echo "boom" >&2\n'
        'exit 5\n'
    )
    binary.chmod(0o755)
    store = tmp_path / "store"
    (store / "v5" / "units").mkdir(parents=True)
    monkeypatch.setenv(helper.ENV_HELPER, str(binary))
    with pytest.raises(helper.HelperError, match="exit 5"):
        list(helper.stream_dump(store))


def test_helper_source_dir_dev_mode():
    source = helper.helper_source_dir()
    assert (source / "Package.swift").is_file()


def test_helper_source_dir_env_override(monkeypatch, tmp_path: Path):
    fake = tmp_path / "swift-helper-fake"
    fake.mkdir()
    (fake / "Package.swift").write_text("// stub")
    monkeypatch.setenv(helper.ENV_HELPER_SOURCE, str(fake))
    assert helper.helper_source_dir() == fake


def test_build_helper_fails_without_swift(monkeypatch, tmp_path: Path):
    fake = tmp_path / "swift-helper"
    fake.mkdir()
    (fake / "Package.swift").write_text("// stub")
    monkeypatch.setenv(helper.ENV_HELPER_SOURCE, str(fake))
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(helper.HelperError, match="swift toolchain"):
        helper.build_helper()

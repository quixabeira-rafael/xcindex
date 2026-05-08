from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Iterator

from xcindex import __version__ as XCINDEX_VERSION

ENV_HELPER = "XCINDEX_HELPER"
ENV_HELPER_SOURCE = "XCINDEX_HELPER_SOURCE"

INSTALLED_HELPER_DIR = Path.home() / ".local" / "share" / "xcindex" / "bin"
INSTALLED_HELPER = INSTALLED_HELPER_DIR / "xcindex-helper"

EXPECTED_SCHEMA_VERSION = 2


class HelperError(Exception):
    """Raised when the Swift helper fails to locate, build, or invoke."""


@dataclass(frozen=True)
class HelperVersion:
    helper_version: str
    schema_version: int
    swift_version: str
    binary_path: Path


def locate_helper() -> Path | None:
    """Return path to the helper binary, or None if not found.

    Resolution order: XCINDEX_HELPER env, installed location, dev mode (.build/release).
    """
    override = os.environ.get(ENV_HELPER)
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    if INSTALLED_HELPER.is_file() and os.access(INSTALLED_HELPER, os.X_OK):
        return INSTALLED_HELPER
    dev = _dev_helper_path()
    if dev is not None and dev.is_file() and os.access(dev, os.X_OK):
        return dev
    return None


def helper_source_dir() -> Path:
    """Return path to the swift-helper SwiftPM package source.

    Resolution order:
        1. XCINDEX_HELPER_SOURCE env var
        2. Installed wheel: importlib.resources package data
        3. Dev mode: <repo_root>/swift-helper
    """
    override = os.environ.get(ENV_HELPER_SOURCE)
    if override:
        return Path(override).expanduser()

    try:
        resource = resource_files("xcindex").joinpath("_swift_helper_src")
        path = Path(str(resource))
        if (path / "Package.swift").is_file():
            return path
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    dev = _repo_root() / "swift-helper"
    if (dev / "Package.swift").is_file():
        return dev
    raise HelperError(
        "swift-helper source not found; expected wheel resource 'xcindex/_swift_helper_src' "
        "or repo path '<repo>/swift-helper'"
    )


def ensure_helper(*, allow_build: bool = True) -> Path:
    """Return a usable helper binary, building from source if necessary.

    If a helper exists but reports a stale `schema_version`, it is rebuilt so the
    Python ↔ Swift contract stays in sync after a `pipx reinstall xcindex`.

    Raises HelperError if the binary is missing and either:
        - allow_build is False
        - build cannot be performed (Swift toolchain absent)
        - build itself fails
    """
    existing = locate_helper()
    if existing is not None:
        try:
            info = get_version(existing)
        except HelperError:
            info = None
        if info is not None and info.schema_version == EXPECTED_SCHEMA_VERSION:
            return existing
        if not allow_build:
            return existing  # caller will surface the version mismatch
        # Stale binary — rebuild from source.
    if not allow_build:
        raise HelperError(
            "xcindex-helper binary not found. Run `xcindex setup install` "
            "or set XCINDEX_HELPER to its path."
        )
    return build_helper()


def build_helper() -> Path:
    """Build the helper from source (release config) and copy into the installed location."""
    source = helper_source_dir()
    if shutil.which("swift") is None:
        raise HelperError("swift toolchain not available; cannot build helper")

    sys.stderr.write("Building xcindex-helper (one-time, ~60s)... ")
    sys.stderr.flush()
    try:
        subprocess.run(
            ["swift", "build", "-c", "release"],
            cwd=str(source),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("timed out\n")
        raise HelperError("swift build timed out after 10 minutes")
    except subprocess.CalledProcessError as exc:
        sys.stderr.write("failed\n")
        raise HelperError(
            "swift build failed: "
            f"exit {exc.returncode}\nstderr:\n{exc.stderr or ''}"
        ) from exc
    sys.stderr.write("done\n")

    built = source / ".build" / "release" / "xcindex-helper"
    if not built.is_file():
        raise HelperError(f"build succeeded but binary not found at {built}")

    INSTALLED_HELPER_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, INSTALLED_HELPER)
    INSTALLED_HELPER.chmod(0o755)
    return INSTALLED_HELPER


def get_version(helper_path: Path | None = None) -> HelperVersion:
    """Invoke `xcindex-helper version` and parse the JSON response."""
    binary = helper_path or ensure_helper(allow_build=False)
    result = subprocess.run(
        [str(binary), "version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise HelperError(
            f"xcindex-helper version failed: exit {result.returncode}\n{result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HelperError(f"helper version emitted non-JSON: {exc}\n{result.stdout!r}")
    return HelperVersion(
        helper_version=str(payload.get("helper_version", "")),
        schema_version=int(payload.get("schema_version", 0)),
        swift_version=str(payload.get("swift_version", "")),
        binary_path=binary,
    )


def stream_dump(
    index_store_path: Path,
    *,
    include_system: bool = False,
    helper_path: Path | None = None,
) -> Iterator[dict]:
    """Yield parsed NDJSON records from the helper's `dump` subcommand.

    Each yielded record is a dict with `type` in {unit, symbol, occurrence, relation, file_unit}.
    Stderr lines are forwarded to sys.stderr (the helper writes structured info there).
    """
    binary = helper_path or ensure_helper()
    args = [str(binary), "dump", "--index-store", str(index_store_path)]
    if include_system:
        args.append("--include-system")
    yield from _stream_helper(args, label="dump")


def stream_dump_files(
    index_store_path: Path,
    files: list[Path] | list[str],
    *,
    include_system: bool = False,
    helper_path: Path | None = None,
) -> Iterator[dict]:
    """Yield NDJSON records for `dump-files` (incremental, file-scoped)."""
    binary = helper_path or ensure_helper()
    args = [str(binary), "dump-files", "--index-store", str(index_store_path)]
    for f in files:
        args.append("--file")
        args.append(str(f))
    if include_system:
        args.append("--include-system")
    yield from _stream_helper(args, label="dump-files")


def _stream_helper(args: list[str], *, label: str) -> Iterator[dict]:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise HelperError(f"helper emitted non-JSON line: {exc}\n{line!r}")
        process.stdout.close()
    finally:
        rc = process.wait()
        stderr_output = ""
        if process.stderr is not None:
            stderr_output = process.stderr.read()
            process.stderr.close()
        if rc != 0:
            raise HelperError(
                f"xcindex-helper {label} failed: exit {rc}\nstderr:\n{stderr_output}"
            )
        if stderr_output:
            sys.stderr.write(stderr_output)


# --- Internal helpers --------------------------------------------------------

def _dev_helper_path() -> Path | None:
    src = _repo_root() / "swift-helper"
    if (src / "Package.swift").is_file():
        return src / ".build" / "release" / "xcindex-helper"
    return None


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here.parent.parent, here.parent.parent.parent):
        if (candidate / "swift-helper" / "Package.swift").is_file():
            return candidate
    return here.parent.parent

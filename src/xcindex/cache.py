from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

CACHE_ROOT = Path.home() / ".cache" / "xcindex"
META_FILENAME = "meta.json"
LOCK_FILENAME = ".dump.lock"
LIVE_SQLITE_NAME = "index.sqlite"
LEGACY_PREFIX = "legacy_"
KEEP_LAST_N_LEGACY = 3


@dataclass(frozen=True)
class CacheEntry:
    project_fingerprint: str
    project_path: Path
    index_hash: str
    sqlite_path: Path
    size_bytes: int
    mtime_ns: int
    role: str  # "live" | "legacy"


def project_fingerprint(project_path: Path) -> str:
    """Stable, short identifier for a project artifact path.

    Uses md5 of the absolute path; presented as 16 hex chars (collision-safe at
    our scale and tolerable in directory names).
    """
    digest = hashlib.md5(str(project_path.resolve()).encode("utf-8")).hexdigest()
    return digest[:16]


def project_cache_dir(project_path: Path) -> Path:
    return CACHE_ROOT / project_fingerprint(project_path)


def cache_root() -> Path:
    return CACHE_ROOT


def ensure_cache_dir(project_path: Path) -> Path:
    directory = project_cache_dir(project_path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sqlite_path_for(project_path: Path, index_hash: str) -> Path:
    """Path to a content-addressed snapshot file (legacy / v1 compatible)."""
    return project_cache_dir(project_path) / f"{index_hash}.sqlite"


def canonical_sqlite_path(project_path: Path) -> Path:
    """Path to the live, mutable cache for a project."""
    return project_cache_dir(project_path) / LIVE_SQLITE_NAME


def legacy_sqlite_path(project_path: Path, index_hash: str) -> Path:
    return project_cache_dir(project_path) / f"{LEGACY_PREFIX}{index_hash}.sqlite"


def migrate_v1_caches(project_path: Path) -> int:
    """Rename any pre-existing `<hash>.sqlite` (v1 layout) to `legacy_<hash>.sqlite`.

    Returns the number of files renamed. Idempotent: if no v1 caches exist, no-op.
    Files already named `legacy_*` are left as-is. The live `index.sqlite` is
    excluded from migration.
    """
    directory = project_cache_dir(project_path)
    if not directory.exists():
        return 0
    renamed = 0
    for entry in directory.glob("*.sqlite"):
        name = entry.name
        if name == LIVE_SQLITE_NAME or name.startswith(LEGACY_PREFIX):
            continue
        target = entry.with_name(f"{LEGACY_PREFIX}{name}")
        with contextlib.suppress(FileNotFoundError):
            entry.rename(target)
            renamed += 1
    return renamed


def list_caches(project_path: Path | None = None) -> list[CacheEntry]:
    """Return all cached SQLite files. If project_path is None, list across all projects.

    Entries are tagged with `role`: "live" for the active `index.sqlite`,
    "legacy" for preserved v1 snapshots.
    """
    if project_path is not None:
        directories = [project_cache_dir(project_path)]
    else:
        if not CACHE_ROOT.exists():
            return []
        directories = [d for d in CACHE_ROOT.iterdir() if d.is_dir()]

    entries: list[CacheEntry] = []
    for directory in directories:
        if not directory.exists():
            continue
        meta = _read_meta(directory)
        recorded_path = meta.get("project_path")
        proj_path = Path(recorded_path) if recorded_path else (project_path or Path("?"))
        for sqlite_file in sorted(directory.glob("*.sqlite")):
            try:
                stat = sqlite_file.stat()
            except FileNotFoundError:
                continue
            name = sqlite_file.name
            if name == LIVE_SQLITE_NAME:
                role = "live"
                index_hash = "live"
            elif name.startswith(LEGACY_PREFIX):
                role = "legacy"
                index_hash = sqlite_file.stem[len(LEGACY_PREFIX):]
            else:
                # Pre-migration v1 cache that hasn't been renamed yet.
                role = "legacy"
                index_hash = sqlite_file.stem
            entries.append(
                CacheEntry(
                    project_fingerprint=directory.name,
                    project_path=proj_path,
                    index_hash=index_hash,
                    sqlite_path=sqlite_file,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    role=role,
                )
            )
    return entries


@dataclass(frozen=True)
class GCCandidate:
    """A cache directory observed by gc_idle_caches()."""
    project_fingerprint: str
    project_path: Path | None       # None if no project_path was recorded in meta
    cache_dir: Path
    sqlite_mtime_ns: int
    idle_seconds: float
    size_bytes: int


@dataclass(frozen=True)
class GCResult:
    """Outcome of one `cache gc` pass."""
    pruned: list[GCCandidate]
    kept: list[GCCandidate]
    bytes_freed: int
    threshold_seconds: int
    dry_run: bool


def gc_idle_caches(
    *,
    max_idle_seconds: int = 3600,
    dry_run: bool = False,
) -> GCResult:
    """Remove caches whose live `index.sqlite` has not been touched recently.

    A cache is eligible when:
      `time.time() - mtime(index.sqlite) > max_idle_seconds`

    Mtime is updated by every cold/incremental materialize() (and by the
    helper's atomic-rename pattern). Read-only queries do NOT update mtime,
    so a cache with stale mtime means "no build/incremental activity for
    `max_idle_seconds`". Empty cache directories without a live sqlite are
    also treated as ancient (no live data worth keeping).
    """
    pruned: list[GCCandidate] = []
    kept: list[GCCandidate] = []
    bytes_freed = 0
    now = time.time()

    if not CACHE_ROOT.exists():
        return GCResult(pruned=[], kept=[], bytes_freed=0,
                        threshold_seconds=max_idle_seconds, dry_run=dry_run)

    for directory in CACHE_ROOT.iterdir():
        if not directory.is_dir():
            continue

        live_sqlite = directory / LIVE_SQLITE_NAME
        meta = _read_meta(directory)
        recorded_path = meta.get("project_path")
        proj_path: Path | None = Path(recorded_path) if recorded_path else None

        try:
            mtime_ns = live_sqlite.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = 0  # no live sqlite — treat as ancient
        except OSError:
            continue

        idle_seconds = now - (mtime_ns / 1_000_000_000)

        try:
            size = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
        except OSError:
            size = 0

        candidate = GCCandidate(
            project_fingerprint=directory.name,
            project_path=proj_path,
            cache_dir=directory,
            sqlite_mtime_ns=mtime_ns,
            idle_seconds=idle_seconds,
            size_bytes=size,
        )

        if idle_seconds > max_idle_seconds:
            pruned.append(candidate)
            bytes_freed += size
            if not dry_run:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    pass
        else:
            kept.append(candidate)

    return GCResult(
        pruned=pruned, kept=kept, bytes_freed=bytes_freed,
        threshold_seconds=max_idle_seconds, dry_run=dry_run,
    )


def clear_caches(project_path: Path | None = None, *, all_projects: bool = False) -> int:
    """Remove cached SQLite files. Returns count of files removed.

    - project_path given: remove caches for that project only.
    - all_projects=True: remove the entire cache root.
    """
    if all_projects:
        if not CACHE_ROOT.exists():
            return 0
        count = sum(1 for _ in CACHE_ROOT.rglob("*.sqlite"))
        shutil.rmtree(CACHE_ROOT)
        return count

    if project_path is None:
        return 0
    directory = project_cache_dir(project_path)
    if not directory.exists():
        return 0
    count = sum(1 for _ in directory.glob("*.sqlite"))
    shutil.rmtree(directory)
    return count


def write_meta(project_path: Path, *, latest_hash: str | None = None) -> None:
    directory = ensure_cache_dir(project_path)
    meta = _read_meta(directory)
    meta["project_path"] = str(project_path.resolve())
    if latest_hash is not None:
        meta["latest_hash"] = latest_hash
    meta_path = directory / META_FILENAME
    meta_path.write_text(json.dumps(meta, indent=2))


def _read_meta(directory: Path) -> dict:
    path = directory / META_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# --- Index hashing ----------------------------------------------------------

def compute_index_hash(
    index_store_path: Path,
    *,
    swift_version: str | None = None,
    helper_version: str | None = None,
) -> str:
    """Compute a content-addressed hash for the IndexStore.

    Inputs:
        - filename, size, mtime_ns of every file in <store>/v5/units/
        - swift_version (USRs change between compiler versions)
        - helper_version (helper output shape changes invalidate cached SQLite)
    """
    units_dir = index_store_path / "v5" / "units"
    if not units_dir.exists():
        raise FileNotFoundError(f"units dir missing: {units_dir}")

    digest = hashlib.md5()
    for entry in sorted(units_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except FileNotFoundError:
            continue
        digest.update(entry.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(stat.st_size).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))
        digest.update(b"\x00")
    if swift_version:
        digest.update(b"swift:")
        digest.update(swift_version.encode("utf-8"))
        digest.update(b"\x00")
    if helper_version:
        digest.update(b"helper:")
        digest.update(helper_version.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


# --- Locking + atomic write -------------------------------------------------

@contextlib.contextmanager
def acquire_lock(project_path: Path, timeout_seconds: float = 600.0) -> Iterator[None]:
    """Acquire an exclusive lock on the project's cache dir.

    Used to serialize concurrent dumps. Other processes block until the lock holder
    releases (typically after writing the SQLite atomically).
    """
    directory = ensure_cache_dir(project_path)
    lock_path = directory / LOCK_FILENAME
    deadline = time.monotonic() + timeout_seconds
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"timed out waiting for cache lock at {lock_path}"
                    )
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write(target: Path, payload_path: Path) -> None:
    """Move payload_path to target via os.replace (atomic on same filesystem)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(payload_path), str(target))


@contextlib.contextmanager
def staged_write(target: Path) -> Iterator[Path]:
    """Yield a temp path; on success, atomically rename it to target.

    Caller writes their payload to the yielded path. On normal exit we fsync the
    file (best effort) and rename. On exception, the temp file is removed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    try:
        yield temp_path
        if temp_path.exists():
            try:
                with temp_path.open("rb") as fh:
                    os.fsync(fh.fileno())
            except OSError:
                pass
            os.replace(str(temp_path), str(target))
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


# --- Garbage collection -----------------------------------------------------

def gc_caches(project_path: Path, keep_last_n_legacy: int = KEEP_LAST_N_LEGACY) -> int:
    """Trim legacy snapshots to the `keep_last_n_legacy` most-recent.

    The live `index.sqlite` is never collected. Only `legacy_*.sqlite` (and any
    un-migrated `<hash>.sqlite` from older v1 layouts) are subject to GC.
    Returns the number of files removed.
    """
    directory = project_cache_dir(project_path)
    if not directory.exists():
        return 0
    legacy = [
        p for p in directory.glob("*.sqlite")
        if p.name != LIVE_SQLITE_NAME
    ]
    legacy.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    to_remove = legacy[keep_last_n_legacy:]
    removed = 0
    for sqlite_file in to_remove:
        with contextlib.suppress(FileNotFoundError):
            sqlite_file.unlink()
            removed += 1
    return removed

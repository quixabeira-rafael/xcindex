"""Filesystem watcher that triggers `prewarm` when the IndexStore changes.

Designed as a long-running but stateless process: subscribes to FSEvents on
the IndexStore's `units/` directory, debounces bursts of writes from a single
build, and spawns `xcindex prewarm --quiet` as a subprocess on each settled
event. The watcher itself holds no SQLite connection in memory — its only
state is the on-disk JSON status file used by `xcindex doctor`.

Resilience:
  - prewarm subprocess failures are logged + counted; the watcher keeps running.
  - watchdog observer crashes are caught + the watcher exits cleanly.
  - SIGINT/SIGTERM trigger a clean shutdown that releases the PID lock.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from xcindex import cache as cache_module


class WatchError(Exception):
    """Raised when the watcher cannot start (already running, missing path, ...)."""


@dataclass
class WatchState:
    """Mutable status persisted at `<cache>/<project>/watch.json`."""
    pid: int
    project_path: str
    index_store_path: str
    started_at: str
    debounce_ms: int
    last_event_at: str | None = None
    last_prewarm_at: str | None = None
    last_prewarm_mode: str | None = None
    last_prewarm_seconds: float | None = None
    prewarm_count: int = 0
    prewarm_errors: int = 0
    last_error: str | None = None
    last_error_at: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> WatchState:
        return cls(**data)


# --- Path helpers -----------------------------------------------------------


def state_path(project_path: Path) -> Path:
    """JSON status file location: ~/.cache/xcindex/<fingerprint>/watch.json."""
    return cache_module.project_cache_dir(project_path) / "watch.json"


def read_state(project_path: Path) -> WatchState | None:
    """Load the status file if present and parseable, else None."""
    path = state_path(project_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return WatchState.from_json(data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def write_state(project_path: Path, state: WatchState) -> None:
    path = state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), indent=2))


def clear_state(project_path: Path) -> None:
    path = state_path(project_path)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# --- Liveness ---------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_watcher_running(project_path: Path) -> bool:
    """True only if a state file exists AND the recorded pid is alive."""
    state = read_state(project_path)
    return state is not None and _pid_alive(state.pid)


def acquire_watch_lock(project_path: Path) -> None:
    """Refuse to start if another live watcher claims this project.
    Cleans up stale state files (pid not alive) automatically."""
    state = read_state(project_path)
    if state is not None:
        if _pid_alive(state.pid):
            raise WatchError(
                f"another `xcindex watch` is already running for this project "
                f"(pid={state.pid}, started {state.started_at})"
            )
        # stale lock; clean it up
        clear_state(project_path)


# --- Debouncer --------------------------------------------------------------


class Debouncer:
    """Coalesces a burst of trigger() calls into a single callback.

    Each trigger() resets the wait timer. The callback fires once when the
    timer expires without further triggers, receiving the count of events
    that arrived in the burst.
    """

    def __init__(self, wait_seconds: float, callback: Callable[[int], None]) -> None:
        self.wait = wait_seconds
        self.callback = callback
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending = 0

    def trigger(self) -> None:
        with self._lock:
            self._pending += 1
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.wait, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            count = self._pending
            self._pending = 0
            self._timer = None
        if count:
            self.callback(count)

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = 0


# --- Main loop --------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_relevant_event(path: str, units_dir: Path) -> bool:
    """Accept any file event whose path lies inside the units directory.

    Unit files in the IndexStore don't share a single suffix — they're named
    after source files plus per-build hashes (e.g. `Foo.swift.o-2XK9...`,
    `arm.swiftinterface-1B92...`). The directory boundary is the right filter,
    not the suffix.
    """
    try:
        return Path(path).resolve().parent == units_dir.resolve()
    except OSError:
        return False


def _spawn_prewarm(project_path: Path) -> tuple[bool, dict, str]:
    """Run `xcindex prewarm --quiet --format json --project <path>` as subprocess.

    Returns (ok, parsed_summary, error_message). If JSON parsing fails or the
    subprocess errors, ok=False and error_message describes why.
    """
    cmd = [
        sys.executable,
        "-c",
        "import sys; from xcindex.cli import main; sys.exit(main(sys.argv[1:]))",
        "prewarm",
        "--quiet",
        "--format", "json",
        "--project", str(project_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {}, f"prewarm subprocess failed to run: {exc}"
    if proc.returncode != 0:
        return False, {}, f"prewarm exited {proc.returncode}: {proc.stderr.strip()[:200]}"
    try:
        payload = json.loads(proc.stdout)
        return True, payload.get("summary") or {}, ""
    except json.JSONDecodeError as exc:
        return False, {}, f"prewarm output not JSON: {exc}"


def run_watch_loop(
    project_path: Path,
    index_store_path: Path,
    *,
    debounce_seconds: float = 0.5,
    log_writer: Callable[[str], None] | None = None,
    _spawn_prewarm_override: Callable[[Path], tuple[bool, dict, str]] | None = None,
    _stop_after_seconds: float | None = None,
) -> int:
    """Block on FSEvents for the IndexStore and dispatch prewarm on settled bursts.

    Returns the process exit code: 0 on clean SIGINT/SIGTERM, non-zero on
    fatal setup error (the lock conflict is raised before this is called).
    `_spawn_prewarm_override` and `_stop_after_seconds` exist to make this
    deterministic in tests.
    """
    units_dir = index_store_path / "v5" / "units"
    if not units_dir.exists():
        raise WatchError(f"IndexStore units dir does not exist: {units_dir}")

    log = log_writer or (lambda msg: print(msg, flush=True))
    spawn = _spawn_prewarm_override or _spawn_prewarm

    # Initial state
    state = WatchState(
        pid=os.getpid(),
        project_path=str(project_path),
        index_store_path=str(index_store_path),
        started_at=_now_utc_iso(),
        debounce_ms=int(debounce_seconds * 1000),
    )
    write_state(project_path, state)
    log(f"xcindex watch — pid {state.pid}, watching {units_dir} (debounce {state.debounce_ms}ms)")

    def on_settled(event_count: int) -> None:
        state.last_event_at = _now_utc_iso()
        log(f"[{state.last_event_at}] settled after {event_count} event(s); running prewarm...")
        ok, summary, err = spawn(project_path)
        state.last_prewarm_at = _now_utc_iso()
        state.prewarm_count += 1
        if ok:
            state.last_prewarm_mode = summary.get("mode")
            state.last_prewarm_seconds = float(summary.get("wall_seconds") or 0.0)
            log(
                f"[{state.last_prewarm_at}] prewarm: {state.last_prewarm_mode}"
                f" ({state.last_prewarm_seconds:.1f}s)"
            )
        else:
            state.prewarm_errors += 1
            state.last_error = err
            state.last_error_at = state.last_prewarm_at
            log(f"[{state.last_prewarm_at}] prewarm FAILED ({state.prewarm_errors}/{state.prewarm_count}): {err}")
        # persist state regardless of outcome — the watcher KEEPS running
        try:
            write_state(project_path, state)
        except OSError as state_exc:
            log(f"warning: could not write state file: {state_exc}")

    debouncer = Debouncer(debounce_seconds, on_settled)

    # Lazy import — watchdog isn't loaded at all in test paths that override spawn.
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            self._react(event.src_path)

        def on_modified(self, event):
            self._react(event.src_path)

        def on_moved(self, event):
            self._react(getattr(event, "dest_path", event.src_path))

        def _react(self, path: str) -> None:
            if not _is_relevant_event(path, units_dir):
                return
            debouncer.trigger()

    observer = Observer()
    observer.schedule(_Handler(), str(units_dir), recursive=False)
    observer.start()

    stopped = threading.Event()

    def _shutdown(signum, frame):
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    deadline = None
    if _stop_after_seconds is not None:
        deadline = time.monotonic() + _stop_after_seconds

    exit_code = 0
    try:
        while not stopped.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            stopped.wait(timeout=0.5)
    except Exception as exc:
        log(f"watcher loop crashed: {exc}")
        exit_code = 1
    finally:
        observer.stop()
        observer.join(timeout=2)
        debouncer.cancel()
        clear_state(project_path)
        log("xcindex watch stopped")

    return exit_code

"""Append-only state under the Hermes root.

Lines are not rewritten. A torn tail is kept beside the file; a bad committed
line makes the file unreadable.
"""

import contextlib
import fcntl
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RECORD_VERSION = 1


class RecordError(ValueError):
    """A committed line cannot be read. Checks must not pass."""


def state_dir(root: Path) -> Path:
    return root / "taskwatch" / "state"


class State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = state_dir(root)
        # flock on a second descriptor deadlocks this thread. Nested calls reuse
        # the owner only in this process; a forked child must take the file lock.
        self._held = threading.local()

    @contextmanager
    def lock(self) -> Iterator[None]:
        if getattr(self._held, "depth", 0) and getattr(self._held, "pid", None) == os.getpid():
            self._held.depth += 1
            try:
                yield
            finally:
                self._held.depth -= 1
            return
        _make_dirs(self.dir)
        handle = (self.dir / "lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._held.depth = 1
            self._held.pid = os.getpid()
            try:
                yield
            finally:
                self._held.depth = 0
                self._held.pid = None
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def append_record(self, name: str, payload: Mapping[str, object], now: datetime) -> None:
        """One versioned JSON line in a state file that has no typed reader (runs, rejects)."""
        with self.lock():
            _append_line(self.dir / name, {"version": RECORD_VERSION, **payload}, now)

    def read_records(self, name: str) -> tuple[dict, ...]:
        """Committed lines of such a file. A bad line raises RecordError."""
        with self.lock():
            return _read_objects(self.dir / name)


def _append_line(path: Path, payload: Mapping[str, object], now: datetime) -> None:
    _repair_torn(path, now)
    _read_objects(path)
    _append_validated_line(path, payload)


def _append_validated_line(path: Path, payload: Mapping[str, object]) -> None:
    """Append after the caller validated committed records and repaired the tail."""
    line = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    _make_dirs(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        try:
            _write_all(fd, line)
            if size == 0:
                _publish(fd, path)
            else:
                os.fsync(fd)
        except BaseException:
            # A line that was not synced may still be visible. Cut back to the
            # size before this call and sync the cut, so the caller's failure and
            # the file agree after a restart. If the cut fails the line stays, with
            # a body that is already durable. If the sync of the cut fails, a
            # restart may bring the line back and a retry then sees a duplicate.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, size)
                os.fsync(fd)
            raise
    finally:
        os.close(fd)


def _repair_torn(path: Path, now: datetime) -> Path | None:
    if not path.exists():
        return None
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        if not size:
            return None
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return None
        end = size
        chunks = []
        while end:
            start = max(0, end - 8192)
            handle.seek(start)
            chunk = handle.read(end - start)
            newline = chunk.rfind(b"\n")
            chunks.append(chunk[newline + 1 :] if newline >= 0 else chunk)
            if newline >= 0:
                break
            end = start
        fragment = b"".join(reversed(chunks))
        keep = size - len(fragment)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = Path(f"{path}.torn-{stamp}")
    suffix = 1
    while dest.exists():
        dest = Path(f"{path}.torn-{stamp}-{suffix}")
        suffix += 1
    # Name the fragment durably before truncating. Rewriting the prefix would
    # empty the file first, so a crash there would drop confirmed lines.
    _create_new(dest, fragment, 0o644)
    # Only the uncommitted suffix is removed.
    fd = os.open(path, os.O_RDWR)
    try:
        os.ftruncate(fd, keep)
        os.fsync(fd)
    finally:
        os.close(fd)
    return dest


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs(path: Path) -> None:
    """Create missing directories; fsync the parent of every component, every call.

    A name is published only after its parent's fsync succeeded. A failed fsync
    leaves the directory in place, so a retry must sync the parent again instead
    of trusting that the directory exists.
    """
    for directory in reversed((path, *path.parents)):
        if directory.parent == directory:
            continue
        if not directory.exists():
            directory.mkdir(exist_ok=True)
        _fsync(directory.parent)


def _publish(fd: int, path: Path) -> None:
    """A new file is durable only when its bytes and the directory entry naming it are."""
    os.fsync(fd)
    _fsync(path.parent)


def _create_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        _write_all(fd, data)
        _publish(fd, path)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _committed(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RecordError(f"{path.name}: unreadable") from exc
    if not data:
        return ""
    if not data.endswith(b"\n"):
        newline = data.rfind(b"\n")
        data = b"" if newline < 0 else data[: newline + 1]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecordError(f"{path.name}: not utf-8") from exc


def _read_objects(path: Path) -> tuple[dict, ...]:
    if not path.exists():
        return ()
    text = _committed(path)
    objects = []
    for lineno, line in enumerate(text.splitlines(), 1):
        where = f"{path.name}:{lineno}"
        if not line:
            raise RecordError(f"{where}: empty line")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordError(f"{where}: not json") from exc
        if not isinstance(obj, dict):
            raise RecordError(f"{where}: not an object")
        version = obj.get("version")
        if isinstance(version, bool) or version != RECORD_VERSION:
            raise RecordError(f"{where}: unknown version")
        objects.append(obj)
    return tuple(objects)

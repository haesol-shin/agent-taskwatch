"""File-backed execution for one-shot agent commands, without job or reply policy."""

import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DIAGNOSTIC_CHARS = 4000
_INPUT_CHARS = 16384
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout_path: Path
    stderr_path: Path
    aborted: bool


class RunError(Exception):
    """Execution failed; the message is bounded and raw logs remain on disk."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message[-DIAGNOSTIC_CHARS:])
        self.returncode = returncode


def tail(path: Path, size: int = DIAGNOSTIC_CHARS) -> str:
    """Read at most `size` trailing bytes, decoded as a bounded UTF-8 excerpt."""
    if size <= 0:
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - size))
            return stream.read(size).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _diagnostic(path: Path, message: str, returncode: int | None = None) -> RunError:
    detail = tail(path)
    # Keep the failure category even when stderr fills the diagnostic allowance.
    prefix = message[: DIAGNOSTIC_CHARS // 2]
    room = DIAGNOSTIC_CHARS - len(prefix) - 2
    return RunError(f"{prefix}: {detail[-room:]}" if detail else prefix, returncode)


class _Input:
    def __init__(self, message: str) -> None:
        self.chunks = (
            message[start : start + _INPUT_CHARS].encode("utf-8")
            for start in range(0, len(message), _INPUT_CHARS)
        )
        self.pending = memoryview(next(self.chunks, b""))
        self.done = not self.pending

    def send(self, fd: int) -> None:
        try:
            written = os.write(fd, self.pending)
        except BlockingIOError:
            return
        if written == 0:
            raise RunError("the agent command stdin feed made no progress")
        self.pending = self.pending[written:]
        if not self.pending:
            self.pending = memoryview(next(self.chunks, b""))
            self.done = not self.pending


def _supervise(
    proc: subprocess.Popen,
    message: str,
    deadline: float,
    every: float,
    tick: Callable[[], None],
    abort: threading.Event,
) -> bool:
    assert proc.stdin is not None
    feed = _Input(message)
    os.set_blocking(proc.stdin.fileno(), False)
    next_tick = time.monotonic() + every
    with selectors.DefaultSelector() as selector:
        if feed.done:
            proc.stdin.close()
        else:
            selector.register(proc.stdin, selectors.EVENT_WRITE)
        while True:
            if abort.is_set():
                return True
            now = time.monotonic()
            if now >= deadline:
                raise RunError("the agent command exceeded its execution deadline")
            if now >= next_tick:
                tick()
                next_tick = time.monotonic() + every
                continue
            if proc.poll() is not None:
                if not feed.done:
                    raise RunError("the agent command exited before stdin feed completed")
                return False
            delay = min(_POLL_SECONDS, deadline - now, next_tick - now)
            for key, _ in selector.select(max(0, delay)):
                try:
                    feed.send(key.fd)
                except OSError as exc:
                    raise RunError(f"the agent command stdin feed failed: {exc}") from exc
                if feed.done:
                    selector.unregister(key.fd)
                    proc.stdin.close()


def _group_running(pgid: int) -> bool:
    # A zombie thread-group leader can still have live siblings sharing its files.
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                fields = Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and (fields[0] not in {"Z", "X", "x"} or int(fields[17]) > 1):
                return True
    return False


def _stop(proc: subprocess.Popen) -> None:
    # The leader may already be reaped while a descendant still owns the group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        finally:
            proc.wait()
            # SIGKILL delivery is asynchronous, and wait() only covers the leader.
            # Observe all same-group members finishing exit_files before return.
            while _group_running(proc.pid):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(_POLL_SECONDS)


def run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    message: str,
    *,
    deadline: float,
    every: float,
    tick: Callable[[], None],
    abort: threading.Event,
    stdout_path: Path,
    stderr_path: Path,
) -> Result:
    """Feed UTF-8 and EOF, supervise until exit, and retain both raw output files.

    `deadline` is absolute monotonic time and includes input feeding. Cancellation
    returns an aborted Result; other execution failures raise RunError. Tick errors
    are wrapped with their original exception as the cause. Every exit tears down
    remaining same-group descendants, not deliberately detached processes.
    """
    if every <= 0:
        raise RunError("the agent command tick interval must be positive")
    if stdout_path.resolve() == stderr_path.resolve():
        raise RunError("the agent command stdout and stderr paths must be distinct")
    try:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                bufsize=0,
                start_new_session=True,
            )
            try:
                aborted = _supervise(proc, message, deadline, every, tick, abort)
            finally:
                _stop(proc)
        if proc.returncode != 0 and not aborted:
            raise RunError(f"the agent command exited with code {proc.returncode}", proc.returncode)
        return Result(proc.returncode, stdout_path, stderr_path, aborted)
    except Exception as exc:
        returncode = exc.returncode if isinstance(exc, RunError) else None
        raise _diagnostic(stderr_path, str(exc), returncode) from exc

"""Synthetic commands exercise the neutral subprocess lifecycle without model calls."""

import os
import signal
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from taskwatch import agent_process


def launch(tmp_path, code, message="", *, seconds=3, tick=lambda: None, abort=None):
    return agent_process.run(
        [sys.executable, "-c", code],
        tmp_path,
        dict(os.environ),
        message,
        deadline=time.monotonic() + seconds,
        every=0.01,
        tick=tick,
        abort=abort if abort is not None else threading.Event(),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )


def assert_dead(pid):
    # Check immediately: waiting here would hide an incomplete teardown barrier.
    try:
        status = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return
    assert status in {"Z", "X", "x"}, f"child {pid} still executes after supervisor completion"


def test_unicode_input_is_complete_and_eof_is_delivered(tmp_path):
    message = "Unicode: 한글 🌱\n" * 20000
    result = launch(
        tmp_path,
        "import sys; "
        "sys.stdout.buffer.write(sys.stdin.buffer.read()); "
        "sys.stderr.write('EOF reached')",
        message,
    )
    assert result == agent_process.Result(
        0, tmp_path / "stdout.log", tmp_path / "stderr.log", False
    )
    assert result.stdout_path.read_bytes() == message.encode("utf-8")
    assert result.stderr_path.read_text() == "EOF reached"


def test_cwd_and_environment_are_inherited_exactly(tmp_path):
    result = agent_process.run(
        [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['SYNTHETIC'])"],
        tmp_path,
        {"SYNTHETIC": "fixture.invalid"},
        "",
        deadline=time.monotonic() + 3,
        every=1,
        tick=lambda: None,
        abort=threading.Event(),
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
    )
    assert result.stdout_path.read_text().splitlines() == [str(tmp_path), "fixture.invalid"]


def test_large_stdout_and_stderr_are_file_backed_with_bounded_memory(tmp_path):
    tracemalloc.start()
    try:
        result = launch(
            tmp_path,
            "import os, sys\n"
            "sys.stdin.buffer.read()\n"
            "for _ in range(256):\n"
            " os.write(1, b'o' * 32768)\n"
            " os.write(2, b'e' * 32768)\n",
            seconds=5,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.stdout_path.stat().st_size == 8 * 1024 * 1024
    assert result.stderr_path.stat().st_size == 8 * 1024 * 1024
    assert peak < 2 * 1024 * 1024
    with result.stdout_path.open("rb") as stream:
        assert stream.read(32) == b"o" * 32
    with result.stderr_path.open("rb") as stream:
        assert stream.read(32) == b"e" * 32


@pytest.mark.parametrize("message", ["", "x" * (4 * 1024 * 1024)], ids=["wait", "feed"])
def test_abort_interrupts_wait_and_unread_input(tmp_path, message):
    abort = threading.Event()
    ready = tmp_path / "ready"
    ticks = []

    def tick():
        ticks.append(time.monotonic())
        if ready.exists():
            abort.set()

    started = time.monotonic()
    result = launch(
        tmp_path,
        "import os, pathlib, signal; "
        "pathlib.Path('ready').write_text(str(os.getpid())); signal.pause()",
        message,
        tick=tick,
        abort=abort,
    )
    assert result.aborted and result.returncode == -signal.SIGKILL
    assert ticks and time.monotonic() - started < 3
    assert_dead(int(ready.read_text()))


@pytest.mark.parametrize("message", ["", "x" * (4 * 1024 * 1024)], ids=["wait", "feed"])
def test_absolute_deadline_covers_feed_and_wait(tmp_path, message):
    ticks = []
    started = time.monotonic()
    with pytest.raises(agent_process.RunError, match="execution deadline") as raised:
        launch(
            tmp_path,
            "import signal; signal.pause()",
            message,
            seconds=0.2,
            tick=lambda: ticks.append(time.monotonic()),
        )
    assert raised.value.returncode is None
    assert ticks
    assert time.monotonic() - started < 2
    assert (tmp_path / "stdout.log").exists()
    assert (tmp_path / "stderr.log").exists()


@pytest.mark.parametrize("failure", ["deadline", "feed", "tick"])
def test_http429_diagnostic_without_command_exit_has_no_returncode(tmp_path, failure):
    def tick():
        if failure == "tick" and (tmp_path / "ready").exists():
            raise RuntimeError("synthetic tick failure")

    close_stdin = "os.close(0)\n" if failure == "feed" else ""
    with pytest.raises(agent_process.RunError) as raised:
        launch(
            tmp_path,
            "import os, pathlib, signal\n"
            "os.write(2, b'HTTP 429 synthetic diagnostic')\n"
            "pathlib.Path('ready').touch()\n" + close_stdin + "signal.pause()\n",
            "x" * (4 * 1024 * 1024) if failure == "feed" else "",
            seconds=0.3 if failure == "deadline" else 3,
            tick=tick,
        )
    assert "HTTP 429" in str(raised.value)
    assert raised.value.returncode is None


def test_closed_input_and_zero_exit_are_not_success(tmp_path):
    with pytest.raises(agent_process.RunError, match="stdin feed") as raised:
        launch(
            tmp_path,
            "import os; os.close(0); os.write(1, b'not consumed'); os._exit(0)",
            "x" * (4 * 1024 * 1024),
        )
    assert raised.value.returncode is None
    assert (tmp_path / "stdout.log").read_bytes() == b"not consumed"


@pytest.mark.parametrize("exit_code", [17, -signal.SIGTERM])
def test_nonzero_and_signal_preserve_raw_logs_and_bounded_diagnostic(tmp_path, exit_code):
    ending = f"sys.exit({exit_code})" if exit_code > 0 else f"os.kill(os.getpid(), {-exit_code})"
    with pytest.raises(agent_process.RunError, match=f"code {exit_code}") as raised:
        launch(
            tmp_path,
            "import os, sys; sys.stdin.buffer.read(); "
            "os.write(1, b'raw output'); "
            "os.write(2, b'e' * (2 * 1024 * 1024) + b' final diagnostic'); " + ending,
        )
    assert len(str(raised.value)) <= agent_process.DIAGNOSTIC_CHARS
    assert raised.value.returncode == exit_code
    assert "final diagnostic" in str(raised.value)
    assert (tmp_path / "stderr.log").stat().st_size > 2 * 1024 * 1024
    assert (tmp_path / "stdout.log").read_bytes() == b"raw output"


def test_missing_executable_preserves_empty_logs(tmp_path):
    with pytest.raises(agent_process.RunError, match="No such file") as raised:
        agent_process.run(
            [str(tmp_path / "unavailable.invalid")],
            tmp_path,
            {},
            "prompt",
            deadline=time.monotonic() + 1,
            every=1,
            tick=lambda: None,
            abort=threading.Event(),
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )
    assert raised.value.returncode is None
    assert (tmp_path / "stdout.log").read_bytes() == b""
    assert (tmp_path / "stderr.log").read_bytes() == b""


@pytest.mark.parametrize("state", ["Z", "X", "x", "R", "S", "T", "D"])
@pytest.mark.parametrize("threads", [1, 2])
def test_group_barrier_ignores_only_exited_members(tmp_path, monkeypatch, state, threads):
    member = tmp_path / "123"
    member.mkdir()
    fields = [state, "1", "321", *(["0"] * 14), str(threads)]
    (member / "stat").write_text("123 (synthetic ) command) " + " ".join(fields))
    scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda _: scandir(tmp_path))
    assert agent_process._group_running(321) is (state not in {"Z", "X", "x"} or threads > 1)
    assert not agent_process._group_running(999)


@pytest.mark.parametrize("leader_exits", [False, True])
@pytest.mark.parametrize("zombie_main", [False, True])
def test_remaining_group_descendants_die_even_after_leader_exit(
    tmp_path, monkeypatch, leader_exits, zombie_main
):
    ready = tmp_path / "descendant"
    writes = tmp_path / "descendant-writes"
    killpg = os.killpg
    group_running = agent_process._group_running
    observed_running = []
    groups = []

    def delay_descendant_signal(pgid, sig):
        # Model asynchronous group signal delivery: the leader exits first.
        groups.append(pgid)
        try:
            os.kill(pgid, sig)
        except ProcessLookupError:
            pass

    def observe_then_deliver(pgid):
        running = group_running(pgid)
        if running:
            observed_running.append(pgid)
            killpg(pgid, signal.SIGKILL)
        return running

    monkeypatch.setattr(os, "killpg", delay_descendant_signal)
    monkeypatch.setattr(agent_process, "_group_running", observe_then_deliver)
    abort = threading.Event()

    def tick():
        if not leader_exits and ready.exists():
            abort.set()

    # The pipe handshake proves the descendant exists before the leader exits.
    try:
        result = launch(
            tmp_path,
            "import ctypes, os, pathlib, signal, threading, time\n"
            "r, w = os.pipe()\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            " os.close(r)\n"
            " def writer():\n"
            + (
                "  stat = pathlib.Path('/proc/self/stat')\n"
                "  while stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':\n"
                "   time.sleep(0.001)\n"
                if zombie_main
                else ""
            )
            + "  fd = os.open('descendant-writes', os.O_WRONLY | os.O_CREAT, 0o600)\n"
            "  os.write(fd, b'first write')\n"
            "  pathlib.Path('descendant').write_text(str(os.getpid()))\n"
            "  os.write(w, b'1')\n"
            "  os.close(w)\n"
            "  while True:\n"
            "   os.write(fd, b'x')\n"
            "   os.write(1, b'o')\n"
            "   os.write(2, b'e')\n"
            "   time.sleep(0.001)\n"
            + (
                " threading.Thread(target=writer).start()\n ctypes.CDLL(None).pthread_exit(None)\n"
                if zombie_main
                else " writer()\n"
            )
            + "os.close(w)\n"
            "os.read(r, 1)\n"
            "os.close(r)\n" + ("os._exit(0)\n" if leader_exits else "signal.pause()\n"),
            tick=tick,
            abort=abort,
        )
        assert result.aborted is not leader_exits
        assert observed_running
        assert_dead(int(ready.read_text()))
        paths = [writes, result.stdout_path, result.stderr_path]
        sizes = [path.stat().st_size for path in paths]
        threading.Event().wait(0.03)
        assert [path.stat().st_size for path in paths] == sizes
    finally:
        for pgid in groups:
            try:
                killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("message", ["", "x" * (4 * 1024 * 1024)], ids=["wait", "feed"])
def test_tick_exception_kills_group_reaps_leader_and_closes_descriptors(tmp_path, message):
    ready = tmp_path / "ready"
    before_fds = len(list(Path("/proc/self/fd").iterdir()))
    before_threads = set(threading.enumerate())

    def tick():
        if ready.exists():
            raise RuntimeError("synthetic tick failure")

    with pytest.raises(agent_process.RunError, match="synthetic tick failure") as raised:
        launch(
            tmp_path,
            "import os, pathlib, signal; "
            "pathlib.Path('ready').write_text(str(os.getpid())); signal.pause()",
            message,
            tick=tick,
        )
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.returncode is None
    pid = int(ready.read_text())
    assert_dead(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert len(list(Path("/proc/self/fd").iterdir())) == before_fds
    assert set(threading.enumerate()) == before_threads


def test_tail_is_bounded_and_handles_missing_and_partial_utf8(tmp_path):
    path = tmp_path / "stderr.log"
    assert agent_process.tail(path) == ""
    path.write_bytes(b"prefix" * 10000 + "한글".encode())
    assert agent_process.tail(path, 6) == "한글"
    assert agent_process.tail(path, 2) == "��"
    assert agent_process.tail(path, 0) == ""
    assert len(agent_process.tail(path)) <= agent_process.DIAGNOSTIC_CHARS

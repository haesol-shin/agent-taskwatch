"""OpenCode adapter protocol tests without a vendor CLI or model request."""

import errno
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "examples" / "worker" / "opencode.py"
spec = importlib.util.spec_from_file_location("opencode_worker", WRAPPER)
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def fake_cli(tmp_path, body):
    executable = tmp_path / "opencode"
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return {**os.environ, "PATH": str(tmp_path), "SYNTHETIC_VALUE": "inherited"}


@pytest.mark.parametrize("prompt", ["한글 🙂\nsecond line\n--not-an-option\n", ""])
def test_unicode_prompt_is_one_argument_with_inherited_execution_context(tmp_path, prompt):
    env = fake_cli(
        tmp_path,
        "import json, os, sys\n"
        "print(json.dumps([sys.argv[1:], os.getcwd(), os.environ['SYNTHETIC_VALUE'], "
        "os.getpid()]))\n"
        "print('synthetic stderr', file=sys.stderr)",
    )
    process = subprocess.Popen(
        [sys.executable, str(WRAPPER)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(prompt.encode("utf-8"), timeout=5)
    assert process.returncode == 0
    assert json.loads(stdout) == [["run", "--", prompt], str(tmp_path), "inherited", process.pid]
    assert stderr == b"synthetic stderr\n"


def test_option_shaped_prompt_is_not_a_cli_option(tmp_path):
    env = fake_cli(
        tmp_path,
        "import argparse, json, sys\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('command')\n"
        "parser.add_argument('message')\n"
        "args = parser.parse_args()\n"
        "print(json.dumps([args.command, args.message]))",
    )
    result = subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=b"--help",
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["run", "--help"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_exit_status_is_cli_status(tmp_path, exit_code):
    env = fake_cli(tmp_path, f"raise SystemExit({exit_code})")
    result = subprocess.run(
        [sys.executable, str(WRAPPER)], input=b"prompt", env=env, capture_output=True, timeout=5
    )
    assert result.returncode == exit_code


def test_signal_is_not_converted_into_an_exit_code(tmp_path):
    env = fake_cli(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)")
    result = subprocess.run(
        [sys.executable, str(WRAPPER)], input=b"prompt", env=env, capture_output=True, timeout=5
    )
    assert result.returncode == -signal.SIGTERM


@pytest.mark.parametrize(
    "prompt, diagnostic",
    [
        (b"bad\xff", "utf-8"),
        (b"embedded\0nul", "embedded NUL"),
        (b"x" * 131072, "limit"),
        (("🙂" * 40000).encode("utf-8"), "limit"),
    ],
    ids=["invalid-utf8", "nul", "oversize-ascii", "oversize-unicode"],
)
def test_invalid_prompt_fails_before_cli_launch(tmp_path, prompt, diagnostic):
    marker = tmp_path / "launched"
    env = fake_cli(tmp_path, "from pathlib import Path\nPath('launched').touch()")
    result = subprocess.run(
        [sys.executable, str(WRAPPER)],
        cwd=tmp_path,
        input=prompt,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert diagnostic in result.stderr.decode()
    assert not marker.exists()


def test_aggregate_size_includes_environment_and_pointers(monkeypatch):
    monkeypatch.setattr(adapter.os, "sysconf", lambda key: 4096 if key == "SC_PAGESIZE" else 8192)
    with pytest.raises(ValueError, match="aggregate"):
        adapter.check_size([b"opencode", b"run", b"x" * 3500], {"ENV": "y" * 3500})
    with pytest.raises(ValueError, match="aggregate"):
        adapter.check_size([b"opencode", b"run", b""], {str(i): "" for i in range(1000)})


def test_per_argument_size_counts_terminator(monkeypatch):
    monkeypatch.setattr(adapter.os, "sysconf", lambda key: 4096 if key == "SC_PAGESIZE" else 999999)
    with pytest.raises(ValueError, match="per-argument"):
        adapter.check_size([b"opencode", b"run", b"x" * 131072], {})


@pytest.mark.parametrize(
    "error, status, diagnostic",
    [(errno.ENOENT, 127, "not found"), (errno.E2BIG, 126, "E2BIG")],
)
def test_exec_errors_are_diagnosed_without_retry(monkeypatch, capsys, error, status, diagnostic):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"prompt"), encoding="utf-8"))
    calls = []

    def fail(file, argv):
        calls.append((file, argv))
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(adapter.os, "execvp", fail)
    assert adapter.main() == status
    assert len(calls) == 1
    assert diagnostic in capsys.readouterr().err


def test_missing_cli_is_a_launch_failure(tmp_path):
    result = subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=b"prompt",
        env={"PATH": str(tmp_path)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 127
    assert b"opencode executable not found" in result.stderr

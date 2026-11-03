#!/usr/bin/env python3
"""Forward UTF-8 stdin as one argument to OpenCode's documented run command."""

import errno
import os
import struct
import sys


def check_size(argv: list[bytes], environ: dict[str, str]) -> None:
    """Use conservative Linux limits, including NULs and argv/envp pointers."""
    per_argument = min(32 * os.sysconf("SC_PAGESIZE"), 128 * 1024)
    aggregate = min(os.sysconf("SC_ARG_MAX"), 128 * 1024)
    strings = [*argv, *(os.fsencode(f"{key}={value}") for key, value in environ.items())]
    if any(b"\0" in value for value in strings):
        raise ValueError("embedded NUL in prompt, argv or environment")
    if any(len(value) + 1 > per_argument for value in strings):
        raise ValueError("prompt, argument or environment entry exceeds Linux per-argument limit")
    total = sum(len(value) + 1 for value in strings)
    total += (len(strings) + 2) * struct.calcsize("P")
    # Reserve room for executable resolution and kernel bookkeeping.
    if total + 2048 > aggregate:
        raise ValueError("argv and environment exceed conservative Linux aggregate limit")


def main() -> int:
    try:
        prompt = sys.stdin.buffer.read()
        prompt.decode("utf-8")
        argv = [b"opencode", b"run", b"--", prompt]
        check_size(argv, os.environ)
        os.execvp(argv[0], argv)
    except (UnicodeError, ValueError) as exc:
        print(f"opencode-worker: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            diagnostic = "opencode executable not found"
        elif exc.errno == errno.E2BIG:
            diagnostic = "Linux rejected argv/environment size (E2BIG); prompt was not retried"
        else:
            diagnostic = f"cannot execute opencode: {exc.strerror}"
        print(f"opencode-worker: {diagnostic}", file=sys.stderr)
        return 127 if exc.errno == errno.ENOENT else 126
    return 126


if __name__ == "__main__":
    raise SystemExit(main())

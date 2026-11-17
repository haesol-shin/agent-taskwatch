"""Run the checks CI runs, locally, with CI's arguments.

The command selects the test files of changed modules instead of the whole suite: the
measured slow test and every path the selector cannot map stay for CI. Commit messages
of the branch range are checked with the same arguments as CI.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = Path("examples/omp/taskwatch")
SLOW_TESTS = frozenset({"tests/test_job_land.py"})
TAIL_LINES = 40
DEFAULT_BASE = "origin/main"

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Check:
    """One command CI runs, with the directory it runs in."""

    name: str
    argv: tuple[str, ...]
    cwd: Path


class GitError(Exception):
    """A git command the test selection needs failed."""


def run_command(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(("git", *args), cwd=root, check=False, capture_output=True, text=True)
    if done.returncode != 0:
        message = done.stderr.strip() or done.stdout.strip()
        raise GitError(f"git {' '.join(args)}: {message}")
    return done.stdout


def merge_base(base: str, root: Path) -> str:
    return _git(root, "merge-base", "HEAD", base).strip()


def changed_paths(base: str, root: Path) -> list[str]:
    """The committed paths CI sees between the merge base of HEAD and base and HEAD."""
    merge_base(base, root)
    output = _git(root, "diff", "--name-only", f"{base}...HEAD")
    return [line for line in output.splitlines() if line]


def tests_for_paths(paths: Iterable[str], root: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Return the test files to run and the changed paths left to CI, with a reason each."""
    selected: set[str] = set()
    only_ci: list[tuple[str, str]] = []
    for path in paths:
        test = _test_for_path(path, root)
        if test is None:
            only_ci.append((path, "not mapped"))
        elif test in SLOW_TESTS:
            only_ci.append((path, "measured slow"))
        else:
            selected.add(test)
    return sorted(selected), only_ci


def _test_for_path(path: str, root: Path) -> str | None:
    if path.startswith("taskwatch/") and path.endswith(".py"):
        candidate = f"tests/test_{Path(path).stem}.py"
        return candidate if (root / candidate).is_file() else None
    if path.startswith("tests/test_") and path.endswith(".py") and (root / path).is_file():
        return path
    return None


def build_checks(root: Path, tests: Sequence[str], prefix: Path, base: str) -> tuple[Check, ...]:
    """The CI commands, in the order a local run reports them, against the merge base."""
    checks = [
        Check("uv sync", ("uv", "sync", "--locked"), root),
        Check("ruff check", ("uv", "run", "ruff", "check"), root),
        Check("ruff format", ("uv", "run", "ruff", "format", "--check"), root),
        Check("review docs", ("uv", "run", "python", "scripts/check_review_docs.py"), root),
        Check("install package", ("uv", "pip", "install", "--prefix", str(prefix), "."), root),
        Check(
            "installed review docs",
            (
                "uv",
                "run",
                "python",
                "scripts/check_review_docs.py",
                "--tree",
                str(prefix / "share"),
            ),
            root,
        ),
        Check(
            "commit messages",
            (
                "uv",
                "run",
                "python",
                "scripts/check_commit_messages.py",
                "--range",
                f"{base}..HEAD",
            ),
            root,
        ),
    ]
    if tests:
        checks.append(Check("pytest", ("uv", "run", "pytest", *tests), root))
    extension = root / EXTENSION
    checks += [
        Check("bun install", ("bun", "install", "--frozen-lockfile"), extension),
        Check("bun typecheck", ("bun", "run", "typecheck"), extension),
        Check("bun test", ("bun", "run", "test"), extension),
    ]
    return tuple(checks)


def _tail(text: str) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-TAIL_LINES:])


def _captured(done: subprocess.CompletedProcess[str]) -> str:
    return f"{done.stdout or ''}{done.stderr or ''}".rstrip()


def run_checks(checks: Iterable[Check], runner: Runner, out: TextIO) -> bool:
    """Run each check until one fails, keeping each check's output either way."""
    for check in checks:
        print(f"start: {check.name}", file=out, flush=True)
        done = runner(check.argv, check.cwd)
        output = _captured(done)
        if done.returncode != 0:
            print(f"fail: {check.name} (exit {done.returncode})", file=out)
            tail = _tail(output)
            if tail:
                print(tail, file=out)
            return False
        if output:
            print(output, file=out)
        print(f"ok: {check.name}", file=out)
    return True


def main(
    argv: list[str] | None = None,
    runner: Runner = run_command,
    root: Path = ROOT,
    out: TextIO | None = None,
) -> int:
    stream = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="ref to compare HEAD with")
    args = parser.parse_args(argv)
    try:
        base = merge_base(args.base, root)
        paths = changed_paths(args.base, root)
    except GitError as error:
        print(error, file=sys.stderr)
        return 2
    tests, only_ci = tests_for_paths(paths, root)
    print(f"fast check: base {base[:12]}", file=stream)
    for path, reason in only_ci:
        print(f"CI only: {path} ({reason})", file=stream)
    if tests:
        print(f"tests: {' '.join(tests)}", file=stream)
    else:
        print("tests: no test file selected; CI only", file=stream)
    with tempfile.TemporaryDirectory(prefix="check-fast-") as folder:
        checks = build_checks(root, tests, Path(folder) / "installed", base)
        if not run_checks(checks, runner, stream):
            return 1
    print("CI only: extension smoke (starts services)", file=stream)
    print("fast check: ok", file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

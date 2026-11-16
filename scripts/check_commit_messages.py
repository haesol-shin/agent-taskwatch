"""Check commit messages against the job runner's common rules.

The rules live in `taskwatch.checks.message_problems` so a job check and a local
or CI check of a hand-written commit cannot drift apart. Merge commits are
skipped: a branch update made by GitHub carries a message this check would
reject, and the commits it brings in are checked on their own.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from taskwatch.checks import message_problems


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return done.stdout


def check_range(rev_range: str) -> int:
    try:
        commits = _git("rev-list", "--reverse", "--parents", rev_range).splitlines()
    except subprocess.CalledProcessError as error:
        print(f"git rev-list failed: {error.stderr.strip()}", file=sys.stderr)
        return 2
    failed = False
    for commit in commits:
        sha, *parents = commit.split()
        if len(parents) > 1:
            continue
        try:
            message = _git("log", "-1", "--format=%B", sha)
        except subprocess.CalledProcessError as error:
            print(f"git log failed for {sha}: {error.stderr.strip()}", file=sys.stderr)
            return 2
        for problem in message_problems(message):
            print(f"{sha}: {problem}")
            failed = True
    if failed:
        return 1
    print("ok")
    return 0


def check_message_file(path: str) -> int:
    try:
        message = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        print(f"{path}: cannot read: {error}", file=sys.stderr)
        return 2
    problems = message_problems(message)
    for problem in problems:
        print(f"{path}: {problem}")
    if problems:
        return 1
    print("ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--range", dest="rev_range", metavar="BASE..HEAD")
    source.add_argument("--message-file", metavar="PATH")
    args = parser.parse_args(argv)
    if args.message_file is not None:
        return check_message_file(args.message_file)
    return check_range(args.rev_range)


if __name__ == "__main__":
    raise SystemExit(main())

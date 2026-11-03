"""Result fingerprints.

A Git fingerprint is the commit SHA, and only if the work copy has no
uncommitted changes. A non-Git fingerprint is the hash of the sorted
path list, so an add, delete, or rename cannot keep the old value.
"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

_SHA = r"[0-9a-f]{40}"


class FingerprintError(ValueError):
    """The result cannot be identified or frozen."""


def git_fingerprint(sha: str) -> str:
    if re.fullmatch(_SHA, sha) is None:
        raise FingerprintError("git fingerprint must be a full lowercase sha")
    return sha


def uncommitted_changes(repo: Path, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Porcelain lines, including untracked files.

    A review of a SHA does not cover changes that are not in that commit.
    """
    proc = subprocess.run(
        _git_cmd(
            repo, ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"]
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "git status failed"
        raise FingerprintError(detail)
    return tuple(line for line in proc.stdout.splitlines() if line.strip())


def head_sha(repo: Path, env: Mapping[str, str] | None = None) -> str:
    """The commit the work copy is on. The fingerprint of a Git result is this value."""
    return git_fingerprint(_git_ok(repo, ["rev-parse", "HEAD"], env=env).decode("utf-8").strip())


def resolve_commit(repo: Path, ref: str, env: Mapping[str, str] | None = None) -> str:
    """The full sha a ref names, or a FingerprintError."""
    out = _git_ok(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], env=env)
    return git_fingerprint(out.decode("utf-8").strip())


def is_ancestor(
    repo: Path, ancestor: str, descendant: str, env: Mapping[str, str] | None = None
) -> bool:
    """Whether `ancestor` is `descendant` or reachable from it; a FingerprintError on any other
    git failure, so an unreadable repository is never read as an answer."""
    proc = subprocess.run(
        _git_cmd(repo, ["merge-base", "--is-ancestor", ancestor, descendant]),
        check=False,
        capture_output=True,
        env=env,
    )
    if proc.returncode not in {0, 1}:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "git merge-base failed"
        raise FingerprintError(detail)
    return proc.returncode == 0


def range_messages(
    repo: Path, base: str, head: str, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """(sha, message) of each commit of `base..head`, oldest first."""
    out = _git_ok(
        repo, ["log", "--reverse", "--format=%H%n%B%x00", f"{base}..{head}", "--"], env=env
    ).decode("utf-8", "replace")
    rows = []
    for chunk in out.split("\0"):
        sha, _, message = chunk.lstrip("\n").partition("\n")
        if sha:
            rows.append((sha, message.rstrip("\n")))
    return rows


def tree_sha(repo: Path, commit: str, env: Mapping[str, str] | None = None) -> str:
    out = _git_ok(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"], env=env)
    return git_fingerprint(out.decode("utf-8").strip())


EVIDENCE_PREFIX = "Review evidence:"


def without_evidence(body: str) -> str:
    """The body without its one `Review evidence:` line, which names the review result."""
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if line.startswith(EVIDENCE_PREFIX):
            del lines[index]
            break
    return "\n".join(lines).rstrip("\n")


def text_fingerprint(title: str, body: str, messages: Sequence[str], commit: str | None) -> str:
    """Hash reviewed text and step messages; None reads historical two-file evidence."""
    rows = [title.rstrip("\n"), without_evidence(body), [m.rstrip("\n") for m in messages]]
    if commit is not None:
        rows = [2, title, without_evidence(body), [m.rstrip("\n") for m in messages], commit]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def files_fingerprint(root: Path, relative_paths: Sequence[str]) -> str:
    return _list_hash(_file_rows(root, relative_paths))


def _file_rows(root: Path, relative_paths: Sequence[str]) -> list[tuple[str, str]]:
    # The one place a non-Git source is hashed. The fingerprint and the copy's
    # expected digests both come from this list, so a change between two hashes
    # cannot give the copy a different name than its content.
    return [(_logical(rel), _sha256(_file_under(root, rel))) for rel in relative_paths]


def basis_fingerprint(files: Sequence[tuple[str, Path]]) -> str:
    """Same list hash as a non-Git result, for intent, spec, and guidelines."""
    rows = []
    for name, path in files:
        logical = _logical(name)
        if not path.is_file():
            raise FingerprintError(f"missing basis file: {logical}")
        rows.append((logical, _sha256(path)))
    return _list_hash(rows)


def _git_cmd(repo: Path, args: Sequence[str]) -> list[str]:
    # A replace ref can swap the bytes behind an approved object id.
    return ["git", "--no-replace-objects", "-C", os.fspath(repo.absolute()), *args]


def _git_ok(
    repo: Path,
    args: list[str],
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    proc = subprocess.run(
        _git_cmd(repo, args),
        input=stdin,
        check=False,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise FingerprintError(detail)
    return proc.stdout


def _list_hash(rows: Sequence[tuple[str, str]]) -> str:
    ordered = sorted(rows)
    if len(ordered) != len(set(ordered)):
        raise FingerprintError("duplicate fingerprint path")
    if len({name for name, _digest in ordered}) != len(ordered):
        raise FingerprintError("duplicate fingerprint path")
    payload = "".join(f"{name}\t{digest}\n" for name, digest in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _logical(value: str) -> str:
    # Tab and newline separate rows in the list hash. A path that contains a
    # control character can impersonate another list, so those paths are refused
    # instead of length-prefixing every row.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise FingerprintError(f"path contains a control character: {value}")
    pure = PurePosixPath(value)
    if not value or "\\" in value or pure.is_absolute() or ".." in pure.parts or pure.parts == ():
        raise FingerprintError(f"path escapes root: {value}")
    return pure.as_posix()


def _file_under(root: Path, relative: str) -> Path:
    logical = _logical(relative)
    root_resolved = root.resolve()
    try:
        candidate = (root / logical).resolve()
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise FingerprintError(f"path escapes root: {logical}") from exc
    if not candidate.is_file():
        raise FingerprintError(f"missing output: {logical}")
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

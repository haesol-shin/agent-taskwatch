"""`file:line` pointers in text: find them, and check that each names a file that has the line."""

import re
from pathlib import Path, PurePosixPath

from .fingerprint import _git_ok, git_fingerprint

# A path with an extension, then `:line` or `:first-last`. A URL or a clock time does not match:
# the path must start at a boundary that is not `/`, `:`, `.`, `-` or a word character.
POINTER = re.compile(r"(?<![\w.:/-])(/?(?:[\w.-]+/)*[\w-][\w.-]*\.\w+):(\d+)(?:-(\d+))?")


def pointers(text: str) -> list[tuple[str, int, int]]:
    return [
        (m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) for m in POINTER.finditer(text)
    ]


def resolve(rel: str, roots: tuple[Path, ...]) -> Path | None:
    """The existing file `rel` names under one of the roots, else None."""
    for root in roots:
        try:
            base = root.resolve()
            candidate = (base / rel).resolve()
        except OSError:
            continue
        if candidate.is_file() and (candidate == base or base in candidate.parents):
            return candidate
    return None


def pointer_problem(
    rel: str,
    first: int,
    last: int,
    roots: tuple[Path, ...],
    *,
    commit: tuple[Path, str] | None = None,
) -> list[str]:
    if commit is not None:
        repo, sha = commit
        try:
            git_fingerprint(sha)
            _git_ok(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
            name = PurePosixPath(rel)
            if name.is_absolute() or ".." in name.parts:
                return [f"pointer names no file: {rel}"]
            entry = _git_ok(repo, ["ls-tree", "-z", sha, "--", name.as_posix()])
            if entry:
                if entry.split(b"\t", 1)[0].split()[1] != b"blob":
                    return [f"pointer names no file: {rel}"]
                content = _git_ok(repo, ["cat-file", "blob", f"{sha}:{name.as_posix()}"])
                return _range_problem(
                    rel, first, last, len(content.decode("utf-8", errors="replace").splitlines())
                )
        except (OSError, ValueError) as exc:
            return [f"pointer commit cannot be read: {rel}: {exc}"]
    path = resolve(rel, roots)
    if path is None:
        return [f"pointer names no file: {rel}"]
    try:
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return [f"pointer file cannot be read: {rel}: {exc}"]
    return _range_problem(rel, first, last, count)


def _range_problem(rel: str, first: int, last: int, count: int) -> list[str]:
    if first < 1 or last < first or last > count:
        return [f"pointer {rel}:{first}-{last} is outside the file ({count} lines)"]
    return []

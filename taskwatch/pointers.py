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


def _commit_content(repo: Path, sha: str, name: PurePosixPath) -> bytes | None:
    """Resolve checkout-style links without consulting the mutable working tree."""
    pending = list(name.parts)
    resolved: list[str] = []
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if not resolved:
                raise ValueError("symlink target leaves the repository")
            resolved.pop()
            continue
        path = "/".join((*resolved, part))
        entry = _git_ok(repo, ["ls-tree", "-z", sha, "--", path])
        if not entry:
            if hops:
                raise ValueError("symlink target is missing")
            return None
        mode, kind, oid = entry.split(b"\t", 1)[0].split()
        if mode == b"120000":
            hops += 1
            if hops > 40:
                raise ValueError("symlink hop limit exceeded")
            target = PurePosixPath(_git_ok(repo, ["cat-file", "blob", oid.decode()]).decode())
            if target.is_absolute():
                raise ValueError("symlink target leaves the repository")
            pending = [*target.parts, *pending]
        elif kind == b"tree" and pending:
            resolved.append(part)
        elif kind == b"blob" and not pending:
            return _git_ok(repo, ["cat-file", "blob", oid.decode()])
        elif not hops:
            # A plain path that does not name a file in the commit may be job-folder evidence.
            return None
        else:
            raise ValueError("pointer does not name a file")
    raise ValueError("pointer does not name a file")


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
            content = _commit_content(repo, sha, name)
            if content is not None:
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

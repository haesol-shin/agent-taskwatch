"""`file:line` pointers in text: find them, and check that each names a file that has the line."""

import re
from pathlib import Path

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


def pointer_problem(rel: str, first: int, last: int, roots: tuple[Path, ...]) -> list[str]:
    path = resolve(rel, roots)
    if path is None:
        return [f"pointer names no file: {rel}"]
    try:
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return [f"pointer file cannot be read: {rel}: {exc}"]
    if first < 1 or last < first or last > count:
        return [f"pointer {rel}:{first}-{last} is outside the file ({count} lines)"]
    return []

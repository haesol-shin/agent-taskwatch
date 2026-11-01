"""The worker's part of a job's decision brief: `brief.md` in the job folder.

The contract is in docs/kinds/brief.md. This module checks only what code can check: the five
headings, the size limits, and that every `file:line` pointer names a file that has the line.
Whether the brief is complete and honest is the reviewer's rule C-19.
"""

import re
from pathlib import Path

SECTIONS = (
    "Why",
    "What changed and why",
    "Choices made",
    "Decisions needed",
    "Where to look",
)
MAX_LINES = 60
MAX_WHY_LINES = 3
MAX_ITEMS = 8
# A path with an extension, then `:line` or `:first-last`. A URL or a clock time does not match:
# the path must start at a boundary that is not `/`, `:`, `.`, `-` or a word character.
POINTER = re.compile(r"(?<![\w.:/-])(/?(?:[\w.-]+/)*[\w-][\w.-]*\.\w+):(\d+)(?:-(\d+))?")
_HEADING = re.compile(r"^##\s+(.*?)\s*$")


def sections(text: str) -> dict[str, list[str]]:
    """The non-empty lines under each `## <name>` heading, by name."""
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            current = match.group(1)
            found.setdefault(current, [])
        elif current is not None and line.strip():
            found[current].append(line.strip())
    return found


def problems(path: Path, roots: tuple[Path, ...]) -> list[str]:
    """Why the brief at `path` fails the contract; empty when it passes."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"required file is missing: {path.name}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path.name} cannot be read: {exc}"]
    found = sections(text)
    issues = [f"{path.name}: missing section `## {name}`" for name in SECTIONS if name not in found]
    issues += [
        f"{path.name}: section `{name}` is empty"
        for name in SECTIONS
        if name in found and _empty(name, found[name])
    ]
    if len(text.splitlines()) > MAX_LINES:
        issues.append(f"{path.name}: more than {MAX_LINES} lines")
    issues += _size_problems(found)
    for rel, first, last in _pointers(text):
        issues += _pointer_problem(rel, first, last, roots)
    return issues


def _empty(name: str, lines: list[str]) -> bool:
    """`none` is an answer only for Decisions needed."""
    if not lines:
        return True
    return name != "Decisions needed" and len(lines) == 1 and lines[0].lower().strip(".`") == "none"


def _size_problems(found: dict[str, list[str]]) -> list[str]:
    issues = []
    if len(found.get("Why", [])) > MAX_WHY_LINES:
        issues.append(f"brief.md: Why is more than {MAX_WHY_LINES} lines")
    changed = [line for line in found.get("What changed and why", []) if line.startswith("-")]
    if len(changed) > MAX_ITEMS:
        issues.append(f"brief.md: What changed and why has more than {MAX_ITEMS} bullets")
    issues += [
        f"brief.md: bullet does not end with a file:line pointer: {line[:60]}"
        for line in changed
        if not _ends_with_pointer(line)
    ]
    if len(found.get("Where to look", [])) > MAX_ITEMS:
        issues.append(f"brief.md: Where to look has more than {MAX_ITEMS} lines")
    return issues


def _ends_with_pointer(line: str) -> bool:
    last = None
    for last in POINTER.finditer(line):
        pass
    return last is not None and not line[last.end() :].strip(" .,;:)`*_")


def _pointers(text: str) -> list[tuple[str, int, int]]:
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


def _pointer_problem(rel: str, first: int, last: int, roots: tuple[Path, ...]) -> list[str]:
    path = resolve(rel, roots)
    if path is None:
        return [f"brief.md: pointer names no file: {rel}"]
    try:
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return [f"brief.md: pointer file cannot be read: {rel}: {exc}"]
    if first < 1 or last < first or last > count:
        return [f"brief.md: pointer {rel}:{first}-{last} is outside the file ({count} lines)"]
    return []

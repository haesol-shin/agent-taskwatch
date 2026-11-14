"""Where the shared review documents live, which of them a review reads, and their links.

The documents ship as one tree: the package docs at its root, their review folder, the launch
and job contracts, and the examples their relative links name. A job copies the documents its
guides reach, in the installed shape, and both the install and the copy are checked with the
same link rule, so a relative link of a guide resolves wherever the documents are read.
"""

import re
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

PROFILES = {
    "code": "code.md",
    "design": "design.md",
    "source": "source.md",
}
COMMON = ("README.md", "common.md")


SOURCE_OUTPUT = "claims.json"

# The folder the package documents occupy in the installed share tree, and the review folder
# inside it. The example files their links name ship beside the package at examples/.
PACKAGE_DIR = "agent-taskwatch"
GUIDE_DIR = "review"

INLINE_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
REF_DEF = re.compile(
    r"^[ ]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|\S+)",
    re.MULTILINE,
)


class DocsError(Exception):
    """The shipped documents are missing, so a copy of them would be incomplete."""


def profile_names(review: str, outputs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The documents a review reads. A job whose outputs include the claims file also answers to
    the source rules, whatever its own review profile (a design with research included)."""
    names = (*COMMON, PROFILES[review])
    if SOURCE_OUTPUT in outputs and PROFILES["source"] not in names:
        names = (*names, PROFILES["source"])
    return names


def all_names() -> tuple[str, ...]:
    return (*COMMON, *PROFILES.values())


def docs_root() -> Path | None:
    """The folder the installed documents mirror: a checkout's docs folder, else the installed
    `share/agent-taskwatch` folder."""
    for base in _doc_roots():
        if base.is_dir():
            return base
    return None


def docs_dir(folder: str) -> Path | None:
    """The source checkout's docs folder, else the one installed with the package."""
    for base in _doc_roots():
        if (base / folder).is_dir():
            return base / folder
    return None


def _doc_roots() -> tuple[Path, ...]:
    return (
        Path(__file__).resolve().parent.parent / "docs",
        Path(sys.prefix) / "share" / "agent-taskwatch",
    )


def guide_dir(target: Path) -> Path:
    """The review folder inside a job's copy of the shipped documents."""
    return target / PACKAGE_DIR / GUIDE_DIR


def copy_name(folder: Path, path: Path) -> str:
    """The name a copied document has in the review basis: its path under the job's
    `review-docs` copy, or its file name for a record from before the copy kept the shape."""
    try:
        return path.relative_to(folder / "review-docs").as_posix()
    except ValueError:
        return path.name


def shipped_files(root: Path) -> list[Path]:
    """Every shipped file of a copy or an install under `root`."""
    return sorted(
        path
        for name in (PACKAGE_DIR, "examples")
        for path in (root / name).rglob("*")
        if path.is_file()
    )


def shipped_markdown(root: Path) -> list[Path]:
    """The shipped Markdown of a copy or an install under `root`."""
    return [path for path in shipped_files(root) if path.suffix == ".md"]


def copy_docs(target: Path, names: Iterable[str]) -> list[Path]:
    """Copy the shipped documents the named guides reach into `target` in the installed shape,
    and return every shipped file of the copy. A file already there stays, so a job keeps the
    copy it started with; guides a record wrote flat move into the shape before it is filled."""
    docs = docs_root()
    if docs is None:
        raise DocsError("the review documents were not found")
    names = tuple(dict.fromkeys(names))
    base = target.resolve()
    _migrate(target, names)
    pending: list[Path] = []
    for name in names:
        path = guide_dir(target) / name
        if not path.is_file():
            _copy(docs / GUIDE_DIR / name, path)
        pending.append(path)
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if path.suffix != ".md":
            continue
        for local in _links(path):
            resolved = (path.parent / local).resolve()
            relative = _inside(resolved, base, local)
            copied = base / relative
            if not copied.is_file():
                source = _source(relative, docs)
                if not source.is_file():
                    shown = path.relative_to(base).as_posix()
                    raise DocsError(f"{shown}: link does not resolve: {local}")
                _copy(source, copied)
            pending.append(copied)
    return shipped_files(target)


def _migrate(target: Path, names: tuple[str, ...]) -> None:
    """A record from before the copy kept the installed shape wrote its guides flat; move those
    bytes into the shape, so the job keeps the text it recorded."""
    for name in names:
        legacy = target / name
        path = guide_dir(target) / name
        if legacy.is_file() and not path.is_file():
            _copy(legacy, path)


def _links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    targets = (local_target(target) for target in link_targets(text))
    return [local for local in targets if local is not None]


def _inside(resolved: Path, base: Path, local: str) -> Path:
    try:
        return resolved.relative_to(base)
    except ValueError:
        raise DocsError(f"a guide link leaves the shipped tree: {local}") from None


def _source(relative: Path, docs: Path) -> Path:
    parts = relative.parts
    if parts and parts[0] == PACKAGE_DIR:
        return docs / Path(*parts[1:])
    if parts and parts[0] == "examples":
        return docs.parent / "examples" / Path(*parts[1:])
    raise DocsError(f"a guide link names a file the install does not ship: {relative.as_posix()}")


def _copy(source: Path, dest: Path) -> None:
    if not source.is_file():
        raise DocsError(f"the shipped document is missing: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)


def link_targets(text: str) -> list[str]:
    """The targets of the inline and reference links of a Markdown text."""
    return [*INLINE_LINK.findall(text), *REF_DEF.findall(text)]


def local_target(target: str) -> str | None:
    """The local path a Markdown link names, or None for a web, anchor or empty target."""
    raw = target.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        raw = "" if end < 1 else raw[1:end].strip()
    elif raw:
        raw = raw.split()[0]
    if raw.startswith(("http://", "https://", "mailto:")) or raw.startswith("#") or raw == "":
        return None
    return raw.split("#", 1)[0]


def link_problems(root: Path) -> list[str]:
    """Every relative Markdown link of the shipped documents under `root` must name a file or
    folder inside the shipped tree."""
    if not root.is_dir():
        return [f"missing document tree: {root}"]
    bases = [(root / name).resolve() for name in (PACKAGE_DIR, "examples")]
    problems: list[str] = []
    for path in shipped_markdown(root):
        shown = path.relative_to(root).as_posix()
        for local in _links(path):
            resolved = (path.parent / local).resolve()
            if not any(resolved.is_relative_to(base) for base in bases):
                problems.append(f"{shown}: link leaves the tree: {local}")
            elif not resolved.exists():
                problems.append(f"{shown}: link does not resolve: {local}")
    return problems

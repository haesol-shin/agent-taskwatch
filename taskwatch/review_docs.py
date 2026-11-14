"""Where the shared review documents live, which of them a review reads, and their links.

The documents ship as one tree: the package docs at its root, their review folder, the launch
and job contracts, and the examples their relative links name. The install keeps that tree
under `share`, the job copy keeps the same shape, and both are checked the same way, so a
relative link of a guide resolves wherever the documents are read.
"""

import re
import shutil
import sys
from pathlib import Path

PROFILES = {
    "code": "code.md",
    "design": "design.md",
    "source": "source.md",
}
COMMON = ("README.md", "common.md")


SOURCE_OUTPUT = "claims.json"

# The folder the package documents occupy in the installed share tree, the review folder inside
# it, and the files that ship beside them. The example files are named by links in the documents.
PACKAGE_DIR = "agent-taskwatch"
GUIDE_DIR = "review"
ROOT_DOCS = (
    "checks.md",
    "commands.md",
    "configuration.md",
    "review-launch.md",
    "spec-template.md",
)
SUBDIRS = ("review", "launch", "jobs")
EXAMPLES = (
    "hermes/skills/taskwatch/SKILL.md",
    "omp/reviewer.md",
    "worker/opencode.py",
)

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


def shipped_files(root: Path) -> list[tuple[Path, Path]]:
    """Each shipped file of the tree and its path in the installed share tree."""
    pairs = [(root / name, Path(PACKAGE_DIR) / name) for name in ROOT_DOCS]
    for folder in SUBDIRS:
        pairs += [
            (path, Path(PACKAGE_DIR) / folder / path.name)
            for path in sorted((root / folder).glob("*"))
            if path.is_file()
        ]
    examples = root.parent / "examples"
    return [*pairs, *((examples / name, Path("examples") / name) for name in EXAMPLES)]


def guide_dir(target: Path) -> Path:
    """The review folder inside a job's copy of the shipped documents."""
    return target / PACKAGE_DIR / GUIDE_DIR


def copy_docs(target: Path) -> Path:
    """Copy the shipped documents into `target` in the installed shape and return the review
    folder inside it. A file already there stays, so a job keeps the copy it started with."""
    root = docs_root()
    if root is None:
        raise DocsError("the review documents were not found")
    pairs = shipped_files(root)
    missing = [str(source) for source, _ in pairs if not source.is_file()]
    if missing:
        raise DocsError("the review documents are incomplete: " + ", ".join(missing))
    for source, relative in pairs:
        path = target / relative
        if path.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
    guides = guide_dir(target)
    absent = [name for name in all_names() if not (guides / name).is_file()]
    if absent:
        raise DocsError("the review guides are missing: " + ", ".join(absent))
    return guides


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


def shipped_markdown(root: Path) -> list[Path]:
    """The Markdown files of the shipped tree under an install root. Other files of the root
    are not shipped, so a job copy that still holds the older flat guides ignores them."""
    return sorted(
        path for name in (PACKAGE_DIR, "examples") for path in (root / name).rglob("*.md")
    )


def link_problems(root: Path) -> list[str]:
    """Every relative Markdown link of the shipped documents under `root` must name a file or
    folder inside the shipped tree."""
    if not root.is_dir():
        return [f"missing document tree: {root}"]
    bases = [(root / name).resolve() for name in (PACKAGE_DIR, "examples")]
    problems: list[str] = []
    for path in shipped_markdown(root):
        shown = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        targets = (local_target(target) for target in link_targets(text))
        for local in dict.fromkeys(target for target in targets if target is not None):
            resolved = (path.parent / local).resolve()
            if not any(resolved.is_relative_to(base) for base in bases):
                problems.append(f"{shown}: link leaves the tree: {local}")
            elif not resolved.exists():
                problems.append(f"{shown}: link does not resolve: {local}")
    return problems

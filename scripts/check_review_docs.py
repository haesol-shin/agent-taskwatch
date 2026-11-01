"""Check the shape of docs/review. No model call and no meaning check."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_FILES = (
    "README.md",
    "common.md",
    "code.md",
    "design.md",
    "source.md",
)

# Headings the reviewer relies on. A rename fails this check on purpose.
REQUIRED_SECTIONS = {
    "README.md": ("How to read", "Project rules", "Rule ids", "Sources"),
    "common.md": (
        "Precedence",
        "Purpose",
        "Intent and spec",
        "Evidence",
        "Grouping",
        "Severity",
        "Categories",
        "Rounds",
        "Incomplete review",
        "Repository conventions",
        "Finding content",
        "Design questions",
        "Brief",
        "Sources",
    ),
    "code.md": (
        "What to review",
        "Reporting bar",
        "Quality",
        "Failure modes",
        "Public text",
        "Scope and write targets",
        "Re-review",
        "Sources",
    ),
    "design.md": (
        "What to review",
        "Decision method",
        "Scope",
        "Splitting a plan",
        "Sources",
    ),
    "source.md": ("What to review", "Coverage", "Sources"),
}

RULE_ID = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}-\d+)(?![A-Za-z0-9])")
INLINE_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
REF_DEF = re.compile(
    r"^[ ]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|\S+)",
    re.MULTILINE,
)
HEADING = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


def repo_review_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / "review"


def check_review_dir(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return [f"missing review dir: {root}"]
    texts: dict[str, str] = {}
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file():
            errors.append(f"missing file: {name}")
            continue
        texts[name] = path.read_text(encoding="utf-8")
        errors.extend(_section_errors(name, texts[name]))
        errors.extend(_link_errors(path, texts[name]))
    errors.extend(_duplicate_id_errors(texts))
    return errors


def _section_errors(name: str, text: str) -> list[str]:
    found = HEADING.findall(text)
    missing = [section for section in REQUIRED_SECTIONS[name] if section not in found]
    return [f"{name}: missing section: {section}" for section in missing]


def _link_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    targets = [*INLINE_LINK.findall(text), *REF_DEF.findall(text)]
    for target in targets:
        local = _local_target(target)
        if local is None:
            continue
        resolved = (path.parent / local).resolve()
        if not resolved.is_file() and not resolved.is_dir():
            errors.append(f"{path.name}: missing local reference: {local}")
    return errors


def _local_target(target: str) -> str | None:
    raw = target.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        raw = "" if end < 1 else raw[1:end].strip()
    elif raw:
        raw = raw.split()[0]
    if raw.startswith(("http://", "https://", "mailto:")) or raw.startswith("#") or raw == "":
        return None
    return raw.split("#", 1)[0]


def _duplicate_id_errors(texts: dict[str, str]) -> list[str]:
    owners: dict[str, list[str]] = {}
    errors: list[str] = []
    for name, text in texts.items():
        seen: set[str] = set()
        for rule_id in RULE_ID.findall(text):
            if rule_id in seen:
                errors.append(f"{name}: duplicate rule id: {rule_id}")
            seen.add(rule_id)
            owners.setdefault(rule_id, []).append(name)
    for rule_id, names in sorted(owners.items()):
        unique = sorted(set(names))
        if len(unique) > 1:
            errors.append(f"rule id {rule_id} appears in: {', '.join(unique)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check docs/review shape.")
    parser.add_argument("--root", type=Path, default=repo_review_dir())
    args = parser.parse_args(argv)
    errors = check_review_dir(args.root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

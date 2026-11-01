"""Deterministic checks: the checklist a job keeps and the common commit message rules."""

import json
import re
from pathlib import Path


def checklist_problems(path: str | None) -> tuple[str, ...] | None:
    if not path or not Path(path).is_file():
        return ("missing",)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        return None
    failing = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("passes"), bool):
            return None
        if not item["passes"]:
            failing.append(str(item.get("id", "item")))
    return tuple(failing)


LINE_MAX = 72
_SUBJECT = re.compile(r"[a-z]+(\([^()\s]+\))?: \S.*")
_TRAILER = re.compile(
    r"(co-authored-by|signed-off-by|reviewed-by|acked-by|tested-by|reported-by|"
    r"suggested-by|helped-by|assisted-by|generated-by|cc|change-id):",
    re.IGNORECASE,
)


_BULLET = re.compile(r"[-*] \S")
_INDENTED_TEXT = re.compile(r"[ \t]+(?![-*] )\S")


def message_problems(message: str, *, bullet_one_line: bool = False) -> list[str]:
    """The common commit message rules: subject shape and length, layout, line length, trailers.

    `bullet_one_line` (opt-in) also fails a body bullet continued on an indented line.
    Meaning is not judged here; the reviewer reads what the message says.
    """
    lines = message.rstrip("\n").split("\n")
    subject = lines[0]
    problems = []
    if _SUBJECT.fullmatch(subject) is None:
        problems.append("subject is not `type(scope): summary` or `type: summary`")
    if len(subject) > LINE_MAX:
        problems.append(f"subject is {len(subject)} characters, the limit is {LINE_MAX}")
    if len(lines) > 1 and (lines[1] != "" or (len(lines) > 2 and lines[2] == "")):
        problems.append("one blank line must follow the subject")
    for number, line in enumerate(lines[1:], start=2):
        if len(line) > LINE_MAX:
            problems.append(f"line {number} is {len(line)} characters, the limit is {LINE_MAX}")
        if _TRAILER.match(line):
            problems.append(f"line {number} is a trailer: {line.split(':', 1)[0]}")
    if bullet_one_line:
        problems += _wrapped_bullets(lines)
    return problems


def _wrapped_bullets(lines: list[str]) -> list[str]:
    problems = []
    bullet = 0
    for number, line in enumerate(lines[1:], start=2):
        if _BULLET.match(line):
            bullet = number
        elif bullet and _INDENTED_TEXT.match(line):
            problems.append(f"line {number} continues the bullet on line {bullet}")
        else:
            bullet = 0
    return problems

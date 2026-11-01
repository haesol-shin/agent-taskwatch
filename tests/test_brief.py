from pathlib import Path

import pytest

from taskwatch import brief

GOOD = """## Why
Synthetic job.

## What changed and why
- Added the helper. See src/a.py:2
- Updated the guide (docs/g.md:1-2)

## Choices made
| choice | picked | reason | source | who |
|---|---|---|---|---|
| layout | flat | repo rule | AGENTS.md:1 | default |

## Decisions needed
none

## Where to look
src/a.py:3
"""


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    work, folder = tmp_path / "work", tmp_path / "folder"
    (work / "src").mkdir(parents=True)
    (work / "docs").mkdir()
    folder.mkdir()
    (work / "src/a.py").write_text("a\nb\nc\n", encoding="utf-8")
    (work / "docs/g.md").write_text("one\ntwo\n", encoding="utf-8")
    (work / "AGENTS.md").write_text("rule\n", encoding="utf-8")
    return work, folder


def check(roots, text: str) -> list[str]:
    path = roots[1] / "brief.md"
    path.write_text(text, encoding="utf-8")
    return brief.problems(path, roots)


def test_a_complete_brief_passes(roots) -> None:
    assert check(roots, GOOD) == []


def test_a_missing_file_is_one_problem(roots) -> None:
    assert brief.problems(roots[1] / "brief.md", roots) == ["required file is missing: brief.md"]


def test_a_missing_or_empty_heading_fails(roots) -> None:
    assert check(roots, GOOD.replace("## Why\n", "## Reason\n")) == [
        "brief.md: missing section `## Why`"
    ]
    assert check(roots, GOOD.replace("Synthetic job.\n", "")) == [
        "brief.md: section `Why` is empty"
    ]


def test_none_answers_only_decisions_needed(roots) -> None:
    assert check(roots, GOOD.replace("Synthetic job.", "none")) == [
        "brief.md: section `Why` is empty"
    ]


@pytest.mark.parametrize(
    ("pointer", "message"),
    [
        ("src/missing.py:1", "pointer names no file: src/missing.py"),
        ("src/a.py:4", "outside the file (3 lines)"),
        ("src/a.py:0", "outside the file"),
        ("docs/g.md:2-3", "outside the file (2 lines)"),
        ("../outside.py:1", "pointer names no file"),
    ],
)
def test_a_pointer_must_name_an_existing_line(roots, pointer: str, message: str) -> None:
    problems = check(roots, GOOD.replace("src/a.py:3", pointer))
    assert len(problems) == 1 and message in problems[0]


def test_urls_and_times_are_not_pointers(roots) -> None:
    text = GOOD.replace("Synthetic job.", "See https://example.invalid/x.html:80 at 10:30.")
    assert check(roots, text) == []


def test_a_bullet_needs_a_pointer_and_the_lists_are_bounded(roots) -> None:
    assert check(roots, GOOD.replace(" See src/a.py:2", " Done")) == [
        "brief.md: bullet does not end with a file:line pointer: - Added the helper. Done"
    ]
    many = "".join("- Added the helper. See src/a.py:2\n" for _ in range(9))
    crowded = GOOD.replace("- Added", many + "- Added", 1)
    assert any("more than 8 bullets" in p for p in check(roots, crowded))
    long_why = GOOD.replace("Synthetic job.", "a\nb\nc\nd")
    assert check(roots, long_why) == ["brief.md: Why is more than 3 lines"]


def test_the_brief_is_at_most_sixty_lines(roots) -> None:
    padded = GOOD.replace("none\n", "none\n" + "\n" * 60, 1)
    assert check(roots, padded) == ["brief.md: more than 60 lines"]

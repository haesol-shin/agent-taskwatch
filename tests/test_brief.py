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


# --- composition ---------------------------------------------------------------------------


def _job(tmp_path: Path, brief_text: str, findings: list[dict]):
    from dataclasses import replace

    from taskwatch.jobs import JobConfig, Jobs
    from tests.test_jobs import payload

    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 1000.0)
    folder = jobs.folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "brief.md").write_text(brief_text, encoding="utf-8")
    review = {
        "verdict": "PASS",
        "findings": findings,
        "checked": "the result against the spec",
        "not_checked": "other sources",
    }
    job = replace(
        job,
        stage="done",
        reviews=(review,),
        events=({"type": "submit", "at": 1000.0},),
        ended_at=1000.0 + 3 * 3600 + 60,
    )
    return jobs, JobConfig(), job


def _finding(n: int, severity: str = "P2") -> dict:
    return {
        "finding": f"F{n}",
        "severity": severity,
        "category": "clarity",
        "rule": f"K-{n}",
        "location": f"doc.md:{n}",
    }


def test_the_card_is_cut_to_25_lines_and_the_full_brief_keeps_everything(tmp_path: Path) -> None:
    decisions = "".join(f"{n}. Choose option {n}\n" for n in range(1, 11))
    text = GOOD.replace("none\n", decisions, 1)
    jobs, config, job = _job(tmp_path, text, [_finding(n) for n in range(1, 8)])
    card = brief.compose(jobs, config, job, card=True)
    full = brief.compose(jobs, config, job)
    assert len(card) <= 25
    assert "... 6 more in the full brief" in card
    assert "... 5 more in the full brief" in card
    assert sum(line.startswith("- F") for line in full) == 7
    assert sum(line[:3] == "10." for line in full) == 1
    assert "Reviewer checked: the result against the spec" in full
    assert not any(line.startswith("Reviewer") for line in card)


def test_the_facts_count_open_findings_and_time(tmp_path: Path) -> None:
    jobs, config, job = _job(tmp_path, GOOD, [_finding(1), _finding(2, "P3"), _finding(3, "P3")])
    facts = brief.compose(jobs, config, job)[:5]
    assert facts[1] == "Verdict: PASS; review round 1; no check; 3h 1m"
    assert facts[4] == "Open findings: P2 1, P3 2"


@pytest.mark.parametrize(
    ("body", "count"),
    [
        ("none\n", 0),
        ("1. a\n2. b\n3) c\n", 3),
        ("Pick a layout.\n", 1),
    ],
)
def test_decision_count_reads_numbered_items(body: str, count: int) -> None:
    assert brief.decision_count(GOOD.replace("none\n", body, 1)) == count
    assert brief.decision_count("") == 0


def test_a_finding_line_has_its_id_and_the_reviewers_fix(tmp_path: Path) -> None:
    jobs, config, job = _job(tmp_path, GOOD, [_finding(1), _finding(2, "P3")])
    body = (
        "### F1\nThe text is vague. Fix: name the file in the sentence.\n{json}\n### F2\nNo fix.\n"
    )
    review = {**job.reviews[0], "body": body}
    out = brief.compose(jobs, config, replace_job(job, review))
    assert "- F1 P2 K-1 (clarity): doc.md:1 -- fix: name the file in the sentence." in out
    assert "- F2 P3 K-2 (clarity): doc.md:2" in out


def test_the_cap_holds_the_whole_output_and_keeps_the_tail() -> None:
    body = [f"line {n}" for n in range(100)]
    tail = ("Worker brief: /x/brief.md", "reply: ok")
    out = brief.cap(body, tail)
    assert len(out) == 80
    assert out[-2:] == list(tail)
    assert out[-3] == "... 23 more lines omitted from this output"
    assert out[-4] == "line 76"
    assert brief.cap(body[:78], tail) == [*body[:78], *tail]


def replace_job(job, review):
    from dataclasses import replace

    return replace(job, reviews=(review,))

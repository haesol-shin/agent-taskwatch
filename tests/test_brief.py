import os
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

## Card
Synthetic job done; nothing for you to decide.
- The helper now exists.
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


def test_the_full_brief_keeps_every_decision_and_finding(tmp_path: Path) -> None:
    decisions = "".join(f"{n}. Choose option {n}\n" for n in range(1, 11))
    text = GOOD.replace("none\n", decisions, 1)
    jobs, config, job = _job(tmp_path, text, [_finding(n) for n in range(1, 8)])
    full = brief.compose(jobs, config, job)
    assert sum(line.startswith("- F") for line in full) == 7
    assert sum(line[:3] == "10." for line in full) == 1
    assert "Reviewer checked: the result against the spec" in full
    assert not any("more in the full brief" in line for line in full)


R1_CARD = """## Card
R1 bundle passed review.
- A step starts within 2 seconds of the one before.
- Approval is not asked before the earlier job ends.
- Each result says why it was done and what was decided.
"""


def _card_job(tmp_path: Path, card: str, findings: list[dict], not_checked: str, language="en"):
    from dataclasses import replace

    jobs, config, job = _job(tmp_path, GOOD.split("## Card")[0] + card, findings)
    review = {**job.reviews[0], "not_checked": not_checked}
    return jobs, replace(config, language=language), replace_job(job, review)


def _summary_finding(n: int, severity: str = "P2", summary: str | None = None) -> dict:
    return {**_finding(n, severity), "summary": summary or f"Problem {n} for the user."}


def test_the_card_is_the_conclusion_and_only_the_lists_that_have_content(tmp_path: Path) -> None:
    jobs, config, job = _card_job(tmp_path, R1_CARD, [], "runtime")
    assert brief.card(jobs, config, job) == [
        "R1 bundle passed review.",
        "",
        "Changed",
        "- A step starts within 2 seconds of the one before.",
        "- Approval is not asked before the earlier job ends.",
        "- Each result says why it was done and what was decided.",
        "",
        "Not checked",
        "- runtime",
    ]
    jobs, config, job = _card_job(tmp_path, "## Card\nDone.\n", [], "")
    assert brief.card(jobs, config, job) == ["Done."]


def test_the_card_takes_small_problems_from_summaries_worst_first(tmp_path: Path) -> None:
    findings = [_summary_finding(1, "P3"), _summary_finding(2, "P2"), _finding(3, "P3")]
    jobs, config, job = _card_job(tmp_path, R1_CARD, findings, "x")
    lines = brief.card(jobs, config, job)
    start = lines.index("Small problems left")
    assert lines[start : start + 4] == [
        "Small problems left",
        "- Problem 2 for the user.",
        "- Problem 1 for the user.",
        "",
    ]
    assert not any("doc.md" in line or "K-3" in line for line in lines)


def test_a_finding_that_blocks_never_reaches_the_card(tmp_path: Path) -> None:
    findings = [_summary_finding(1, "P1"), _summary_finding(2, "P0")]
    jobs, config, job = _card_job(tmp_path, R1_CARD, findings, "x")
    assert "Small problems left" not in brief.card(jobs, config, job)


def test_a_summary_of_the_full_length_is_not_cut(tmp_path: Path) -> None:
    summary = "s" * brief.MAX_CARD_BULLET_CHARS
    jobs, config, job = _card_job(tmp_path, R1_CARD, [_summary_finding(1, summary=summary)], "x")
    assert f"- {summary}" in brief.card(jobs, config, job)


def test_not_checked_splits_on_semicolons(tmp_path: Path) -> None:
    jobs, config, job = _card_job(tmp_path, R1_CARD, [], "the live host; the rollback;  ")
    assert brief.card(jobs, config, job)[-3:] == [
        "Not checked",
        "- the live host",
        "- the rollback",
    ]


def test_the_korean_labels(tmp_path: Path) -> None:
    findings = [_summary_finding(1)]
    jobs, config, job = _card_job(tmp_path, R1_CARD, findings, "x", language="ko")
    lines = brief.card(jobs, config, job)
    assert [x for x in lines if x and not x.startswith(("-", "R1"))] == [
        "\ubc14\ub010 \uac83",
        "\ub0a8\uc740 \uc791\uc740 \ubb38\uc81c",
        "\ud655\uc778\ud558\uc9c0 \ubabb\ud55c \uac83",
    ]


def test_a_seventh_item_shows_as_a_count_and_a_long_item_is_cut(tmp_path: Path) -> None:
    findings = [_summary_finding(n) for n in range(1, 9)]
    jobs, config, job = _card_job(tmp_path, R1_CARD, findings, "w" * 300)
    lines = brief.card(jobs, config, job)
    start = lines.index("Small problems left")
    assert lines[start + 6 : start + 8] == ["- Problem 6 for the user.", "- +2"]
    cut = lines[-1]
    assert len(cut) == brief.MAX_CARD_BULLET_CHARS + 2 and cut.endswith("\u2026")


def test_the_worst_card_fits_a_telegram_message(tmp_path: Path) -> None:
    summary = "s" * 150
    items = "; ".join("n" * 300 for _ in range(9))
    findings = [_summary_finding(n, summary=summary) for n in range(1, 10)]
    jobs, config, job = _card_job(tmp_path, R1_CARD, findings, items, language="ko")
    assert len("\n".join(brief.card(jobs, config, job))) < 4096


def test_the_labels_exist_for_every_language() -> None:
    from taskwatch.jobs import LANGUAGES

    assert set(brief.LABELS) == set(LANGUAGES)


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


def _code_job(tmp_path: Path, files: dict[str, bytes]):
    from dataclasses import replace

    from tests.git_fixture import git, make_repo

    jobs, config, job = _job(tmp_path, GOOD, [])
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    for name, data in files.items():
        (repo / name).write_bytes(data)
        git(repo, "add", name)
    git(repo, "commit", "-m", "work")
    return jobs, config, replace(job, review="code", workspace=str(repo), base=base)


def _changed(tmp_path: Path, files: dict[str, bytes]) -> str:
    jobs, config, job = _code_job(tmp_path, files)
    return next(line for line in brief.compose(jobs, config, job) if line.startswith("Changed:"))


def test_changed_counts_files_and_lines_of_a_code_job(tmp_path: Path) -> None:
    files = {"new.txt": b"a\nb\n"}
    files["out.txt"] = b"uno\ntwo\n"  # replaces the fixture's "one": +2 -1
    assert _changed(tmp_path, files) == "Changed: 2 files, +4 -1: new.txt, out.txt"


def test_changed_shows_four_names_and_a_binary_file_adds_no_lines(tmp_path: Path) -> None:
    files = {f"f{n}.txt": b"x\n" for n in range(5)} | {"blob.bin": b"\0\1\2"}
    line = _changed(tmp_path, files)
    assert line.startswith("Changed: 6 files, +5 -0: ")
    assert line.endswith("(+2 more)")


def test_changed_keeps_the_output_list_for_a_job_without_a_diff(tmp_path: Path) -> None:
    jobs, config, job = _job(tmp_path, GOOD, [])
    line = next(x for x in brief.compose(jobs, config, job) if x.startswith("Changed:"))
    assert line == "Changed: 1 files: result.txt"


def _card(roots, card: str) -> list[str]:
    head = GOOD.split("## Card")[0]
    return check(roots, head + "## Card\n" + card)


def test_a_card_is_a_conclusion_and_short_bullets(roots) -> None:
    assert _card(roots, "Done; nothing to decide.\n- One.\n- Two.\n") == []
    assert _card(roots, "Done.\n") == []  # no bullets is allowed


def test_a_card_needs_one_or_two_conclusion_lines(roots) -> None:
    assert any("found 0" in p for p in _card(roots, "- Only a bullet.\n"))
    assert any("found 3" in p for p in _card(roots, "a\nb\nc\n- One.\n"))
    assert _card(roots, "a\nb\n- One.\n") == []


def test_a_card_with_seven_bullets_says_the_job_was_too_big(roots) -> None:
    bullets = "".join(f"- Change {n}.\n" for n in range(7))
    problems = _card(roots, "Done.\n" + bullets)
    assert problems == [
        "brief.md: Card has more than 6 bullets: the job was too big; "
        "split it (docs/review/design.md D-13)"
    ]
    assert _card(roots, "Done.\n" + bullets[: -len("- Change 6.\n")]) == []


def test_a_card_bullet_is_at_most_160_characters(roots) -> None:
    assert _card(roots, "Done.\n- " + "x" * 158 + "\n") == []
    assert any("over 160" in p for p in _card(roots, "Done.\n- " + "x" * 159 + "\n"))


def test_a_card_holds_no_pointer(roots) -> None:
    problems = _card(roots, "Done.\n- The helper is new. See src/a.py:2\n")
    assert len(problems) == 1 and "must not hold a file:line pointer" in problems[0]


def test_a_missing_or_empty_card_fails(roots) -> None:
    head = GOOD.split("## Card")[0]
    assert check(roots, head) == ["brief.md: missing section `## Card`"]
    assert check(roots, head + "## Card\n") == ["brief.md: section `Card` is empty"]


def _changed_after(tmp_path: Path, before: dict[str, bytes], edit) -> str:
    """The `Changed:` line for a commit that `edit(repo)` makes on top of `before`."""
    from dataclasses import replace

    from tests.git_fixture import git

    jobs, config, job = _code_job(tmp_path, before)
    repo = Path(job.workspace)
    base = git(repo, "rev-parse", "HEAD")
    edit(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "edit")
    job = replace(job, base=base)
    return next(x for x in brief.compose(jobs, config, job) if x.startswith("Changed:"))


def test_changed_counts_a_rename_as_one_file(tmp_path: Path) -> None:
    from tests.git_fixture import git

    def edit(repo: Path) -> None:
        git(repo, "mv", "old.txt", "new.txt")
        (repo / "new.txt").write_bytes(b"a\nb\nc\nd\n")
        (repo / "sub").mkdir()
        git(repo, "mv", "out.txt", "sub/out.txt")

    line = _changed_after(tmp_path, {"old.txt": b"a\nb\nc\n"}, edit)
    assert line == "Changed: 2 files, +1 -0: new.txt, sub/out.txt"


def test_changed_survives_a_path_that_is_not_utf8(tmp_path: Path) -> None:
    def edit(repo: Path) -> None:
        (repo / os.fsdecode(b"caf\xe9.txt")).write_bytes(b"x\n")

    assert _changed_after(tmp_path, {"keep.txt": b"k\n"}, edit).startswith(
        "Changed: 1 files, +1 -0: "
    )


def test_taskwatch_brief_ends_with_the_paths_that_left_the_card(tmp_path: Path, capsys) -> None:
    from taskwatch.job_cli import main

    jobs, _, job = _card_job(tmp_path, R1_CARD, [], "runtime")
    assert main(["--root", str(tmp_path / "root"), "init"]) == 0
    folder = jobs.folder(job)
    (folder / "delivery-review.txt").write_text("body", encoding="utf-8")
    capsys.readouterr()
    assert main(["--root", str(tmp_path / "root"), "brief", job.id]) == 0
    tail = capsys.readouterr().out.splitlines()[-3:]
    assert tail[0] == f"Full brief: taskwatch --root {tmp_path / 'root'} brief {job.id}"
    assert tail[1] == f"Review: {folder / 'delivery-review.txt'}"
    assert tail[2] == f"Worker brief: {folder / 'brief.md'}"


def _bot_job(tmp_path: Path, count=1, language="en"):
    from dataclasses import replace

    findings = [_summary_finding(n) for n in range(1, count + 1)]
    jobs, config, job = _card_job(tmp_path, "## Card\nDone.\n", [], "", language)
    original = {**job.reviews[-1], "round": 1, "findings": findings}
    latest = {**job.reviews[-1], "round": 2, "findings": [_summary_finding(1, "P3")]}
    bot = {
        "apps": ["review-app"],
        "wait": 600,
        "head": "a" * 40,
        "window_end": 1600,
        "outcome": {"review-app": "fallback:timeout"},
        "threads": {
            f"thread-{n}": {
                "finding": f"r1.F{n}",
                "root_id": n,
                "url": f"https://forge.invalid/owner/repo/pull/1#discussion_r{n}",
                "path": "doc.md",
                "line": n,
                "severity": "P2",
                "digest": f"digest-{n}",
                "status": "deferred",
                "head": "a" * 40,
            }
            for n in range(1, count + 1)
        },
        "followup": {
            "id": f"bot-followup:{job.id}",
            "findings": [f"r1.F{n}" for n in range(1, count + 1)],
        },
    }
    return jobs, config, replace(job, reviews=(original, latest), land_state={"bot": bot})


def test_bot_followup_retains_old_round_and_distinguishes_same_finding_id(tmp_path: Path) -> None:
    jobs, config, job = _bot_job(tmp_path)
    card = brief.card(jobs, config, job)
    deferred = next(line for line in card if "deferred" in line)
    assert f"P2 deferred [bot-followup:{job.id}]" in deferred
    assert "Problem 1 for the user." in deferred
    assert "- Problem 1 for the user." in card  # r2.F1 is a distinct finding.
    full = brief.compose(jobs, config, job)
    retained = next(line for line in full if line.startswith("- r1.F1 "))
    assert "doc.md:1 https://forge.invalid/owner/repo/pull/1#discussion_r1" in retained
    assert any(f"followup bot-followup:{job.id} (1 findings)" in line for line in full)
    assert any("bot review-app fallback:timeout" in line for line in full)


def test_bot_followup_deduplicates_latest_findings(tmp_path: Path) -> None:
    from dataclasses import replace

    jobs, config, job = _bot_job(tmp_path)
    job = replace(job, reviews=job.reviews[:1])
    small = brief.card(jobs, config, job)
    assert sum("Problem 1 for the user." in line for line in small) == 1
    assert sum(line.startswith("- r1.F1 ") for line in brief.compose(jobs, config, job)) == 1


def test_bot_followup_full_list_survives_card_bounds(tmp_path: Path) -> None:
    jobs, config, job = _bot_job(tmp_path, count=8)
    card = brief.card(jobs, config, job)
    assert "- +3" in card  # Eight retained entries plus the latest low finding.
    full = brief.compose(jobs, config, job)
    assert sum(line.startswith("- r1.F") for line in full) == 8
    assert all(
        f"doc.md:{n} https://forge.invalid/owner/repo/pull/1#discussion_r{n}" in "\n".join(full)
        for n in range(1, 9)
    )
    assert all(len(line[2:]) <= 160 for line in card if line.startswith("- "))


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("outcome", ["absent", "timeout", "not_success:cancelled"])
def test_bot_fallback_and_final_read_race_are_localized(
    tmp_path: Path, language: str, outcome: str
) -> None:
    from dataclasses import replace

    jobs, config, job = _bot_job(tmp_path, language=language)
    bot = {**job.land_state["bot"], "outcome": {"review-app": f"fallback:{outcome}"}}
    job = replace(job, land_state={"bot": bot})
    card = brief.card(jobs, config, job)
    if language == "ko":
        assert "확인하지 못한 것" in card
        assert any(f"fallback:{outcome}" in line and "자체 리뷰로 대체" in line for line in card)
        assert any("마지막 확인과 병합 사이" in line for line in card)
        assert any("P2 보류" in line for line in card)
    else:
        assert "Not checked" in card
        assert any(f"fallback:{outcome}" in line and "accepted own review" in line for line in card)
        assert any("final read and merge" in line for line in card)


def test_unconfigured_bot_state_does_not_add_card_limitations(tmp_path):
    from dataclasses import replace

    jobs, config, job = _card_job(tmp_path, "## Card\nDone.\n", [], "")
    original = brief.card(jobs, config, job)
    job = replace(job, land_state={"bot": {"apps": [], "wait": 600, "outcome": {}}})
    assert brief.card(jobs, config, job) == original


def test_full_brief_cli_never_truncates_active_bot_source_links(tmp_path, capsys):
    from taskwatch.job_cli import main

    jobs, _, job = _bot_job(tmp_path, count=90)
    assert main(["--root", str(tmp_path / "root"), "init"]) == 0
    jobs.save(job, 2000)
    capsys.readouterr()
    assert main(["--root", str(tmp_path / "root"), "brief", job.id]) == 0
    rendered = capsys.readouterr().out
    assert all(
        f"https://forge.invalid/owner/repo/pull/1#discussion_r{n}" in rendered for n in range(1, 91)
    )


@pytest.mark.parametrize(
    "severity,status,count",
    [
        ("P0", "open", 1),
        ("P1", "open", 90),
        ("P2", "open", 1),
        ("P2", "deferred", 1),
        ("P2", "closed", 1),
    ],
)
def test_bot_observation_keeps_the_accepted_review_in_card_brief_and_delivery(
    tmp_path, capsys, severity, status, count
):
    from dataclasses import replace

    from taskwatch import job_delivery
    from taskwatch.job_cli import main

    jobs, config, job = _card_job(tmp_path, "## Card\nDone.\n", [], "runtime")
    accepted = {
        **job.reviews[0],
        "seq": 2,
        "round": 1,
        "ok": True,
        "verdict": "PASS",
        "body": "Looks fine.\n",
        "findings": [_summary_finding(1)],
    }
    observation = {
        **accepted,
        "seq": 3,
        "round": 2,
        "ok": False,
        "verdict": "FAIL",
        "bot_observation": True,
        "accepted_review_seq": 2,
        "reviewer": "bot:review-app",
        "body": "### F1\nbot text",
        "findings": [
            _summary_finding(n, severity, f"Bot finding {n}.") for n in range(1, count + 1)
        ],
    }
    bot = {
        "apps": ["review-app"],
        "wait": 600,
        "threads": {
            f"thread-{n}": {
                "finding": f"r2.F{n}",
                "root_id": n,
                "author": "review-app",
                "url": f"https://forge.invalid/review/{n}",
                "path": "doc.md",
                "line": n,
                "severity": severity,
                "digest": "d",
                "parser": "prefix",
                "status": status,
                "resolved": False,
                "head": "a" * 40,
            }
            for n in range(1, count + 1)
        },
        "followup": {
            "id": f"bot-followup:{job.id}",
            "findings": [f"r2.F{n}" for n in range(1, count + 1)] if status == "deferred" else [],
        },
    }
    job = replace(
        job,
        stage="user_wait",
        wait_reason="land_bot_findings",
        resume_stage="land",
        land=True,
        review="code",
        reviews=(accepted, observation),
        land_state={"bot": bot},
    )
    card = brief.card(jobs, config, job)
    assert "- Problem 1 for the user." in card
    if severity == "P2" and status != "closed":
        assert any(f"P2 {status}" in line and "Bot finding 1." in line for line in card)
        assert any(f"bot-followup:{job.id}" in line for line in card) == (status == "deferred")
    else:
        assert not any("Bot finding" in line for line in card)
    full = brief.compose(jobs, config, job)
    assert "- F1 P2 K-1 (clarity): doc.md:1" in full
    assert "Reviewer checked: the result against the spec" in full
    expected = "P2 1"
    if status != "closed":
        expected = f"P2 {count + 1}" if severity == "P2" else f"{severity} {count}, P2 1"
        for n in range(1, count + 1):
            line = next(line for line in full if line.startswith(f"- r2.F{n} "))
            assert f"{severity} {status}" in line and f"Bot finding {n}." in line
            assert f"doc.md:{n} https://forge.invalid/review/{n}" in line
    else:
        assert not any("Bot finding" in line for line in full)
    assert f"Open findings: {expected}" in full
    job_delivery._message(jobs, config, job)
    review_text = (jobs.folder(job) / "delivery-review.txt").read_text()
    assert review_text == "Looks fine.\n"
    assert main(["--root", str(tmp_path / "root"), "init"]) == 0
    jobs.save(job, 2000)
    capsys.readouterr()
    assert main(["--root", str(tmp_path / "root"), "brief", job.id]) == 0
    rendered = capsys.readouterr().out
    assert f"Open findings: {expected}" in rendered
    if status != "closed":
        assert f"- r2.F{count} {severity} {status}" in rendered
        assert f"https://forge.invalid/review/{count}" in rendered

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import brief, choices
from taskwatch.jobs import Jobs
from tests.spec_fixture import VALID_SPEC
from tests.test_jobs import payload

VALID = {
    "picks": [
        {
            "id": "python.dep_manager",
            "question": "Which dependency manager?",
            "options": [
                {"key": "A", "label": "uv", "evidence": "pyproject.toml:2"},
                {"key": "B", "label": "pip", "evidence": "https://example.invalid/pip"},
            ],
            "recommended": "A",
            "why": "The repo already has uv.lock.",
            "reversible": "cheap",
            "affects": "this repo",
        }
    ],
    "decided": [
        {"id": "layout.tests", "chosen": "tests/", "why": "repo rule", "source": "AGENTS.md:1"}
    ],
    "existing_tools": [
        {
            "need": "review",
            "searched": ["omp review", "agent review", "pr review"],
            "found": [
                {"name": "omp /review", "source": "https://x.invalid", "reuse": "no", "why": "w"}
            ],
        }
    ],
}
DESIGN = "# Design\n\n## Writer choices\n\n1. layout.tests: tests live in tests/.\n\n## Other\n"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("rule\n", encoding="utf-8")
    (tmp_path / "design.md").write_text(DESIGN, encoding="utf-8")
    return tmp_path


def check(work: Path, data) -> list[str]:
    text = data if isinstance(data, str) else json.dumps(data)
    (work / "choices.json").write_text(text, encoding="utf-8")
    return choices.problems(work, (work,), ("design.md", "choices.json"))


def mutated(change) -> dict:
    data = copy.deepcopy(VALID)
    change(data)
    return data


def test_a_complete_file_passes(work: Path) -> None:
    assert check(work, VALID) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["picks"].extend(copy.deepcopy(d["picks"]) * 5), "at most 5 picks"),
        (lambda d: d["picks"][0]["options"].pop(), "needs at least 2 options"),
        (lambda d: d["picks"][0]["options"][1].update(evidence=" "), "needs a label and evidence"),
        (lambda d: d["picks"][0].update(recommended="Z"), "recommended is not an option key"),
        (lambda d: d["picks"][0].update(why=""), "needs a question and a why"),
        (lambda d: d["picks"][0].update(reversible="never"), "reversible must be cheap or costly"),
        (lambda d: d["picks"][0]["options"][0].update(evidence="gone.toml:1"), "names no file"),
        (lambda d: d["picks"][0]["options"][0].update(evidence="AGENTS.md:9"), "outside the file"),
        (lambda d: d["decided"][0].pop("source"), "needs chosen, why and source"),
        (
            lambda d: d["decided"][0].update(id="other.id"),
            "other.id is not in the `Writer choices`",
        ),
        (lambda d: d["decided"][0].update(id="python.dep_manager"), "used twice"),
        (lambda d: d.update(existing_tools=[]), "existing_tools is empty"),
        (lambda d: d["existing_tools"][0].update(searched=["a", "b"]), "at least 3 different"),
        (lambda d: d["existing_tools"][0].update(searched=[]), "needs searched terms"),
        (lambda d: d["existing_tools"][0].pop("found"), "needs a `found` list"),
        (lambda d: d["existing_tools"][0]["found"][0].update(reuse="maybe"), "reuse"),
        (lambda d: d.pop("picks"), "`picks` must be a list"),
    ],
)
def test_a_file_that_breaks_the_schema_is_refused(work: Path, change, message: str) -> None:
    problems = check(work, mutated(change))
    assert any(message in p for p in problems), problems


def test_not_json_and_missing_files_are_one_problem(work: Path) -> None:
    assert check(work, "{")[0].startswith("choices.json is not valid JSON")
    (work / "choices.json").unlink()
    assert choices.problems(work, (work,), ()) == ["required file is missing: choices.json"]


def test_found_may_be_empty_when_the_searches_are_recorded(work: Path) -> None:
    data = mutated(lambda d: d["existing_tools"][0].update(found=[]))
    assert check(work, data) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("msg\nGate 2026-11-01: PICKS: ok\nfree", {"ok": True, "answers": {}}),
        ("PICKS: a=A b=B", {"ok": False, "answers": {"a": "A", "b": "B"}}),
        ("Gate 2026-11-01: PICKS: ok b=B", {"ok": True, "answers": {"b": "B"}}),
        ('PICKS: a="use pnpm please"', {"ok": False, "answers": {"a": "use pnpm please"}}),
        ("PICKS: a=A\nGate d: PICKS: a=B", {"ok": False, "answers": {"a": "B"}}),
        ("no picks here", None),
        ("PICKS: a", None),
        ('PICKS: a="open', None),
    ],
)
def test_picks_line_of_the_approval_source(source: str, expected) -> None:
    assert choices.parse_picks(source) == expected


def test_ok_means_the_recommended_key_and_a_missing_answer_stays_missing() -> None:
    pick = {"id": "x", "recommended": "A"}
    assert choices.answer_for(pick, {"ok": True, "answers": {}}) == "A"
    assert choices.answer_for(pick, {"ok": True, "answers": {"x": "B"}}) == "B"
    assert choices.answer_for(pick, {"ok": False, "answers": {}}) is None


def test_the_pick_list_is_rendered_from_the_file() -> None:
    card = choices.render_picks(VALID, card=True)
    assert card == [
        "1. python.dep_manager: Which dependency manager? (recommend A: uv)",
    ]
    full = choices.render_picks(VALID, card=False)
    assert "   B) pip [https://example.invalid/pip]" in full
    assert "   why: The repo already has uv.lock." in full


# --- what the brief must list, with the real job store -------------------------------------


def _payload(tmp_path: Path, receipt: str, **extra) -> dict:
    data = payload(tmp_path, receipt) | extra
    Path(data["spec"]).write_text(VALID_SPEC, encoding="utf-8")
    return data


def _jobs(tmp_path: Path):
    jobs = Jobs(tmp_path / "root")
    design = jobs.submit(_payload(tmp_path, "r1", review="design"), 1000.0)
    work = tmp_path / "design-work"
    work.mkdir()
    (work / "choices.json").write_text(json.dumps(VALID), encoding="utf-8")
    design = replace(design, stage="done", workspace=str(work))
    jobs.save(design, 1001.0)
    return jobs, design, work


def test_a_design_brief_must_mark_each_pick_as_awaiting_the_owner(tmp_path: Path) -> None:
    jobs, design, work = _jobs(tmp_path)
    awaiting, listed, issues = choices.brief_expectations(jobs, design, work)
    assert (awaiting, listed, issues) == (["python.dep_manager"], [], [])
    table = "\n".join(["| python.dep_manager | A | repo | pyproject.toml:2 | awaiting owner |"])
    assert brief._choice_problems([table], tuple(awaiting), ()) == []
    assert brief._choice_problems(["| python.dep_manager | A |"], tuple(awaiting), ()) == [
        "brief.md: Choices made must mark pick python.dep_manager as `awaiting owner`"
    ]


def test_an_implement_job_after_a_design_must_carry_the_owner_picks(tmp_path: Path) -> None:
    jobs, design, _ = _jobs(tmp_path)
    build = jobs.submit(_payload(tmp_path, "r2", review="code", predecessors=[design.id]), 1002.0)
    awaiting, listed, issues = choices.brief_expectations(jobs, build, tmp_path / "w")
    assert awaiting == [] and listed == ["python.dep_manager", "layout.tests"]
    assert issues == [f"the approval source has no PICKS: line for the picks of job {design.id}"]
    gated = replace(build, approval_source="msg-1\nGate 2026-11-01: PICKS: ok")
    assert choices.brief_expectations(jobs, gated, tmp_path / "w")[2] == []
    refused = replace(build, approval_source="PICKS: layout.tests=x")
    assert choices.brief_expectations(jobs, refused, tmp_path / "w")[2] == [
        f"no answer to pick python.dep_manager of job {design.id} in the PICKS: line"
    ]
    assert brief._choice_problems(["| layout.tests | tests/ |"], (), tuple(listed)) == [
        "brief.md: Choices made does not list python.dep_manager"
    ]


def test_a_design_card_shows_the_picks_and_ends_with_the_reply_template(tmp_path: Path) -> None:
    from taskwatch.jobs import JobConfig

    jobs, design, _ = _jobs(tmp_path)
    folder = jobs.folder(design)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "brief.md").write_text(
        "## Decisions needed\n1. worker text that the picks replace\n", encoding="utf-8"
    )
    card = brief.compose(jobs, JobConfig(), design, card=True)
    assert "1. python.dep_manager: Which dependency manager? (recommend A: uv)" in card
    assert not any("worker text" in line for line in card)
    assert brief.reply_template(design) == "reply: ok | <id>=<key|text> ... | free text"
    plain = replace(design, review="source")
    assert brief.reply_template(plain) is None

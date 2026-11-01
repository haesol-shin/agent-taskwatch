"""Finding lines, the worker's record, repeats, root-cause notes and the index."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch import job_findings, job_runner
from taskwatch.executor import _finding_lines
from taskwatch.job_findings import notes, repeated_rules, sync_index
from tests.git_fixture import git
from tests.test_public_text import UnitEnv
from tests.verdicts import CODE_FAIL as FAIL
from tests.verdicts import CODE_PASS as PASS


def line(
    ident="F1", severity="P2", category="evidence", rule="K-7", location="a.py:1", summary=None
) -> str:
    obj = {
        "finding": ident,
        "severity": severity,
        "category": category,
        "rule": rule,
        "location": location,
    }
    if summary is not None:
        obj["summary"] = summary
    return json.dumps(obj)


def body(*findings: str, headings=None) -> str:
    ids = headings if headings is not None else [json.loads(f)["finding"] for f in findings]
    return "\n".join([*(f"### {i}\ntext" for i in ids), *findings])


# --- the finding lines ---------------------------------------------------------------------


def test_finding_lines_are_read_with_their_headings():
    found = _finding_lines(body(line("F1"), line("F2", "P3", "clarity", "other", "b")), "PASS")
    assert [f["finding"] for f in found] == ["F1", "F2"]
    assert found[1]["category"] == "clarity"


def test_no_findings_needs_no_lines():
    assert _finding_lines("No findings.", "PASS") == []


@pytest.mark.parametrize(
    "text",
    [
        body(line("F1"), line("F1")),
        body(line(severity="P5")),
        body(line(category="style")),
        body(line(rule="")),
        body(line(location=" ")),
        body(line("one")),
        body(line("F1"), headings=[]),
        body(line("F1"), headings=["F1", "F2"]),
    ],
    ids=["duplicate", "severity", "category", "rule", "location", "id", "no heading", "extra"],
)
def test_a_bad_finding_line_is_rejected(text):
    with pytest.raises(ValueError):
        _finding_lines(text, "FAIL")


@pytest.mark.parametrize("severity", ["P0", "P1"])
def test_a_pass_cannot_carry_a_p0_or_p1_finding(severity):
    text = body(line(severity=severity))
    assert _finding_lines(text, "FAIL")[0]["severity"] == severity
    with pytest.raises(ValueError, match="PASS"):
        _finding_lines(text, "PASS")


def test_a_finding_line_without_a_summary_still_parses():
    assert "summary" not in _finding_lines(body(line()), "PASS")[0]


def test_a_summary_is_kept_trimmed():
    found = _finding_lines(body(line(summary="  Old jobs lose a line.  ")), "PASS")
    assert found[0]["summary"] == "Old jobs lose a line."


@pytest.mark.parametrize("summary", ["", "  ", "x" * 161, "two\nlines", 5])
def test_a_bad_summary_is_rejected(summary):
    with pytest.raises(ValueError, match="summary"):
        _finding_lines(body(line(summary=summary)), "PASS")


# --- the review step records findings ------------------------------------------------------


@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    return UnitEnv(tmp_path, monkeypatch)


def failed_review(env: UnitEnv, *findings: str) -> None:
    env.scenario([{"reply": "\n".join([FAIL, body(*findings)])}])
    env.commit("feat(x): add\n\nWhy.")
    assert env.problems() == []
    assert env.run("review") == 0


def test_the_review_record_keeps_the_findings_and_a_bad_block_costs_a_round(unit_env):
    failed_review(unit_env, line("F1", "P1", rule="K-15"), line("F2", "P2"))
    job = unit_env.get()
    assert [f["rule"] for f in job.reviews[-1]["findings"]] == ["K-15", "K-7"]
    assert job.stage == "fix"
    unit_env.scenario([{"reply": f"{PASS}\n{body(line('F1', 'P1'))}"}] * 2)
    unit_env.put("review")
    assert (
        job_runner.run_job(unit_env.jobs, unit_env.config, unit_env.job.id, unit_env.attempt) == 0
    )
    last = unit_env.get().reviews[-1]
    assert not last["ok"] and any("PASS cannot carry" in p for p in last["problems"])


@pytest.mark.parametrize("severity", ["P2", "P3"])
def test_a_pass_with_a_p2_or_p3_finding_needs_a_summary(unit_env, severity):
    unit_env.commit("feat(x): add\n\nWhy.")
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": f"{PASS}\n{body(line(severity=severity))}"}] * 2)
    assert unit_env.run("review") == 0
    last = unit_env.get().reviews[-1]
    assert not last["ok"]
    assert any("F1 needs a summary" in p for p in last["problems"])


def test_a_pass_with_a_summary_is_accepted(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": f"{PASS}\n{body(line(summary='Old jobs lose a line.'))}"}])
    assert unit_env.run("review") == 0
    last = unit_env.get().reviews[-1]
    assert last["ok"] and last["findings"][0]["summary"] == "Old jobs lose a line."


# --- the worker's record -------------------------------------------------------------------


def write_entries(env: UnitEnv, entries: list[dict]) -> None:
    (env.folder() / "findings.json").write_text(json.dumps(entries), encoding="utf-8")


def test_a_fix_round_needs_an_entry_for_every_finding(unit_env):
    failed_review(unit_env, line("F1"), line("F2", rule="K-8"))
    assert any("findings.json is missing" in p for p in unit_env.problems())
    write_entries(unit_env, [{"id": "r1.F1", "status": "not_fixed", "reason": "by design"}])
    assert unit_env.problems() == ["findings.json has no entry for r1.F2"]


def test_fixed_needs_a_change_since_the_review(unit_env):
    failed_review(unit_env, line("F1"))
    write_entries(unit_env, [{"id": "r1.F1", "status": "fixed"}])
    problems = unit_env.problems()
    assert len(problems) == 1 and "neither the code nor the public text changed" in problems[0]


def test_fixed_passes_when_only_the_body_was_edited(unit_env):
    failed_review(unit_env, line("F1"))
    unit_env.write_text("body.md", "a corrected body\n")
    write_entries(unit_env, [{"id": "r1.F1", "status": "fixed"}])
    assert unit_env.problems() == []
    assert unit_env.get().stage == "review"


def test_fixed_passes_when_the_code_changed(unit_env):
    failed_review(unit_env, line("F1"))
    (unit_env.tree / "more.txt").write_text("m\n", encoding="utf-8")
    git(unit_env.tree, "add", "more.txt")
    git(unit_env.tree, "commit", "-m", "fix(x): more\n\nWhy.")
    write_entries(unit_env, [{"id": "r1.F1", "status": "fixed", "commit": "unchecked"}])
    assert unit_env.problems() == []


@pytest.mark.parametrize("entry", [{"status": "not_fixed"}, {"status": "not_fixed", "reason": " "}])
def test_not_fixed_needs_a_reason(unit_env, entry):
    failed_review(unit_env, line("F1"))
    write_entries(unit_env, [{"id": "r1.F1", **entry}])
    assert any("needs a reason" in p for p in unit_env.problems())


def test_a_not_fixed_reason_reaches_the_next_reviewer(unit_env):
    failed_review(unit_env, line("F1", "P1"))
    write_entries(unit_env, [{"id": "r1.F1", "status": "not_fixed", "reason": "premise is false"}])
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": PASS}])
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert "r1.F1 (P1) not_fixed: premise is false" in prompt


# --- repeats and notes ---------------------------------------------------------------------


def row(job, rule, target="/t"):
    return {"job": job, "target": target, "rule": rule, "finding": "r1.F1", "status": "open"}


@pytest.fixture
def job(unit_env):
    return replace(unit_env.get(), target="/t")


def review_of(*rules):
    return {"findings": [{"finding": f"F{i}", "rule": r} for i, r in enumerate(rules, 1)]}


def test_the_same_rule_in_an_earlier_round_is_a_repeat(job):
    assert repeated_rules(job, [review_of("K-7")], review_of("K-7", "K-8"), []) == ["K-7"]


def test_a_rule_seen_in_an_earlier_job_of_the_target_is_a_repeat(job):
    assert repeated_rules(job, [], review_of("K-7"), [row("old", "K-7")]) == ["K-7"]


def test_a_rule_on_another_target_or_in_this_job_is_not_a_repeat(job):
    rows = [row("old", "K-7", target="/other"), row(job.id, "K-7")]
    assert repeated_rules(job, [], review_of("K-7"), rows) == []


def test_other_never_repeats(job):
    assert repeated_rules(job, [review_of("other")], review_of("other"), []) == []


def test_the_window_is_the_last_ten_jobs(job):
    rows = [row("job0", "K-7"), *(row(f"job{n}", "K-1") for n in range(1, 11))]
    assert repeated_rules(job, [], review_of("K-7"), rows) == []
    assert repeated_rules(job, [], review_of("K-7"), rows[1:] + [row("job11", "K-7")]) == ["K-7"]


NOTE = "## K-7\nCause: rushed\nMissed by: the text check\nPrevention: a spec heading\n"


def test_a_complete_note_passes(tmp_path):
    (tmp_path / "root-cause.md").write_text(NOTE, encoding="utf-8")
    assert job_findings.note_problems(tmp_path, ["K-7"]) == []
    assert job_findings.named_preventions(tmp_path, ["K-7"]) == {"K-7": "a spec heading"}


@pytest.mark.parametrize("label", ["Cause:", "Missed by:", "Prevention:"])
def test_a_note_missing_a_labeled_line_fails(tmp_path, label):
    text = "\n".join(x for x in NOTE.splitlines() if not x.startswith(label))
    (tmp_path / "root-cause.md").write_text(text, encoding="utf-8")
    problems = job_findings.note_problems(tmp_path, ["K-7"])
    assert len(problems) == 1 and label in problems[0]


def test_a_missing_note_or_section_fails_and_an_empty_line_fails(tmp_path):
    assert job_findings.note_problems(tmp_path, ["K-7"])
    (tmp_path / "root-cause.md").write_text(NOTE.replace("rushed", ""), encoding="utf-8")
    assert job_findings.note_problems(tmp_path, ["K-7"])
    assert notes(tmp_path)["K-7"]["Missed by:"] == "the text check"


def test_an_accepted_prevention_names_none(tmp_path):
    (tmp_path / "root-cause.md").write_text(
        NOTE.replace("a spec heading", "accepted: rare"), encoding="utf-8"
    )
    assert job_findings.named_preventions(tmp_path, ["K-7"]) == {}


def test_a_repeat_after_a_named_prevention_is_told_to_the_reviewer(unit_env):
    """Round 1 and 2 break K-7 and the note names a prevention; round 3 breaks it again."""
    (unit_env.folder() / "root-cause.md").write_text(NOTE, encoding="utf-8")
    failed_review(unit_env, line("F1", rule="K-7"))
    write_entries(unit_env, [{"id": "r1.F1", "status": "not_fixed", "reason": "r"}])
    assert unit_env.problems() == []  # no earlier K-7: no note needed, `noted` is empty
    assert unit_env.get().checks[-1].get("recurred") == {}
    unit_env.scenario([{"reply": "\n".join([FAIL, body(line("F1", rule="K-7"))])}])
    assert unit_env.run("review") == 0
    write_entries(unit_env, [{"id": "r2.F1", "status": "not_fixed", "reason": "r"}])
    assert unit_env.problems() == []  # repeated in round 2: the note is complete
    check = unit_env.get().checks[-1]
    assert check["noted"] == {"K-7": "a spec heading"} and check["recurred"] == {}
    unit_env.scenario([{"reply": "\n".join([FAIL, body(line("F1", rule="K-7"))])}])
    assert unit_env.run("review") == 0
    write_entries(unit_env, [{"id": "r3.F1", "status": "not_fixed", "reason": "r"}])
    assert unit_env.problems() == []
    assert unit_env.get().checks[-1]["recurred"] == {"K-7": "a spec heading"}
    unit_env.scenario([{"reply": PASS}])
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert "repeated after a note named this prevention: a spec heading" in prompt
    assert "Root-cause note for K-7" in prompt


def test_a_repeat_without_a_note_goes_back_to_the_fix_round(unit_env):
    failed_review(unit_env, line("F1", rule="K-7"))
    write_entries(unit_env, [{"id": "r1.F1", "status": "not_fixed", "reason": "r"}])
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": "\n".join([FAIL, body(line("F1", rule="K-7"))])}])
    assert unit_env.run("review") == 0
    write_entries(unit_env, [{"id": "r2.F1", "status": "not_fixed", "reason": "r"}])
    assert any("root-cause.md needs a section `## K-7`" in p for p in unit_env.problems())


# --- the index -----------------------------------------------------------------------------


def index_rows(env: UnitEnv) -> list[dict]:
    return list(env.jobs.state.read_records("findings.jsonl"))


def test_the_index_gets_one_line_per_finding_and_a_status_change_appends(unit_env):
    failed_review(unit_env, line("F1"), line("F2", rule="K-8"))
    rows = index_rows(unit_env)
    assert [(r["finding"], r["rule"], r["status"]) for r in rows] == [
        ("r1.F1", "K-7", "open"),
        ("r1.F2", "K-8", "open"),
    ]
    assert rows[0]["target"] == unit_env.get().target and rows[0]["job"] == unit_env.job.id
    now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    sync_index(unit_env.jobs.state, unit_env.get(), unit_env.folder(), now)
    assert len(index_rows(unit_env)) == 2  # a second read adds nothing
    write_entries(
        unit_env,
        [{"id": "r1.F1", "status": "fixed"}, {"id": "r1.F2", "status": "not_fixed", "reason": "x"}],
    )
    sync_index(unit_env.jobs.state, unit_env.get(), unit_env.folder(), now)
    rows = index_rows(unit_env)
    assert [(r["finding"], r["status"]) for r in rows[2:]] == [
        ("r1.F1", "fixed"),
        ("r1.F2", "not_fixed"),
    ]
    sync_index(unit_env.jobs.state, unit_env.get(), unit_env.folder(), now)
    assert len(index_rows(unit_env)) == 4


def test_a_review_without_findings_writes_no_index_line(unit_env):
    unit_env.scenario([{"reply": PASS}])
    unit_env.commit("feat(x): add\n\nWhy.")
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    assert not Path(unit_env.jobs.state.dir / "findings.jsonl").exists() or not index_rows(unit_env)

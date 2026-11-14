"""Finding lines, the worker's record, repeats, root-cause notes and the index."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch import job_findings, job_runner
from taskwatch.executor import _finding_lines
from taskwatch.job_cli import main as cli_main
from taskwatch.job_findings import notes, repeated_rules, sync_index
from taskwatch.jobs import _json_value
from tests.git_fixture import git
from tests.test_job_runner import GOOD_FILES, Env
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


def head(env: UnitEnv) -> str:
    return git(env.tree, "rev-parse", "HEAD")


def more_commit(env: UnitEnv, name: str = "more.txt") -> str:
    (env.tree / name).write_text("m\n", encoding="utf-8")
    git(env.tree, "add", name)
    git(env.tree, "commit", "-m", "fix(x): more\n\nWhy.")
    return head(env)


def fixed(commit=None, ident="r1.F1", **extra) -> dict:
    entry = {"id": ident, "status": "fixed", **extra}
    return {**entry, "commit": commit} if commit is not None else entry


def test_fixed_needs_a_change_since_the_review(unit_env):
    failed_review(unit_env, line("F1"))
    write_entries(unit_env, [fixed(head(unit_env))])
    problems = unit_env.problems()
    assert len(problems) == 1 and "neither the code nor the public text changed" in problems[0]


def test_a_code_fix_names_a_later_commit_that_head_contains(unit_env):
    failed_review(unit_env, line("F1"))
    first = more_commit(unit_env)
    second = more_commit(unit_env, "again.txt")
    for commit in (first, second):  # the fix may be any commit between the review and HEAD
        write_entries(unit_env, [fixed(commit)])
        assert unit_env.problems() == []
    assert unit_env.get().stage == "review"


def side_commit(env: UnitEnv) -> str:
    """A commit that follows the reviewed head but that HEAD does not contain."""
    git(env.tree, "checkout", "-q", "-b", "side")
    side = more_commit(env, "side.txt")
    git(env.tree, "checkout", "-q", "-")
    return side


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("missing", "needs `commit`"),
        ("text", "needs `commit`"),
        ("short", "needs `commit`"),
        ("unresolved", "is not a commit of the work folder"),
        ("reviewed", "is not a later commit than the reviewed"),
        ("base", "is not a later commit than the reviewed"),
        ("unreachable", "is not a later commit than the reviewed"),
    ],
)
def test_a_code_fix_without_verifiable_commit_evidence_is_rejected(unit_env, kind, fragment):
    failed_review(unit_env, line("F1"))
    reviewed = head(unit_env)
    side = side_commit(unit_env)
    more_commit(unit_env)
    commit = {
        "missing": None,
        "text": "unchecked",
        "short": reviewed[:12],
        "unresolved": "0" * 40,
        "reviewed": reviewed,  # a pre-review commit
        "base": git(unit_env.tree, "rev-parse", f"{reviewed}~1"),
        "unreachable": side,
    }[kind]
    write_entries(unit_env, [fixed(commit)])
    problems = unit_env.problems()
    assert len(problems) == 1 and fragment in problems[0], problems


def test_a_text_only_fix_names_the_current_head_and_a_changed_text(unit_env):
    failed_review(unit_env, line("F1"))
    reviewed = head(unit_env)
    unit_env.write_text("body.md", "a corrected body\n")
    for commit, ok in ((None, False), ("0" * 40, False), (reviewed, True)):
        write_entries(unit_env, [fixed(commit)])
        problems = unit_env.problems()
        assert (problems == []) is ok, problems
    assert unit_env.get().stage == "review"


def test_a_text_only_fix_cannot_borrow_an_older_commit(unit_env):
    failed_review(unit_env, line("F1"))
    unit_env.write_text("body.md", "a corrected body\n")
    write_entries(unit_env, [fixed(git(unit_env.tree, "rev-parse", "HEAD~1"))])
    assert any("public text only names the current HEAD" in p for p in unit_env.problems())


def test_a_legacy_review_without_a_reviewed_head_cannot_prove_a_code_fix(unit_env):
    failed_review(unit_env, line("F1"))
    job = unit_env.get()
    legacy = {k: v for k, v in job.reviews[-1].items() if k not in {"reviewed_head", "texts"}}
    unit_env.jobs.save(replace(job, reviews=(*job.reviews[:-1], legacy)), 3000.0)
    write_entries(unit_env, [fixed(more_commit(unit_env))])
    assert any("recorded no reviewed head" in p for p in unit_env.problems())


def test_a_legacy_review_can_anchor_on_its_recorded_unit_head(unit_env):
    failed_review(unit_env, line("F1"))
    job = unit_env.get()
    legacy = {k: v for k, v in job.reviews[-1].items() if k != "reviewed_head"}
    unit_env.jobs.save(replace(job, reviews=(*job.reviews[:-1], legacy)), 3000.0)
    write_entries(unit_env, [fixed(more_commit(unit_env))])
    assert unit_env.problems() == []


def test_a_new_code_review_records_the_head_it_judged(unit_env):
    failed_review(unit_env, line("F1"))
    assert unit_env.get().reviews[-1]["reviewed_head"] == head(unit_env)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("{not json", "is not valid JSON"),
        ('{"id": "r1.F1"}', "must be a JSON list"),
        ('["r1.F1"]', "entry 1 needs an id"),
        ('[{"status": "not_fixed", "reason": "x"}]', "entry 1 needs an id"),
        ('[{"id": "F1", "status": "not_fixed", "reason": "x"}]', "entry 1 needs an id"),
        (
            '[{"id": "r1.F1", "status": "not_fixed", "reason": "x"},'
            ' {"id": "r1.F1", "status": "not_fixed", "reason": "y"}]',
            "more than one entry for r1.F1",
        ),
        ('[{"id": "r1.F1", "status": "done"}]', "needs status fixed or not_fixed"),
        ('[{"id": "r1.F1"}]', "needs status fixed or not_fixed"),
        ("[]", "has no entry for r1.F1"),
    ],
)
def test_a_malformed_findings_record_is_rejected(unit_env, text, fragment):
    failed_review(unit_env, line("F1"))
    (unit_env.folder() / "findings.json").write_text(text, encoding="utf-8")
    assert any(fragment in p for p in unit_env.problems())


def test_a_non_code_result_needs_a_changed_result_and_no_commit(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.scenario([{"reply": "\n".join([FAIL, body(line("F1"))])}])
    env.to_review()
    assert env.run("review") == 0
    entries = [{"id": "r1.F1", "status": "fixed"}]
    (env.folder() / "findings.json").write_text(json.dumps(entries), encoding="utf-8")
    assert env.run("check") == 0
    assert any(
        "neither the code nor the public text changed" in p
        for p in env.get().checks[-1]["problems"]
    )
    env.seed({**GOOD_FILES, "out.txt": "ok\n\n"})  # the check still passes; the result changed
    assert env.run("check") == 0
    assert not env.get().checks[-1]["problems"]


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


@pytest.mark.parametrize("malformed", ["no verdict", f"{PASS}\n{body(line('F2'))}"])
def test_a_malformed_retry_keeps_the_last_substantive_findings_and_reasons(unit_env, malformed):
    failed_review(unit_env, line("F1", "P1", location="retained.py:12"))
    write_entries(unit_env, [{"id": "r1.F1", "status": "not_fixed", "reason": "premise is false"}])
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": malformed}] * 2 + [{"reply": PASS}])
    assert unit_env.run("review") == 0
    assert unit_env.get().stage == "review"
    job_findings.sync_index(unit_env.jobs.state, unit_env.get(), unit_env.folder(), 3000.0)
    assert all(
        row["finding"] != "r2.F2" for row in unit_env.jobs.state.read_records(job_findings.INDEX)
    )
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert "r1.F1 (P1) not_fixed: premise is false" in prompt
    assert "retained.py:12" in prompt


def test_an_unknown_status_in_retained_history_is_rejected(unit_env):
    failed_review(unit_env, line("F1"))
    write_entries(
        unit_env,
        [
            {"id": "r0.F1", "status": "done"},
            {"id": "r1.F1", "status": "not_fixed", "reason": "premise is false"},
        ],
    )
    assert any("r0.F1 needs status fixed or not_fixed" in p for p in unit_env.problems())


def test_a_fixed_finding_reaches_the_next_reviewer_with_its_verified_commit(unit_env):
    failed_review(unit_env, line("F1", "P1"))
    commit = more_commit(unit_env)
    write_entries(unit_env, [fixed(commit)])
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": PASS}])
    assert unit_env.run("review") == 0
    assert f"r1.F1 (P1) fixed {commit[:12]}" in unit_env.prompts()[-1]


def test_findings_of_the_last_failed_review_are_checked_across_a_resume(unit_env):
    failed_review(unit_env, line("F1"))
    write_entries(unit_env, [])
    with unit_env.jobs.state.lock():
        job = unit_env.get()
        unit_env.jobs.save(replace(job, review_cycle_start=len(job.reviews)), 3000.0)
    assert unit_env.problems() == ["findings.json has no entry for r1.F1"]


def test_permission_aware_worker_records_findings_and_the_root_cause_note(unit_env):
    env = unit_env
    failed_review(env, line("F1", rule="K-7"))
    folder = env.folder()
    # Round 1: findings.json, title and body are written through the named grants only.
    first = {
        str(folder / "findings.json"): json.dumps([fixed(head(env))]),
        str(folder / "pr/pr/title.txt"): "fix(x): correct evidence\n",
        str(folder / "pr/pr/body.md"): "Corrected evidence, round 1.\n",
    }
    env.scenario([{"writes": first, "require_write_grants": True}])
    assert env.run("fix") == 0
    assert env.problems() == []
    env.scenario([{"reply": "\n".join([FAIL, body(line("F1", rule="K-7"))])}])
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "root_cause_required")
    # The owner resumes after the analysis; the next fix writes its record and note by grant.
    (folder / "root-cause.md").write_text(NOTE, encoding="utf-8")
    assert cli_main(["--root", str(env.jobs.state.root), "resume", job.id, "--reason", "ok"]) == 0
    second = {
        str(folder / "findings.json"): json.dumps([fixed(head(env), "r2.F1")]),
        str(folder / "pr/pr/body.md"): "Corrected evidence, round 2.\n",
        str(folder / "root-cause.md"): NOTE + "\nMore: checked.\n",
    }
    env.scenario([{"writes": second, "require_write_grants": True}])
    assert env.run("fix") == 0
    assert env.problems() == []
    assert env.get().checks[-1]["noted"] == {"K-7": "a spec heading"}
    assert git(env.tree, "status", "--porcelain") == ""
    assert (folder / "root-cause.md").read_text().startswith(NOTE)
    assert not (env.tree / "pr").exists()


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


def reply(*rules: str) -> dict:
    lines = [line(f"F{n}", rule=rule) for n, rule in enumerate(rules, 1)]
    return {"reply": "\n".join([FAIL, body(*lines)])}


def next_round(env: UnitEnv, rules: tuple[str, ...], answered: int) -> None:
    """Answer the last review's findings with a verified commit, then run the next failing
    review."""
    commit = more_commit(env, f"round{answered}.txt")
    last = job_findings.last_failed(env.get().reviews)
    write_entries(env, [fixed(commit, job_findings.finding_id(last, f)) for f in last["findings"]])
    assert env.problems() == []
    env.scenario([reply(*rules)])
    assert env.run("review") == 0


def test_the_same_rule_in_two_consecutive_rounds_stops_for_a_root_cause(unit_env):
    failed_review(unit_env, line("F1", rule="K-7"))
    commit = more_commit(unit_env)
    write_entries(unit_env, [fixed(commit)])
    assert unit_env.problems() == []
    (unit_env.folder() / "root-cause.md").write_text(NOTE, encoding="utf-8")  # an older note
    unit_env.scenario([reply("K-7")])
    prompts = len(unit_env.prompts())
    assert unit_env.run("review") == 0
    job = unit_env.get()
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "user_wait",
        "root_cause_required",
        "fix",
    )
    assert "K-7" in job.error and "rounds 1 and 2" in job.error
    stop = [e for e in job.events if e.get("reason") == "root_cause_required"][-1]
    assert (stop["rules"], stop["rounds"], stop["exhausted"]) == (("K-7",), (1, 2), False)
    assert stop["note_digest"] == job_findings.note_digest(unit_env.folder())
    assert len(unit_env.prompts()) == prompts + 1  # nothing ran after the stop


def resume(env: UnitEnv) -> int:
    argv = ["--root", str(env.jobs.state.root), "resume", env.get().id, "--reason", "analysed"]
    return cli_main(argv)


def test_a_resume_after_the_stop_needs_a_changed_complete_note(unit_env, capsys):
    failed_review(unit_env, line("F1", rule="K-7"))
    (unit_env.folder() / "root-cause.md").write_text(NOTE, encoding="utf-8")
    next_round(unit_env, ("K-7",), 1)
    assert unit_env.get().wait_reason == "root_cause_required"
    assert resume(unit_env) == 1  # the note that existed before the stop is not enough
    assert "has not changed since the stop" in capsys.readouterr().err
    note = unit_env.folder() / "root-cause.md"
    note.write_text(NOTE.replace("Prevention: a spec heading\n", "") + "\n", encoding="utf-8")
    assert resume(unit_env) == 1
    assert "needs a `Prevention:` line" in capsys.readouterr().err
    note.write_text(NOTE.replace("rushed", "rushed again in round 2"), encoding="utf-8")
    assert resume(unit_env) == 0
    job = unit_env.get()
    assert (job.stage, job.resume_stage, job.reviews_used) == ("queued", "fix", 0)
    assert job.review_cycle_start == len(job.reviews) == 2


@pytest.mark.parametrize(
    "rounds",
    [
        [("K-7",), ("K-8",)],
        [("other",), ("other",)],
        [("K-7",), ("K-8",), ("K-7",)],
        [("K-7", "K-8"), ("K-9",)],
    ],
    ids=["different", "other", "not consecutive", "no shared rule"],
)
def test_only_a_rule_shared_by_consecutive_rounds_stops(unit_env, rounds):
    failed_review(unit_env, *[line(f"F{n}", rule=r) for n, r in enumerate(rounds[0], 1)])
    for answered, rules in enumerate(rounds[1:], 1):
        next_round(unit_env, rules, answered)
        job = unit_env.get()
        last = answered == len(rounds) - 1 and len(rounds) == 3  # the cycle's last permitted review
        expected = ("user_wait", "review_exhausted") if last else ("fix", "")
        assert (job.stage, job.wait_reason) == expected, job.error


def test_a_malformed_round_does_not_erase_the_preceding_substantive_round(unit_env):
    failed_review(unit_env, line("F1", rule="K-7"))
    commit = more_commit(unit_env)
    write_entries(unit_env, [fixed(commit)])
    assert unit_env.problems() == []
    unit_env.scenario([{"reply": "no verdict"}] * 2 + [reply("K-7")])
    assert unit_env.run("review") == 0
    job = unit_env.get()
    assert (job.stage, job.reviews_used, job.reviews[-1]["findings"]) == ("review", 2, ())
    assert unit_env.run("review") == 0
    job = unit_env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "root_cause_required")
    assert [r["round"] for r in job.reviews] == [1, 2, 3]
    assert [e for e in job.events if e.get("reason") == "root_cause_required"][-1]["rounds"] == (
        1,
        3,
    )


def test_a_repeat_in_a_new_cycle_does_not_trigger_the_consecutive_stop(unit_env):
    unit_env.jobs.save(replace(unit_env.get(), max_reviews=1), 1500.0)
    failed_review(unit_env, line("F1", rule="K-7"))
    assert unit_env.get().wait_reason == "review_exhausted"
    assert resume(unit_env) == 0
    next_round(unit_env, ("K-7",), 1)
    job = unit_env.get()
    # The new cycle holds one review, so the repeat is not consecutive; it still exhausts it.
    assert (job.stage, job.wait_reason) == ("user_wait", "review_exhausted")


def test_root_cause_takes_precedence_over_an_exhausted_cycle(unit_env):
    unit_env.jobs.save(replace(unit_env.get(), max_reviews=2), 1500.0)
    failed_review(unit_env, line("F1", rule="K-7"))
    next_round(unit_env, ("K-7",), 1)
    job = unit_env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "root_cause_required")
    assert "review limit is also reached" in job.error
    assert [e for e in job.events if e.get("reason") == "root_cause_required"][-1]["exhausted"]


def test_a_repeat_that_is_not_consecutive_still_needs_the_note(unit_env):
    failed_review(unit_env, line("F1", rule="K-7"))
    next_round(unit_env, ("K-8",), 1)
    next_round(unit_env, ("K-7",), 2)  # K-8 came between: not the consecutive stop
    job = unit_env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "review_exhausted")
    assert resume(unit_env) == 0
    write_entries(unit_env, [fixed(more_commit(unit_env, "round3.txt"), "r3.F1")])
    assert any("root-cause.md needs a section `## K-7`" in p for p in unit_env.problems())


def test_a_repeat_after_a_named_prevention_is_told_to_the_reviewer(unit_env):
    """Round 1 and 2 break K-7 and stop; after the analysis round 3 breaks it again."""
    (unit_env.folder() / "root-cause.md").write_text(NOTE, encoding="utf-8")
    failed_review(unit_env, line("F1", rule="K-7"))
    next_round(unit_env, ("K-7",), 1)
    assert unit_env.get().wait_reason == "root_cause_required"
    (unit_env.folder() / "root-cause.md").write_text(NOTE + "\nMore: round 2.\n", encoding="utf-8")
    assert resume(unit_env) == 0
    next_round(unit_env, ("K-7",), 2)
    check = unit_env.get().checks[-1]
    assert check["noted"] == {"K-7": "a spec heading"} and check["recurred"] == {}
    commit = more_commit(unit_env, "round3.txt")
    write_entries(unit_env, [fixed(commit, "r3.F1")])
    assert unit_env.problems() == []
    assert unit_env.get().checks[-1]["recurred"] == {"K-7": "a spec heading"}
    unit_env.scenario([{"reply": PASS}])
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert "repeated after a note named this prevention: a spec heading" in prompt
    assert "Root-cause note for K-7" in prompt


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


@pytest.mark.parametrize(
    ("body", "severity", "parser"),
    [
        (
            "<!-- P0: hidden -->\n<details><summary>P1: hidden</summary>x</details>\nP3: low",
            "P3",
            "prefix",
        ),
        *[(f"\n  P{n}: problem\nMore text", f"P{n}", "prefix") for n in range(4)],
        *[
            (s, "P2", "default")
            for s in ("", "P4: bad", "P9: bad", "P1 bad", "First line\nP1: later", "p1: lower")
        ],
    ],
)
def test_bot_priority_uses_only_first_visible_line(body, severity, parser):
    parsed = job_findings.bot_text(body)
    assert (parsed["severity"], parsed["parser"]) == (severity, parser)


def test_bot_parser_cleans_before_capping_and_digest_tracks_hidden_tail():
    text = "<!--" + "x" * 2200 + "-->\nP1: `" + "a" * 2200
    parsed = job_findings.bot_text(text)
    assert parsed["severity"] == "P1" and parsed["body"].startswith("P1: '")
    assert len(parsed["body"]) == 2000
    assert parsed["digest"] != job_findings.bot_text(text + "tail")["digest"]


def test_deferred_index_is_not_closed_by_unjudged_worker_claim(unit_env):
    failed_review(unit_env, line("F1"))
    env = unit_env
    job = replace(
        env.get(),
        land_state={"bot": {"followup": {"id": "bot-followup:synthetic", "findings": ["r1.F1"]}}},
    )
    write_entries(env, [{"id": "r1.F1", "status": "not_fixed", "reason": "claim"}])
    job_findings.sync_index(env.jobs.state, job, env.folder(), 2000)
    assert index_rows(env)[-1]["status"] == "deferred"


def test_multi_app_observations_keep_attribution_binding_and_root_order(unit_env):
    from tests.test_job_land import bot_thread

    job = unit_env.get()
    accepted = {"seq": 1, "round": 1, "ok": True, "verdict": "PASS"}
    job = replace(job, reviews=(accepted,))
    bot = {"apps": ["review-bot-example", "second-app"], "threads": {}}
    roots = [bot_thread(1), bot_thread(2, author="second-app"), bot_thread(3)]
    records, threads = job_findings.bot_updates(job, accepted, bot, roots, "a" * 40, 1000)
    assert [r["reviewer"] for r in records[1:]] == ["bot:review-bot-example", "bot:second-app"]
    assert [r["accepted_review_seq"] for r in records[1:]] == [1, 1]
    assert [r["round"] for r in records[1:]] == [2, 3]
    assert list(threads) == ["thread-1", "thread-2", "thread-3"]
    assert threads["thread-3"]["finding"] == "r2.F2"
    assert threads["thread-2"]["finding"] == "r3.F1"


@pytest.mark.parametrize(
    "login,author_type,recognized",
    [
        ("review-bot-example[bot]", "Bot", True),
        ("review-bot-example", "Bot", True),
        ("review-bot-example[bot]", "User", False),
        ("other-app[bot]", "Bot", False),
        ("review-bot-example[bot][bot]", "Bot", False),
    ],
)
def test_bot_root_matches_exact_configured_app_and_typed_login(
    unit_env, login, author_type, recognized
):
    from tests.test_job_land import bot_thread

    accepted = {"seq": 1, "round": 1, "ok": True, "verdict": "PASS"}
    job = replace(unit_env.get(), reviews=(accepted,))
    bot = {"apps": ["review-bot-example"], "threads": {}}
    records, threads = job_findings.bot_updates(
        job, accepted, bot, [bot_thread(author=login, author_type=author_type)], "a" * 40, 1000
    )
    assert bool(threads) == recognized
    if recognized:
        assert records[-1]["reviewer"] == "bot:review-bot-example"


def _disposition_job(unit_env):
    accepted = {
        "seq": 1,
        "round": 1,
        "ok": True,
        "verdict": "PASS",
        "reviewed_head": "a" * 40,
        "fingerprint": "a" * 40,
    }
    from tests.test_job_land import bot_thread

    job = replace(unit_env.get(), reviews=(accepted,))
    records, threads = job_findings.bot_updates(
        job,
        accepted,
        {"apps": ["review-bot-example", "second-app"], "threads": {}},
        [bot_thread(1), bot_thread(2, author="second-app")],
        "b" * 40,
        1000,
    )
    return replace(
        job,
        reviews=tuple(records),
        land_state={"bot": {"threads": threads, "round": {"awaiting": True}}},
    )


def test_dispositions_freeze_exact_bodies_and_survive_later_pass(unit_env):
    job = _disposition_job(unit_env)
    write_entries(
        unit_env,
        [
            {"id": "r2.F1", "status": "fixed", "commit": "c" * 40},
            {"id": "r3.F1", "status": "not_fixed", "reason": "Ask @synthetic.\nFull reason."},
        ],
    )
    candidates = job_findings.bot_dispositions(job, unit_env.folder())
    assert [p["thread_id"] for p in candidates] == ["thread-1", "thread-2"]
    assert candidates[0]["body"] == (
        "Fixed in the latest head of this pull request.\n\n"
        f"<!-- taskwatch:fixed thread={candidates[0]['root_id']} -->"
    )
    assert candidates[1]["body"] == (
        "Not changed in this PR: Ask @\u200bsynthetic.\nFull reason.\n\n"
        f"<!-- taskwatch:answer thread={candidates[1]['root_id']} -->"
    )
    thread = job.land_state["bot"]["threads"]["thread-2"]
    for key in ("root_id", "finding", "path", "digest", "severity"):
        assert candidates[1][key] == thread[key]
    later = replace(job, reviews=(*job.reviews, {"ok": True, "verdict": "PASS"}))
    assert job_findings.bot_dispositions(later, unit_env.folder()) == candidates
    assert all(r["reviewed_head"] == "a" * 40 for r in job.reviews[1:])


@pytest.mark.parametrize("status", ["fixed", "answered", "deferred", "closed"])
def test_only_awaiting_open_round_threads_generate_replies(unit_env, status):
    job = _disposition_job(unit_env)
    write_entries(
        unit_env,
        [
            {"id": "r2.F1", "status": "not_fixed", "reason": "Synthetic reason."},
            {"id": "r3.F1", "status": "not_fixed", "reason": "Other reason."},
        ],
    )
    bot = _json_value(job.land_state["bot"])
    bot["threads"]["thread-1"]["status"] = status
    bot["round"]["findings"] = ["r2.F1"]
    assert (
        job_findings.bot_dispositions(replace(job, land_state={"bot": bot}), unit_env.folder())
        == []
    )
    bot["threads"]["thread-1"]["status"] = "open"
    candidates = job_findings.bot_dispositions(
        replace(job, land_state={"bot": bot}), unit_env.folder()
    )
    assert len(candidates) == 1
    bot["round"]["awaiting"] = False
    assert (
        job_findings.bot_dispositions(replace(job, land_state={"bot": bot}), unit_env.folder())
        == []
    )


def test_bot_checks_all_apps_and_index_ignores_worker_claims(unit_env):
    job = _disposition_job(unit_env)
    write_entries(unit_env, [{"id": "r3.F1", "status": "not_fixed", "reason": "Synthetic reason."}])
    problems = job_findings.bot_worker_problems(job, unit_env.folder(), "c" * 40, {})
    assert any("r2.F1" in problem for problem in problems)
    job_findings.sync_index(unit_env.jobs.state, job, unit_env.folder(), 3000)
    assert [row["status"] for row in index_rows(unit_env)] == ["open", "open"]
    bot = _json_value(job.land_state["bot"])
    bot["threads"]["thread-2"]["status"] = "answered"
    job = replace(job, land_state={"bot": bot})
    job_findings.sync_index(unit_env.jobs.state, job, unit_env.folder(), 3001)
    assert index_rows(unit_env)[-1]["status"] == "answered"

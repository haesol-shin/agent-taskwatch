"""Pull request units: submit validation and the commit message rules of the check step."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_cli
from taskwatch.checks import message_problems
from taskwatch.jobs import Jobs
from tests.git_fixture import git, make_repo
from tests.spec_fixture import VALID_SPEC
from tests.test_job_runner import GOOD_FILES, RECORDS, Env
from tests.test_jobs import payload

# --- the message rules ---------------------------------------------------------------------


def test_a_plain_message_passes():
    assert message_problems("fix(jobs): reject a stale review\n\nThe reason, in a line.\n") == []
    assert message_problems("fix: reject\n") == []


@pytest.mark.parametrize("subject", ["Fix things", "fix(a b): x", "fix(): x", "fix:x", "fix: "])
def test_a_subject_without_the_shape_fails(subject):
    assert message_problems(subject)


def test_subject_of_73_characters_fails_and_72_passes():
    head = "fix(jobs): "
    assert message_problems(head + "x" * (72 - len(head))) == []
    assert message_problems(head + "x" * (73 - len(head)))


def test_body_line_of_73_characters_fails_and_72_passes():
    assert message_problems("fix: x\n\n" + "y" * 72) == []
    problems = message_problems("fix: x\n\n" + "y" * 73)
    assert len(problems) == 1 and "line 3" in problems[0]


def test_five_bullets_pass():
    body = "\n".join(f"- reason {n}" for n in range(5))
    assert message_problems(f"fix: x\n\n{body}\n") == []


@pytest.mark.parametrize(
    "line", ["Co-authored-by: A <a@example.invalid>", "Signed-off-by: A", "cc: someone"]
)
def test_a_trailer_fails(line):
    problems = message_problems(f"fix: x\n\nwhy\n\n{line}\n")
    assert len(problems) == 1 and "trailer" in problems[0]


def test_rejected_and_other_labels_are_not_trailers():
    assert message_problems("fix: x\n\nRejected: other way\nCause: old\n") == []


@pytest.mark.parametrize("text", ["fix: x\nbody", "fix: x\n\n\nbody"])
def test_one_blank_line_must_follow_the_subject(text):
    assert any("blank line" in p for p in message_problems(text))


# --- submit ---------------------------------------------------------------------------------


def code_payload(tmp_path: Path, units) -> dict:
    repo = make_repo(tmp_path, "repo")
    data = payload(tmp_path)
    Path(data["spec"]).write_text(VALID_SPEC, encoding="utf-8")
    return {**data, "review": "code", "target": str(repo), "public_text": units}


def test_submit_defaults_head_and_keeps_units(tmp_path):
    job = Jobs(tmp_path / "root").submit(code_payload(tmp_path, [{"id": "pr", "base": "main"}]), 10)
    assert [dict(u) for u in job.public_text] == [{"id": "pr", "base": "main", "head": "HEAD"}]


def test_submit_accepts_a_stacked_base_that_is_another_units_head(tmp_path):
    units = [
        {"id": "a", "base": "main", "head": "step-a"},
        {"id": "b", "base": "step-a", "head": "HEAD"},
    ]
    assert len(Jobs(tmp_path / "root").submit(code_payload(tmp_path, units), 10).public_text) == 2


@pytest.mark.parametrize(
    "units",
    [
        [{"id": "pr", "base": "no-such-ref"}],
        [{"id": "pr", "base": "main"}, {"id": "pr", "base": "main"}],
        [{"id": "Bad Id", "base": "main"}],
        [{"id": "pr", "base": "--upload-pack=x"}],
        [{"id": "pr"}],
        [],
    ],
)
def test_submit_refuses_bad_units(tmp_path, units):
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError):
        jobs.submit(code_payload(tmp_path, units), 10)
    assert jobs.load() == {}


def test_submit_refuses_units_on_a_job_that_builds_no_commits(tmp_path):
    data = {**payload(tmp_path), "public_text": [{"id": "pr", "base": "main"}]}
    with pytest.raises(ValueError, match="public_text"):
        Jobs(tmp_path / "root").submit(data, 10)


WRAPPED = "fix: x\n\n- the reason is long\n  and goes on here\n- another\n"


def test_a_wrapped_bullet_is_found_only_when_asked():
    assert message_problems(WRAPPED) == []
    assert message_problems(WRAPPED, bullet_one_line=True) == [
        "line 4 continues the bullet on line 3"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "- one\n- two\n",
        "* one\n* two\n",
        "Plain paragraph\n  indented code\n",
        "- one\n  - nested\n",
        "- one\n\n  after a blank line\n",
    ],
)
def test_one_line_bullets_and_other_layouts_pass_the_strict_rule(body):
    assert message_problems(f"fix: x\n\n{body}", bullet_one_line=True) == []


def test_every_continuation_line_names_its_bullet():
    text = "fix: x\n\n* a\n  b\n\tc\n- d\n"
    assert message_problems(text, bullet_one_line=True) == [
        "line 4 continues the bullet on line 3",
        "line 5 continues the bullet on line 3",
    ]


# --- the check step -------------------------------------------------------------------------


class UnitEnv(Env):
    """A code job whose work folder is a real worktree with one unit."""

    def __init__(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path, "repo")
        super().__init__(
            tmp_path,
            monkeypatch,
            review="code",
            target=repo,
            public_text=[{"id": "pr", "base": "main"}],
        )
        assert self.run("check") == 0  # creates the work folder; nothing is built yet
        self.tree = self.folder() / "worktree"
        for name, text in GOOD_FILES.items():
            if name in RECORDS:
                (self.folder() / name).write_text(text, encoding="utf-8")
        (self.folder() / "pr" / "pr").mkdir(parents=True, exist_ok=True)
        self.write_text("title.txt", "feat(x): add\n")
        self.write_text("body.md", "body\n")

    def write_text(self, name: str, text: str) -> None:
        (self.folder() / "pr" / "pr" / name).write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        (self.tree / "out.txt").write_text("ok\n", encoding="utf-8")
        git(self.tree, "add", "out.txt")
        git(self.tree, "commit", "-m", message)

    def problems(self) -> list[str]:
        assert self.run("check") == 0
        return list(self.get().checks[-1]["problems"])


@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    return UnitEnv(tmp_path, monkeypatch)


def test_a_good_unit_reaches_review(unit_env):
    unit_env.commit("feat(x): add\n\nWhy it is needed.")
    assert unit_env.problems() == []
    assert unit_env.get().stage == "review"


def test_a_failing_message_rule_goes_to_the_fix_round_without_a_review(unit_env):
    unit_env.commit("feat(x): " + "y" * 70)
    problems = unit_env.problems()
    assert len(problems) == 1 and "public text pr: commit" in problems[0]
    job = unit_env.get()
    assert job.stage == "fix" and "characters" in job.error and job.reviews == ()


def test_a_missing_body_is_a_check_problem(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    (unit_env.folder() / "pr" / "pr" / "body.md").unlink()
    assert unit_env.problems() == ["required file is missing: pr/pr/body.md"]


def test_a_unit_without_commits_is_a_check_problem(unit_env):
    problems = unit_env.problems()
    assert any("no commits" in p for p in problems)


def test_an_unresolved_head_is_a_check_problem(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    unit = {**unit_env.get().public_text[0], "head": "gone"}
    assert unit_env.run("check", public_text=(unit,)) == 0
    assert any("gone" in p for p in unit_env.get().checks[-1]["problems"])


def test_the_commit_rule_switch_decides_whether_a_wrapped_bullet_fails(unit_env):
    unit_env.commit("feat(x): add\n\n- the reason is long\n  and goes on")
    assert unit_env.problems() == []
    unit_env.config = replace(unit_env.config, bullet_one_line=True)
    problems = unit_env.problems()
    assert len(problems) == 1 and "continues the bullet on line 3" in problems[0]
    assert unit_env.get().stage == "fix"


# --- the check command ----------------------------------------------------------------------


def run_check(env, capsys, *extra) -> tuple[int, str]:
    if not (env.jobs.state.root / "taskwatch" / "jobs-config.json").exists():
        assert job_cli.main(["--root", str(env.jobs.state.root), "init"]) == 0
        capsys.readouterr()
    code = job_cli.main(["--root", str(env.jobs.state.root), "check", env.job.id, *extra])
    return code, capsys.readouterr().out


def test_the_check_command_names_a_missing_body_and_passes_once_it_is_added(unit_env, capsys):
    unit_env.commit("feat(x): add\n\nWhy.")
    body = unit_env.folder() / "pr" / "pr" / "body.md"
    body.unlink()
    before = unit_env.get()
    assert run_check(unit_env, capsys) == (1, "- required file is missing: pr/pr/body.md\n")
    assert unit_env.get() == before  # read only: no check record, no stage change
    unit_env.write_text("body.md", "body\n")
    assert run_check(unit_env, capsys) == (0, "no problems\n")


def test_the_check_command_runs_the_test_command_only_when_asked(unit_env, capsys):
    unit_env.commit("feat(x): add\n\nWhy.")
    marker = unit_env.tmp / "ran"
    script = f"open({str(marker)!r}, 'w').close()"
    unit_env.run("check", check_command=(sys.executable, "-c", script))
    marker.unlink()
    assert run_check(unit_env, capsys)[0] == 0 and not marker.exists()
    assert run_check(unit_env, capsys, "--run-command")[0] == 0 and marker.exists()


def test_the_check_command_refuses_an_unknown_job(unit_env, capsys):
    code = job_cli.main(["--root", str(unit_env.jobs.state.root), "check", "nope"])
    assert code == 2 and "unknown job" in capsys.readouterr().err


def test_the_workflow_end_section_names_the_check_command(unit_env):
    end = (unit_env.folder() / "WORKFLOW.md").read_text(encoding="utf-8").split("## End")[1]
    assert "taskwatch.job_cli" in end and f"check {unit_env.job.id}" in end

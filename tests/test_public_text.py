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


def test_submit_defaults_head_and_pins_an_external_base_to_its_sha(tmp_path):
    job = Jobs(tmp_path / "root").submit(code_payload(tmp_path, [{"id": "pr", "base": "main"}]), 10)
    sha = git(tmp_path / "repo", "rev-parse", "main")
    assert [dict(u) for u in job.public_text] == [{"id": "pr", "base": sha, "head": "HEAD"}]


def test_submit_stores_a_declared_blank_squash_body_only_when_declared(tmp_path):
    unit = {"id": "pr", "base": "main", "blank_squash_body": True}
    job = Jobs(tmp_path / "root").submit(code_payload(tmp_path, [unit]), 10)
    assert job.public_text[0]["blank_squash_body"] is True
    reloaded = Jobs(tmp_path / "root").load()[job.id]
    assert reloaded.public_text[0]["blank_squash_body"] is True


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_submit_refuses_a_blank_squash_body_that_is_not_true(tmp_path, value):
    unit = {"id": "pr", "base": "main", "blank_squash_body": value}
    with pytest.raises(ValueError, match="blank_squash_body"):
        Jobs(tmp_path / "root").submit(code_payload(tmp_path, [unit]), 10)


def test_a_moved_ref_does_not_change_the_stored_external_base(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "moving"}])
    repo = tmp_path / "repo"
    git(repo, "branch", "moving")
    first = git(repo, "rev-parse", "moving")
    job = Jobs(tmp_path / "root").submit(data, 10)
    git(repo, "commit", "--allow-empty", "-m", "advance")
    git(repo, "branch", "-f", "moving", "main")
    assert git(repo, "rev-parse", "moving") != first
    assert Jobs(tmp_path / "root").load()[job.id].public_text[0]["base"] == first


@pytest.mark.parametrize(
    "units",
    [
        [
            {"id": "a", "base": "main~1", "head": "step-a"},
            {"id": "b", "base": "main", "head": "HEAD"},
        ],
        [
            {"id": "a", "base": "main", "head": "step-a"},
            {"id": "b", "base": "step-a", "head": "HEAD"},
        ],
        [
            {"id": "a", "base": "main", "head": "HEAD~1"},
            {"id": "b", "base": "HEAD~1", "head": "HEAD"},
        ],
    ],
)
def test_submit_refuses_multiple_units_and_instructs_split_into_jobs(tmp_path, units):
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError, match="exactly one unit; split.*into jobs"):
        jobs.submit(code_payload(tmp_path, units), 10)
    assert jobs.load() == {}


def test_submit_pins_a_base_even_when_it_also_names_the_unit_head(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "HEAD", "head": "HEAD"}])
    job = Jobs(tmp_path / "root").submit(data, 10)
    assert job.public_text[0]["base"] == git(tmp_path / "repo", "rev-parse", "HEAD")


def test_submit_preserves_workspace_default_base_selection(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    job = Jobs(tmp_path / "root").submit(data, 10)
    assert job.base == ""


def test_submit_accepts_an_explicit_base_resolving_to_the_same_commit(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    job = Jobs(tmp_path / "root").submit({**data, "base": "HEAD"}, 10)
    assert job.base == "HEAD"
    assert job.public_text[0]["base"] == git(tmp_path / "repo", "rev-parse", "HEAD")


def test_submit_refuses_a_unit_base_that_omits_part_of_the_explicit_job_range(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    repo = tmp_path / "repo"
    git(repo, "commit", "--allow-empty", "-m", "advance")
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError, match="public_text base must equal the effective job base"):
        jobs.submit({**data, "base": "HEAD~1"}, 10)
    assert jobs.load() == {}


@pytest.mark.parametrize("branch_exists", [False, True])
def test_public_text_follows_the_receipt_base_of_an_unfinished_predecessor(tmp_path, branch_exists):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    jobs = Jobs(tmp_path / "root")
    predecessor = jobs.submit({**data, "receipt_key": "predecessor"}, 10)
    branch = f"taskwatch/{predecessor.id[:8]}"
    if branch_exists:
        git(tmp_path / "repo", "branch", branch)
    chain = {"receipt_key": "successor", "base": "@predecessor", "predecessors": ["predecessor"]}
    # A commit frozen now cannot be the base the predecessor's result gives later.
    with pytest.raises(ValueError, match="same @<receipt key> as base"):
        jobs.submit({**data, **chain}, 11)
    unit = [{"id": "pr", "base": "@predecessor"}]
    successor = jobs.submit({**data, **chain, "public_text": unit}, 11)
    assert successor.base == branch
    assert successor.predecessors == (predecessor.id,)
    assert successor.public_text[0]["base"] == "@predecessor"


def test_historical_multiple_units_and_two_file_text_evidence_remain_readable(tmp_path):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(code_payload(tmp_path, [{"id": "pr", "base": "main"}]), 10)
    base = job.public_text[0]["base"]
    units = (
        {"id": "first", "base": base, "head": "step-a"},
        {"id": "second", "base": "step-a", "head": "HEAD"},
    )
    texts = {
        unit["id"]: {
            "base": base,
            "head": base,
            "tree": "0" * 40,
            "text_fingerprint": "1" * 64,
            "title": "fix: preserve historical evidence",
            "body": "Historical title and body only.\n",
        }
        for unit in units
    }
    historical = replace(job, public_text=units, texts=texts)
    jobs.save(historical, 11)
    loaded = Jobs(tmp_path / "root").load()[job.id]
    assert loaded.public_text == historical.public_text
    assert loaded.texts == historical.texts


def test_a_code_job_without_public_text_remains_a_nonpublisher(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    del data["public_text"]
    job = Jobs(tmp_path / "root").submit(data, 10)
    assert job.public_text == ()


@pytest.mark.parametrize("field", ["text_contract", "public_text_version"])
def test_submit_does_not_accept_a_public_text_version_key(tmp_path, field):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    with pytest.raises(ValueError, match="unknown fields"):
        Jobs(tmp_path / "root").submit({**data, field: 2}, 10)


@pytest.mark.parametrize(
    "units",
    [
        [{"id": "pr", "base": "no-such-ref"}],
        [{"id": "pr", "base": "main"}, {"id": "pr", "base": "main"}],
        [{"id": "Bad Id", "base": "main"}],
        [{"id": "pr", "base": "--upload-pack=x"}],
        [{"id": "a", "base": "main", "head": "other"}, {"id": "b", "base": "HEAD~1"}],
        [{"id": "pr"}],
        [],
        {"id": "pr", "base": "main"},
        [{"id": "pr", "base": "main", "text_contract": 2}],
        [{"id": "pr", "base": "main", "version": 2}],
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

    def __init__(self, tmp_path, monkeypatch, base="main", blank_body=False):
        repo = make_repo(tmp_path, "repo")
        if base != "main":
            git(repo, "branch", base)
        super().__init__(
            tmp_path,
            monkeypatch,
            review="code",
            target=repo,
            public_text=[
                {"id": "pr", "base": base, **({"blank_squash_body": True} if blank_body else {})}
            ],
        )
        assert self.run("check") == 0  # creates the work folder; nothing is built yet
        self.tree = self.folder() / "worktree"
        for name, text in GOOD_FILES.items():
            if name in RECORDS:
                (self.folder() / name).write_text(text, encoding="utf-8")
        (self.folder() / "pr" / "pr").mkdir(parents=True, exist_ok=True)
        self.write_text("title.txt", "feat(x): add\n")
        self.write_text("body.md", "body\n")
        self.write_text("commit.txt", "" if blank_body else "- Preserve the complete result\n")

    def write_text(self, name: str, text: str) -> None:
        (self.folder() / "pr" / "pr" / name).write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        (self.tree / "out.txt").write_text("ok\n", encoding="utf-8")
        git(self.tree, "add", "out.txt")
        git(self.tree, "commit", "-m", message)

    def problems(self) -> list[str]:
        assert self.run("check") == 0
        return list(self.get().checks[-1]["problems"])


@pytest.mark.parametrize("body", ["", "x" * 73, "Co-authored-by: A <a@example.invalid>"])
def test_invalid_squash_body_cannot_reach_review(unit_env, body):
    unit_env.commit("feat(x): add\n\nWhy.")
    unit_env.write_text("commit.txt", body)
    assert any("squash message" in p for p in unit_env.problems())
    assert unit_env.get().stage == "fix" and unit_env.get().reviews == ()


def test_blank_squash_body_is_an_error_unless_declared(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    unit_env.write_text("commit.txt", "")
    assert any("squash body" in p for p in unit_env.problems())
    assert unit_env.get().stage == "fix"


@pytest.mark.parametrize("body", ["", "\n"])
def test_a_declared_blank_squash_body_reaches_review(tmp_path, monkeypatch, body):
    env = UnitEnv(tmp_path, monkeypatch, blank_body=True)
    env.commit("feat(x): add\n\nWhy.")
    env.write_text("commit.txt", body)
    assert env.problems() == []
    assert env.get().stage == "review"


def test_a_declared_blank_squash_body_refuses_text(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch, blank_body=True)
    env.commit("feat(x): add\n\nWhy.")
    env.write_text("commit.txt", "- Explain the reason\n")
    assert any("must be blank" in p for p in env.problems())
    assert env.get().stage == "fix"


def test_a_declared_blank_squash_body_still_needs_the_file_title_and_body(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch, blank_body=True)
    env.commit("feat(x): add\n\nWhy.")
    env.write_text("commit.txt", "")
    (env.folder() / "pr/pr/commit.txt").unlink()
    assert env.problems() == ["required file is missing: pr/pr/commit.txt"]
    env.write_text("commit.txt", "")
    env.write_text("body.md", "\n")
    assert any("nonblank" in p for p in env.problems())


def test_squash_bullet_rules_use_host_configuration_without_rewriting(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    body = "- Explain the reason\n  on a second line\n"
    unit_env.write_text("commit.txt", body)
    assert unit_env.problems() == []
    unit_env.config = replace(unit_env.config, bullet_one_line=True)
    assert any("squash message" in p and "continues" in p for p in unit_env.problems())
    assert (unit_env.folder() / "pr/pr/commit.txt").read_text() == body


def test_missing_squash_body_is_a_required_file_problem(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    (unit_env.folder() / "pr/pr/commit.txt").unlink()
    assert unit_env.problems() == ["required file is missing: pr/pr/commit.txt"]


def test_a_multiline_title_cannot_become_a_squash_subject(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    unit_env.write_text("title.txt", "feat(x): add\n\nHidden body\n")
    assert any("exactly one subject line" in p for p in unit_env.problems())
    assert unit_env.get().stage == "fix"


@pytest.mark.parametrize("name", ["title.txt", "commit.txt"])
def test_non_utf8_publication_text_routes_to_worker_fix(unit_env, name):
    unit_env.commit("feat(x): add\n\nWhy.")
    (unit_env.folder() / "pr/pr" / name).write_bytes(b"\xff\n")
    assert any("UTF-8" in p for p in unit_env.problems())
    assert unit_env.get().stage == "fix"


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

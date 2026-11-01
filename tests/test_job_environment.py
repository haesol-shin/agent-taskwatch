import sys
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.fingerprint import uncommitted_changes
from taskwatch.job_env import job_environment
from taskwatch.jobs import load_job_config
from tests.git_fixture import make_repo
from tests.test_job_delivery import write_config
from tests.test_job_runner import GIT, GOOD_FILES, Env, work_turn

PROBE = "TW_PROBE"


def _probe(expect: str) -> tuple[str, ...]:
    code = f"import os, sys; sys.exit(0 if os.environ.get('{PROBE}') == '{expect}' else 1)"
    return (sys.executable, "-c", code)


def test_path_entries_expand_home_and_extend_the_inherited_path():
    base = {"HOME": "/h", "PATH": "/usr/bin:/bin"}
    config = {"PATH": "~/.local/bin:$HOME/.bun/bin:$PATH:/opt/x", "A": "~/$HOME"}
    env = job_environment(config, base)
    assert env["PATH"] == "/h/.local/bin:/h/.bun/bin:/usr/bin:/bin:/opt/x"
    assert env["A"] == "~/$HOME"
    assert base["PATH"] == "/usr/bin:/bin"


def test_empty_env_keeps_the_inherited_environment():
    base = {"HOME": "/h", "PATH": "/usr/bin"}
    assert job_environment({}, base) == base


def test_config_env_defaults_to_empty_and_loads_values(tmp_path: Path):
    write_config(tmp_path)
    assert load_job_config(tmp_path).env == {}
    write_config(tmp_path, env={"PATH": "~/.local/bin:$PATH"})
    assert load_job_config(tmp_path).env == {"PATH": "~/.local/bin:$PATH"}


@pytest.mark.parametrize(
    "value", [["PATH"], "PATH=x", {"PATH": 1}, {"A": None}, {"": "x"}, {"A=B": "x"}]
)
def test_config_refuses_a_bad_env(tmp_path: Path, value):
    write_config(tmp_path, env=value)
    with pytest.raises(ValueError, match="env"):
        load_job_config(tmp_path)


def test_worker_and_check_get_the_configured_environment(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.config = replace(env.config, env={PROBE: "set"})
    writer = [
        sys.executable,
        "-c",
        f"import os; open('worker.txt','w').write(os.environ.get('{PROBE}',''))",
    ]
    env.scenario([work_turn(run=[writer])])
    assert env.run("work") == 0
    assert (env.folder() / "work" / "worker.txt").read_text() == "set"
    assert env.run("check", check_command=_probe("set")) == 0
    assert env.get().stage == "review"


def test_check_without_the_environment_does_not_see_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.seed(GOOD_FILES)
    assert env.run("check", check_command=_probe("set")) == 0
    assert env.get().stage == "fix"


def test_records_live_in_the_job_folder_and_the_clean_check_needs_no_exception(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path, "repo")
    env = Env(tmp_path, monkeypatch, review="code", target=repo)
    env.scenario([work_turn(run=[["git", "add", "-A"], [*GIT, "commit", "-m", "work"]])])
    assert env.run("work") == 0
    worktree = env.folder() / "worktree"
    env.seed({"PROGRESS.md": "done\n", "checklist.json": GOOD_FILES["checklist.json"]})
    assert (env.folder() / "PROGRESS.md").is_file()
    assert not (worktree / "PROGRESS.md").exists()
    assert uncommitted_changes(worktree) == ()
    (worktree / "stray.txt").write_text("x\n", encoding="utf-8")
    assert uncommitted_changes(worktree) == ("?? stray.txt",)


def test_worker_prompt_and_workflow_name_absolute_record_paths(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.scenario([work_turn()])
    assert env.run("work") == 0
    folder = env.folder()
    prompt = env.prompts()[0]
    assert f"Progress file: {folder / 'PROGRESS.md'}" in prompt
    assert f"Checklist file: {folder / 'checklist.json'}" in prompt
    workflow = (folder / "WORKFLOW.md").read_text(encoding="utf-8")
    assert (
        f"`{folder / 'PROGRESS.md'}`" in workflow and f"`{folder / 'checklist.json'}`" in workflow
    )


def test_old_record_naming_taskwatch_paths_still_loads_and_resolves_in_the_folder(
    tmp_path, monkeypatch
):
    env = Env(tmp_path, monkeypatch)
    env.seed({})
    old = replace(
        env.get(), checklist=".taskwatch/checklist.json", progress=".taskwatch/progress.md"
    )
    with env.jobs.state.lock():
        env.jobs.save(old, 1500.0)
    loaded = env.get()
    assert (loaded.checklist, loaded.progress) == (
        ".taskwatch/checklist.json",
        ".taskwatch/progress.md",
    )
    records = env.folder() / ".taskwatch"
    records.mkdir()
    (records / "progress.md").write_text("done\n", encoding="utf-8")
    (records / "checklist.json").write_text(GOOD_FILES["checklist.json"], encoding="utf-8")
    env.seed({"out.txt": "ok\n", "brief.md": GOOD_FILES["brief.md"]})
    assert env.run("check") == 0
    assert env.get().checks[-1]["problems"] == ()


def test_write_rules_allow_the_job_records_outside_the_workspace(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.scenario([work_turn()])
    assert env.run("work") == 0
    prompt = env.prompts()[0]
    assert "outside the workspace (except this job's progress, checklist and brief files" in prompt
    workflow = (env.folder() / "WORKFLOW.md").read_text(encoding="utf-8")
    assert "Work only in the workspace" in workflow
    assert "Exception: the progress, checklist and brief files named below" in workflow
    assert (
        "outside the workspace (except this job's progress, checklist and brief files" in workflow
    )

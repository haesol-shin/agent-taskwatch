"""Work-folder lifecycle of code jobs, on real temporary repositories with a local bare origin."""

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskwatch import job_runner, job_scan, job_workspace
from taskwatch.job_cli import main as cli_main
from taskwatch.job_env import job_environment
from taskwatch.jobs import JobConfig, Jobs, JobState, _digest, load_job_config
from tests.git_fixture import git, make_repo
from tests.spec_fixture import VALID_SPEC
from tests.test_job_scan import Board

ENV = job_environment({})


class Remote:
    """A bare origin, a clone that plays the job target, and a second clone that merges."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True)
        seed = make_repo(root, "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "origin", "main")
        subprocess.run(["git", "clone", str(self.origin), str(root / "repo")], check=True)
        subprocess.run(["git", "clone", str(self.origin), str(root / "other")], check=True)
        self.target = root / "repo"
        self.other = root / "other"

    def publish(self, name: str, text: str, message: str = "upstream") -> str:
        """Commit a file in the other clone and push it to origin's main."""
        git(self.other, "pull", "--quiet", "origin", "main")
        (self.other / name).write_text(text, encoding="utf-8")
        git(self.other, "add", name)
        git(self.other, "commit", "-m", message)
        git(self.other, "push", "--quiet", "origin", "main")
        return git(self.other, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return Remote(tmp_path)


def code_job(tmp_path: Path, target: Path, **extra: object) -> JobState:
    (tmp_path / "intent.md").write_text("intent", encoding="utf-8")
    (tmp_path / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
    payload = {
        "receipt_key": f"k{len(list(tmp_path.iterdir()))}",
        "approval_source": "msg-1",
        "approval_scope": "task-1",
        "prompt": "do it",
        "review": "code",
        "target": str(target),
        "intent": str(tmp_path / "intent.md"),
        "spec": str(tmp_path / "spec.md"),
        "outputs": ["out.txt"],
        "check_command": ["true"],
        "checklist": "checklist.json",
        "progress": "progress.md",
        **extra,
    }
    return Jobs(tmp_path / "home").submit(payload, 1000.0)


def start(job: JobState, config: JobConfig | None = None) -> tuple[JobState, Path]:
    """What the runner does on a first step: create the worktree and record it."""
    path = Path(job.target).parent / "wt" / job.id[:8]
    changes = job_workspace.create(job, path, config or JobConfig(), ENV)
    return replace(job, workspace=str(path), **changes), path


# --- base and branch -----------------------------------------------------------------------


def test_fresh_base_picks_up_a_commit_pushed_after_the_clone(tmp_path, remote):
    job = code_job(tmp_path, remote.target)
    pushed = remote.publish("new.txt", "new\n")
    job, path = start(job)
    assert job.base == pushed and job.base_note == ""
    assert (path / "new.txt").read_text() == "new\n"
    assert git(remote.target, "rev-parse", "main") != pushed


def test_fetch_failure_uses_the_local_ref_and_records_why(tmp_path, remote):
    before = git(remote.target, "rev-parse", "origin/main")
    remote.publish("new.txt", "new\n")
    git(remote.target, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    job, path = start(code_job(tmp_path, remote.target))
    assert job.base == before
    assert job.base_note.startswith("base may be stale: fetch failed")
    assert not (path / "new.txt").exists()


def test_no_origin_falls_back_to_the_local_branch(tmp_path):
    repo = make_repo(tmp_path, "solo")
    job, _ = start(code_job(tmp_path, repo))
    assert job.base == git(repo, "rev-parse", "main")
    assert job.base_note == "base may be stale: no origin remote"


def test_base_field_overrides_the_base(tmp_path, remote):
    git(remote.target, "switch", "-c", "stack")
    (remote.target / "stacked.txt").write_text("s\n", encoding="utf-8")
    git(remote.target, "add", "stacked.txt")
    git(remote.target, "commit", "-m", "stack")
    git(remote.target, "switch", "main")
    remote.publish("new.txt", "new\n")
    job, path = start(code_job(tmp_path, remote.target, base="stack"))
    assert job.base == git(remote.target, "rev-parse", "stack") and job.base_note == ""
    assert (path / "stacked.txt").exists() and not (path / "new.txt").exists()


def test_unknown_base_is_refused(tmp_path, remote):
    job = code_job(tmp_path, remote.target, base="nowhere")
    with pytest.raises(job_workspace.WorkspaceError, match="not a commit"):
        start(job)


@pytest.mark.parametrize("prefix", [None, "work/"])
def test_branch_name_uses_the_prefix(tmp_path, remote, prefix):
    config = JobConfig() if prefix is None else JobConfig(branch_prefix=prefix)
    job, path = start(code_job(tmp_path, remote.target), config)
    expected = f"{prefix or 'taskwatch/'}{job.id[:8]}"
    assert job.branch == expected
    assert git(path, "branch", "--show-current") == expected
    assert (job.workspace_state, job.branch_state) == ("active", "kept")


@pytest.mark.parametrize("prefix", ["", "a b", "-x", "a//b", "a..b", "x.lock", 7])
def test_bad_branch_prefix_is_a_config_error(tmp_path, prefix):
    folder = tmp_path / "taskwatch"
    folder.mkdir()
    config = {
        "version": 1,
        "slots": 1,
        "stall_seconds": 1,
        "omp_command": ["omp"],
        "rpc_timeout": 1,
        "deliver_command": ["x", "{message}"],
        "branch_prefix": prefix,
    }
    (folder / "jobs-config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="branch_prefix"):
        load_job_config(tmp_path)


def test_base_only_for_code_jobs_that_build(tmp_path, remote):
    with pytest.raises(ValueError, match="base"):
        code_job(tmp_path, remote.target, review="source", base="main")
    with pytest.raises(ValueError, match="base"):
        code_job(tmp_path, remote.target, base="-x")


def test_resume_reuses_the_worktree_and_branch(tmp_path, remote):
    job = code_job(tmp_path, remote.target)
    folder = Jobs(tmp_path / "home").folder(job)
    config = JobConfig()
    path, changes = job_runner._workspace(job, folder, config, ENV)
    recorded = replace(job, workspace=str(path), **changes)
    (path / "mark.txt").write_text("kept\n", encoding="utf-8")
    again, more = job_runner._workspace(recorded, folder, config, ENV)
    assert again == path and more == {}
    assert (path / "mark.txt").exists()
    assert git(path, "branch", "--show-current") == recorded.branch


def test_interrupted_setup_is_adopted_on_resume(tmp_path, remote):
    job = code_job(tmp_path, remote.target)
    folder = Jobs(tmp_path / "home").folder(job)
    _, changes = job_runner._workspace(job, folder, JobConfig(), ENV)
    _, adopted = job_runner._workspace(job, folder, JobConfig(), ENV)  # nothing was recorded
    assert adopted["branch"] == changes["branch"] and adopted["workspace_state"] == "active"


# --- cleanup -------------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path, remote: Remote) -> Board:
    return Board(tmp_path)


def settled(board: Board, **extra: object) -> tuple[str, Path]:
    """A code job on the clone with a real worktree, as the runner leaves it."""
    job_id = board.add(target="repo", **extra)
    job = board.get(job_id)
    path = board.jobs.folder(job) / "worktree"
    board.set(job_id, workspace=str(path), **job_workspace.create(job, path, board.config, ENV))
    return job_id, path


def commit_in(path: Path, name: str, text: str) -> None:
    (path / name).write_text(text, encoding="utf-8")
    git(path, "add", name)
    git(path, "commit", "-m", f"add {name}")


@pytest.mark.parametrize("stage", ["done", "cancelled"])
def test_clean_worktree_is_removed_at_done_and_cancelled(board, stage):
    job_id, path = settled(board)
    board.finish_step(job_id, stage)
    board.scan()
    job = board.get(job_id)
    assert job.workspace_state == "removed" and not path.exists()
    assert (board.jobs.folder(job) / "inputs").is_dir()


def test_worktree_stays_while_the_result_is_undelivered(board, monkeypatch):
    monkeypatch.setattr(job_scan, "deliver_job", lambda jobs, config, job, now: job)
    job_id, path = settled(board)
    board.finish_step(job_id, "deliver_pending")
    board.scan()
    job = board.get(job_id)
    assert job.stage == "deliver_pending" and path.exists()
    assert job.workspace_state == "active"


def test_delivery_to_done_removes_the_worktree(board, monkeypatch):
    def deliver(jobs, config, job, now):
        done = replace(job, stage="done")
        jobs.save(done, now)
        return done

    monkeypatch.setattr(job_scan, "deliver_job", deliver)
    job_id, path = settled(board)
    board.finish_step(job_id, "deliver_pending")
    board.scan()
    assert board.get(job_id).stage == "done" and not path.exists()


def test_dirty_worktree_is_kept_with_the_reason_then_removed_when_clean(board):
    job_id, path = settled(board)
    (path / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    board.finish_step(job_id, "done")
    board.scan()
    job = board.get(job_id)
    assert path.exists() and job.workspace_state.startswith("kept: ")
    assert "untracked" in job.workspace_state or "modified" in job.workspace_state
    assert job.branch_state == "kept"
    (path / "scratch.txt").unlink()
    board.scan()
    assert board.get(job_id).workspace_state == "removed" and not path.exists()


def test_live_unit_blocks_cleanup(board):
    job_id, path = settled(board)
    board.scan()
    board.service.stop_ok = False
    board.set(job_id, stage="cancelled")
    board.scan()
    assert path.exists() and board.get(job_id).workspace_state == "active"


# --- branch deletion -----------------------------------------------------------------------


def branch_exists(remote: Remote, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(remote.target), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
    )
    return proc.returncode == 0


def test_unmerged_branch_is_kept_then_deleted_after_a_fast_forward_merge(board, remote):
    job_id, path = settled(board)
    commit_in(path, "out.txt", "built\n")
    board.finish_step(job_id, "done")
    board.scan()
    job = board.get(job_id)
    assert (job.workspace_state, job.branch_state) == ("removed", "kept")
    assert branch_exists(remote, job.branch)
    git(remote.target, "push", "--quiet", "origin", f"{job.branch}:main")
    board.scan()
    job = board.get(job_id)
    assert job.branch_state == "deleted" and not branch_exists(remote, job.branch)


def test_squash_merge_deletes_the_branch(board, remote):
    job_id, path = settled(board)
    commit_in(path, "feature.txt", "feature\n")
    board.finish_step(job_id, "done")
    board.scan()
    assert board.get(job_id).branch_state == "kept"
    remote.publish("feature.txt", "feature\n", "squash of the job")
    board.scan()
    job = board.get(job_id)
    assert job.branch_state == "deleted" and not branch_exists(remote, job.branch)


def test_branch_is_kept_when_the_squash_differs(board, remote):
    job_id, path = settled(board)
    commit_in(path, "feature.txt", "feature\n")
    commit_in(path, "extra.txt", "only on the branch\n")
    board.finish_step(job_id, "done")
    remote.publish("feature.txt", "feature\n", "partial squash")
    board.scan()
    job = board.get(job_id)
    assert job.branch_state == "kept" and branch_exists(remote, job.branch)
    remote.publish("feature.txt", "feature changed upstream\n", "conflicting change")
    board.scan()
    assert board.get(job_id).branch_state == "kept"


def test_cancelled_job_with_unmerged_work_keeps_its_branch(board, remote):
    job_id, path = settled(board)
    commit_in(path, "half.txt", "half\n")
    board.set(job_id, stage="cancelled")
    board.scan()
    job = board.get(job_id)
    assert (job.workspace_state, job.branch_state) == ("removed", "kept")
    assert branch_exists(remote, job.branch)


# --- lock and records ----------------------------------------------------------------------


def test_two_code_jobs_on_one_target_run_together(board):
    first, second = board.add(target="repo"), board.add(target="repo")
    assert board.scan() == [first, second]


def test_code_job_does_not_block_a_shared_folder_job_but_slots_still_limit(tmp_path, remote):
    board = Board(tmp_path, slots=2)
    code = board.add(target="repo")
    doc = board.add(target="repo", review="source")
    third = board.add(target="repo")
    assert board.scan() == [code, doc]
    assert board.get(third).wait_reason == "slot"


def test_non_code_jobs_on_one_target_still_wait(board):
    first = board.add(target="repo", review="source")
    second = board.add(target="repo", review="source")
    assert board.scan() == [first]
    assert board.get(second).wait_reason == "target"


def test_record_without_the_new_fields_loads(tmp_path, remote):
    job = code_job(tmp_path, remote.target)
    jobs = Jobs(tmp_path / "legacy")
    data = job.to_json()
    for key in ("base", "base_note", "branch", "workspace_state", "branch_state"):
        del data[key]
    jobs.state.append_record(
        "jobs.jsonl",
        {"job": data, "sha256": _digest(data)},
        datetime.fromtimestamp(2000.0, timezone.utc),
    )
    loaded = jobs.load()[job.id]
    assert (loaded.base, loaded.branch, loaded.workspace_state, loaded.branch_state) == ("",) * 4


def test_old_record_is_not_cleaned_up(board, remote):
    job_id = board.add(target="repo")
    path = board.jobs.folder(board.get(job_id)) / "worktree"
    git(remote.target, "worktree", "add", "--detach", str(path), "HEAD")
    board.set(job_id, workspace=str(path))
    board.finish_step(job_id, "done")
    board.scan()
    assert path.exists() and board.get(job_id).workspace_state == ""


def test_show_prints_the_lifecycle_fields(tmp_path, remote, capsys):
    job, _ = start(code_job(tmp_path, remote.target))
    Jobs(tmp_path / "home").save(job, 2000.0)
    assert cli_main(["--root", str(tmp_path / "home"), "show", job.id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["branch"] == job.branch and shown["workspace_state"] == "active"
    assert shown["base"] == job.base and shown["branch_state"] == "kept"


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        (None, False),
        ({}, False),
        ({"bullet_one_line": True}, True),
        ({"bullet_one_line": False}, False),
    ],
)
def test_commit_rules_are_read_from_the_config(tmp_path, rules, expected):
    assert _config_with(tmp_path, rules).bullet_one_line is expected


@pytest.mark.parametrize("rules", [{"width": 80}, {"bullet_one_line": "yes"}, [], True])
def test_bad_commit_rules_are_a_config_error(tmp_path, rules):
    with pytest.raises(ValueError, match="commit_rules"):
        _config_with(tmp_path, rules)


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("en", "en"), ("ko", "ko")])
def test_language_is_read_from_the_config(tmp_path, value, expected):
    assert _config_with(tmp_path, None, value).language == expected


@pytest.mark.parametrize("value", ["fr", "", "KO", 1, None])
def test_an_unknown_language_is_a_config_error(tmp_path, value):
    with pytest.raises(ValueError, match="language"):
        _config_with(tmp_path, None, value, explicit=True)


def _config_with(root, rules, language=None, explicit=False):
    folder = root / "taskwatch"
    folder.mkdir(exist_ok=True)
    config = {
        "version": 1,
        "slots": 1,
        "stall_seconds": 1,
        "omp_command": ["omp"],
        "rpc_timeout": 1,
        "deliver_command": ["x", "{message}"],
    }
    if rules is not None:
        config["commit_rules"] = rules
    if language is not None or explicit:
        config["language"] = language
    (folder / "jobs-config.json").write_text(json.dumps(config), encoding="utf-8")
    return load_job_config(root)


def test_repository_key_unifies_aliases_and_linked_worktrees(tmp_path, remote):
    alias = tmp_path / "alias"
    alias.symlink_to(remote.target, target_is_directory=True)
    linked = tmp_path / "linked"
    git(remote.target, "worktree", "add", "--detach", str(linked), "HEAD")
    expected = str((remote.target / ".git").resolve())
    assert job_workspace.repository_key(str(remote.target), ENV) == expected
    assert job_workspace.repository_key(str(alias), ENV) == expected
    assert job_workspace.repository_key(str(linked), ENV) == expected
    assert job_workspace.repository_key(str(remote.other), ENV) != expected


def _landed_predecessor(tmp_path, remote, **changes):
    job = code_job(
        tmp_path,
        remote.target,
        receipt_key="predecessor",
        land=True,
        public_text=[{"id": "result", "base": "HEAD"}],
    )
    sha = remote.publish("landed.txt", "approved tree\n")
    git(remote.target, "fetch", "origin", "main")
    state = {
        "S": sha,
        "T": git(remote.target, "rev-parse", f"{sha}^{{tree}}"),
        "tree_verified": True,
        "install": {"status": "succeeded"},
        **changes,
    }
    job = replace(job, stage="done", branch="taskwatch/predecessor", land_state=state)
    jobs = Jobs(tmp_path / "home")
    jobs.save(job, 2000)
    return jobs, job


@pytest.mark.parametrize("status", ["succeeded", "disabled"])
def test_receipt_base_uses_landed_sha_after_branch_cleanup(tmp_path, remote, status):
    jobs, predecessor = _landed_predecessor(tmp_path, remote, install={"status": status})
    sha = predecessor.land_state["S"]
    git(remote.target, "branch", predecessor.branch, "HEAD")
    git(remote.target, "branch", "-d", predecessor.branch)
    remote.publish("later.txt", "later unrelated main\n")
    git(remote.target, "fetch", "origin", "main")
    successor = code_job(
        tmp_path,
        remote.target,
        receipt_key="successor",
        base="@predecessor",
        predecessors=["predecessor"],
    )
    path = tmp_path / "successor-worktree"
    changes = job_workspace.create(successor, path, JobConfig(), ENV, jobs)
    assert changes["base"] == sha and changes["base_note"] == ""
    assert successor.base_predecessor == predecessor.id
    assert git(path, "rev-parse", "HEAD") == sha
    assert (path / "landed.txt").read_text() == "approved tree\n"
    assert not (path / "later.txt").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"S": ""},
        {"S": "a" * 40},
        {"T": "b" * 40},
        {"tree_verified": False},
        {"install": {"status": "not_started"}},
        {"install": {"status": "started"}},
        {"install": {"status": "failed"}},
        {"install": {"status": "unknown"}},
    ],
)
def test_receipt_base_refuses_unverified_or_uninstalled_landing(tmp_path, remote, change):
    jobs, _ = _landed_predecessor(tmp_path, remote, **change)
    successor = code_job(tmp_path, remote.target, receipt_key="successor", base="@predecessor")
    with pytest.raises(job_workspace.WorkspaceError, match="predecessor"):
        job_workspace.create(successor, tmp_path / "successor-worktree", JobConfig(), ENV, jobs)
    assert not (tmp_path / "successor-worktree").exists()


def test_landed_predecessor_requires_readiness_and_same_repository(tmp_path, remote):
    jobs, predecessor = _landed_predecessor(tmp_path, remote)
    successor = code_job(tmp_path, remote.target, receipt_key="successor", base="@predecessor")
    jobs.save(replace(predecessor, stage="user_wait"), 2001)
    with pytest.raises(job_workspace.WorkspaceError, match="completed"):
        job_workspace.create(successor, tmp_path / "successor-worktree", JobConfig(), ENV, jobs)
    jobs.save(predecessor, 2002)
    foreign = replace(successor, target=str(remote.other))
    with pytest.raises(job_workspace.WorkspaceError, match="different repository"):
        job_workspace.create(foreign, tmp_path / "successor-worktree", JobConfig(), ENV, jobs)


@pytest.mark.parametrize("created", [False, True])
def test_interrupted_predecessor_worktree_setup_preserves_base_on_retry(
    tmp_path, remote, monkeypatch, created
):
    jobs, predecessor = _landed_predecessor(tmp_path, remote)
    sha = predecessor.land_state["S"]
    git(remote.target, "branch", predecessor.branch, "HEAD")
    jobs.save(replace(predecessor, stage="work"), 2001)
    successor = code_job(tmp_path, remote.target, receipt_key="successor", base="@predecessor")
    assert successor.base == predecessor.branch
    successor = replace(successor, stage="work", attempt=1)
    jobs.save(successor, 2002)
    jobs.save(predecessor, 2003)
    folder = jobs.folder(successor)
    original = job_workspace.create

    def interrupted(job, path, config, env, jobs=None):
        saved = jobs.load()[job.id]
        assert saved.base == sha and saved.workspace_state == "prepared"
        if created:
            original(job, path, config, env, jobs)
        raise RuntimeError("interrupted setup")

    monkeypatch.setattr(job_workspace, "create", interrupted)
    with pytest.raises(RuntimeError, match="interrupted setup"):
        job_runner._workspace(successor, folder, JobConfig(), ENV, jobs)
    saved = jobs.load()[successor.id]
    assert saved.base == sha
    # Neither a moving branch nor later predecessor metadata may change the saved base.
    later = remote.publish("later.txt", "later unrelated result\n")
    git(remote.target, "fetch", "origin", "main")
    git(remote.target, "branch", "-f", predecessor.branch, later)
    jobs.save(replace(predecessor, stage="user_wait"), 2004)
    monkeypatch.setattr(job_workspace, "create", original)
    path, changes = job_runner._workspace(saved, folder, JobConfig(), ENV, jobs)
    assert git(path, "rev-parse", "HEAD") == sha
    assert jobs.load()[successor.id].base == sha
    assert changes["workspace_state"] == "active"
    assert (path / "landed.txt").read_text() == "approved tree\n"
    assert not (path / "later.txt").exists()


def test_nonlanding_receipt_base_keeps_existing_branch_behavior(tmp_path, remote):
    jobs = Jobs(tmp_path / "home")
    predecessor = code_job(tmp_path, remote.target, receipt_key="predecessor")
    branch = f"taskwatch/{predecessor.id[:8]}"
    git(remote.target, "branch", branch, "HEAD")
    successor = code_job(tmp_path, remote.target, receipt_key="successor", base="@predecessor")
    path = tmp_path / "successor-worktree"
    changes = job_workspace.create(successor, path, JobConfig(), ENV, jobs)
    assert changes["base"] == git(remote.target, "rev-parse", branch)


def test_public_text_receipt_base_resolves_with_the_job_base(tmp_path, remote):
    jobs, predecessor = _landed_predecessor(tmp_path, remote)
    unit = {"id": "result", "base": "@predecessor"}
    successor = code_job(
        tmp_path,
        remote.target,
        receipt_key="successor",
        base="@predecessor",
        predecessors=["predecessor"],
        public_text=[unit],
    )
    assert successor.public_text[0]["base"] == "@predecessor"
    prepared = job_workspace.prepare(successor, ENV, jobs)
    landed = predecessor.land_state["S"]
    assert prepared["base"] == landed
    assert [u["base"] for u in prepared["public_text"]] == [landed]
    assert prepared["public_text"][0]["id"] == "result"
    # A unit pinned to a commit at submit has nothing to resolve.
    pinned = code_job(
        tmp_path,
        remote.target,
        receipt_key="pinned",
        public_text=[{"id": "result", "base": "HEAD"}],
    )
    assert "public_text" not in job_workspace.prepare(pinned, ENV, jobs)


@pytest.mark.parametrize("damage", ["none", "tip", "review", "repository"])
def test_accepted_tip_pins_workspace_and_public_text_together(tmp_path, remote, damage):
    jobs, prior = _landed_predecessor(tmp_path, remote)
    accepted = prior.land_state["S"]
    git(remote.target, "branch", prior.branch, accepted)
    prior = replace(prior, stage="land", land_state={}, reviews=({"fingerprint": accepted},))
    jobs.save(prior, 2001)
    successor = code_job(
        tmp_path,
        remote.target,
        receipt_key="child",
        base="@predecessor",
        land=True,
        public_text=[{"id": "pr", "base": "@predecessor"}],
    )
    if damage == "tip":
        git(remote.target, "branch", "-f", prior.branch, "HEAD")
    elif damage == "review":
        jobs.save(replace(prior, reviews=()), 2002)
    elif damage == "repository":
        successor = replace(successor, target=str(remote.other))
    if damage != "none":
        with pytest.raises(job_workspace.WorkspaceError, match="predecessor"):
            job_workspace.prepare(successor, ENV, jobs)
        return
    prepared = job_workspace.prepare(successor, ENV, jobs)
    assert prepared["base"] == prepared["public_text"][0]["base"] == accepted
    pinned = replace(successor, **prepared)
    path = tmp_path / "child-worktree"
    job_workspace.create(pinned, path, JobConfig(), ENV, jobs)
    assert git(path, "rev-parse", "HEAD") == accepted

"""The fast check selects tests from changed paths and never runs the real suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import check_fast
from tests.git_fixture import git, make_repo


class Recorder:
    """A runner that records commands and fails the one named in `failure`."""

    def __init__(self, failure: str | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.failure = failure

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        if self.failure is not None and self.failure in " ".join(argv):
            return subprocess.CompletedProcess(argv, 1, "boom\nlast line\n", "")
        return subprocess.CompletedProcess(argv, 0, "fine\n", "warn\n")


def test_a_changed_module_selects_its_same_name_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_jobs.py").write_text("", encoding="utf-8")
    tests, only_ci = check_fast.tests_for_paths(["taskwatch/jobs.py"], tmp_path)
    assert tests == ["tests/test_jobs.py"]
    assert only_ci == []


def test_a_changed_test_file_selects_itself(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_jobs.py").write_text("", encoding="utf-8")
    tests, _ = check_fast.tests_for_paths(["tests/test_jobs.py"], tmp_path)
    assert tests == ["tests/test_jobs.py"]


def test_a_changed_module_without_a_test_stays_for_ci(tmp_path: Path) -> None:
    tests, only_ci = check_fast.tests_for_paths(["taskwatch/views.py"], tmp_path)
    assert tests == []
    assert only_ci == [("taskwatch/views.py", "not mapped")]


def test_the_measured_slow_test_stays_for_ci(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_jobs.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_job_land.py").write_text("", encoding="utf-8")
    tests, only_ci = check_fast.tests_for_paths(
        ["taskwatch/job_land.py", "tests/test_job_land.py", "taskwatch/jobs.py"], tmp_path
    )
    assert tests == ["tests/test_jobs.py"]
    assert ("taskwatch/job_land.py", "measured slow") in only_ci
    assert ("tests/test_job_land.py", "measured slow") in only_ci


def test_a_new_test_file_is_selected(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("", encoding="utf-8")
    tests, _ = check_fast.tests_for_paths(["tests/test_new.py"], tmp_path)
    assert tests == ["tests/test_new.py"]


def test_build_checks_calls_the_ci_commands(tmp_path: Path) -> None:
    prefix = tmp_path / "installed"
    base = "0" * 40
    checks = check_fast.build_checks(tmp_path, ["tests/test_jobs.py"], prefix, base)
    commands = [check.argv for check in checks]
    assert commands[0] == ("uv", "sync", "--locked")
    assert ("uv", "run", "ruff", "check") in commands
    assert ("uv", "run", "ruff", "format", "--check") in commands
    assert ("uv", "run", "python", "scripts/check_review_docs.py") in commands
    assert ("uv", "pip", "install", "--prefix", str(prefix), ".") in commands
    assert (
        "uv",
        "run",
        "python",
        "scripts/check_review_docs.py",
        "--tree",
        str(prefix / "share"),
    ) in commands
    commit_check = (
        "uv",
        "run",
        "python",
        "scripts/check_commit_messages.py",
        "--range",
        f"{base}..HEAD",
    )
    assert commit_check in commands
    assert commands.index(commit_check) < commands.index(
        ("uv", "run", "pytest", "tests/test_jobs.py")
    )
    assert ("uv", "run", "pytest", "tests/test_jobs.py") in commands
    assert commands[-3:] == [
        ("bun", "install", "--frozen-lockfile"),
        ("bun", "run", "typecheck"),
        ("bun", "run", "test"),
    ]
    assert all(check.cwd == tmp_path / check_fast.EXTENSION for check in checks[-3:])


def test_no_selected_test_skips_pytest(tmp_path: Path) -> None:
    checks = check_fast.build_checks(tmp_path, [], tmp_path / "installed", "0" * 40)
    assert all(check.argv[:3] != ("uv", "run", "pytest") for check in checks)


def test_a_failing_check_stops_and_keeps_the_output_tail(tmp_path: Path, capsys) -> None:
    recorder = Recorder(failure="ruff format")
    checks = check_fast.build_checks(tmp_path, [], tmp_path / "installed", "0" * 40)
    assert check_fast.run_checks(checks, recorder, sys.stdout) is False
    printed = capsys.readouterr().out
    assert "fail: ruff format (exit 1)" in printed
    assert "last line" in printed
    assert recorder.calls[-1][0] == ("uv", "run", "ruff", "format", "--check")


def test_a_clean_run_keeps_each_checks_output(tmp_path: Path, capsys) -> None:
    recorder = Recorder()
    checks = check_fast.build_checks(tmp_path, [], tmp_path / "installed", "0" * 40)
    assert check_fast.run_checks(checks, recorder, sys.stdout) is True
    printed = capsys.readouterr().out
    assert printed.count("ok: ") == len(checks)
    assert printed.count("fine") == len(checks)
    assert printed.count("warn") == len(checks)


def test_changed_paths_uses_the_merge_base(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-b", "topic")
    (repo / "topic.txt").write_text("topic\n", encoding="utf-8")
    git(repo, "add", "topic.txt")
    git(repo, "commit", "-m", "add topic")
    git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", "main.txt")
    git(repo, "commit", "-m", "add main")
    git(repo, "checkout", "topic")
    assert check_fast.changed_paths("main", repo) == ["topic.txt"]


def test_main_reports_ok_and_runs_only_the_selected_test(tmp_path: Path, capsys) -> None:
    repo = make_repo(tmp_path)
    for folder in ("taskwatch", "tests"):
        (repo / folder).mkdir()
    (repo / "taskwatch" / "jobs.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "taskwatch" / "job_land.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "tests" / "test_jobs.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_job_land.py").write_text("", encoding="utf-8")
    git(repo, "add", "taskwatch", "tests")
    git(repo, "commit", "-m", "add jobs")
    merge_base = git(repo, "rev-parse", "HEAD~1")
    recorder = Recorder()
    assert check_fast.main(["--base", "HEAD~1"], runner=recorder, root=repo) == 0
    printed = capsys.readouterr().out
    assert "fast check: ok" in printed
    assert "CI only: taskwatch/job_land.py (measured slow)" in printed
    pytest_calls = [argv for argv, _ in recorder.calls if argv[:3] == ("uv", "run", "pytest")]
    assert pytest_calls == [("uv", "run", "pytest", "tests/test_jobs.py")]
    commit_calls = [
        argv for argv, _ in recorder.calls if "scripts/check_commit_messages.py" in argv
    ]
    assert commit_calls == [
        (
            "uv",
            "run",
            "python",
            "scripts/check_commit_messages.py",
            "--range",
            f"{merge_base}..HEAD",
        )
    ]


def test_an_unknown_base_exits_two_before_running_checks(tmp_path: Path, capsys) -> None:
    repo = make_repo(tmp_path)
    recorder = Recorder()
    assert check_fast.main(["--base", "missing"], runner=recorder, root=repo) == 2
    assert "merge-base" in capsys.readouterr().err
    assert recorder.calls == []

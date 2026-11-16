import os
import subprocess
import sys
from pathlib import Path

from tests.git_fixture import git, make_repo

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_commit_messages.py"


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def head_commit(repo: Path, message: str) -> str:
    git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def good_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    head_commit(repo, "feat(cli): add a flag")
    return repo


def test_a_good_range_passes(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 0
    assert done.stdout.strip() == "ok"


def test_a_bad_subject_fails_with_the_sha(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    sha = head_commit(repo, "update things")
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 1
    assert f"{sha}: subject is not" in done.stdout
    assert "ok" not in done.stdout


def test_a_missing_blank_line_fails(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    sha = head_commit(repo, "feat: add a flag\nBecause the old flag was missing.")
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 1
    assert f"{sha}: one blank line must follow the subject" in done.stdout


def test_a_long_line_fails_with_its_number(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    sha = head_commit(repo, "feat: add a flag\n\n" + "x" * 73)
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 1
    assert f"{sha}: line 3 is 73 characters, the limit is 72" in done.stdout


def test_a_trailer_fails_with_its_number(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    sha = head_commit(repo, "feat: add a flag\n\nCo-authored-by: A <a@example.invalid>")
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 1
    assert f"{sha}: line 3 is a trailer: Co-authored-by" in done.stdout


def test_a_merge_commit_in_the_range_is_skipped(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    git(repo, "checkout", "-b", "topic")
    head_commit(repo, "fix(api): handle an empty list")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "topic", "-m", "Merge branch 'topic'")
    done = run(repo, "--range", "HEAD~1..HEAD")
    assert done.returncode == 0
    assert done.stdout.strip() == "ok"


def test_a_good_message_file_passes(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    path = tmp_path / "message.txt"
    path.write_text("feat: add a flag\n\nBecause the old flag was missing.\n", encoding="utf-8")
    done = run(repo, "--message-file", str(path))
    assert done.returncode == 0
    assert done.stdout.strip() == "ok"


def test_a_bad_message_file_names_the_path(tmp_path: Path) -> None:
    repo = good_repo(tmp_path)
    path = tmp_path / "message.txt"
    path.write_text("update things\n", encoding="utf-8")
    done = run(repo, "--message-file", str(path))
    assert done.returncode == 1
    assert f"{path}: subject is not" in done.stdout

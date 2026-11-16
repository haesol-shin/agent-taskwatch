"""Rebuildable files of a finished job: review tool caches go, the review text stays."""

from pathlib import Path

import pytest

from taskwatch.job_cleanup import REVIEW_TOOL_CACHES
from tests.test_job_scan import Board


@pytest.fixture
def board(tmp_path: Path) -> Board:
    return Board(tmp_path)


def make_review(folder: Path) -> Path:
    root = folder / "reviews" / "review-0-1"
    for cache in REVIEW_TOOL_CACHES:
        (root / cache).mkdir(parents=True)
        (root / cache / "file").write_text("cache\n", encoding="utf-8")
    (root / "review.md").write_text("verdict\n", encoding="utf-8")
    (root / "snapshot").mkdir()
    (root / "snapshot" / "result.txt").write_text("text\n", encoding="utf-8")
    return root


def test_review_tool_caches_are_removed_for_a_finished_job(board: Board) -> None:
    job_id = board.add()
    root = make_review(board.jobs.folder(board.get(job_id)))
    board.finish_step(job_id, "done")
    board.scan()
    assert not any((root / cache).exists() for cache in REVIEW_TOOL_CACHES)
    assert (root / "review.md").is_file()
    assert (root / "snapshot" / "result.txt").is_file()


def test_a_running_job_keeps_its_review_tool_caches(board: Board) -> None:
    job_id = board.add()
    board.scan()
    root = make_review(board.jobs.folder(board.get(job_id)))
    board.scan()
    assert (root / "uv-cache" / "file").is_file()

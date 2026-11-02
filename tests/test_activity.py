import json
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.activity import activity
from taskwatch.job_cli import main
from taskwatch.jobs import Jobs
from tests.test_jobs import payload


@pytest.mark.parametrize("stage", ["work", "fix", "check", "review"])
def test_activity_formats_active_stage_and_last_progress_line(tmp_path: Path, stage: str) -> None:
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path, f"msg:{stage}"), 100.0)
    folder = jobs.folder(job)
    (folder / "progress.md").write_text(
        "# Progress\n- first step done\n  -   second\tstep   running  \n   \n",
        encoding="utf-8",
    )
    running = replace(job, stage=stage, started_at=280.0, last_progress_at=820.0)
    assert activity(running, folder, now=1000.0) == (
        f"{stage} 12m; last progress 3m ago: - second step running"
    )


def test_activity_truncates_progress_line_to_120_chars_and_handles_missing_progress(
    tmp_path: Path,
) -> None:
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 100.0)
    folder = jobs.folder(job)
    running = replace(job, stage="work", started_at=400.0, last_progress_at=880.0)
    assert activity(running, folder, now=1000.0) == "work 10m; last progress 2m ago"

    (folder / "progress.md").write_text("\n   \t  \n", encoding="utf-8")
    assert activity(running, folder, now=1000.0) == "work 10m; last progress 2m ago"

    long_line = "x" * 150
    (folder / "progress.md").write_text(f"short\n{long_line}\n", encoding="utf-8")
    result = activity(running, folder, now=1000.0)
    prefix = "work 10m; last progress 2m ago: "
    assert result is not None and result.startswith(prefix)
    tail = result[len(prefix) :]
    assert len(tail) == 120 and tail == ("x" * 117) + "..."


@pytest.mark.parametrize(
    "stage",
    ["queued", "approval_wait", "user_wait", "deliver_pending", "done", "cancelled"],
)
def test_activity_is_none_for_non_running_stages_without_reading_progress(
    tmp_path: Path, stage: str
) -> None:
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path, f"msg:{stage}"), 100.0)
    folder = jobs.folder(job)
    (folder / "progress.md").write_text("- should not appear\n", encoding="utf-8")
    stopped = replace(job, stage=stage, started_at=280.0, last_progress_at=820.0)
    assert activity(stopped, folder, now=1000.0) is None


def test_show_cli_includes_derived_activity_without_storing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    jobs = Jobs(root)
    active = jobs.submit(payload(tmp_path, "msg:active"), 100.0)
    finished = jobs.submit(payload(tmp_path, "msg:done"), 100.0)
    (jobs.folder(active) / "progress.md").write_text("- running tests\n", encoding="utf-8")
    (jobs.folder(finished) / "progress.md").write_text("- done work\n", encoding="utf-8")
    jobs.save(
        replace(active, stage="work", started_at=280.0, last_progress_at=820.0),
        820.0,
    )
    jobs.save(
        replace(finished, stage="done", started_at=100.0, ended_at=500.0, last_progress_at=500.0),
        500.0,
    )
    monkeypatch.setattr("taskwatch.job_cli.time.time", lambda: 1000.0)

    assert main(["--root", str(root), "show", active.id]) == 0
    shown_active = json.loads(capsys.readouterr().out)
    assert shown_active["activity"] == "work 12m; last progress 3m ago: - running tests"

    assert main(["--root", str(root), "show", finished.id]) == 0
    shown_finished = json.loads(capsys.readouterr().out)
    assert shown_finished["activity"] is None

    assert main(["--root", str(root), "show"]) == 0
    shown_all = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert shown_all[active.id]["activity"] == "work 12m; last progress 3m ago: - running tests"
    assert shown_all[finished.id]["activity"] is None

    for row in jobs.state.read_records("jobs.jsonl"):
        assert "activity" not in row["job"]

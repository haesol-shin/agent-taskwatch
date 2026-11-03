import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskwatch import job_runner, job_scan
from taskwatch.job_cli import main as cli_main
from taskwatch.job_env import job_environment
from taskwatch.job_runner import run_job
from taskwatch.job_scan import STALL_GRACE_SECONDS, Systemd, scan_jobs
from taskwatch.jobs import (
    MAX_RECOVERIES,
    JobConfig,
    Jobs,
    JobState,
    progress_fingerprint,
)
from tests.spec_fixture import VALID_SPEC

REAL_WAKE = Systemd.wake

BOOT = "boot-1"
STALL = 100


class FakeService:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.stop_ok = True
        self.start_error: Exception | None = None

    def status(self, unit: str) -> str:
        return self.states.get(unit, "missing")

    def start(self, job: JobState) -> None:
        if self.start_error:
            raise self.start_error
        self.started.append(job.unit)
        self.states[job.unit] = "active"

    def stop(self, unit: str) -> bool:
        self.stopped.append(unit)
        if self.stop_ok:
            self.states[unit] = "inactive"
        return self.stop_ok


class Board:
    def __init__(self, root: Path, slots: int = 2) -> None:
        self.root = root
        self.jobs = Jobs(root / "home")
        self.service = FakeService()
        self.config = JobConfig(
            slots=slots,
            stall_seconds=STALL,
            deliver_command=(
                sys.executable,
                "-c",
                'print(\'{"success": true, "message_id": "synthetic-message"}\')',
                "{message}",
            ),
        )
        self.clock = 1000.0
        self.count = 0

    def add(self, target: str = "", **extra: object) -> str:
        payload = self.payload(target, **extra)
        self.clock += 1
        return self.jobs.submit(payload, self.clock).id

    def payload(self, target: str = "", **extra: object) -> dict:
        self.count += 1
        base = self.root / "src"
        base.mkdir(exist_ok=True)
        (base / "intent.md").write_text("intent", encoding="utf-8")
        (base / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
        target_dir = self.root / (target or f"target-{self.count}")
        target_dir.mkdir(exist_ok=True)
        return {
            "receipt_key": f"k{self.count}",
            "approval_source": "msg-1",
            "approval_scope": "task-1",
            "prompt": "do it",
            "review": "code",
            "target": str(target_dir),
            "intent": str(base / "intent.md"),
            "spec": str(base / "spec.md"),
            "outputs": ["out.txt"],
            "check_command": ["true"],
            "checklist": "checklist.md",
            "progress": "progress.md",
            **extra,
        }

    def get(self, job_id: str) -> JobState:
        return self.jobs.load()[job_id]

    def set(self, job_id: str, **changes: object) -> None:
        job = self.get(job_id)
        job = replace(job, **changes)
        self.jobs.save(job, self.clock)

    def scan(self, advance: float = 1.0, boot: str = BOOT) -> list[str]:
        self.clock += advance
        return scan_jobs(self.jobs, self.config, now=self.clock, boot_id=boot, service=self.service)

    def finish_step(self, job_id: str, stage: str, *, alive: bool = False) -> None:
        """What a runner does when a step ends: next stage plus ended_at, in one write.

        By default the service has also exited; alive=True models a still-exiting service.
        """
        if not alive:
            self.kill(job_id)
        self.set(job_id, stage=stage, ended_at=self.clock)

    def kill(self, job_id: str) -> None:
        self.service.states[self.get(job_id).unit] = "inactive"


@pytest.fixture
def board(tmp_path: Path) -> Board:
    return Board(tmp_path)


def test_parallel_jobs_fill_slots_and_the_rest_wait(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=3)
    ids = [board.add() for _ in range(5)]
    assert board.scan() == ids[:3]
    assert [board.get(i).stage for i in ids] == ["work"] * 3 + ["queued"] * 2
    assert board.get(ids[3]).wait_reason == "slot"
    assert board.scan() == []
    board.finish_step(ids[0], "deliver_pending")
    board.finish_step(ids[1], "done")
    assert board.scan() == ids[3:]
    assert board.get(ids[0]).unit == ""
    assert len(set(board.service.started)) == 5


def test_same_target_waits_until_the_first_job_leaves(board: Board) -> None:
    first = board.add(target="shared", review="source")
    second = board.add(target="shared", review="source")
    other = board.add(target="elsewhere")
    assert board.scan() == [first, other]
    assert board.get(second).wait_reason == "target"
    board.finish_step(first, "deliver_pending")
    board.finish_step(other, "user_wait")
    assert board.scan() == [second]


@pytest.mark.parametrize("stage", ["check", "review", "fix"])
def test_slot_and_target_survive_the_gap_between_steps(tmp_path: Path, stage: str) -> None:
    board = Board(tmp_path, slots=1)
    first = board.add(target="shared")
    waiting = board.add()
    board.scan()
    board.finish_step(first, stage)
    assert board.scan() == [first]
    job = board.get(first)
    assert (job.stage, job.attempt, job.ended_at) == (stage, 2, 0.0)
    assert board.get(waiting).stage == "queued"
    assert board.get(waiting).wait_reason == "slot"
    assert board.service.started == [f"taskwatch-{first}-a1.service", job.unit]


def test_next_step_waits_until_the_old_service_is_confirmed_stopped(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=1)
    first = board.add()
    waiting = board.add()
    board.scan()
    old = board.get(first).unit
    board.finish_step(first, "check", alive=True)
    board.service.stop_ok = False
    assert board.scan() == []
    assert board.get(first).attempt == 1
    assert board.get(waiting).stage == "queued"
    board.service.stop_ok = True
    assert board.scan() == [first]
    assert board.service.stopped[-1] == old
    assert board.get(first).attempt == 2


def test_a_dead_review_is_relaunched_on_a_new_reservation_not_a_recovery(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.set(
        job_id, stage="review", reviews_used=1, review_inflight=1, recoveries=1, session_dir="/s"
    )
    board.kill(job_id)
    assert board.scan() == [job_id]
    job = board.get(job_id)
    assert (job.stage, job.reviews_used, job.review_inflight, job.recoveries) == ("review", 2, 2, 1)
    assert job.session_dir == "/s"


def test_dead_step_resumes_once_and_stale_attempt_is_fenced(board: Board) -> None:
    job_id = board.add()
    board.scan()
    old = board.get(job_id)
    board.kill(job_id)
    assert board.scan() == [job_id]
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.attempt, job.recoveries, job.stage) == (2, 1, "work")
    assert job.unit != old.unit
    assert board.service.started.count(job.unit) == 1
    # The runner of the replaced attempt can neither start nor publish; the live one can.
    assert run_job(board.jobs, board.config, job_id, old.attempt) == 2
    assert not job_runner._cas(board.jobs, old, {"work"}, lambda j: {"error": "late"})
    assert job_runner._cas(board.jobs, job, {"work"}, lambda j: {"error": "live"})
    assert board.get(job_id).error == "live"
    board.set(job_id, stage="cancelled")
    assert not job_runner._cas(board.jobs, job, {"work"}, lambda j: {"error": "after cancel"})
    assert board.get(job_id).error == "live"


def test_recoveries_are_capped_then_the_job_waits_for_the_user(board: Board) -> None:
    job_id = board.add()
    board.scan()
    for _ in range(MAX_RECOVERIES):
        board.kill(job_id)
        assert board.scan() == [job_id]
    board.kill(job_id)
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "user_wait",
        "recoveries_exhausted",
        "work",
    )
    assert job.unit == ""


def _free_space(monkeypatch, free_by_path: dict[Path, int]) -> list[Path]:
    """Report the given free bytes for the folder (or its parent) asked about."""
    asked: list[Path] = []

    def usage(path):
        asked.append(Path(path))
        for root, free in free_by_path.items():
            if Path(path) == root or root in Path(path).parents:
                return SimpleNamespace(free=free)
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(job_scan.shutil, "disk_usage", usage)
    return asked


@pytest.mark.parametrize("where", ["folder", "tmpdir"])
def test_a_dead_step_on_a_full_disk_parks_without_using_a_recovery(
    board: Board, monkeypatch, tmp_path: Path, where: str
) -> None:
    job_id = board.add()
    board.scan()
    temp = tmp_path / "tmp"
    board.config = replace(board.config, env={"TMPDIR": str(temp)})
    full = board.jobs.folder(board.get(job_id)) if where == "folder" else temp
    _free_space(monkeypatch, {full: 1234})
    board.kill(job_id)
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.recoveries, job.resume_stage) == (
        "user_wait",
        "disk_full",
        0,
        "work",
    )
    assert str(full) in job.error and "1234 bytes free" in job.error
    assert (job.unit, board.service.started) == ("", [board.service.started[0]])


def test_enough_free_space_still_recovers(board: Board, monkeypatch) -> None:
    job_id = board.add()
    board.scan()
    _free_space(monkeypatch, {})
    board.kill(job_id)
    assert board.scan() == [job_id]
    assert board.get(job_id).recoveries == 1


def test_a_full_disk_is_reported_even_when_recoveries_are_used_up(
    board: Board, monkeypatch
) -> None:
    job_id = board.add()
    board.scan()
    board.set(job_id, recoveries=MAX_RECOVERIES)
    _free_space(monkeypatch, {board.root: 0})
    board.kill(job_id)
    board.scan()
    assert board.get(job_id).wait_reason == "disk_full"


def test_review_launches_stop_at_the_cap_and_resume_opens_a_fresh_cycle(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.set(job_id, stage="review", reviews_used=1, review_inflight=1)
    seen = []
    for _ in range(2):
        board.kill(job_id)
        assert board.scan() == [job_id]
        seen.append(board.get(job_id).reviews_used)
    board.kill(job_id)
    assert board.scan() == []
    job = board.get(job_id)
    assert seen == [2, 3]
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "user_wait",
        "review_exhausted",
        "review",
    )
    assert (job.reviews_used, job.recoveries, job.unit) == (3, 0, "")
    root = str(board.jobs.state.root)
    assert cli_main(["--root", root, "resume", job_id, "--reason", "approve a new cycle"]) == 0
    assert board.get(job_id).reviews_used == 0
    assert board.scan() == [job_id]
    assert board.get(job_id).reviews_used == 1


def test_resume_into_check_repair_keeps_the_recovery_budget(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.set(job_id, stage="fix", recoveries=MAX_RECOVERIES)
    board.kill(job_id)
    board.scan()
    assert board.get(job_id).wait_reason == "recoveries_exhausted"
    root = str(board.jobs.state.root)
    assert cli_main(["--root", root, "resume", job_id, "--reason", "retry"]) == 0
    assert board.get(job_id).reviews_used == 0
    assert board.scan() == [job_id]
    board.kill(job_id)
    assert board.scan() == [job_id]
    assert (board.get(job_id).reviews_used, board.get(job_id).recoveries) == (0, 1)


def test_unreadable_progress_parks_only_that_job_and_the_scan_continues(
    board: Board, tmp_path: Path
) -> None:
    bad = board.add()
    board.scan()
    work = tmp_path / "workspace"
    work.mkdir()
    output = work / "out.txt"
    output.write_text("output")
    board.set(
        bad,
        workspace=str(work),
        progress_hash=progress_fingerprint(board.get(bad), work, board.jobs.folder(board.get(bad))),
    )
    good = board.add()
    output.chmod(0)
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes; the read failure cannot be produced")
    try:
        assert board.scan() == [good]
    finally:
        output.chmod(0o600)
    parked = board.get(bad)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "progress_unreadable")
    assert "out.txt" in parked.error or "Permission" in parked.error
    assert board.get(good).stage == "work"


def test_stall_is_suspected_first_then_stopped_and_recovered(board: Board) -> None:
    job_id = board.add()
    board.scan()
    assert board.scan(STALL + 1) == []
    job = board.get(job_id)
    assert job.stall_suspected is True
    assert board.service.stopped == []
    assert board.scan(STALL_GRACE_SECONDS) == [job_id]
    assert board.service.stopped == [f"taskwatch-{job_id}-a1.service"]
    job = board.get(job_id)
    assert (job.attempt, job.recoveries, job.stall_suspected) == (2, 1, False)


def test_a_scan_seconds_after_the_suspicion_does_not_stop_the_service(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.scan(STALL + 1)
    assert board.scan(10) == []
    assert board.service.stopped == []
    assert board.get(job_id).stall_suspected is True
    assert board.scan(STALL_GRACE_SECONDS - 10 - 1) == []
    assert board.service.stopped == []


def test_launch_records_the_stage_it_started(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.finish_step(job_id, "check")
    board.scan()
    launched = [e for e in board.get(job_id).events if e.get("kind") == "launched"]
    assert [(e["stage"], e["attempt"]) for e in launched] == [("work", 1), ("check", 2)]


def test_progress_clears_a_stall_suspicion(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.scan(STALL + 1)
    assert board.get(job_id).stall_suspected is True
    board.set(job_id, last_progress_at=board.clock)
    assert board.scan() == []
    assert board.get(job_id).stall_suspected is False
    assert board.service.stopped == []


def _workspace(board: Board, job_id: str) -> Path:
    workspace = board.root / "ws"
    workspace.mkdir(exist_ok=True)
    board.set(job_id, workspace=str(workspace))
    return workspace


@pytest.mark.parametrize("name", ["progress.md", "out.txt"])
def test_progress_or_output_change_counts_as_progress(board: Board, name: str) -> None:
    job_id = board.add()
    board.scan()
    workspace = _workspace(board, job_id)
    board.scan()  # records the baseline hash
    board.scan(STALL - 10)
    # The progress record lives in the job folder, the output in the workspace.
    base = board.jobs.folder(board.get(job_id)) if name == "progress.md" else workspace
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text("step 1", encoding="utf-8")
    board.scan(20)  # 110 s since the baseline: stalled unless the change counted
    assert board.get(job_id).stall_suspected is False
    assert board.get(job_id).progress_hash != ""


def test_unchanged_files_do_not_count_as_progress(board: Board) -> None:
    job_id = board.add()
    board.scan()
    _workspace(board, job_id)
    folder = board.jobs.folder(board.get(job_id))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.md").write_text("step 1", encoding="utf-8")
    board.scan()
    board.scan(STALL + 1)
    assert board.get(job_id).stall_suspected is True


def test_log_growth_does_not_count_as_progress(board: Board) -> None:
    job_id = board.add()
    board.scan()
    workspace = _workspace(board, job_id)
    board.scan()
    (workspace / "agent.log").write_text("noise", encoding="utf-8")
    board.scan(STALL + 1)
    assert board.get(job_id).stall_suspected is True


def test_agent_evidence_timestamp_counts_as_progress(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.scan()
    board.set(job_id, evidence_at=board.clock + STALL)
    board.scan(STALL + 1)
    assert board.get(job_id).stall_suspected is False


def test_expected_wait_extends_the_stall_limit(board: Board) -> None:
    job_id = board.add(expected_wait_seconds=500)
    board.scan()
    board.scan()
    board.scan(STALL + 1)
    assert board.get(job_id).stall_suspected is False
    board.scan(500)
    assert board.get(job_id).stall_suspected is True


def test_unconfirmed_stall_stop_parks_but_keeps_slot_until_confirmed(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=1)
    job_id = board.add()
    waiting = board.add()
    board.scan()
    board.scan(STALL + 1)
    board.service.stop_ok = False
    assert board.scan(STALL_GRACE_SECONDS) == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.attempt) == (
        "user_wait",
        "stall_stop_unconfirmed",
        1,
    )
    assert job.unit != ""
    assert board.scan() == []
    assert board.get(waiting).stage == "queued"
    board.service.stop_ok = True
    assert board.scan() == [waiting]
    assert board.get(job_id).unit == ""


def test_unknown_service_state_parks_but_keeps_unit_and_target(board: Board) -> None:
    job_id = board.add(target="shared", review="source")
    other = board.add(target="shared", review="source")
    board.scan()
    unit = board.get(job_id).unit
    board.service.states[unit] = "unknown"
    board.service.stop_ok = False
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.unit) == ("user_wait", "service_unknown", unit)
    assert board.get(other).wait_reason == "target"
    board.service.stop_ok = True
    assert board.scan() == [other]
    assert board.get(job_id).unit == ""


def test_reboot_confirms_the_old_group_dead_before_resuming(board: Board) -> None:
    job_id = board.add()
    board.scan()
    old = board.get(job_id).unit
    board.service.states[old] = "active"
    assert board.scan(boot="boot-2") == [job_id]
    assert board.service.stopped == [old]
    job = board.get(job_id)
    assert (job.boot_id, job.attempt, job.recoveries) == ("boot-2", 2, 1)
    assert board.scan(boot="boot-2") == []


def test_reboot_with_unconfirmed_old_group_does_not_resume(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.service.states[board.get(job_id).unit] = "active"
    board.service.stop_ok = False
    assert board.scan(boot="boot-2") == []
    job = board.get(job_id)
    assert (job.attempt, job.recoveries, job.stage) == (1, 0, "work")


def test_exhausted_recoveries_never_free_a_live_unit(board: Board) -> None:
    job_id = board.add(target="shared", review="source")
    other = board.add(target="shared", review="source")
    board.scan()
    board.set(job_id, recoveries=MAX_RECOVERIES)
    board.service.states[board.get(job_id).unit] = "active"
    board.service.stop_ok = False
    assert board.scan(boot="boot-2") == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.unit != "") == (
        "user_wait",
        "recoveries_exhausted",
        True,
    )
    assert board.get(other).wait_reason == "target"


def test_reboot_does_not_touch_waiting_or_finished_jobs(board: Board) -> None:
    approval = board.add()
    done = board.add()
    board.scan()
    board.set(approval, stage="approval_wait", unit="")
    board.set(done, stage="done", unit="", ended_at=board.clock)
    assert board.scan(boot="boot-2") == []
    assert board.get(approval).stage == "approval_wait"


def test_approval_wait_releases_slot_only_after_the_old_unit_is_dead(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=1)
    job_id = board.add()
    waiting = board.add()
    board.scan()
    unit = board.get(job_id).unit
    board.set(job_id, stage="approval_wait", resume_stage="work")
    board.service.stop_ok = False
    assert board.scan() == []
    assert board.get(job_id).unit == unit
    assert board.get(waiting).stage == "queued"
    board.service.stop_ok = True
    assert board.scan() == [waiting]
    assert board.get(job_id).unit == ""
    assert board.get(job_id).stage == "approval_wait"


def test_approved_job_resumes_stage_after_old_unit_confirmed_dead(board: Board) -> None:
    job_id = board.add()
    board.scan()
    unit = board.get(job_id).unit
    board.set(job_id, stage="queued", approval_required=False, resume_stage="fix")
    board.service.stop_ok = False
    assert board.scan() == []
    assert board.get(job_id).wait_reason == "unit"
    board.service.stop_ok = True
    assert board.scan() == [job_id]
    job = board.get(job_id)
    assert (job.stage, job.attempt) == ("fix", 2)
    assert job.unit != unit


def test_unapproved_job_waits_for_approval_without_starting(board: Board) -> None:
    job_id = board.add(approval_required=True)
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "approval_wait",
        "approval_required",
        "work",
    )
    assert board.service.started == []


def test_cancelled_job_holds_slot_until_stop_is_confirmed(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=1)
    job_id = board.add(target="shared")
    waiting = board.add(target="shared")
    board.scan()
    board.set(job_id, stage="cancelled")
    board.service.stop_ok = False
    assert board.scan() == []
    assert board.get(job_id).unit != ""
    assert board.get(waiting).stage == "queued"
    board.service.stop_ok = True
    assert board.scan() == [waiting]
    assert board.get(job_id).unit == ""
    assert board.get(job_id).stage == "cancelled"
    assert board.scan() == []


def test_job_waits_for_predecessors_to_pass(board: Board) -> None:
    first = board.add(review="source")
    second = board.add(predecessors=[first])
    assert board.scan() == [first]
    assert board.get(second).wait_reason == f"predecessor:{first}"
    board.finish_step(first, "review")
    assert board.scan() == [first]
    assert board.get(second).stage == "queued"
    job = board.get(first)
    workspace = board.jobs.folder(job) / "work"
    workspace.mkdir()
    (workspace / "out.txt").write_text("accepted result", encoding="utf-8")
    board.set(
        first,
        workspace=str(workspace),
        reviews=(
            {
                "ok": True,
                "verdict": "PASS",
                "fingerprint": job_runner._fingerprint(job, workspace, job_environment({})),
                "outputs_fingerprint": job_runner.files_fingerprint(workspace, job.outputs),
                "basis": job_runner._basis(job, board.jobs.folder(job)),
            },
        ),
        guideline_hashes={
            str(p): job_runner._file_sha(p)
            for p in (board.jobs.folder(job) / "review-docs").iterdir()
        },
    )
    board.finish_step(first, "deliver_pending")
    assert board.scan() == [second]


@pytest.mark.parametrize("stage", ["user_wait", "cancelled"])
def test_blocked_predecessor_keeps_followers_waiting(board: Board, stage: str) -> None:
    first = board.add()
    second = board.add(predecessors=[first])
    board.scan()
    board.set(first, stage=stage, unit="")
    assert board.scan() == []
    assert board.get(second).wait_reason == f"predecessor:{first}"


def test_gate_opens_only_after_every_predecessor_is_done(board: Board) -> None:
    first = board.add()
    other = board.add()
    gated = board.add(predecessors=[first, other], approval_required=True)
    board.scan()
    assert (board.get(gated).stage, board.get(gated).wait_reason) == (
        "queued",
        f"predecessor:{first}",
    )
    board.kill(first)
    board.set(first, stage="done", unit="", ended_at=board.clock)
    board.scan()
    assert board.get(gated).stage == "queued"  # `other` still runs
    board.kill(other)
    board.set(other, stage="done", unit="", ended_at=board.clock)
    board.scan()
    job = board.get(gated)
    assert (job.stage, job.wait_reason, job.resume_stage, job.attempt) == (
        "approval_wait",
        "approval_required",
        "work",
        0,
    )


def test_gate_stays_closed_while_a_predecessor_awaits_delivery(board: Board, monkeypatch) -> None:
    first = board.add()
    gated = board.add(predecessors=[first], approval_required=True)
    board.scan()
    board.finish_step(first, "deliver_pending")
    monkeypatch.setattr(job_scan, "deliver_job", lambda jobs, config, job, now: job)
    board.scan()
    assert board.get(first).stage == "deliver_pending"
    assert (board.get(gated).stage, board.get(gated).wait_reason) == (
        "queued",
        f"predecessor:{first}",
    )
    assert board.get(gated).attempt == 0


@pytest.mark.parametrize("stage", ["user_wait", "cancelled"])
def test_gated_job_behind_a_blocked_predecessor_stays_queued(board: Board, stage: str) -> None:
    first = board.add()
    gated = board.add(predecessors=[first], approval_required=True)
    board.scan()
    board.set(first, stage=stage, unit="")
    board.scan()
    assert (board.get(gated).stage, board.get(gated).wait_reason) == (
        "queued",
        f"predecessor:{first}",
    )


def test_gated_job_without_predecessors_asks_at_once(board: Board) -> None:
    job_id = board.add(approval_required=True)
    board.scan()
    assert board.get(job_id).stage == "approval_wait"


def test_approve_appends_the_owner_words_to_the_approval_source(board: Board) -> None:
    job_id = board.add(approval_required=True)
    board.scan()
    assert _run_cli(board, "approve", job_id, "--reason", "PICKS: a=A\nuse uv") == 0
    job = board.get(job_id)
    today = time.strftime("%Y-%m-%d")
    assert job.approval_source == f"msg-1\nGate {today}: PICKS: a=A\nuse uv"
    assert (job.stage, job.approval_required, job.approval_scope) == ("queued", False, "task-1")


def test_corrupt_journal_starts_nothing(board: Board) -> None:
    board.add()
    board.add()
    path = board.jobs.state.dir / "job-changes.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(["{garbage", *lines[1:]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        board.scan()
    assert board.service.started == []


def test_tampered_snapshot_starts_nothing(board: Board) -> None:
    ident = board.add()
    path = board.jobs.folder(board.get(ident)) / "state.json"
    path.write_text(path.read_text(encoding="utf-8").replace("do it", "do bad"), encoding="utf-8")
    with pytest.raises(ValueError):
        board.scan()
    assert board.service.started == []


def test_changed_input_parks_the_job_instead_of_starting(board: Board) -> None:
    bad = board.add()
    good = board.add()
    copied = Path(next(iter(board.get(bad).input_hashes)))
    copied.write_text("tampered", encoding="utf-8")
    assert board.scan() == [good]
    job = board.get(bad)
    assert (job.stage, job.wait_reason, job.unit) == ("user_wait", "inputs_invalid", "")


def test_changed_input_in_a_gap_parks_only_after_the_old_unit_is_confirmed(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.finish_step(job_id, "check", alive=True)
    Path(next(iter(board.get(job_id).input_hashes))).write_text("x", encoding="utf-8")
    board.service.stop_ok = False
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.unit != "") == ("check", True)
    board.service.stop_ok = True
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.unit) == ("user_wait", "inputs_invalid", "")


def test_lost_start_ack_does_not_spawn_twice(board: Board) -> None:
    job_id = board.add()
    board.scan()
    unit = board.get(job_id).unit
    board.service.started.clear()
    assert board.scan() == []
    assert board.service.started == []
    assert board.get(job_id).unit == unit


def test_failed_start_keeps_the_reservation_and_counts_as_a_recovery(board: Board) -> None:
    job_id = board.add()
    board.service.start_error = RuntimeError("no bus")
    assert board.scan() == [job_id]
    job = board.get(job_id)
    assert (job.attempt, job.unit, job.error) == (
        1,
        f"taskwatch-{job_id}-a1.service",
        "start_failed: no bus",
    )
    board.service.start_error = None
    assert board.scan() == [job_id]
    assert board.get(job_id).attempt == 2


def test_scan_events_record_why_a_step_was_relaunched(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.kill(job_id)
    board.scan()
    kinds = [e["kind"] for e in board.get(job_id).events if e.get("source") == "scan"]
    assert kinds == ["launched", "service_dead", "launched"]


# -- production adapter, with systemctl faked at the subprocess boundary ----------------------


def _fake_systemctl(
    monkeypatch: pytest.MonkeyPatch, outputs: list[str], calls: list, code: int = 0
) -> None:
    def run(argv, **kwargs):
        calls.append(argv)
        text = outputs.pop(0) if argv[2] == "show" else ""
        return subprocess.CompletedProcess(argv, code if argv[2] == "show" else 0, text, "")

    monkeypatch.setattr(job_scan.subprocess, "run", run)


@pytest.mark.parametrize(
    ("show", "expected"),
    [
        ("LoadState=loaded\nActiveState=active\nControlGroup=/x\n", "active"),
        ("LoadState=loaded\nActiveState=deactivating\nControlGroup=/x\n", "active"),
        ("LoadState=loaded\nActiveState=inactive\nControlGroup=\n", "inactive"),
        ("LoadState=loaded\nActiveState=failed\nControlGroup=\n", "inactive"),
        ("LoadState=not-found\nActiveState=inactive\nControlGroup=\n", "missing"),
        ("LoadState=loaded\nActiveState=maintenance\nControlGroup=\n", "unknown"),
    ],
)
def test_systemd_status_maps_states(monkeypatch, tmp_path: Path, show: str, expected: str) -> None:
    _fake_systemctl(monkeypatch, [show], [])
    assert Systemd(tmp_path).status("u.service") == expected


def test_systemd_missing_unit_is_missing_even_when_systemctl_exits_nonzero(
    monkeypatch, tmp_path: Path
) -> None:
    _fake_systemctl(monkeypatch, ["LoadState=not-found\nActiveState=inactive\n"], [], code=4)
    assert Systemd(tmp_path).status("u.service") == "missing"


def test_systemd_nonzero_without_a_clear_answer_is_unknown(monkeypatch, tmp_path: Path) -> None:
    _fake_systemctl(monkeypatch, [""], [], code=1)
    assert Systemd(tmp_path).status("u.service") == "unknown"


def test_systemd_inactive_with_live_cgroup_processes_is_not_dead(monkeypatch, tmp_path) -> None:
    cgroup = tmp_path / "cg" / "u"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text("123\n", encoding="utf-8")
    monkeypatch.setattr(job_scan, "CGROUP_ROOT", tmp_path / "cg")
    show = "LoadState=loaded\nActiveState=inactive\nControlGroup=/u\n"
    _fake_systemctl(monkeypatch, [show, show], [])
    assert Systemd(tmp_path).status("u.service") == "active"
    assert Systemd(tmp_path).stop("u.service") is False


def test_systemd_descendant_cgroup_process_is_not_dead(monkeypatch, tmp_path) -> None:
    child = tmp_path / "cg" / "u" / "sub"
    child.mkdir(parents=True)
    (tmp_path / "cg" / "u" / "cgroup.procs").write_text("", encoding="utf-8")
    (child / "cgroup.procs").write_text("456\n", encoding="utf-8")
    monkeypatch.setattr(job_scan, "CGROUP_ROOT", tmp_path / "cg")
    show = "LoadState=loaded\nActiveState=inactive\nControlGroup=/u\n"
    _fake_systemctl(monkeypatch, [show], [])
    assert Systemd(tmp_path).status("u.service") == "active"


def test_systemd_populated_cgroup_events_is_not_dead(monkeypatch, tmp_path) -> None:
    cgroup = tmp_path / "cg" / "u"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="utf-8")
    monkeypatch.setattr(job_scan, "CGROUP_ROOT", tmp_path / "cg")
    show = "LoadState=loaded\nActiveState=inactive\nControlGroup=/u\n"
    _fake_systemctl(monkeypatch, [show], [])
    assert Systemd(tmp_path).status("u.service") == "active"


def test_systemd_stop_confirms_after_stopping(monkeypatch, tmp_path: Path) -> None:
    calls: list = []
    _fake_systemctl(monkeypatch, ["LoadState=loaded\nActiveState=inactive\nControlGroup=\n"], calls)
    assert Systemd(tmp_path).stop("u.service") is True
    assert calls[0][:4] == ["systemctl", "--user", "stop", "u.service"]


def test_systemd_unreachable_is_unknown(monkeypatch, tmp_path: Path) -> None:
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(job_scan.subprocess, "run", run)
    assert Systemd(tmp_path).status("u.service") == "unknown"
    assert Systemd(tmp_path).stop("u.service") is False


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_dead_review_attempts_never_exceed_max_reviews(tmp_path: Path, cap: int) -> None:
    board = Board(tmp_path)
    job_id = board.add(max_reviews=cap)
    board.scan()
    board.set(job_id, stage="review", ended_at=board.clock)
    board.kill(job_id)
    for _ in range(5):
        board.scan()
        board.kill(job_id)
    assert len(board.service.started) == 1 + cap  # the work step, then one launch per review
    job = board.get(job_id)
    assert (job.stage, job.wait_reason, job.reviews_used, job.recoveries) == (
        "user_wait",
        "review_exhausted",
        cap,
        0,
    )


def test_fix_after_a_review_uses_the_recovery_budget(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.set(job_id, stage="fix", reviews_used=1)
    board.kill(job_id)
    assert board.scan() == [job_id]
    job = board.get(job_id)
    assert (job.reviews_used, job.recoveries) == (1, 1)


def test_a_lost_review_start_consumes_a_reservation(board: Board) -> None:
    job_id = board.add()
    board.scan()
    board.set(job_id, stage="review", reviews_used=1, review_inflight=1)
    board.kill(job_id)
    board.service.start_error = RuntimeError("no bus")
    assert board.scan() == [job_id]
    board.service.start_error = None
    assert board.scan() == [job_id]
    assert board.get(job_id).reviews_used == 3


def cool(board: Board, job_id: str, until: float, stage: str = "work", **extra: object) -> None:
    """What the runner leaves after a terminal 429 with a known deadline: the saved stage, the
    deadline, and an ended step whose service is dead."""
    board.set(job_id, stage=stage, ended_at=board.clock, retry_not_before=until, **extra)
    board.kill(job_id)


def test_a_rate_limited_job_frees_its_slot_and_retries_once_when_due(tmp_path: Path) -> None:
    board = Board(tmp_path, slots=1)
    first, other = board.add(), board.add()
    board.scan()
    deadline = board.clock + 100
    cool(board, first, deadline)
    assert board.scan() == [other]  # the slot went to another job
    job = board.get(first)
    assert (job.stage, job.unit, job.attempt, job.recoveries) == ("work", "", 1, 0)
    assert job.retry_not_before == deadline
    board.finish_step(other, "done")
    assert board.scan(advance=10) == []  # not due: nothing launches, however often it scans
    assert board.scan() == []
    assert board.scan(advance=100) == [first]
    job = board.get(first)
    assert (job.stage, job.attempt, job.retry_not_before, job.recoveries) == ("work", 2, 0.0, 0)
    assert board.scan() == []


def test_a_cooling_job_keeps_its_target_from_another_writer(board: Board) -> None:
    first = board.add(target="shared", review="source")
    board.scan()
    second = board.add(target="shared", review="source")
    cool(board, first, board.clock + 100)
    assert board.scan() == []
    assert board.get(second).wait_reason == "target"
    assert board.scan(advance=200) == [first]
    assert board.get(second).stage == "queued"


def test_the_deadline_survives_a_reboot_and_gates_the_retry(board: Board) -> None:
    job_id = board.add()
    board.scan()
    cool(board, job_id, board.clock + 100)
    assert board.scan(boot="boot-2") == []
    assert board.get(job_id).retry_not_before > 0
    assert board.scan(advance=200, boot="boot-2") == [job_id]
    assert board.get(job_id).boot_id == "boot-2"


def test_cancel_removes_the_retry_for_good(board: Board) -> None:
    job_id = board.add()
    board.scan()
    cool(board, job_id, board.clock + 100)
    board.scan()
    root = str(board.jobs.state.root)
    assert cli_main(["--root", root, "cancel", job_id, "--reason", "no"]) == 0
    assert (board.get(job_id).stage, board.get(job_id).retry_not_before) == ("cancelled", 0.0)
    assert board.scan(advance=500) == []


def test_approval_and_resume_never_skip_a_known_deadline(board: Board) -> None:
    job_id = board.add()
    board.scan()
    deadline = board.clock + 100
    cool(board, job_id, deadline, stage="review", reviews_used=1, review_inflight=0)
    board.scan()
    root = str(board.jobs.state.root)
    assert cli_main(["--root", root, "wait", job_id, "--reason", "need a gate"]) == 0
    assert cli_main(["--root", root, "approve", job_id, "--reason", "ok"]) == 0
    assert board.scan() == []
    job = board.get(job_id)
    assert (job.stage, job.resume_stage, job.retry_not_before) == ("queued", "review", deadline)
    board.set(job_id, stage="user_wait", wait_reason="review_exhausted")
    assert cli_main(["--root", root, "resume", job_id, "--reason", "again"]) == 0
    assert board.get(job_id).retry_not_before == deadline
    assert board.scan() == []
    assert board.scan(advance=200) == [job_id]
    assert board.get(job_id).reviews_used == 1  # the resume opened a cycle; this is its first


def test_a_review_launch_after_a_rate_limit_reserves_its_own_attempt(board: Board) -> None:
    job_id = board.add(max_reviews=1)
    board.scan()
    cool(board, job_id, board.clock + 10, stage="review", reviews_used=0)
    assert board.scan(advance=20) == [job_id]
    job = board.get(job_id)
    assert (job.reviews_used, job.review_inflight, job.retry_not_before) == (1, 2, 0.0)


def test_wake_runs_one_scan_two_seconds_later_within_a_second(monkeypatch, tmp_path: Path) -> None:
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv))
    REAL_WAKE(Systemd(tmp_path))
    assert seen == [
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--on-active=2s",
            "--timer-property=AccuracySec=1s",
            sys.executable,
            "-m",
            "taskwatch.job_cli",
            "--root",
            str(tmp_path),
            "scan",
        ]
    ]


def _run_cli(board: Board, *argv: str) -> int:
    return cli_main(["--root", str(board.jobs.state.root), *argv])


def test_commands_that_start_work_wake_once_and_the_others_never(
    board: Board, monkeypatch, no_real_wake
) -> None:
    submit_file = board.root / "job.json"
    submit_file.write_text(json.dumps(board.payload(approval_required=True)), encoding="utf-8")
    assert _run_cli(board, "submit", str(submit_file)) == 0
    job_id = next(iter(board.jobs.load()))
    assert len(no_real_wake) == 1
    board.scan()
    assert _run_cli(board, "init") == 0
    monkeypatch.setattr(job_scan, "scan_jobs", lambda *a, **k: [])
    assert _run_cli(board, "scan") == 0
    assert len(no_real_wake) == 1
    assert _run_cli(board, "approve", job_id, "--reason", "ok") == 0
    assert len(no_real_wake) == 2
    board.scan()
    board.kill(job_id)
    board.set(job_id, stage="user_wait", resume_stage="work")
    assert _run_cli(board, "resume", job_id, "--reason", "retry") == 0
    assert len(no_real_wake) == 3
    monkeypatch.setattr(job_runner, "run_job", lambda *a: 0)
    assert _run_cli(board, "run", job_id, "--attempt", "1") == 0
    assert len(no_real_wake) == 4
    assert _run_cli(board, "wait", job_id, "--reason", "need input") == 0
    assert _run_cli(board, "cancel", job_id, "--reason", "stop") == 0
    assert len(no_real_wake) == 4


def test_a_failed_step_does_not_wake_so_recovery_keeps_its_backoff(
    board: Board, monkeypatch, no_real_wake
) -> None:
    job_id = board.add()
    monkeypatch.setattr(job_runner, "run_job", lambda *a: 1)
    assert _run_cli(board, "run", job_id, "--attempt", "1") == 1
    assert no_real_wake == []


def test_a_failing_wake_keeps_the_exit_code_and_warns(board: Board, monkeypatch, capsys) -> None:
    def fail(self) -> None:
        raise subprocess.CalledProcessError(1, "systemd-run")

    monkeypatch.setattr(Systemd, "wake", fail)
    job_id = board.add()
    board.set(job_id, stage="user_wait", resume_stage="work")
    assert _run_cli(board, "resume", job_id, "--reason", "retry") == 0
    assert "warning: wake failed" in capsys.readouterr().err

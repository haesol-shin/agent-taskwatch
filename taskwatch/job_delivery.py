"""Send a checked result once; uncertain external outcomes need a user decision."""

import fcntl
import hashlib
import json
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

from . import brief
from .evidence import basis_of
from .fingerprint import files_fingerprint
from .job_env import job_environment
from .job_runner import _fingerprint, _kill_group, _requeue, _write_durable
from .job_text import texts_changed
from .jobs import JobConfig, Jobs, JobState

DELIVERY_TIMEOUT = 60


def _accepted(job: JobState, review: dict) -> bool:
    """A review-only job delivers its verdict whatever it is; other jobs deliver only a PASS."""
    if job.review_target is not None:
        return bool(review.get("final"))
    return bool(review.get("ok") and review.get("verdict") == "PASS")


def deliver_job(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> JobState:
    """Called under the state lock, after the old runner service has stopped."""
    if job.deliveries and job.deliveries[-1]["status"] == "started":
        record = {**job.deliveries[-1], "status": "unknown", "error": "delivery interrupted"}
        return _finish(jobs, job, record, now)
    review = job.reviews[-1] if job.reviews else {}
    try:
        current = (
            _accepted(job, review)
            and _fingerprint(job, Path(job.workspace), job_environment(config.env))
            == review.get("fingerprint")
            and files_fingerprint(Path(job.workspace), job.outputs)
            == review.get("outputs_fingerprint")
            and not texts_changed(
                job,
                Path(job.workspace),
                jobs.folder(job),
                job_environment(config.env),
                review.get("texts", {}),
            )
            and bool(job.guideline_hashes)
            and basis_of(
                Path(job.intent), Path(job.spec), tuple(Path(p) for p in job.guideline_hashes)
            )
            == review.get("basis")
        )
    except (ValueError, OSError):
        current = False
    if not current:
        job = replace(job, **_requeue(job))
        jobs.save(job, now)
        return job
    # Inherit this lock into the sender so even a killed scanner cannot overlap an orphan send.
    with (jobs.folder(job) / "delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return job
        try:
            message = _message(jobs, config, job)
        except OSError as exc:
            stopped = replace(
                job,
                stage="user_wait",
                resume_stage="deliver_pending",
                wait_reason="delivery_unreadable",
                error=str(exc),
            )
            jobs.save(stopped, now)
            return stopped
        command = [str(message) if part == "{message}" else part for part in config.deliver_command]
        record = {
            "at": now,
            "command": command,
            "message_file": str(message),
            "message_sha256": hashlib.sha256(message.read_bytes()).hexdigest(),
            "status": "started",
            "exit_code": None,
            "message_id": None,
            "stdout": "",
            "stderr": "",
            "error": "",
        }
        job = replace(job, deliveries=(*job.deliveries, record))
        jobs.save(job, now)
        return _finish(jobs, job, {**record, **_send(command, lock.fileno())}, now)


def _message(jobs: Jobs, config: JobConfig, job: JobState) -> Path:
    """The card the owner reads and nothing else; the paths are at the end of `taskwatch brief`."""
    folder = jobs.folder(job)
    review = job.reviews[-1] if job.reviews else {}
    _write_durable(folder / "delivery-review.txt", review.get("body", ""))
    message = folder / "delivery.txt"
    _write_durable(message, "\n".join(brief.card(jobs, config, job)) + "\n")
    return message


def _send(command: list[str], lock_fd: int) -> dict:
    result = {
        "status": "unknown",
        "exit_code": None,
        "message_id": None,
        "stdout": "",
        "stderr": "",
        "error": "",
    }
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(lock_fd,),
        ) as proc:
            try:
                out, err = proc.communicate(timeout=DELIVERY_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid)
                out, err = proc.communicate()
                result["error"] = "delivery timed out"
            finally:
                _kill_group(proc.pid)
            result.update(
                exit_code=proc.returncode,
                stdout=out.decode("utf-8", errors="backslashreplace"),
                stderr=err.decode("utf-8", errors="backslashreplace"),
            )
        if result["error"]:
            return result
        payload = json.loads(out.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("delivery response is not an object")
        result["message_id"] = payload.get("message_id")
        if (
            proc.returncode == 0
            and payload.get("success")
            and not payload.get("error")
            and (payload.get("message_id") is not None)
        ):
            result["status"] = "success"
        else:
            result["error"] = str(payload.get("error") or "delivery response does not confirm send")
    except (OSError, ValueError, UnicodeError) as exc:
        result["error"] = str(exc)
    return result


def _finish(jobs: Jobs, job: JobState, record: dict, now: float) -> JobState:
    success = record["status"] == "success"
    job = replace(
        job,
        deliveries=(*job.deliveries[:-1], record),
        stage="done" if success else "user_wait",
        resume_stage="deliver_pending",
        wait_reason="" if success else "delivery_unknown",
        error=record["error"],
        ended_at=now,
    )
    jobs.save(job, now)
    return job


def notify_wait(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> None:
    """Reserve under the state lock, send outside it, and merge only the notice."""
    # A concurrent scan must not mistake a live send for an interrupted attempt.
    with (jobs.folder(job) / "notice.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with jobs.state.lock():
            job = jobs.load()[job.id]
            if job.stage not in {"user_wait", "approval_wait"}:
                return
            index = len(job.notices)
            record = _reserve_notice(jobs, config, job, now)
        if record is None:
            return
        if record["status"] == "started":
            record.update(_send(record["command"], lock.fileno()))
        with jobs.state.lock():
            latest = jobs.load()[job.id]
            notices = list(latest.notices)
            notices[index] = record
            jobs.save(replace(latest, notices=tuple(notices)), now)


def _reserve_notice(jobs: Jobs, config: JobConfig, job: JobState, now: float) -> dict | None:
    # Only resume/approve can leave a wait for another runnable cycle.
    entry = sum(event.get("type") in {"resume", "approve"} for event in job.events)
    if job.notices and job.notices[-1]["entry"] == entry:
        if job.notices[-1]["status"] == "started":
            record = {**job.notices[-1], "status": "unknown", "error": "notice interrupted"}
            jobs.save(replace(job, notices=(*job.notices[:-1], record)), now)
        return None
    message = jobs.folder(job) / f"notice-{len(job.notices)}.txt"
    command = [str(message) if part == "{message}" else part for part in config.deliver_command]
    record = {
        "at": now,
        "entry": entry,
        "stage": job.stage,
        "wait_reason": job.wait_reason,
        "command": command,
        "message_file": str(message),
        "status": "started",
    }
    job = replace(job, notices=(*job.notices, record))
    jobs.save(job, now)
    try:
        _write_durable(
            message,
            "\n".join(
                [
                    f"Job {job.id}: {job.prompt.strip().splitlines()[0]}",
                    f"Stage: {job.stage}; wait_reason: {job.wait_reason}",
                    f"Error: {job.error}",
                    *(
                        shlex.join(
                            [
                                "taskwatch",
                                "--root",
                                str(jobs.state.root),
                                command,
                                job.id,
                                "--reason",
                                "<reason>",
                            ]
                        )
                        for command in ("resume", "approve", "cancel")
                    ),
                ]
            )
            + "\n",
        )
        record["message_sha256"] = hashlib.sha256(message.read_bytes()).hexdigest()
    except OSError as exc:
        record.update(status="unknown", error=str(exc))
    jobs.save(replace(job, notices=(*job.notices[:-1], record)), now)
    return record

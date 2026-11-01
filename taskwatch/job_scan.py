"""Timer scan for the independent job board: admit, launch, recover, stop.

Scheduling holds the State lock so a runner cannot change the snapshot mid-decision.
Stop notice senders run after releasing it. A step is one systemd service. The runner
marks a finished step by setting ``ended_at`` together with the next stage; the scan
then launches that next step under a new attempt while the job keeps its slot and target.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from .job_delivery import deliver_job, notify_wait
from .job_env import job_environment
from .job_workspace import settle
from .jobs import MAX_FIX_ROUNDS, MAX_RECOVERIES, JobState, progress_fingerprint

SLOT_STAGES = frozenset({"work", "check", "review", "fix"})
DONE_STAGES = frozenset({"deliver_pending", "done"})
DEAD = frozenset({"inactive", "missing"})
LIVE_STATES = frozenset({"active", "activating", "reloading", "deactivating"})
_SPAWN_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)
_INPUT_ERRORS = (ValueError, OSError, KeyError)
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
CGROUP_ROOT = Path("/sys/fs/cgroup")
# A stall must stay suspected this long before the service is stopped. Extra scans after a
# step ends can run seconds apart, so "unchanged on the next scan" no longer means minutes.
STALL_GRACE_SECONDS = 300
WAKE_DELAY = "2s"


class Systemd:
    """User systemd. Anything not proven dead or alive is reported as unknown."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def status(self, unit: str) -> str:
        info = self._show(unit)
        if info is None:
            return "unknown"
        if info.get("LoadState") == "not-found":
            return "missing"
        state = info.get("ActiveState", "")
        # A stray process left in the cgroup means the step is not dead.
        if state in LIVE_STATES or not _cgroup_empty(info.get("ControlGroup", "")):
            return "active"
        return "inactive" if state in ("inactive", "failed") else "unknown"

    def start(self, job: JobState) -> None:
        argv = [
            "systemd-run",
            "--user",
            "--quiet",
            f"--unit={job.unit}",
            "--property=KillMode=control-group",
            sys.executable,
            "-m",
            "taskwatch.job_cli",
            "--root",
            str(self.root),
            "run",
            job.id,
            "--attempt",
            str(job.attempt),
        ]
        subprocess.run(argv, check=True, capture_output=True, timeout=30)

    def wake(self) -> None:
        """Run one extra scan shortly, so the next step does not wait for the timer."""
        argv = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--on-active={WAKE_DELAY}",
            sys.executable,
            "-m",
            "taskwatch.job_cli",
            "--root",
            str(self.root),
            "scan",
        ]
        subprocess.run(argv, check=True, capture_output=True, timeout=30)

    def stop(self, unit: str) -> bool:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return self.status(unit) in DEAD

    def _show(self, unit: str) -> dict[str, str] | None:
        argv = ["systemctl", "--user", "show", unit]
        argv += ["-p", "LoadState", "-p", "ActiveState", "-p", "ControlGroup"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        info = dict(line.partition("=")[::2] for line in proc.stdout.splitlines())
        # systemctl may exit nonzero for a unit it reports as not-found; that is a clear answer.
        if proc.returncode != 0 and info.get("LoadState") != "not-found":
            return None
        return info


def _cgroup_empty(cgroup: str) -> bool:
    """True only when neither the group nor any descendant group holds a process."""
    if not cgroup:
        return True
    base = CGROUP_ROOT / cgroup.lstrip("/")
    try:
        if "populated 1" in (base / "cgroup.events").read_text(encoding="utf-8").splitlines():
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        return not any(p.read_text(encoding="utf-8").strip() for p in base.rglob("cgroup.procs"))
    except OSError:
        return False


def scan_jobs(
    jobs,
    config,
    *,
    now: float | None = None,
    boot_id: str | None = None,
    service=None,
) -> list[str]:
    """Run one scan and return the IDs of jobs whose step was launched.

    A damaged journal raises before any job is touched, so nothing starts on bad data.
    """
    scan = _Scan(
        jobs,
        config,
        time.time() if now is None else now,
        boot_id if boot_id is not None else BOOT_ID_PATH.read_text(encoding="utf-8").strip(),
        service if service is not None else Systemd(jobs.state.root),
    )
    with jobs.state.lock():
        launched = scan.run()
        waiting = [
            job for job in jobs.load().values() if job.stage in {"user_wait", "approval_wait"}
        ]
    for job in waiting:
        notify_wait(jobs, config, job, scan.now)
    return launched


class _Scan:
    def __init__(self, jobs, config, now: float, boot: str, service) -> None:
        self.jobs = jobs
        self.config = config
        self.now = now
        self.boot = boot
        self.service = service
        self.launched: list[str] = []
        self.fetched: set[str] = set()
        self.all: dict[str, JobState] = {}
        self.order: list[str] = []

    def _save(self, job: JobState) -> None:
        self.jobs.save(job, self.now)
        self.all[job.id] = job

    def run(self) -> list[str]:
        self.all = self.jobs.load()
        self.order = [job.id for job in sorted(self.all.values(), key=_rank)]
        for identifier in self.order:
            self._tend(self.all[identifier])
        for identifier in self.order:
            job = self.all[identifier]
            if _is_gap(job) and self._confirm_dead(job):
                self._launch(job, job.stage)
        for identifier in self.order:
            job = self.all[identifier]
            if _is_pending(job):
                self._admit(job)
        return self.launched

    # -- existing jobs ----------------------------------------------------------------------

    def _tend(self, job: JobState) -> None:
        if job.stage in SLOT_STAGES:
            if job.unit and job.ended_at <= 0:
                self._watch(job)
        elif job.unit:
            self._release(job)
        job = self.all[job.id]
        if job.stage == "queued" and job.approval_required:
            first = "review" if job.review_target is not None else "work"
            job = replace(
                job, stage="approval_wait", resume_stage=first, wait_reason="approval_required"
            )
            self._save(self._event(job, "approval_wait"))
        if job.stage == "deliver_pending" and not job.unit:
            self.all[job.id] = deliver_job(self.jobs, self.config, job, self.now)
        self._settle(self.all[job.id])

    def _settle(self, job: JobState) -> None:
        """Remove a finished job's clean worktree and its integrated branch; keep the rest.

        Only `done` and `cancelled` qualify: at `deliver_pending` the result is still unsent.
        A job whose unit is not yet confirmed dead may still use the folder.
        """
        if job.stage not in {"done", "cancelled"} or job.unit:
            return
        try:
            changes = settle(job, job_environment(self.config.env), self.fetched)
        except (OSError, subprocess.SubprocessError):
            return  # Retried by the next scan; one repository must not stop the others.
        if changes:
            self._save(replace(job, **changes))

    def _confirm_dead(self, job: JobState) -> bool:
        """True when the job's whole old service is dead; never assumed, even after a reboot."""
        if self.service.status(job.unit) in DEAD or self.service.stop(job.unit):
            return True
        if job.error != "stop_unconfirmed":
            self._save(replace(job, error="stop_unconfirmed"))
        return False

    def _release(self, job: JobState) -> None:
        """Free a unit that no stage owns (cancel, wait, done, re-queue) once it is dead.

        The slot and target stay held until the whole service is confirmed stopped.
        """
        if self._confirm_dead(job):
            self._save(self._event(replace(job, unit=""), "unit_released"))

    def _watch(self, job: JobState) -> None:
        if job.boot_id and job.boot_id != self.boot:
            self._recover(self._event(job, "reboot_interrupted"))
            return
        status = self.service.status(job.unit)
        if status in DEAD:
            self._recover(self._event(job, "service_dead"))
        elif status == "active":
            self._check_stall(job)
        else:
            self._park(job, "service_unknown")

    def _check_stall(self, job: JobState) -> None:
        job = self._refresh_progress(job)
        if job is None:
            return
        seen = max(job.last_progress_at, job.evidence_at)
        stalled = self.now - seen >= self.config.stall_seconds + job.expected_wait_seconds
        if not stalled:
            if job.stall_suspected:
                self._save(replace(job, stall_suspected=False))
        elif not job.stall_suspected:
            self._save(self._event(replace(job, stall_suspected=True), "stall_suspected"))
        elif self.now - _suspected_at(job) < STALL_GRACE_SECONDS:
            return
        elif self.service.stop(job.unit):
            self._recover(self._event(job, "stall_stopped"))
        else:
            self._park(job, "stall_stop_unconfirmed")

    def _refresh_progress(self, job: JobState) -> JobState | None:
        """Return None after parking a job whose progress files cannot be read."""
        if not job.workspace:
            return job
        try:
            digest = progress_fingerprint(job, Path(job.workspace), self.jobs.folder(job))
        except OSError as exc:
            # One unreadable file must not stop the scan of unrelated jobs.
            self._park(job, "progress_unreadable", f"progress_unreadable: {exc}")
            return None
        if digest == job.progress_hash:
            return job
        # The first hash is only a baseline: absent files are not progress.
        job = replace(
            job,
            progress_hash=digest,
            last_progress_at=self.now if job.progress_hash else job.last_progress_at,
        )
        self._save(job)
        return job

    def _recover(self, job: JobState) -> None:
        if job.stage == "fix" and job.fix_rounds > 0:
            self._retry_review_fix(job)
        elif job.recoveries >= MAX_RECOVERIES:
            self._park(job, "recoveries_exhausted")
        elif self._confirm_dead(job):
            self._launch(replace(job, recoveries=job.recoveries + 1), job.stage)

    def _retry_review_fix(self, job: JobState) -> None:
        """A failed fix after a review counts as a round: the cap bounds model invocations."""
        if job.fix_rounds >= MAX_FIX_ROUNDS:
            self._park(job, "review_exhausted")
        elif self._confirm_dead(job):
            self._launch(replace(job, fix_rounds=job.fix_rounds + 1), "fix")

    # -- launching --------------------------------------------------------------------------

    def _admit(self, job: JobState) -> None:
        reason = self._blocker(job)
        if reason is None:
            self._launch(job, _launch_stage(job))
        elif job.stage == "queued" and job.wait_reason != reason:
            self._save(replace(job, wait_reason=reason))

    def _blocker(self, job: JobState) -> str | None:
        if job.unit:
            return "unit"
        for pred in job.predecessors:
            if pred not in self.all or self.all[pred].stage not in DONE_STAGES:
                return f"predecessor:{pred}"
        slots, targets = self._busy()
        if _shared_target(job) and job.target in targets:
            return "target"
        return "slot" if slots >= self.config.slots else None

    def _busy(self) -> tuple[int, set[str]]:
        held = [job for job in self.all.values() if _holds(job)]
        return len(held), {job.target for job in held if job.target and _shared_target(job)}

    def _launch(self, job: JobState, stage: str) -> None:
        try:
            self.jobs.verify_inputs(job)
        except _INPUT_ERRORS as exc:
            self._park(job, "inputs_invalid", str(exc))
            return
        job = replace(
            job,
            attempt=job.attempt + 1,
            stage=stage,
            unit=f"taskwatch-{job.id}-a{job.attempt + 1}.service",
            boot_id=self.boot,
            started_at=self.now,
            ended_at=0.0,
            last_progress_at=self.now,
            stall_suspected=False,
            wait_reason="",
            error="",
        )
        # Reserve before the service call: a lost ack is found later by unit name.
        job = self._event(job, "launched", stage=stage)
        self._save(job)
        self.launched.append(job.id)
        try:
            self.service.start(job)
        except _SPAWN_ERRORS as exc:
            self._save(self._event(replace(job, error=f"start_failed: {exc}"), "start_failed"))

    def _park(self, job: JobState, reason: str, error: str = "") -> None:
        """Wait for the user. The unit is kept until it is confirmed dead, so slot and target
        are never freed while a process may still run."""
        job = replace(
            job,
            resume_stage=job.stage if job.stage in SLOT_STAGES else job.resume_stage,
            stage="user_wait",
            wait_reason=reason,
            error=error or reason,
            ended_at=self.now,
        )
        job = self._event(job, "parked", reason=reason)
        self._save(job)
        if job.unit:
            self._release(job)

    def _event(self, job: JobState, kind: str, **extra: object) -> JobState:
        event = {"source": "scan", "kind": kind, "at": self.now, "attempt": job.attempt}
        return replace(job, events=(*job.events, {**event, **extra}))


def _rank(job: JobState) -> tuple[float, str]:
    return job.last_progress_at, job.id


def _suspected_at(job: JobState) -> float:
    for event in reversed(job.events):
        if event.get("kind") == "stall_suspected":
            return float(event.get("at", 0.0))
    return 0.0


def _shared_target(job: JobState) -> bool:
    """A code job that builds a result works in its own worktree; every other job reads or
    writes the target folder itself, so two of them must not run on one target."""
    return not (job.review == "code" and job.review_target is None)


def _holds(job: JobState) -> bool:
    # Any recorded unit may still be running, whatever the stage says, so it holds.
    return bool(job.unit)


def _launch_stage(job: JobState) -> str:
    if job.stage in SLOT_STAGES:
        return job.stage
    resume = job.resume_stage
    return resume if resume in SLOT_STAGES else "work"


def _is_gap(job: JobState) -> bool:
    return job.stage in SLOT_STAGES and bool(job.unit) and job.ended_at > 0


def _is_pending(job: JobState) -> bool:
    return job.stage == "queued" or (job.stage in SLOT_STAGES and not job.unit)

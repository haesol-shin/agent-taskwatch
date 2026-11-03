"""Standalone job commands. This module does not load the Hermes adapter."""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .activity import activity
from .job_findings import stop_problems
from .jobs import JobConfig, Jobs, JobState, load_job_config
from .state import RecordError, _create_new, _make_dirs

# Commands after which a scan runs at once: a step ended, or a job became startable.
WAKE_COMMANDS = frozenset({"run", "submit", "approve", "resume"})


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="taskwatch")
    cli.add_argument("--root", type=Path, default=None)
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Write config and timer units without activating them")
    init.add_argument("--unit-dir", type=Path)
    submit = commands.add_parser("submit", help="Durably store an approved job JSON document")
    submit.add_argument("file", type=Path)
    commands.add_parser("scan", help="Schedule job steps once")
    run = commands.add_parser("run", help="Run one reserved job step")
    run.add_argument("job")
    run.add_argument("--attempt", type=int, required=True)
    show = commands.add_parser("show")
    show.add_argument("job", nargs="?")
    needs = commands.add_parser("needs", help="List what needs the owner; read only, no lock")
    needs.add_argument(
        "--from", dest="cursor", type=_seconds(0), help="Cursor from an earlier list"
    )
    needs.add_argument("--wait", action="store_true", help="Block until something new needs you")
    needs.add_argument(
        "--include-done", action="store_true", help="With --wait, wake on every new completion"
    )
    needs.add_argument("--timeout", type=_seconds(1), default=82800, help="Seconds (default 82800)")
    needs.add_argument("--interval", type=_seconds(5), default=30, help="Poll seconds (min 5)")
    brief = commands.add_parser("brief", help="Print the decision brief of a job, composed on read")
    brief.add_argument("job")
    check = commands.add_parser(
        "check", help="Print what the runner's shape checks reject now; read only for the result"
    )
    check.add_argument("job")
    check.add_argument(
        "--run-command", action="store_true", help="Also run the job's check command"
    )
    text = commands.add_parser("text", help="Write the reviewed publication texts")
    text.add_argument("job")
    text.add_argument("--pr", help="Unit id; optional when the job has one unit")
    text.add_argument(
        "--out", type=Path, required=True, help="Folder for title.txt, body.md and commit.txt"
    )
    for name in ("cancel", "wait", "approve", "resume"):
        action = commands.add_parser(name)
        action.add_argument("job")
        action.add_argument("--reason", required=True)
        if name == "approve":
            action.add_argument("--scope", help="Replace the approved scope with this exact text")
    return cli


def _seconds(minimum: int):
    def parse(text: str) -> int:
        value = int(text)
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value

    return parse


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = (
        args.root or Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    ).resolve()
    if args.command == "text":
        return _text(args, root)
    if args.command == "brief":
        return _brief(args, root)
    if args.command == "needs":
        return _needs(args, root)
    if args.command == "check":
        return _check(args, root)
    try:
        result = _dispatch(args, root)
    except (ValueError, OSError, RecordError, KeyError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    if args.command in WAKE_COMMANDS:
        _wake(root)
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


def _wake(root: Path) -> None:
    """A failed wake only costs the wait for the timer, so it never changes the exit code."""
    from .job_scan import Systemd

    try:
        Systemd(root).wake()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"taskwatch: warning: wake failed: {exc}", file=sys.stderr)


def _needs(args: argparse.Namespace, root: Path) -> int:
    """0 listed, 3 time limit, 2 usage error, 1 other error. Nothing is written."""
    from .job_watch import needs

    if args.wait and args.cursor is None:
        print("taskwatch: needs --wait needs --from <cursor>", file=sys.stderr)
        return 2
    if args.include_done and not args.wait:
        print("taskwatch: needs --include-done needs --wait", file=sys.stderr)
        return 2
    try:
        code, report = needs(
            root,
            cursor=args.cursor,
            wait=args.wait,
            include_done=args.include_done,
            timeout=args.timeout,
            interval=args.interval,
        )
    except (ValueError, OSError, RecordError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    print(report)
    return code


def _check(args: argparse.Namespace, root: Path) -> int:
    """0 no problem, 1 problems printed, 2 unknown job or nothing to check."""
    from .job_runner import shape_problems

    try:
        jobs = Jobs(root)
        job = jobs.load().get(args.job)
        if job is None:
            print(f"taskwatch: unknown job {args.job}", file=sys.stderr)
            return 2
        problems = shape_problems(jobs, load_job_config(root), job, args.run_command)
    except (ValueError, OSError, RecordError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("no problems")
    return 0


def _brief(args: argparse.Namespace, root: Path) -> int:
    """0 printed the brief, 2 unknown job. Read only; the brief is composed on every call."""
    from .brief import cap, compose, reply_template, trailer

    try:
        jobs = Jobs(root)
        job = jobs.load().get(args.job)
        if job is None:
            print(f"taskwatch: unknown job {args.job}", file=sys.stderr)
            return 2
        lines = compose(jobs, load_job_config(root), job)
    except (ValueError, OSError, RecordError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    tail = (*trailer(jobs, job), f"Worker brief: {jobs.folder(job) / 'brief.md'}")
    if template := reply_template(job):
        tail += (template,)
    print("\n".join(cap(lines, tail)))
    return 0


def _text(args: argparse.Namespace, root: Path) -> int:
    """0 wrote the copy, 2 the text or snapshot changed since review, 3 no review passed."""
    from .job_env import job_environment
    from .job_text import reviewed_copy

    try:
        jobs = Jobs(root)
        job = jobs.load()[args.job]
        env = job_environment(load_job_config(root).env)
        code, lines = reviewed_copy(job, env, jobs.folder(job), args.pr, args.out)
    except (ValueError, OSError, RecordError, KeyError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines), file=sys.stderr if code else sys.stdout)
    return code


def _dispatch(args: argparse.Namespace, root: Path):
    jobs = Jobs(root)
    if args.command == "init":
        return _init(root, args.unit_dir)
    if args.command == "submit":
        return jobs.submit(json.loads(args.file.read_text(encoding="utf-8"))).to_json()
    if args.command == "show":
        now = time.time()
        records = jobs.load()
        if args.job:
            job = records[args.job]
            return {**job.to_json(), "activity": activity(job, jobs.folder(job), now)}
        return [
            {**job.to_json(), "activity": activity(job, jobs.folder(job), now)}
            for job in records.values()
        ]
    if args.command == "scan":
        from .job_scan import scan_jobs

        return scan_jobs(jobs, load_job_config(root))
    if args.command == "run":
        from .job_runner import run_job

        code = run_job(jobs, load_job_config(root), args.job, args.attempt)
        if code:
            raise ValueError(f"job step failed with exit code {code}")
        return None
    return _action(jobs, args)


def _action(jobs: Jobs, args: argparse.Namespace) -> dict:
    now = time.time()
    with jobs.state.lock():
        job = jobs.load()[args.job]
        command = args.command
        if command == "cancel":
            job = replace(job, stage="cancelled", wait_reason=args.reason, retry_not_before=0.0)
        elif command == "wait":
            if job.stage not in {"work", "check", "review", "fix", "land", "queued"}:
                raise ValueError("only an unfinished job can wait for approval")
            resume = job.resume_stage if job.stage == "queued" else job.stage
            job = replace(job, resume_stage=resume, stage="approval_wait", wait_reason=args.reason)
        elif command == "approve":
            if job.stage != "approval_wait":
                raise ValueError("job is not waiting for approval")
            job = replace(
                job,
                approval_required=False,
                approval_source=f"{job.approval_source}\nGate {_date(now)}: {args.reason}",
                approval_scope=args.scope if args.scope is not None else job.approval_scope,
                stage="queued",
                wait_reason="",
            )
        elif command == "resume":
            if job.stage != "user_wait":
                raise ValueError("job needs user judgment")
            _check_root_cause(jobs, job)
            # A resume opens a new review cycle at the saved stage and keeps every record. It
            # never skips a rate-limit deadline: the scanner still waits for `retry_not_before`.
            landing = job.resume_stage == "land"
            job = replace(
                job,
                stage="deliver_pending" if job.resume_stage == "deliver_pending" else "queued",
                wait_reason="",
                error="",
                recoveries=0,
                # Landing neither spends nor renews review budget; its stops are not review stops.
                reviews_used=job.reviews_used if landing else 0,
                review_cycle_start=job.review_cycle_start if landing else len(job.reviews),
                review_inflight=job.review_inflight if landing else 0,
            )
        entry = sum(e.get("type") in {"resume", "approve"} for e in job.events) + 1
        event = {"type": command, "at": now, "reason": args.reason}
        if command in {"resume", "approve"}:
            # A monotonic owner entry is distinct even when two commands share a timestamp.
            event["entry"] = entry
        job = replace(job, events=(*job.events, event))
        jobs.save(job, now)
        return job.to_json()


def _check_root_cause(jobs: Jobs, job: JobState) -> None:
    """After a root-cause stop, a resume needs a note changed since the stop and complete for
    every rule that triggered it."""
    if job.wait_reason != "root_cause_required":
        return
    stops = [
        event
        for event in job.events
        if event.get("type") == "user_wait" and event.get("reason") == "root_cause_required"
    ]
    problems = stop_problems(jobs.folder(job), stops[-1]) if stops else []
    if problems:
        raise ValueError("resume needs the root-cause analysis: " + "; ".join(problems))


def _date(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _init(root: Path, unit_dir: Path | None) -> dict:
    directory = root / "taskwatch"
    _make_dirs(directory)
    config = directory / "jobs-config.json"
    data = {"version": 1, **asdict(JobConfig())}
    data.pop("review_command")
    data.pop("env")
    data.pop("bullet_one_line")
    data.pop("language")
    data.pop("review_mode")
    data.pop("install_command")
    if not config.exists():
        _create_new(config, (json.dumps(data, indent=2) + "\n").encode(), 0o600)
    load_job_config(root)
    if unit_dir is not None:
        from .units import timer_text

        _make_dirs(unit_dir)
        command = " ".join(
            _unit_arg(item)
            for item in (sys.executable, "-m", "taskwatch.job_cli", "--root", str(root), "scan")
        )
        service = (
            "[Unit]\nDescription=Scan the taskwatch job board once\n\n"
            "[Service]\nType=oneshot\n" + f"ExecStart={command}\n"
        )
        for name, text in (
            ("agent-taskwatch-scan.service", service),
            ("agent-taskwatch-scan.timer", timer_text()),
        ):
            path = unit_dir / name
            if path.exists():
                raise ValueError("unit exists; inspect it before replacing the timer entry point")
            _create_new(path, text.encode(), 0o644)
    return {"config": str(config), "units_written": unit_dir is not None, "activated": False}


def _unit_arg(value: str) -> str:
    if any(char in value for char in "\n\r\x00"):
        raise ValueError("unit arguments cannot contain control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


if __name__ == "__main__":
    raise SystemExit(main())

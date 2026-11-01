"""Standalone job commands. This module does not load the Hermes adapter."""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .jobs import JobConfig, Jobs, load_job_config
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
    watch = commands.add_parser("watch", help="Block until a job needs attention, then report")
    watch.add_argument("job")
    watch.add_argument("--timeout", type=_seconds(1), default=82800, help="Seconds (default 82800)")
    watch.add_argument("--interval", type=_seconds(5), default=30, help="Poll seconds (min 5)")
    needs = commands.add_parser("needs", help="List what needs the owner; read only, no lock")
    needs.add_argument(
        "--from", dest="cursor", type=_seconds(0), help="Cursor from an earlier list"
    )
    needs.add_argument("--wait", action="store_true", help="Block until something new needs you")
    needs.add_argument("--timeout", type=_seconds(1), default=82800, help="Seconds (default 82800)")
    needs.add_argument("--interval", type=_seconds(5), default=30, help="Poll seconds (min 5)")
    brief = commands.add_parser("brief", help="Print the decision brief of a job, composed on read")
    brief.add_argument("job")
    text = commands.add_parser("text", help="Write the reviewed title and body of a pull request")
    text.add_argument("job")
    text.add_argument("--pr", help="Unit id; optional when the job has one unit")
    text.add_argument("--out", type=Path, required=True, help="Folder for title.txt and body.md")
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
    if args.command == "watch":
        return _watch(args, root)
    if args.command == "text":
        return _text(args, root)
    if args.command == "brief":
        return _brief(args, root)
    if args.command == "needs":
        return _needs(args, root)
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


def _watch(args: argparse.Namespace, root: Path) -> int:
    from .job_watch import watch

    try:
        code, report = watch(root, args.job, timeout=args.timeout, interval=args.interval)
    except (ValueError, OSError, RecordError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    print(report, file=sys.stderr if code == 2 else sys.stdout)
    return code


def _needs(args: argparse.Namespace, root: Path) -> int:
    """0 listed, 3 time limit, 2 usage error, 1 other error. Nothing is written."""
    from .job_watch import needs

    if args.wait and args.cursor is None:
        print("taskwatch: needs --wait needs --from <cursor>", file=sys.stderr)
        return 2
    try:
        code, report = needs(
            root,
            cursor=args.cursor,
            wait=args.wait,
            timeout=args.timeout,
            interval=args.interval,
        )
    except (ValueError, OSError, RecordError) as exc:
        print(f"taskwatch: {exc}", file=sys.stderr)
        return 1
    print(report)
    return code


def _brief(args: argparse.Namespace, root: Path) -> int:
    """0 printed the brief, 2 unknown job. Read only; the brief is composed on every call."""
    from .brief import compose

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
    print("\n".join([*lines, f"Worker brief: {jobs.folder(job) / 'brief.md'}"]))
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
        records = jobs.load()
        return records[args.job].to_json() if args.job else [j.to_json() for j in records.values()]
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
            job = replace(job, stage="cancelled", wait_reason=args.reason)
        elif command == "wait":
            if job.stage not in {"work", "check", "review", "fix", "queued"}:
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
            # A resume into a review fix opens a new cycle and spends its first round now, so
            # the cap still bounds fixes; a check repair keeps the separate recovery budget.
            # A review-only job has no fix: its retried review is the round.
            retrying = job.resume_stage == "fix" or job.review_target is not None
            reviewing = retrying and job.fix_rounds > 0
            job = replace(
                job,
                stage="deliver_pending" if job.resume_stage == "deliver_pending" else "queued",
                wait_reason="",
                error="",
                recoveries=0,
                fix_rounds=int(reviewing),
            )
        job = replace(
            job, events=(*job.events, {"type": command, "at": now, "reason": args.reason})
        )
        jobs.save(job, now)
        return job.to_json()


def _date(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _init(root: Path, unit_dir: Path | None) -> dict:
    directory = root / "taskwatch"
    _make_dirs(directory)
    config = directory / "jobs-config.json"
    data = {"version": 1, **asdict(JobConfig())}
    data.pop("review_command")
    data.pop("env")
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
            ("hermes-taskwatch-scan.service", service),
            ("hermes-taskwatch-scan.timer", timer_text()),
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

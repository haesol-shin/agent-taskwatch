"""Delivery of finished jobs, driven through scans and the CLI with a real fake sender process."""

import hashlib
import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_delivery
from taskwatch.fingerprint import files_fingerprint
from taskwatch.job_cli import main as cli_main
from taskwatch.job_env import job_environment
from taskwatch.job_runner import _basis, _fingerprint
from taskwatch.job_scan import scan_jobs
from taskwatch.jobs import Jobs, JobState, load_job_config
from tests.git_fixture import make_repo
from tests.test_job_runner import GIT, Env, work_turn
from tests.test_job_scan import Board
from tests.verdicts import CODE_PASS

SENDER = r"""
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(args[args.index("--file") + 1], encoding="utf-8") as handle:
    text = handle.read()
seen = ""
root = os.environ.get("FAKE_JOBS_FILE", "")
if root:
    from pathlib import Path
    states = Path(root).glob("*/state.json")
    attempts = [a for p in states for a in json.loads(p.read_text())["job"]["deliveries"]]
    seen = attempts[-1]["status"] if attempts else ""
calls = os.path.join(here, "calls.jsonl")
with open(calls, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "text": text, "seen": seen}) + "\n")
    handle.flush()
    index = sum(1 for _ in open(calls, encoding="utf-8")) - 1
with open(os.path.join(here, "modes.json"), encoding="utf-8") as handle:
    modes = json.load(handle)
mode = modes[min(index, len(modes) - 1)]
ok = {"success": True, "message_id": "msg-1"}
if mode == "success":
    print(json.dumps(ok))
elif mode == "slow_success":
    time.sleep(0.5)
    print(json.dumps(ok))
elif mode == "number_id":
    print(json.dumps({"success": True, "message_id": 42}))
elif mode == "accepted_error":
    with open(os.path.join(here, "accepted"), "a", encoding="utf-8") as handle:
        handle.write("accepted\n")
    print(json.dumps({"error": "Telegram send failed: Timed out"}))
    sys.exit(1)
elif mode == "skipped":
    print(json.dumps({"skipped": True, "reason": "duplicate"}))
elif mode == "no_message_id":
    print(json.dumps({"success": True}))
elif mode == "success_false_exit_zero":
    print(json.dumps({"success": False}))
elif mode == "exit_one_success_true":
    print(json.dumps(ok))
    sys.exit(1)
elif mode == "malformed":
    print("not json at all")
elif mode == "array":
    print("[1, 2]")
elif mode == "unreadable":
    sys.stdout.buffer.write(b"\xff\xfe\x00{")
    sys.stdout.flush()
elif mode == "nonjson_failure":
    print("boom", file=sys.stderr)
    sys.exit(2)
elif mode == "killed":
    os.kill(os.getpid(), signal.SIGKILL)
elif mode == "hang":
    time.sleep(30)
"""

PROMPT = "Write the synthetic report\nSecond line must not be the title"
REVIEW_BODY = "Looks fine.\n" + json.dumps({"verdict": "PASS", "blocking": False, "reviewer": "r"})
WORKER_BRIEF = """## Card
The report is written; nothing for you to decide.
- The synthetic report now exists.
"""
CARD = """The report is written; nothing for you to decide.

Changed
- The synthetic report now exists.

Small problems left
- The report has no title.

Not checked
- the live service
- the rollback
"""
UNKNOWN_MODES = [
    "malformed",
    "array",
    "unreadable",
    "killed",
    "skipped",
    "no_message_id",
    "success_false_exit_zero",
    "exit_one_success_true",
    "nonjson_failure",
]


class Sender:
    def __init__(self, directory: Path) -> None:
        directory.mkdir()
        self.dir = directory
        (directory / "sender.py").write_text(SENDER, encoding="utf-8")
        self.command = (
            sys.executable,
            str(directory / "sender.py"),
            "--to",
            "person.invalid",
            "--file",
            "{message}",
            "--json",
        )

    def modes(self, *modes: str) -> None:
        (self.dir / "modes.json").write_text(json.dumps(list(modes)), encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_delivery, "DELIVERY_TIMEOUT", 5)


@pytest.fixture
def sender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sender:
    fake = Sender(tmp_path / "sender")
    fake.modes("success")
    jobs_file = tmp_path / "home" / "taskwatch" / "state" / "jobs"
    monkeypatch.setenv("FAKE_JOBS_FILE", str(jobs_file))
    return fake


@pytest.fixture
def board(tmp_path: Path, sender: Sender) -> Board:
    result = Board(tmp_path)
    result.config = replace(result.config, deliver_command=sender.command)
    return result


def finished_job(board: Board, **changes: object) -> JobState:
    """A job whose review passed and whose step service is already released."""
    job_id = board.add(prompt=PROMPT, review="source")
    folder = board.jobs.folder(board.get(job_id))
    workspace = folder / "work"
    workspace.mkdir(parents=True)
    (workspace / "out.txt").write_text("result", encoding="utf-8")
    log = folder / "logs" / "check-1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("all green", encoding="utf-8")
    (folder / "brief.md").write_text(WORKER_BRIEF, encoding="utf-8")
    job = replace(board.get(job_id), workspace=str(workspace))
    fingerprint = _fingerprint(job, workspace, job_environment({}))
    basis = _basis(job, folder)
    board.set(
        job_id,
        stage="deliver_pending",
        resume_stage="deliver_pending",
        workspace=str(workspace),
        ended_at=board.clock,
        checks=({"seq": 1, "ok": True, "exit_code": 0, "log": str(log)},),
        reviews=(
            {
                "seq": 2,
                "ok": True,
                "verdict": "PASS",
                "body": REVIEW_BODY,
                "findings": [
                    {
                        "finding": "F1",
                        "severity": "P3",
                        "category": "clarity",
                        "rule": "K-6",
                        "location": "out.txt:1",
                        "summary": "The report has no title.",
                    }
                ],
                "not_checked": "the live service; the rollback",
                "fingerprint": fingerprint,
                "outputs_fingerprint": files_fingerprint(workspace, job.outputs),
                "basis": basis,
            },
        ),
        fingerprint=fingerprint,
        basis=basis,
        guideline_hashes={
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (folder / "review-docs").iterdir()
        },
        **changes,
    )
    return board.get(job_id)


def statuses(job: JobState) -> list[str]:
    return [attempt["status"] for attempt in job.deliveries]


def cli(board: Board, action: str, job_id: str, reason: str = "operator") -> None:
    assert cli_main(["--root", str(board.root / "home"), action, job_id, "--reason", reason]) == 0


def test_success_marks_done_and_records_the_provider_message_id(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["success"]
    attempt = done.deliveries[0]
    assert attempt["exit_code"] == 0
    assert attempt["message_id"] == "msg-1"
    assert len(sender.calls()) == 1
    # The destination is the literal argument of the configured command, not the workspace.
    args = sender.calls()[0]["args"]
    assert args[:2] == ["--to", "person.invalid"]
    assert args[-1] == "--json"
    assert job.target not in args


def test_numeric_message_id_counts_as_a_receipt(board: Board, sender: Sender):
    sender.modes("number_id")
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert done.deliveries[0]["message_id"] == 42


def test_the_message_is_built_from_the_record_and_hashed_before_sending(
    board: Board, sender: Sender
):
    job = finished_job(board)
    board.scan()
    folder = board.jobs.folder(job)
    call = sender.calls()[0]
    message_path = Path(call["args"][call["args"].index("--file") + 1])
    assert message_path == folder / "delivery.txt"
    text = message_path.read_text(encoding="utf-8")
    assert text == call["text"]
    assert text == CARD
    assert len(text) < 4096
    assert (folder / "delivery-review.txt").read_text(encoding="utf-8") == REVIEW_BODY
    attempt = board.get(job.id).deliveries[0]
    assert attempt["message_file"] == str(message_path)
    assert attempt["message_sha256"] == hashlib.sha256(message_path.read_bytes()).hexdigest()


def test_attempt_is_durable_as_started_before_the_sender_runs(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    assert sender.calls()[0]["seen"] == "started"
    assert statuses(board.get(job.id)) == ["success"]


def test_acceptance_followed_by_an_error_is_unknown_and_never_resent(board: Board, sender: Sender):
    sender.modes("accepted_error", "success")
    job = finished_job(board)
    board.scan()
    stopped = board.get(job.id)
    assert (sender.dir / "accepted").read_text(encoding="utf-8") == "accepted\n"
    assert (stopped.stage, stopped.wait_reason) == ("user_wait", "delivery_unknown")
    assert stopped.resume_stage == "deliver_pending"
    assert statuses(stopped) == ["unknown"]
    assert stopped.deliveries[0]["exit_code"] == 1
    assert json.loads(stopped.deliveries[0]["stdout"]) == {
        "error": "Telegram send failed: Timed out"
    }
    board.scan()
    assert len(sender.calls()) == 2
    assert "delivery_unknown" in sender.calls()[1]["text"]
    assert stopped.notices[0]["status"] == "success"
    assert (sender.dir / "accepted").read_text(encoding="utf-8") == "accepted\n"
    assert board.get(job.id) == stopped


def test_timeout_is_unknown_and_never_resent(
    board: Board, sender: Sender, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(job_delivery, "DELIVERY_TIMEOUT", 0.5)
    sender.modes("hang")
    job = finished_job(board)
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert statuses(parked) == ["unknown"]
    board.scan()
    board.scan()
    assert len(sender.calls()) == 2
    assert board.get(job.id).notices[0]["status"] == "unknown"
    assert board.get(job.id).stage == "user_wait"


@pytest.mark.parametrize("mode", UNKNOWN_MODES)
def test_an_unprovable_outcome_is_unknown_and_never_resent(board: Board, sender: Sender, mode: str):
    sender.modes(mode, "success")
    job = finished_job(board)
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert statuses(parked) == ["unknown"]
    board.scan()
    board.scan()
    assert len(sender.calls()) == 2
    assert board.get(job.id).stage == "user_wait"


def test_an_interrupted_started_attempt_is_unknown_and_not_resent(board: Board, sender: Sender):
    attempt = {
        "at": 1.0,
        "command": list(sender.command),
        "message_file": "gone",
        "message_sha256": "0" * 64,
        "status": "started",
    }
    job = finished_job(board, deliveries=(attempt,))
    board.scan()
    parked = board.get(job.id)
    assert (parked.stage, parked.wait_reason) == ("user_wait", "delivery_unknown")
    assert len(sender.calls()) == 1
    assert "delivery_unknown" in sender.calls()[0]["text"]
    board.scan()
    assert len(sender.calls()) == 1
    cli(board, "resume", job.id)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["unknown", "success"]
    assert len(sender.calls()) == 2


def test_a_running_unit_defers_delivery_until_it_is_released(board: Board, sender: Sender):
    job = finished_job(board, unit="taskwatch-old.service")
    board.service.states["taskwatch-old.service"] = "active"
    board.service.stop_ok = False
    board.scan()
    assert sender.calls() == []
    assert board.get(job.id).stage == "deliver_pending"
    board.kill(job.id)
    board.service.stop_ok = True
    board.scan()
    assert board.get(job.id).stage == "done"
    assert len(sender.calls()) == 1


def test_resume_after_unknown_sends_once_without_rerunning_work(board: Board, sender: Sender):
    sender.modes("malformed", "success")
    job = finished_job(board)
    board.scan()
    assert board.get(job.id).wait_reason == "delivery_unknown"
    cli(board, "resume", job.id)
    resumed = board.get(job.id)
    assert resumed.stage == "deliver_pending"
    assert board.scan() == []
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["unknown", "success"]
    assert len(sender.calls()) == 3
    assert board.service.started == []
    board.scan()
    assert len(sender.calls()) == 3
    assert done.attempt == job.attempt


def test_cancel_before_delivery_sends_nothing(board: Board, sender: Sender):
    job = finished_job(board)
    cli(board, "cancel", job.id)
    board.scan()
    board.scan()
    assert board.get(job.id).stage == "cancelled"
    assert sender.calls() == []
    assert board.get(job.id).deliveries == ()


def test_a_done_job_is_never_sent_again(board: Board, sender: Sender):
    job = finished_job(board)
    board.scan()
    assert board.get(job.id).stage == "done"
    before = board.get(job.id)
    board.scan()
    board.scan()
    assert len(sender.calls()) == 1
    assert board.get(job.id) == before


def test_a_job_already_done_without_attempts_is_not_sent(board: Board, sender: Sender):
    job_id = board.add()
    board.set(job_id, stage="done")
    board.scan()
    assert sender.calls() == []


def test_concurrent_scans_send_exactly_once(board: Board, sender: Sender):
    sender.modes("slow_success")
    job = finished_job(board)
    root = board.root / "home"

    def run(clock: int) -> None:
        scan_jobs(
            Jobs(root),
            board.config,
            now=board.clock + clock,
            boot_id="boot-1",
            service=board.service,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(4)))
    assert len(sender.calls()) == 1
    done = board.get(job.id)
    assert done.stage == "done"
    assert statuses(done) == ["success"]


@pytest.mark.parametrize("stage", ["user_wait", "approval_wait"])
@pytest.mark.parametrize("mode", ["success", "accepted_error", "malformed", "hang"])
def test_stop_notice_is_once_per_entry_and_does_not_change_stage(
    board: Board, sender: Sender, monkeypatch: pytest.MonkeyPatch, stage: str, mode: str
):
    monkeypatch.setattr(job_delivery, "DELIVERY_TIMEOUT", 0.1)
    sender.modes(mode)
    job_id = board.add(prompt=PROMPT)
    board.set(job_id, stage=stage, wait_reason="synthetic_stop", error="needs judgment")
    board.scan()
    stopped = board.get(job_id)
    assert (stopped.stage, stopped.wait_reason, stopped.error) == (
        stage,
        "synthetic_stop",
        "needs judgment",
    )
    text = sender.calls()[0]["text"]
    for value in (job_id, PROMPT.splitlines()[0], stage, "synthetic_stop", "needs judgment"):
        assert value in text
    assert PROMPT.splitlines()[1] not in text
    commands = [shlex.split(line) for line in text.splitlines() if line.startswith("taskwatch ")]
    assert [args[3] for args in commands] == ["resume", "approve", "cancel"]
    assert stopped.notices[0]["status"] == ("success" if mode == "success" else "unknown")
    board.scan()
    assert len(sender.calls()) == 1
    action = "resume" if stage == "user_wait" else "approve"
    args = next(args for args in commands if args[3] == action)
    assert cli_main(args[1:]) == 0
    board.set(job_id, stage=stage, wait_reason="synthetic_stop")
    board.scan()
    assert len(sender.calls()) == 2
    assert len(board.get(job_id).notices) == 2


@pytest.mark.parametrize("change", ["output", "missing", "basis", "missing_guideline"])
@pytest.mark.parametrize("recoveries", [0, 3])
def test_changed_pass_blocks_delivery_and_spends_recovery_budget(
    board: Board, sender: Sender, change: str, recoveries: int
):
    job = finished_job(board, recoveries=recoveries)
    path = (
        Path(next(iter(job.guideline_hashes)))
        if change == "missing_guideline"
        else Path(job.intent)
        if change == "basis"
        else Path(job.workspace) / "out.txt"
    )
    if change in {"missing", "missing_guideline"}:
        path.unlink()
    else:
        path.write_text("changed", encoding="utf-8")
    with board.jobs.state.lock():
        job_delivery.deliver_job(board.jobs, board.config, job, board.clock)
    stale = board.get(job.id)
    assert stale.stage == ("check" if recoveries < 3 else "user_wait")
    assert stale.recoveries == min(recoveries + 1, 3)
    assert stale.resume_stage == "check"
    assert stale.fingerprint == stale.basis == ""
    assert stale.deliveries == ()
    assert not any(Path(call["args"][3]).name == "delivery.txt" for call in sender.calls())
    if change == "missing_guideline":
        assert not path.exists()


@pytest.mark.parametrize("change", ["unchanged", "changed", "deleted"])
def test_git_ignored_output_is_checked_at_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Sender,
    change: str,
):
    monkeypatch.delenv("FAKE_JOBS_FILE")
    repo = make_repo(tmp_path, "repo")
    env = Env(tmp_path, monkeypatch, review="code", target=repo)
    commit = [
        ["git", "rm", "--cached", "out.txt"],
        ["git", "add", "-A"],
        [*GIT, "commit", "-m", "work"],
    ]
    env.scenario(
        [
            {"writes": {**work_turn()["writes"], ".gitignore": "out.txt\n"}, "run": commit},
            {"reply": CODE_PASS},
        ]
    )
    assert env.run("work") == 0
    assert env.run("check") == 0
    assert env.run("review") == 0
    job = env.get()
    output = env.folder() / "worktree" / "out.txt"
    assert job.stage == "deliver_pending" and output.is_file()
    assert (
        subprocess.run(
            ["git", "-C", job.workspace, "check-ignore", "out.txt"],
            check=True,
            capture_output=True,
        ).stdout.strip()
        == b"out.txt"
    )
    if change == "changed":
        output.write_text("ok\nlater\n", encoding="utf-8")
    elif change == "deleted":
        output.unlink()
    config = replace(env.config, deliver_command=sender.command)
    with env.jobs.state.lock():
        result = job_delivery.deliver_job(env.jobs, config, job, 3000.0)
    delivered = [c for c in sender.calls() if Path(c["args"][3]).name == "delivery.txt"]
    if change == "unchanged":
        assert result.stage == "done" and len(delivered) == 1
        return
    assert result.stage == "check" and result.resume_stage == "check"
    assert result.fingerprint == result.basis == "" and result.deliveries == ()
    assert delivered == []


def test_interrupted_notice_is_unknown_without_resend(board: Board, sender: Sender):
    job_id = board.add()
    board.set(
        job_id,
        stage="user_wait",
        wait_reason="synthetic_stop",
        notices=({"entry": 0, "status": "started"},),
    )
    board.scan()
    board.scan()
    job = board.get(job_id)
    assert job.stage == "user_wait"
    assert job.notices[0]["status"] == "unknown"
    assert sender.calls() == []


def test_notice_failure_does_not_block_results_or_job_launch(board: Board, sender: Sender):
    sender.modes("success", "accepted_error")
    waiting = board.add()
    board.set(waiting, stage="user_wait", wait_reason="synthetic_stop")
    result = finished_job(board)
    pending = board.add()
    assert board.scan() == [pending]
    assert board.get(result.id).stage == "done"
    assert board.get(waiting).stage == "user_wait"
    assert board.get(waiting).notices[0]["status"] == "unknown"
    assert [Path(call["args"][3]).name for call in sender.calls()] == [
        "delivery.txt",
        "notice-0.txt",
    ]


def write_config(root: Path, **changes: object) -> None:
    data = {
        "version": 1,
        "slots": 2,
        "stall_seconds": 60,
        "omp_command": ["omp"],
        "rpc_timeout": 60,
        "deliver_command": ["send", "--to", "person.invalid", "--file", "{message}"],
        **changes,
    }
    data = {key: value for key, value in data.items() if value is not None}
    path = root / "taskwatch" / "jobs-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("command", [None, ["executor", "--role", "reviewer"]])
def test_config_accepts_optional_review_command(tmp_path: Path, command):
    write_config(tmp_path, review_command=command)
    config = load_job_config(tmp_path)
    assert config.review_command == (tuple(command) if command is not None else None)
    assert config.omp_command == ("omp",)


@pytest.mark.parametrize("command", [None, [], "executor", ["executor", ""], [" "], [1]])
def test_config_refuses_invalid_review_command(tmp_path: Path, command):
    write_config(tmp_path)
    path = tmp_path / "taskwatch" / "jobs-config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["review_command"] = command
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="review_command"):
        load_job_config(tmp_path)


@pytest.mark.parametrize(
    "key",
    ["version", "slots", "stall_seconds", "omp_command", "rpc_timeout", "deliver_command"],
)
def test_review_command_does_not_replace_required_config_keys(tmp_path: Path, key):
    write_config(tmp_path, review_command=["executor"], **{key: None})
    with pytest.raises(ValueError):
        load_job_config(tmp_path)


def test_config_requires_the_delivery_command(tmp_path: Path):
    write_config(tmp_path, deliver_command=None)
    with pytest.raises(ValueError):
        load_job_config(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        [],
        "send {message}",
        ["send", "--file", "x"],
        ["send", "{message}", "{message}"],
        ["send", "--file={message}"],
        ["send", "", "{message}"],
        ["send", " ", "{message}"],
        ["send", 1, "{message}"],
        ["send", "{message}{message}"],
    ],
)
def test_config_refuses_an_unsafe_delivery_command(tmp_path: Path, command: object):
    write_config(tmp_path, deliver_command=command)
    with pytest.raises(ValueError):
        load_job_config(tmp_path)


def test_literal_json_in_a_configured_sender_is_not_a_placeholder(board: Board):
    command = [
        sys.executable,
        "-c",
        'print(\'{"success": true, "message_id": "inline-message"}\')',
        "{message}",
    ]
    write_config(board.jobs.state.root, deliver_command=command)
    board.config = load_job_config(board.jobs.state.root)
    job = finished_job(board)
    board.scan()
    done = board.get(job.id)
    assert done.stage == "done"
    assert done.deliveries[-1]["message_id"] == "inline-message"

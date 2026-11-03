"""The runner against a fake omp RPC process: real subprocess, real files, no model."""

import base64
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_runner
from taskwatch.fingerprint import files_fingerprint, head_sha
from taskwatch.job_rpc import FrameDecoder, RpcError
from taskwatch.job_runner import run_job
from taskwatch.jobs import JobConfig, Jobs, JobState
from taskwatch.rate_limit import RateLimit
from tests.git_fixture import make_repo
from tests.spec_fixture import VALID_SPEC

FAKE = Path(__file__).with_name("fake_omp.py")
PASS = (
    '{"verdict": "PASS", "blocking": false, "reviewer": "reviewer.invalid",'
    ' "checked": "the diff", "not_checked": "runtime"}'
)
FAIL = (
    '{"verdict": "FAIL", "blocking": true, "reviewer": "reviewer.invalid",'
    ' "checked": "the diff", "not_checked": "runtime"}'
)
BRIEF = """## Why
Synthetic job.

## What changed and why
- Wrote the result file. See out.txt:1

## Choices made
| choice | picked | reason | source | who |
|---|---|---|---|---|
| format | text | synthetic | none | agent |

## Decisions needed
none

## Where to look
out.txt:1

## Card
Synthetic job done; nothing for you to decide.
- The helper now exists.
"""
CHOICES = (
    '{"picks": [], "decided": [], "existing_tools": [{"need": "x", "searched": ["a", "b", "c"],'
    ' "found": []}]}'
)
GOOD_FILES = {
    "out.txt": "ok\n",
    "PROGRESS.md": "done\n",
    "checklist.json": '{"items": [{"id": "a", "passes": true}]}',
    "brief.md": BRIEF,
}
RECORDS = {"PROGRESS.md", "checklist.json", "brief.md"}
CHECK = [
    sys.executable,
    "-c",
    "import sys; print('to-stdout', flush=True); print('to-stderr', file=sys.stderr);"
    " sys.exit(0 if open('out.txt').read().strip() == 'ok' else 1)",
]
GIT = [
    "git",
    "-c",
    "user.name=Dev",
    "-c",
    "user.email=dev@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


class Env:
    def __init__(self, tmp_path: Path, monkeypatch, review: str = "source", target=None, **extra):
        self.tmp = tmp_path
        self.jobs = Jobs(tmp_path / "root")
        self.config = JobConfig(omp_command=(sys.executable, str(FAKE)), rpc_timeout=5)
        self.state = tmp_path / "fake-state"
        monkeypatch.setenv("FAKE_OMP_SCENARIO", str(tmp_path / "scenario.json"))
        monkeypatch.setenv("FAKE_OMP_STATE", str(self.state))
        monkeypatch.setattr(job_runner, "SETTLE_POLL", 0.1)
        (tmp_path / "intent.md").write_text("intent text\n", encoding="utf-8")
        (tmp_path / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
        (tmp_path / "w.txt").write_text("work only\n", encoding="utf-8")
        (tmp_path / "r.txt").write_text("review only\n", encoding="utf-8")
        if target is None:
            target = tmp_path / "target"
            target.mkdir()
        self.job = self.jobs.submit(
            {
                "receipt_key": "k1",
                "approval_source": "msg-1",
                "approval_scope": "task-1",
                "prompt": "ORIGINAL PROMPT TEXT\nline two",
                "review": review,
                "target": str(target),
                "intent": str(tmp_path / "intent.md"),
                "spec": str(tmp_path / "spec.md"),
                "outputs": ["out.txt", "choices.json"] if review == "design" else ["out.txt"],
                "check_command": CHECK,
                "checklist": "checklist.json",
                "progress": "PROGRESS.md",
                "inputs": {"work": [str(tmp_path / "w.txt")], "review": [str(tmp_path / "r.txt")]},
                **extra,
            },
            1000.0,
        )
        self.attempt = 0
        self.scenario([])

    def scenario(self, turns: list, **extra) -> None:
        # Each scenario starts at its first turn; the counter is shared across fake processes.
        (self.state / "turn.txt").unlink(missing_ok=True)
        (self.tmp / "scenario.json").write_text(
            json.dumps({"turns": turns, **extra}), encoding="utf-8"
        )

    def folder(self) -> Path:
        return self.jobs.folder(self.job)

    def get(self) -> JobState:
        return self.jobs.load()[self.job.id]

    def put(self, stage: str, **changes) -> JobState:
        """A launch as the scanner makes it: a review reserves one attempt before it starts."""
        self.attempt += 1
        with self.jobs.state.lock():
            job = self.get()
            if stage == "review":
                changes.setdefault("reviews_used", job.reviews_used + 1)
                changes.setdefault("review_inflight", self.attempt)
            job = replace(job, stage=stage, attempt=self.attempt, unit="u", ended_at=0.0, **changes)
            self.jobs.save(job, 2000.0 + self.attempt)
        return job

    def run(self, stage: str, **changes) -> int:
        self.put(stage, **changes)
        return run_job(self.jobs, self.config, self.job.id, self.attempt)

    def seed(self, files: dict) -> None:
        """Result files go to the workspace; the job records to the job folder."""
        work = self.folder() / "work"
        work.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            base = self.folder() if name in RECORDS else work
            (base / name).write_text(text, encoding="utf-8")

    def log(self) -> list[dict]:
        return [json.loads(x) for x in (self.state / "log.jsonl").read_text().splitlines()]

    def prompts(self) -> list[str]:
        return [x["message"] for x in self.log() if x.get("type") == "prompt"]

    def to_review(self) -> None:
        self.seed(GOOD_FILES)
        assert self.run("check") == 0
        assert self.get().stage == "review"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def work_turn(**extra) -> dict:
    # The workspace and the job folder are siblings of the records, so `..` reaches them.
    writes = {("../" + k if k in RECORDS else k): v for k, v in GOOD_FILES.items()}
    return {"writes": writes, **extra}


# --- protocol -------------------------------------------------------------------------------


def chunk(chunk_id: str, index: int, count: int, length: int, data: bytes) -> dict:
    return {
        "type": "rpc_chunk",
        "chunkId": chunk_id,
        "index": index,
        "count": count,
        "byteLength": length,
        "data": base64.b64encode(data).decode(),
    }


def test_decoder_reassembles_and_rejects_bad_sequences():
    body = json.dumps({"type": "response", "id": "a"}).encode()
    half = len(body) // 2
    decoder = FrameDecoder()
    assert decoder.push(chunk("c", 0, 2, len(body), body[:half])) is None
    assert decoder.push(chunk("c", 1, 2, len(body), body[half:]))["id"] == "a"
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push({"type": "agent_start"})
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push(chunk("other", 1, 2, len(body), body[half:]))
    decoder = FrameDecoder()
    decoder.push(chunk("c", 0, 2, len(body), body[:half]))
    with pytest.raises(RpcError):
        decoder.push(chunk("c", 1, 2, len(body) + 3, body[half:]))
    with pytest.raises(RpcError):
        FrameDecoder(max_bytes=10).push(chunk("c", 0, 2, 11, b"x"))
    with pytest.raises(RpcError):
        FrameDecoder().push({**chunk("c", 0, 2, 4, b"abcd"), "data": "not base64!"})


# --- work and check -------------------------------------------------------------------------


def test_pipeline_from_work_to_deliver_pending(env):
    env.scenario([work_turn(), {"reply": f"{PASS}\nNo findings."}])
    assert env.run("work") == 0
    job = env.get()
    assert (job.stage, job.resume_stage) == ("check", "check")
    assert job.ended_at > 0 and job.workspace == str(env.folder() / "work")
    assert job.session_path.endswith("s1.jsonl")
    assert job.session_dir == str(env.folder() / "sessions" / "work")
    types = [e["type"] for e in job.events]
    assert {"agent_start", "prompt_result", "session_settled", "step_exit"} <= set(types)
    log = env.log()
    assert {"type": "negotiate_protocol", "id": log[1]["id"], "protocolVersion": 2} == log[1]
    assert log[0]["argv"] == ["--mode", "rpc", "--no-ui"]
    assert log[0]["cwd"] == str(env.folder() / "work")
    assert env.prompts()[0].startswith("ORIGINAL PROMPT TEXT\nline two")
    assert "msg-1" in env.prompts()[0] and "task-1" in env.prompts()[0]
    assert (env.folder() / "WORKFLOW.md").is_file()
    assert (env.folder() / "review-docs" / "source.md").is_file()

    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "review" and job.recoveries == 0
    record = job.checks[-1]
    assert record["ok"] and record["exit_code"] == 0
    assert Path(record["log"]).read_text().split() == ["to-stdout", "to-stderr"]
    assert job.fingerprint == files_fingerprint(env.folder() / "work", ["out.txt"])
    assert job.basis

    assert env.run("review") == 0
    job = env.get()
    assert job.stage == "deliver_pending"
    review = job.reviews[-1]
    assert review["ok"] and review["fingerprint"] == job.fingerprint
    assert "No findings." in review["body"]
    work_prompt, review_prompt = env.prompts()
    assert "work-0.txt" in work_prompt and "review-0.txt" not in work_prompt
    assert "review-0.txt" in review_prompt and "work-0.txt" not in review_prompt


def granted_paths(text: str) -> set[Path]:
    return {Path(p).resolve() for p in re.findall(r"^- `([^`]+)`$", text, re.MULTILINE)}


@pytest.mark.parametrize("public_text", [False, True])
def test_worker_file_grants_match_in_work_fix_and_resume(tmp_path, monkeypatch, public_text):
    repo = make_repo(tmp_path, "repo")
    env = Env(
        tmp_path,
        monkeypatch,
        review="code",
        target=repo,
        progress="records/progress.md",
        checklist="records/checklist.json",
        **({"public_text": [{"id": "change", "base": "main"}]} if public_text else {}),
    )
    expected = {
        env.folder() / name
        for name in (
            "records/progress.md",
            "records/checklist.json",
            "brief.md",
            "findings.json",
            "root-cause.md",
        )
    }
    if public_text:
        expected |= {
            env.folder() / "pr/change" / name for name in ("title.txt", "body.md", "commit.txt")
        }
    env.scenario([{"die_after_content": True}, {}, {}])
    assert env.run("work") == 1
    assert env.run("work", session_dir=env.get().session_dir) == 0
    assert env.run("fix") == 0
    workflow = (env.folder() / "WORKFLOW.md").read_text()
    for text in [workflow, *env.prompts()]:
        assert granted_paths(text) == expected
        assert granted_paths(text.split("## Approval", 1)[1]) == expected
    assert all(text.startswith(env.job.prompt) for text in env.prompts())


def test_review_only_approval_has_no_worker_file_grants(tmp_path, monkeypatch):
    from tests.test_review_only import Review

    review = Review(tmp_path, monkeypatch)
    review.scenario([{"reply": f"{PASS}\nNo findings."}])
    assert review.run() == 0
    assert review.get().stage == "deliver_pending"
    (message,) = review.prompts()
    assert granted_paths(message) == set()


def test_stale_attempt_runs_nothing(env):
    env.put("work")
    assert run_job(env.jobs, env.config, env.job.id, env.attempt - 1) == 2
    assert not (env.state / "log.jsonl").exists()
    assert run_job(env.jobs, env.config, "0" * 32, 1) == 2


def test_check_failures_route_to_fix_then_user_wait(env):
    env.seed({**GOOD_FILES, "out.txt": "bad\n"})
    assert env.run("check") == 0
    job = env.get()
    assert (job.stage, job.recoveries, job.ended_at > 0) == ("fix", 1, True)
    assert "exited 1" in job.error and "to-stderr" in Path(job.checks[-1]["log"]).read_text()
    assert job.fingerprint == job.checks[-1]["fingerprint"]
    assert env.run("check", recoveries=3) == 0
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "check_failed")


def _counting_check(env, counter: Path) -> None:
    script = (
        f"open({str(counter)!r}, 'a').write('x')\n"
        "import sys\nsys.exit(0 if open('out.txt').read().strip() == 'ok' else 1)\n"
    )
    with env.jobs.state.lock():
        env.jobs.save(replace(env.get(), check_command=(sys.executable, "-c", script)), 1500.0)


def test_check_reuses_a_passing_command_run_for_the_same_result(env, tmp_path):
    counter = tmp_path / "runs.txt"
    _counting_check(env, counter)
    env.seed({k: v for k, v in GOOD_FILES.items() if k != "PROGRESS.md"})
    assert env.run("check") == 0
    first = env.get().checks[-1]
    assert counter.read_text() == "x" and first["exit_code"] == 0 and not first["ok"]
    assert any("PROGRESS.md" in p for p in first["problems"])
    # Only a record outside the result was missing: the command is not run again.
    (env.folder() / "PROGRESS.md").write_text("done\n", encoding="utf-8")
    assert env.run("check") == 0
    job = env.get()
    second = job.checks[-1]
    assert counter.read_text() == "x"
    assert second["ok"] and second["reused_check"] == first["seq"]
    assert (second["log"], second["log_sha256"]) == (first["log"], first["log_sha256"])
    assert job.stage == "review"
    # Later reuse points at the run that happened, not at the reuse.
    assert env.run("check") == 0
    assert env.get().checks[-1]["reused_check"] == first["seq"]
    # Reused checks still run the file checks.
    (env.folder() / "PROGRESS.md").unlink()
    assert env.run("check") == 0
    assert any("PROGRESS.md" in p for p in env.get().checks[-1]["problems"])


def test_check_runs_the_command_when_the_result_or_basis_changed(env, tmp_path):
    counter = tmp_path / "runs.txt"
    _counting_check(env, counter)
    env.seed(GOOD_FILES)
    assert env.run("check") == 0
    assert counter.read_text() == "x"
    (env.folder() / "work" / "out.txt").write_text("ok\nmore\n", encoding="utf-8")
    assert env.run("check") == 0
    assert counter.read_text() == "xx" and "reused_check" not in env.get().checks[-1]
    (tmp_path / "spec.md").write_text(VALID_SPEC + "\nchanged\n", encoding="utf-8")
    assert env.run("check") == 0
    assert counter.read_text() == "xxx" and "reused_check" not in env.get().checks[-1]


def test_check_does_not_reuse_a_failed_command_run(env, tmp_path):
    counter = tmp_path / "runs.txt"
    _counting_check(env, counter)
    env.seed({**GOOD_FILES, "out.txt": "bad\n"})
    assert env.run("check") == 0
    assert env.run("check") == 0
    assert counter.read_text() == "xx"


def test_check_requires_files_checklist_and_progress(env):
    env.seed({"out.txt": "ok\n", "checklist.json": '{"items": [{"id": "a", "passes": false}]}'})
    assert env.run("check") == 0
    problems = env.get().checks[-1]["problems"]
    assert any("PROGRESS.md" in p for p in problems)
    assert any("do not pass: a" in p for p in problems)


def test_prompt_result_alone_is_not_acceptance(env):
    env.config = JobConfig(omp_command=env.config.omp_command, rpc_timeout=1)
    env.scenario([work_turn(no_settle=True)])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "timed out" in job.error and job.ended_at == 0
    env.scenario([work_turn(no_agent_start=True)])
    assert env.run("work") == 1
    assert "agent_start" in env.get().error


def test_dead_process_leaves_stage_and_resume_opens_same_session(env):
    env.scenario([{"die_after_content": True}, work_turn()])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "exited" in job.error and job.session_path
    assert env.run("work", session_dir=job.session_dir) == 0
    assert env.get().stage == "check"
    opens = [x for x in env.log() if x.get("type") == "open_session"]
    assert opens[0]["sessionDir"] == opens[1]["sessionDir"]
    assert "interrupted" in env.prompts()[1] and "interrupted" not in env.prompts()[0]
    assert len(Path(job.session_path).read_text().splitlines()) == 2
    assert [x["resumed"] for x in env.get().events if x["type"] == "session_open"] == [
        False,
        True,
    ]


def test_empty_session_is_not_resumed(env):
    env.scenario([{"die": True}, work_turn()])
    assert env.run("work") == 1
    job = env.get()
    assert Path(job.session_path).stat().st_size == 0
    assert env.run("work", session_dir=job.session_dir) == 0
    assert [x["resumed"] for x in env.get().events if x["type"] == "session_open"] == [
        False,
        False,
    ]
    assert all("interrupted" not in p for p in env.prompts())


def test_cancelled_attempt_exits_promptly_and_writes_nothing(env, monkeypatch):
    monkeypatch.setattr(job_runner, "WATCH_EVERY", 0.1)
    env.config = JobConfig(omp_command=env.config.omp_command, rpc_timeout=30)
    env.scenario([work_turn(sleep=6)])
    env.put("work")
    result = []
    started = time.monotonic()
    thread = threading.Thread(
        target=lambda: result.append(run_job(env.jobs, env.config, env.job.id, env.attempt))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (env.state / "log.jsonl").exists():
        time.sleep(0.05)
    time.sleep(0.5)
    with env.jobs.state.lock():
        env.jobs.save(replace(env.get(), stage="cancelled"), 3000.0)
    thread.join(5)
    assert result == [2] and time.monotonic() - started < 5
    job = env.get()
    assert job.stage == "cancelled" and "step_exit" not in [e["type"] for e in job.events]


# --- review ---------------------------------------------------------------------------------


@pytest.mark.parametrize("separate_review", [False, True])
def test_review_command_keeps_work_and_fix_on_the_work_command(env, separate_review):
    work_command = (*env.config.omp_command, "--executor", "worker")
    review_command = (*env.config.omp_command, "--executor", "reviewer")
    env.config = replace(
        env.config,
        omp_command=work_command,
        review_command=review_command if separate_review else None,
    )
    env.scenario([work_turn(), {"reply": f"{FAIL}\nP1: revise"}, work_turn()])
    assert env.run("work") == 0
    assert env.run("check") == 0
    assert env.run("review") == 0
    assert env.get().stage == "fix"
    assert env.run("fix") == 0
    assert env.get().stage == "check"
    argv = [entry["argv"] for entry in env.log() if "argv" in entry]
    worker = ["--executor", "worker", "--mode", "rpc", "--no-ui"]
    reviewer = ["--executor", "reviewer", "--mode", "rpc", "--no-ui"]
    assert argv == [worker, reviewer if separate_review else worker, worker]


def test_a_failed_review_routes_to_fix_and_a_failed_fix_is_not_a_review(env):
    env.scenario([{"reply": f"{FAIL}\nP1: wrong"}, {"die": True}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight) == ("fix", 1, 0)
    assert job.ended_at > 0
    assert not job.reviews[-1]["ok"] and "P1: wrong" in job.reviews[-1]["body"]
    assert env.run("fix") == 1
    job = env.get()
    assert (job.stage, job.reviews_used, job.recoveries) == ("fix", 1, 0)
    assert "P1: wrong" in env.prompts()[-1]


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_the_last_permitted_failed_review_starts_no_fix(env, cap):
    env.scenario([{"reply": f"{FAIL}\nstill wrong"}])
    env.to_review()
    assert env.run("review", reviews_used=cap, max_reviews=cap) == 0
    job = env.get()
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "user_wait",
        "review_exhausted",
        "fix",
    )
    assert job.reviews_used == cap
    assert len(env.prompts()) == 1


def test_incomplete_review_output_cannot_pass_and_retries_the_review_not_a_fix(env):
    env.scenario([{"reply": "looks fine to me"}] * 2)
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.resume_stage, job.reviews_used) == ("review", "review", 1)
    assert "exactly one verdict line" in job.error
    assert len(env.prompts()) == 2  # the review prompt and one follow-up, then the round fails


def test_a_malformed_verdict_is_fixed_in_the_same_session(env):
    env.scenario([{"reply": "looks fine to me"}, {"reply": f"{PASS}\nNo findings."}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.reviews_used, len(job.reviews)) == ("deliver_pending", 1, 1)
    assert job.reviews[-1]["ok"] and "No findings." in job.reviews[-1]["body"]
    first, follow_up = env.prompts()
    assert "independent reviewer" in first
    assert "exactly one verdict line" in follow_up and '"verdict": "PASS" or "FAIL"' in follow_up


def test_a_reply_without_reviewer_is_accepted_and_the_runner_names_it(env):
    reply = json.dumps({"verdict": "PASS", "blocking": False, "checked": "x", "not_checked": "y"})
    env.scenario([{"reply": reply}])
    env.to_review()
    assert env.run("review") == 0
    record = env.get().reviews[-1]
    assert record["ok"] and record["reviewer"] == Path(sys.executable).name
    assert len(env.prompts()) == 1


def test_result_changed_during_review_rejects_pass_and_returns_through_check(env):
    env.scenario([{"writes": {"out.txt": "ok\nchanged\n"}, "reply": PASS}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight) == ("check", 1, 0)
    assert not job.reviews[-1]["ok"] and job.reviews[-1]["stale"]
    assert "changed during the review" in job.error


@pytest.mark.parametrize("case", ["log", "checklist", "review_input"])
def test_review_pass_needs_its_evidence_to_still_hold(env, case):
    env.to_review()
    job = env.get()
    writes = {}
    if case == "log":
        Path(job.checks[-1]["log"]).write_text("MODEL CLAIM: PASS\n", encoding="utf-8")
    elif case == "checklist":
        (env.folder() / "checklist.json").unlink()
    else:
        writes[job.inputs["review"][0]] = "CHANGED REVIEW CRITERIA"
    env.scenario([{"reply": PASS, "writes": writes}])
    assert env.run("review") == (1 if case == "review_input" else 0)
    current = env.get()
    assert current.reviews == ()
    if case == "review_input":
        assert (current.stage, current.wait_reason) == ("user_wait", "inputs_invalid")
    else:
        assert current.stage == "check" and current.fingerprint == ""
        assert "review_stale" in [e["type"] for e in current.events]


def test_result_changed_before_review_goes_back_to_check(env):
    env.to_review()
    (env.folder() / "work" / "out.txt").write_text("ok\nlater\n", encoding="utf-8")
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.fingerprint) == ("check", "")
    assert not (env.state / "log.jsonl").exists()


# --- settlement and transport ---------------------------------------------------------------


def test_nonzero_exit_or_persistence_notice_fails_a_settled_step(env):
    env.scenario([work_turn()], exit_code=1)
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "exited with code 1" in job.error
    env.scenario([work_turn(notice=True)])
    assert env.run("work") == 1
    assert "session-persistence" in env.get().error


def test_startup_tool_diagnostic_is_recorded_without_rate_wait(env):
    peer = (
        "import sys; "
        "print('Unknown tool: ast_grep. Valid tools: read, task', file=sys.stderr); "
        "sys.exit(7)"
    )
    env.config = replace(env.config, omp_command=(sys.executable, "-c", peer))
    assert env.run("work") == 1
    job = env.get()
    assert "ast_grep" in job.error and "read, task" in job.error
    assert "7" in job.error and job.retry_not_before == 0
    assert job.stage == "work" and not job.wait_reason


def test_oversize_frame_is_rejected(env):
    env.scenario([work_turn(oversize=True)])
    assert env.run("work") == 1
    assert "frame limit" in env.get().error


def test_check_judges_files_after_the_command_and_kills_strays(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    stray = env.folder() / "work" / "stray.txt"
    script = (
        "import os, subprocess, sys\n"
        f"os.remove({str(env.folder() / 'checklist.json')!r})\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5);"
        ' open("stray.txt", "w").write("x")\'])\n'
    )
    env.seed(GOOD_FILES)
    with env.jobs.state.lock():
        env.jobs.save(replace(env.get(), check_command=(sys.executable, "-c", script)), 1500.0)
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "fix" and "checklist is missing" in job.error
    time.sleep(2.2)
    assert not stray.exists()


def test_pending_async_work_is_not_accepted(env):
    env.config = replace(env.config, rpc_timeout=1)
    env.scenario([work_turn(pending_async=True)])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "timed out" in job.error


def test_pending_async_review_is_not_accepted(env):
    env.scenario([{"reply": PASS, "pending_async": True}])
    env.to_review()
    env.config = replace(env.config, rpc_timeout=1)
    assert env.run("review") == 1
    job = env.get()
    assert job.stage == "review" and job.reviews == () and "timed out" in job.error


def test_rereview_sees_prior_findings_and_original_request(env):
    env.scenario(
        [
            {"reply": f"{FAIL}\nP1: old finding"},
            work_turn(),
            {"reply": f"{PASS}\nok"},
        ]
    )
    env.to_review()
    assert env.run("review") == 0 and env.get().stage == "fix"
    assert env.run("fix") == 0 and env.run("check") == 0
    assert env.run("review") == 0
    prompt = env.prompts()[-1]
    assert "re-review round 2" in prompt and "P1: old finding" in prompt
    assert "ORIGINAL PROMPT TEXT" in prompt and env.get().stage == "deliver_pending"


def test_async_followup_failure_after_prompt_result_fails_the_step(env):
    env.scenario([work_turn(followup_error="error")])
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and "stopReason error" in job.error


def test_failed_check_after_review_uses_recoveries_not_reviews(env):
    env.seed({**GOOD_FILES, "out.txt": "bad\n"})
    assert env.run("check", reviews_used=1) == 0
    job = env.get()
    assert (job.stage, job.reviews_used, job.recoveries) == ("fix", 1, 1)
    assert env.run("check", recoveries=3) == 0
    job = env.get()
    assert (job.stage, job.wait_reason, job.reviews_used) == ("user_wait", "check_failed", 1)


def test_a_passing_check_resets_recoveries_and_leaves_the_review_cycle(env):
    env.seed(GOOD_FILES)
    assert env.run("check", reviews_used=2, review_cycle_start=0, recoveries=2) == 0
    job = env.get()
    assert (job.stage, job.recoveries, job.reviews_used, job.review_cycle_start) == (
        "review",
        0,
        2,
        0,
    )


def test_whole_review_text_survives_chunked_frames(env):
    body = f"{PASS}\n" + "finding line\n" * 100
    env.scenario([{"reply": body}], chunk_at=500)
    env.to_review()
    assert env.run("review") == 0
    review = env.get().reviews[-1]
    assert review["ok"] and review["body"] == body


# --- code jobs ------------------------------------------------------------------------------


def test_code_job_uses_worktree_and_committed_fingerprint(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "repo")
    env = Env(tmp_path, monkeypatch, review="code", target=repo)
    env.scenario([work_turn(run=[["git", "add", "-A"], [*GIT, "commit", "-m", "work"]])])
    assert env.run("work") == 0
    worktree = env.folder() / "worktree"
    assert env.get().workspace == str(worktree) and (worktree / ".git").exists()
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "review" and job.fingerprint == head_sha(worktree)
    assert (env.folder() / "review-docs" / "code.md").is_file()
    (worktree / "out.txt").write_text("ok\nmore\n", encoding="utf-8")
    assert env.run("check") == 0
    job = env.get()
    assert job.stage == "fix" and "uncommitted" in job.error
    subprocess.run(["git", "-C", str(repo), "worktree", "list"], check=True, capture_output=True)


def test_missing_input_hash_match_stops_job(env):
    Path(next(iter(env.get().input_hashes))).write_text("tampered", encoding="utf-8")
    assert env.run("work") == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "inputs_invalid")
    assert not (env.state / "log.jsonl").exists()


def test_changed_review_profile_stops_job(env):
    env.scenario([work_turn()])
    assert env.run("work") == 0
    profile = env.folder() / "review-docs" / "common.md"
    profile.write_text(profile.read_text() + "\nedited\n", encoding="utf-8")
    assert env.run("check") == 1
    job = env.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "inputs_invalid")


def test_messages_list_each_predecessor_result_without_copying_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, review="design")
    design = replace(env.get(), stage="done", fingerprint="fp-1", workspace=str(tmp_path / "ws"))
    env.jobs.save(design, 1500.0)
    code = env.jobs.submit(
        {
            "receipt_key": "k2",
            "approval_source": "msg-1",
            "approval_scope": "task-1",
            "prompt": "next",
            "review": "code",
            "target": design.target,
            "intent": design.intent,
            "spec": design.spec,
            "outputs": ["out.txt"],
            "check_command": CHECK,
            "checklist": "checklist.json",
            "progress": "PROGRESS.md",
            "predecessors": [design.id],
        },
        1600.0,
    )
    ctx = job_runner.Ctx(env.jobs, env.config, code, env.jobs.folder(code), tmp_path / "ws2")
    for text in (
        job_runner._agent_message(ctx, False),
        job_runner._review_message(ctx, (), False, tmp_path / "rr"),
    ):
        assert f"- {design.id} (design), stage done" in text
        assert f"output: {tmp_path / 'ws' / 'out.txt'}" in text
        assert "result fingerprint: fp-1" in text
        assert f"brief: {env.folder() / 'brief.md'}" in text
    plain = job_runner._agent_message(
        job_runner.Ctx(env.jobs, env.config, env.get(), env.folder(), tmp_path / "ws"), False
    )
    assert "Predecessor results" not in plain


def test_design_messages_name_defaults_file_and_short_hash(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, review="design")
    ctx = job_runner.Ctx(env.jobs, env.config, env.get(), env.folder(), tmp_path / "ws")
    expected_path = env.jobs.state.root / "taskwatch" / "defaults.json"
    for text in (
        job_runner._agent_message(ctx, False),
        job_runner._review_message(ctx, (), False, tmp_path / "rr"),
    ):
        assert f"Owner defaults: {expected_path} (absent)" in text
    payload = b'{"version":1,"defaults":[]}\n'
    expected_path.write_bytes(payload)
    short = hashlib.sha256(payload).hexdigest()[:12]
    for text in (
        job_runner._agent_message(ctx, False),
        job_runner._review_message(ctx, (), False, tmp_path / "rr"),
    ):
        assert f"Owner defaults: {expected_path} (sha256 {short})" in text


# --- the launch selects the model ------------------------------------------------------------

QUOTA = {
    "status": "failed",
    "error": {"message": "429 usage limit reached retry-after-ms=1000"},
}


def set_roles(env, **roles: str) -> None:
    """What each role means for the next launch; the fake omp reads this file when it starts."""
    env.state.mkdir(parents=True, exist_ok=True)
    (env.state / "roles.json").write_text(json.dumps(roles), encoding="utf-8")


def on_model(env, *selection: str) -> None:
    env.config = replace(env.config, omp_command=(*env.config.omp_command, *selection))


def launches(env) -> list[list[dict]]:
    """The commands each fake process received, in order, one list per launch."""
    runs: list[list[dict]] = []
    for entry in env.log():
        if "argv" in entry:
            runs.append([])
        else:
            runs[-1].append(entry)
    return runs


def lines(path: str) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


def test_first_open_binds_the_startup_model_before_the_prompt(env):
    env.scenario([work_turn()])
    assert env.run("work") == 0
    (run,) = launches(env)
    kinds = [entry["type"] for entry in run]
    assert kinds[:5] == ["negotiate_protocol", "get_state", "open_session", "get_state", "prompt"]
    opened = run[2]
    assert (opened["provider"], opened["modelId"]) == ("synthetic", "default")
    assert opened["sessionDir"] == str(env.folder() / "sessions" / "work")
    assert lines(env.get().session_path)[0]["model"] == "synthetic/default"


def test_resume_after_a_quota_failure_uses_the_new_launch_model_and_keeps_the_conversation(env):
    set_roles(env, **{"@task": "alpha/one"})
    on_model(env, "--model", "@task")
    env.scenario([QUOTA, work_turn()])
    assert env.run("work") == 1
    failed = env.get()
    assert failed.stage == "work" and "429" in failed.error
    before = Path(failed.session_path).read_bytes()
    assert lines(failed.session_path)[0]["model"] == "alpha/one"

    set_roles(env, **{"@task": "beta/two"})  # the configured role order changed
    assert env.run("work", session_dir=failed.session_dir) == 0
    job = env.get()
    assert job.stage == "check" and job.session_path == failed.session_path
    first, second = launches(env)
    assert [(e["provider"], e["modelId"]) for e in (first[2], second[2])] == [
        ("alpha", "one"),
        ("beta", "two"),
    ]
    assert [e["type"] for e in second][:5] == [
        "negotiate_protocol",
        "get_state",
        "open_session",
        "get_state",
        "prompt",
    ]
    after = Path(job.session_path).read_bytes()
    assert after.startswith(before)  # the same file, earlier messages untouched
    served = lines(job.session_path)
    assert served[0]["prompt"] == env.prompts()[0] and served[0]["model"] == "alpha/one"
    assert served[1] == {"model_change": "beta/two"}
    assert served[2]["prompt"] == env.prompts()[1] and served[2]["model"] == "beta/two"
    assert "interrupted" in env.prompts()[1]
    assert [x["resumed"] for x in job.events if x["type"] == "session_open"] == [False, True]


def test_a_resume_that_keeps_the_same_launch_model_adds_no_model_change(env):
    on_model(env, "--model", "gamma/three")
    env.scenario([{"die_after_content": True}, work_turn()])
    assert env.run("work") == 1
    assert env.run("work") == 0
    served = lines(env.get().session_path)
    assert [x.get("model") for x in served] == ["gamma/three", "gamma/three"]
    assert all("model_change" not in x for x in served)


def test_an_explicit_model_command_is_bound_as_given(env):
    set_roles(env, **{"@task": "alpha/one"})
    on_model(env, "--model", "delta/four")
    env.scenario([work_turn()])
    assert env.run("work") == 0
    opened = launches(env)[0][2]
    assert (opened["provider"], opened["modelId"]) == ("delta", "four")


def test_a_wrapper_command_reports_the_model_it_launched_with(env, tmp_path):
    set_roles(env, **{"@task": "alpha/one"})
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import os, sys\n"
        f"os.execv(sys.executable, [sys.executable, {str(FAKE)!r}, '--model', '@task',"
        " *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    env.config = replace(env.config, omp_command=(sys.executable, str(wrapper)))
    env.scenario([work_turn()])
    assert env.run("work") == 0
    assert launches(env)[0][2]["modelId"] == "one"
    assert env.log()[0]["argv"] == ["--model", "@task", "--mode", "rpc", "--no-ui"]


def test_work_fix_and_review_each_bind_their_own_launch_model(env):
    set_roles(env, **{"@task": "alpha/one", "@slow": "beta/two"})
    on_model(env, "--model", "@task")
    reviewer = (*env.config.omp_command[:-2], "--model", "@slow")
    env.config = replace(env.config, review_command=reviewer)
    env.scenario([work_turn(), {"reply": f"{FAIL}\nP1: revise"}, work_turn()])
    assert env.run("work") == 0
    assert env.run("check") == 0
    assert env.run("review") == 0
    assert env.get().stage == "fix"
    set_roles(env, **{"@task": "gamma/three", "@slow": "beta/two"})
    assert env.run("fix") == 0
    runs = launches(env)
    assert [[e["type"] for e in run].index("open_session") for run in runs] == [2, 2, 2]
    for run in runs:
        kinds = [e["type"] for e in run]
        assert kinds.index("get_state") < kinds.index("open_session") < kinds.index("prompt")
    assert [(run[2]["provider"], run[2]["modelId"]) for run in runs] == [
        ("alpha", "one"),
        ("beta", "two"),
        ("gamma", "three"),
    ]
    work = lines(env.get().session_path)
    assert [x.get("model") or x.get("model_change") for x in work] == [
        "alpha/one",
        "gamma/three",
        "gamma/three",
    ]


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        ({"startup_state": "missing"}, "get_state reported no model"),
        ({"startup_state": "malformed"}, "get_state reported no model"),
        ({"open": "reject"}, "Model not found"),
        ({"open": "cancel"}, "cancelled"),
        ({"open": "hang"}, "timed out"),
        ({"open": "ignore"}, "bound alpha/one, not beta/two"),
    ],
)
def test_a_failed_model_binding_stops_before_any_prompt_and_keeps_the_transcript(
    env, extra, problem
):
    env.config = replace(env.config, rpc_timeout=1)
    set_roles(env, **{"@task": "alpha/one"})
    on_model(env, "--model", "@task")
    env.scenario([QUOTA])
    assert env.run("work") == 1
    failed = env.get()
    before = Path(failed.session_path).read_bytes()
    events = [e["type"] for e in failed.events]

    set_roles(env, **{"@task": "beta/two"})
    env.scenario([work_turn()], **extra)
    assert env.run("work") == 1
    job = env.get()
    assert job.stage == "work" and problem in job.error
    if extra.get("open") == "reject":
        assert "open_session" in job.error and "provider and modelId" in job.error
    assert len(env.prompts()) == 1  # only the first attempt's prompt
    assert Path(failed.session_path).read_bytes() == before
    assert [e["type"] for e in job.events if e["type"] in {"step_exit", "session_open"}] == [
        e for e in events if e in {"step_exit", "session_open"}
    ]
    assert job.ended_at == 0


# --- terminal 429 ---------------------------------------------------------------------------


def limited(message: str) -> dict:
    return {"status": "error", "error": {"message": message}}


def test_a_429_with_a_deadline_keeps_the_saved_stage_and_spends_nothing(env):
    env.scenario([limited('429 {"type":"rate_limit_error"} retry-after-ms=60000')])
    before = time.time()
    assert env.run("work") == 1
    job = env.get()
    assert (job.stage, job.wait_reason, job.recoveries, job.reviews_used) == ("work", "", 0, 0)
    assert before + 60 <= job.retry_not_before <= time.time() + 60
    assert job.ended_at > 0 and "retry-after-ms=60000" in job.error
    event = next(e for e in job.events if e["type"] == "rate_limited")
    assert (event["stage"], event["retry_not_before"]) == ("work", job.retry_not_before)


@pytest.mark.parametrize("stage", ["work", "review"])
def test_a_pre_epoch_retry_date_is_saved_and_retried_once_by_the_scanner(env, stage):
    from taskwatch.job_scan import scan_jobs
    from tests.test_job_scan import FakeService

    env.scenario([limited("429 Retry-After: Wed, 31 Dec 1969 23:59:59 GMT")])
    if stage == "review":
        env.to_review()
    assert env.run(stage, recoveries=2) == 1
    limited_job = env.get()
    assert (limited_job.stage, limited_job.retry_not_before, limited_job.recoveries) == (
        stage,
        0.0,
        2,
    )
    assert limited_job.ended_at > 0 and limited_job.review_inflight == 0
    assert limited_job.reviews_used == 0
    service = FakeService()
    service.states[limited_job.unit] = "inactive"
    now = time.time()
    assert scan_jobs(env.jobs, env.config, now=now, boot_id="synthetic", service=service) == [
        limited_job.id
    ]
    assert len(service.started) == 1
    assert env.get().attempt == limited_job.attempt + 1
    assert env.get().reviews_used == int(stage == "review")
    assert scan_jobs(env.jobs, env.config, now=now + 1, boot_id="synthetic", service=service) == []


def test_a_429_without_a_hint_waits_for_the_owner_and_spends_nothing(env):
    env.scenario([limited("429 usage limit reached")])
    assert env.run("work", recoveries=2) == 1
    job = env.get()
    assert (job.stage, job.wait_reason, job.resume_stage) == ("user_wait", "rate_limit", "work")
    assert (job.recoveries, job.retry_not_before) == (2, 0.0)
    assert "usage limit reached" in job.error


def test_a_429_in_a_fix_spends_neither_a_recovery_nor_a_review(env):
    env.scenario([{"reply": f"{FAIL}\nP1: wrong"}, limited("429 retry-after-ms=1000")])
    env.to_review()
    assert env.run("review") == 0 and env.get().stage == "fix"
    assert env.run("fix") == 1
    job = env.get()
    assert (job.stage, job.recoveries, job.reviews_used) == ("fix", 0, 1)
    assert job.retry_not_before > 0


@pytest.mark.parametrize("hint", ["retry-after-ms=1000", "no hint"])
def test_a_429_in_a_review_refunds_only_its_own_reservation(env, hint):
    env.scenario([limited(f"429 {hint}"), {"reply": f"{PASS}\nNo findings."}])
    env.to_review()
    assert env.run("review", reviews_used=2, max_reviews=3) == 1  # earlier attempt: 1 spent
    job = env.get()
    assert (job.reviews_used, job.review_inflight, job.reviews) == (1, 0, ())
    assert job.stage == ("review" if "ms" in hint else "user_wait")
    assert job.recoveries == 0
    assert env.run("review") == 0  # the due retry reserves again and completes
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight) == ("deliver_pending", 2, 0)


def test_a_stale_attempt_cannot_refund_or_park_a_newer_attempt(env):
    env.to_review()
    old = env.put("review")
    env.put("review")  # the scanner relaunched: a newer attempt holds the reservation
    assert job_runner._rate_limited(env.jobs, old, "429", RateLimit(None)) == 2
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight) == ("review", 2, env.attempt)


def test_an_accepted_verdict_is_never_refunded(env):
    env.scenario([{"reply": f"{FAIL}\nP1: wrong"}])
    env.to_review()
    old = env.put("review")
    assert run_job(env.jobs, env.config, env.job.id, env.attempt) == 0
    assert job_runner._rate_limited(env.jobs, old, "429", RateLimit(None)) == 2
    job = env.get()
    assert (job.stage, job.reviews_used, len(job.reviews)) == ("fix", 1, 1)


@pytest.mark.parametrize("outcome", ["malformed", "rate_limit", "failure"])
def test_a_completed_attempt_cannot_call_again_or_accept_late_output(env, outcome):
    turns = (
        [{"reply": "not a verdict"}, {"reply": "still not a verdict"}]
        if outcome == "malformed"
        else [limited("429 retry-after-ms=60000" if outcome == "rate_limit" else "unsupported API")]
    )
    env.scenario([*turns, {"reply": PASS}])
    env.to_review()
    old = env.put("review")
    assert run_job(env.jobs, env.config, env.job.id, env.attempt) == (
        0 if outcome == "malformed" else 1
    )
    completed = env.get()
    prompts = len(env.prompts())
    assert completed.stage == "review" and completed.review_inflight == 0
    assert run_job(env.jobs, env.config, env.job.id, env.attempt) == 2
    assert job_runner._rate_limited(env.jobs, old, "429", RateLimit(None)) == 2
    assert env.get() == completed
    assert len(env.prompts()) == prompts


@pytest.mark.parametrize(
    "message",
    ["Unhandled API in mapOptionsForApi: web-search", "upstream said 429 once, retry-after-ms=999"],
)
def test_other_terminal_failures_stay_ordinary(env, message):
    env.scenario([limited(message)])
    assert env.run("work") == 1
    job = env.get()
    assert (job.stage, job.retry_not_before, job.ended_at) == ("work", 0.0, 0.0)
    assert message in job.error
    assert [e["type"] for e in job.events if e["type"] in {"step_failed", "rate_limited"}] == [
        "step_failed"
    ]


def test_a_reply_that_only_mentions_429_is_not_a_rate_limit(env):
    env.scenario([{"reply": f"{PASS}\nThe client retries after a 429, retry-after-ms=5"}])
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert (job.stage, job.retry_not_before) == ("deliver_pending", 0.0)

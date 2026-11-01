"""Review-only jobs: no work stage, a target to review, the existing verdict and round rules."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_delivery, job_runner
from taskwatch.job_cli import main as cli_main
from taskwatch.job_runner import run_job
from taskwatch.job_scan import scan_jobs
from taskwatch.jobs import MAX_FIX_ROUNDS, JobConfig, Jobs, JobState
from tests.test_job_runner import FAIL, FAKE, GIT, PASS
from tests.test_job_scan import FakeService


class Review:
    """A review-only job against a small repository, run through the real runner and a fake omp."""

    def __init__(self, tmp_path: Path, monkeypatch, **target) -> None:
        self.tmp = tmp_path
        self.jobs = Jobs(tmp_path / "root")
        self.config = JobConfig(omp_command=(sys.executable, str(FAKE)), rpc_timeout=5)
        self.state = tmp_path / "fake-state"
        monkeypatch.setenv("FAKE_OMP_SCENARIO", str(tmp_path / "scenario.json"))
        monkeypatch.setenv("FAKE_OMP_STATE", str(self.state))
        monkeypatch.setattr(job_runner, "SETTLE_POLL", 0.1)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("alpha\n", encoding="utf-8")
        for argv in (["init", "-q"], ["add", "a.txt"], ["commit", "-q", "-m", "first"]):
            subprocess.run([*GIT, "-C", str(self.repo), *argv], check=True, capture_output=True)
        (tmp_path / "intent.md").write_text("intent text\n", encoding="utf-8")
        (tmp_path / "spec.md").write_text("spec text\n", encoding="utf-8")
        self.job = self.jobs.submit(
            self.payload(**(target or {"review_target": {"files": ["a.txt"]}})), 1000.0
        )
        self.attempt = 0
        self.scenario([])

    def payload(self, **extra) -> dict:
        return {
            "receipt_key": "r1",
            "approval_source": "msg-1",
            "approval_scope": "review only",
            "prompt": "REVIEW THIS\nline two",
            "review": "source",
            "target": str(self.repo),
            "intent": str(self.tmp / "intent.md"),
            "spec": str(self.tmp / "spec.md"),
            **extra,
        }

    def scenario(self, turns: list) -> None:
        (self.state / "turn.txt").unlink(missing_ok=True)
        (self.tmp / "scenario.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")

    def get(self) -> JobState:
        return self.jobs.load()[self.job.id]

    def run(self, stage: str = "review", **changes) -> int:
        self.attempt += 1
        with self.jobs.state.lock():
            job = replace(self.get(), stage=stage, attempt=self.attempt, unit="u", **changes)
            self.jobs.save(job, 2000.0 + self.attempt)
        return run_job(self.jobs, self.config, self.job.id, self.attempt)

    def prompts(self) -> list[str]:
        rows = (self.state / "log.jsonl").read_text().splitlines()
        return [r["message"] for r in map(json.loads, rows) if r.get("type") == "prompt"]


@pytest.fixture
def review(tmp_path, monkeypatch):
    return Review(tmp_path, monkeypatch)


# --- submission -----------------------------------------------------------------------------


def test_review_only_submission_has_no_work_contract(review):
    job = review.get()
    assert (job.stage, job.resume_stage) == ("queued", "review")
    assert job.to_json()["review_target"] == {"files": ["a.txt"]}
    assert (job.outputs, job.check_command, job.checklist, job.progress) == (("a.txt",), (), "", "")


def test_commit_target_is_pinned_to_a_full_sha(tmp_path, monkeypatch):
    other = Review(tmp_path, monkeypatch, review_target={"commit": "HEAD"})
    sha = subprocess.run(
        ["git", "-C", str(other.repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert other.get().review_target["commit"] == sha
    assert other.get().outputs == ()


@pytest.mark.parametrize(
    "extra",
    [
        {"review_target": {"files": ["missing.txt"]}},
        {"review_target": {"files": ["../a.txt"]}},
        {"review_target": {"files": []}},
        {"review_target": {"commit": "no-such-rev"}},
        {"review_target": {"commit": "--all"}},
        {"review_target": {"files": ["a.txt"], "commit": "HEAD"}},
        {"review_target": {}},
        {"review_target": {"files": ["a.txt"]}, "outputs": ["a.txt"]},
        {"review_target": {"files": ["a.txt"]}, "check_command": ["true"]},
    ],
)
def test_review_only_submission_refuses_a_bad_target(review, extra):
    with pytest.raises(ValueError):
        review.jobs.submit(review.payload(receipt_key="r2", **extra), 1001.0)


def test_a_work_job_still_needs_its_work_contract(review):
    with pytest.raises(ValueError, match="missing or unknown"):
        review.jobs.submit(review.payload(receipt_key="r3"), 1001.0)


# --- running --------------------------------------------------------------------------------


def test_pass_goes_to_delivery_without_any_work_or_check(review):
    review.scenario([{"reply": f"{PASS}\nNo findings."}])
    assert review.run() == 0
    job = review.get()
    assert job.stage == "deliver_pending" and job.checks == ()
    record = job.reviews[-1]
    assert (record["ok"], record["final"], record["round"]) == (True, True, 1)
    assert job.workspace == str(review.repo)
    assert not (review.jobs.folder(job) / "WORKFLOW.md").exists()
    assert not (review.jobs.folder(job) / "worktree").exists()
    (message,) = review.prompts()
    assert "Review target: files a.txt" in message and "Check output" not in message
    assert "REVIEW THIS\nline two" in message and '{"verdict": "PASS" or "FAIL"' in message


def test_fail_is_the_result_of_a_review_only_job_not_a_reason_to_fix(review):
    review.scenario([{"reply": f"{FAIL}\nP1: wrong"}])
    assert review.run() == 0
    job = review.get()
    assert (job.stage, job.fix_rounds) == ("deliver_pending", 0)
    record = job.reviews[-1]
    assert (record["verdict"], record["ok"], record["final"]) == ("FAIL", False, True)
    assert "P1: wrong" in record["body"]


def test_an_answer_without_a_verdict_costs_a_round_and_reviews_again(review):
    review.scenario([{"reply": "looks fine"}, {"reply": PASS}])
    assert review.run() == 0
    job = review.get()
    assert (job.stage, job.fix_rounds, job.reviews[-1]["final"]) == ("review", 1, False)
    assert "exactly one verdict line" in job.error
    assert review.run("review", ended_at=0.0) == 0
    job = review.get()
    assert (job.stage, [r["round"] for r in job.reviews]) == ("deliver_pending", [1, 2])
    assert "round 2" in review.prompts()[-1] and "not accepted" in review.prompts()[-1]


def test_rounds_are_capped_like_other_jobs(review):
    review.scenario([{"reply": "no verdict"}])
    assert review.run("review", fix_rounds=MAX_FIX_ROUNDS) == 0
    job = review.get()
    assert (job.stage, job.wait_reason) == ("user_wait", "review_exhausted")


def test_a_target_that_changed_during_the_review_is_not_accepted(review):
    path = review.repo / "a.txt"
    review.scenario(
        [{"run": [[sys.executable, "-c", f"open({str(path)!r}, 'a').write('x')"]], "reply": PASS}]
    )
    assert review.run() == 0
    job = review.get()
    assert (job.stage, job.reviews[-1]["final"]) == ("review", False)
    assert "the result changed during the review" in job.error


def test_a_commit_target_is_read_through_git_and_needs_the_commit_to_exist(tmp_path, monkeypatch):
    other = Review(tmp_path, monkeypatch, review_target={"commit": "HEAD"})
    other.scenario([{"reply": PASS}])
    assert other.run() == 0
    job = other.get()
    sha = job.review_target["commit"]
    assert f"commit {sha} of the repository" in other.prompts()[0]
    assert job.fingerprint == sha and job.reviews[-1]["final"]


def test_review_command_receives_the_job_guideline_copy_and_review(review):
    review.config = replace(
        review.config,
        review_command=(
            *review.config.omp_command,
            "--docs",
            "{guidelines}",
            "--review",
            "{review}",
        ),
    )
    review.scenario([{"reply": PASS}])
    assert review.run() == 0
    argv = next(
        json.loads(x)["argv"]
        for x in (review.state / "log.jsonl").read_text().splitlines()
        if "argv" in x
    )
    folder = review.jobs.folder(review.get())
    assert argv[:4] == ["--docs", str(folder / "review-docs"), "--review", "source"]


# --- scheduling and delivery ----------------------------------------------------------------


def test_the_scan_starts_a_review_only_job_at_its_review_stage(review):
    service = FakeService()
    started = scan_jobs(review.jobs, review.config, now=1100.0, boot_id="b", service=service)
    assert started == [review.job.id]
    assert review.get().stage == "review"


def test_approval_before_start_resumes_at_review(review):
    review.jobs.save(replace(review.get(), approval_required=True), 1100.0)
    scan_jobs(review.jobs, review.config, now=1101.0, boot_id="b", service=FakeService())
    job = review.get()
    assert (job.stage, job.resume_stage) == ("approval_wait", "review")
    cli_main(["--root", str(review.tmp / "root"), "approve", job.id, "--reason", "ok"])
    scan_jobs(review.jobs, review.config, now=1102.0, boot_id="b", service=FakeService())
    assert review.get().stage == "review"


def deliver(review, monkeypatch) -> tuple[JobState, str]:
    sent = []

    def fake_send(command, fd):
        sent.append(Path(command[-1]).read_text(encoding="utf-8"))
        return {
            "status": "success",
            "exit_code": 0,
            "message_id": "m",
            "stdout": "",
            "stderr": "",
            "error": "",
        }

    monkeypatch.setattr(job_delivery, "_send", fake_send)
    config = replace(review.config, deliver_command=("send", "{message}"))
    job = job_delivery.deliver_job(review.jobs, config, replace(review.get(), unit=""), 3000.0)
    return job, sent[0] if sent else ""


@pytest.mark.parametrize("reply, verdict", [(PASS, "PASS"), (FAIL, "FAIL")])
def test_delivery_sends_the_verdict_whatever_it_is(review, monkeypatch, reply, verdict):
    review.scenario([{"reply": f"{reply}\nbody"}])
    assert review.run() == 0
    job, text = deliver(review, monkeypatch)
    assert job.stage == "done"
    assert f"Verdict: {verdict}" in text and "Reviewed:" in text and "Outputs:" not in text


def test_a_target_changed_after_the_review_is_reviewed_again_not_delivered(review, monkeypatch):
    review.scenario([{"reply": PASS}])
    assert review.run() == 0
    (review.repo / "a.txt").write_text("changed\n", encoding="utf-8")
    job, text = deliver(review, monkeypatch)
    assert (job.stage, text) == ("review", "")

"""`needs` against synthetic ledgers written by the real Jobs store."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.job_watch import needs
from taskwatch.jobs import Jobs
from tests.test_jobs import payload

BRIEF = """## Why
x
## What changed and why
- y. See a.py:1
## Choices made
| a | b | c | d | agent |
## Decisions needed
1. Pick A or B
2. Pick C or D
## Where to look
a.py:1
"""


class Ledger:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "root"
        self.jobs = Jobs(self.root)
        self.clock = 1000.0
        self.count = 0

    def add(self, **changes) -> str:
        self.count += 1
        extra = changes.pop("submit", {})
        job = self.jobs.submit(payload(self.tmp, receipt=f"r{self.count}") | extra, self.clock)
        self.set(job.id, **changes)
        return job.id

    def get(self, job_id: str):
        return self.jobs.load()[job_id]

    def set(self, job_id: str, **changes) -> None:
        if changes:
            self.clock += 1
            self.jobs.save(replace(self.get(job_id), **changes), self.clock)

    def event(self, job_id: str, kind: str, **changes) -> None:
        job = self.get(job_id)
        events = (*job.events, {"type": kind, "at": self.clock})
        self.set(job_id, events=events, **changes)

    def size(self) -> int:
        return (self.jobs.state.dir / "jobs.jsonl").stat().st_size

    def watch(self, cursor: int, sleep):
        return needs(
            self.root,
            cursor=cursor,
            wait=True,
            timeout=1000,
            interval=5,
            sleep=sleep,
            clock=lambda: 0,
        )

    def list(self, **kw):
        return needs(self.root, sleep=lambda s: None, **kw)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path)


def cursor_of(text: str) -> int:
    return int(next(line for line in text.splitlines() if line.startswith("cursor: "))[8:])


def test_the_list_has_a_section_per_kind_of_need(ledger: Ledger) -> None:
    design = ledger.add(stage="done", review="design")
    folder = ledger.jobs.folder(ledger.get(design))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "brief.md").write_text(BRIEF, encoding="utf-8")
    gate = ledger.add(
        stage="approval_wait", wait_reason="approval_required", submit={"predecessors": [design]}
    )
    cancelled = ledger.add(stage="cancelled")
    blocked = ledger.add(stage="queued", submit={"predecessors": [cancelled]})
    review = {"verdict": "FAIL", "findings": [{"severity": "P1", "rule": "K-9", "finding": "F1"}]}
    stuck = ledger.add(
        stage="user_wait", wait_reason="review_exhausted", reviews=(review, review, review)
    )
    ledger.add(stage="work")
    code, text = ledger.list()
    root = ledger.root.resolve()
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "needs: 1 need you, 1 gate, 1 blocked"
    assert lines[2:4] == ["GATE", f"job {gate} research  after job {design} (design) done"]
    assert f"  brief: taskwatch --root {root} brief {design}" in lines
    approve = f"taskwatch --root {root} approve {gate} --reason <your words>"
    assert f"  decisions: 2   then: {approve}" in lines
    assert any(
        line.startswith(f"job {stuck} research user_wait review_exhausted  round 3; open P1 1")
        and line.endswith("repeated rules: K-9")
        for line in lines
    )
    assert f"job {blocked} queued behind cancelled job {cancelled}" in lines
    assert "FINISHED SINCE THE CURSOR" not in lines


def test_finished_jobs_appear_only_after_a_cursor_and_carry_their_decisions(
    ledger: Ledger,
) -> None:
    done = ledger.add(stage="done", review="design")
    review = {"verdict": "PASS", "findings": [{"severity": "P2", "rule": "D-1", "finding": "F1"}]}
    ledger.set(done, reviews=(review,))
    folder = ledger.jobs.folder(ledger.get(done))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "brief.md").write_text(BRIEF, encoding="utf-8")
    _, before = ledger.list()
    cursor = cursor_of(before)
    later = ledger.add(stage="done")
    code, text = ledger.list(cursor=cursor)
    assert code == 0 and "needs: 0 need you, 0 gate, 0 blocked, 1 finished since the cursor" in text
    assert f"job {later} research done none; decisions: 0; open none" in text
    assert done not in text
    code, text = ledger.list(cursor=0)
    assert f"job {done} design done PASS; decisions: 2; open P2 1" in text


def test_wait_ignores_what_was_already_waiting_at_the_cursor(ledger: Ledger) -> None:
    first = ledger.add(stage="user_wait", wait_reason="review_exhausted")
    cursor = ledger.size()
    seen = []

    def sleep(seconds: float) -> None:
        seen.append(seconds)
        if len(seen) == 2:
            ledger.add(stage="approval_wait", wait_reason="approval_required")

    code, text = ledger.watch(cursor, sleep)
    assert (code, len(seen)) == (0, 2)
    assert "1 need you, 1 gate" in text and first in text


def test_wait_times_out_with_code_3(ledger: Ledger) -> None:
    ledger.add(stage="user_wait", wait_reason="review_exhausted")
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    code, text = needs(
        ledger.root,
        cursor=ledger.size(),
        wait=True,
        timeout=12,
        interval=5,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert code == 3 and "nothing new" in text


def test_a_job_that_stops_again_for_the_same_reason_wakes_the_watcher(ledger: Ledger) -> None:
    job_id = ledger.add(stage="user_wait", wait_reason="review_exhausted")
    cursor = ledger.size()
    calls = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 1:  # the same stop saved again: no new item
            ledger.set(job_id, error="more detail")
        elif len(calls) == 2:  # answered with resume, then stopped for the same reason
            ledger.event(job_id, "resume", stage="queued", wait_reason="")
            ledger.set(job_id, stage="user_wait", wait_reason="review_exhausted")

    code, _ = ledger.watch(cursor, sleep)
    assert (code, len(calls)) == (0, 2)


def test_a_cursor_beyond_the_ledger_lists_everything_again(ledger: Ledger) -> None:
    ledger.add(stage="user_wait", wait_reason="review_exhausted")
    code, text = ledger.list(cursor=ledger.size() + 10_000, wait=True)
    assert code == 0 and "1 need you" in text


def test_a_new_blocked_job_and_a_new_finish_wake_the_watcher(ledger: Ledger) -> None:
    ledger.add(stage="work")
    cancelled = ledger.add(stage="cancelled")
    cursor = ledger.size()
    ledger.add(stage="queued", submit={"predecessors": [cancelled]})
    code, text = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and "1 blocked" in text
    cursor = cursor_of(text)
    ledger.add(stage="done")
    code, text = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and "1 finished since the cursor" in text


def test_nothing_is_written(ledger: Ledger) -> None:
    ledger.add(stage="user_wait", wait_reason="review_exhausted")
    before = {p: p.read_bytes() for p in ledger.jobs.state.dir.iterdir() if p.is_file()}
    names = sorted(p.name for p in ledger.jobs.state.dir.iterdir())
    ledger.list(cursor=0)
    after = {p: p.read_bytes() for p in ledger.jobs.state.dir.iterdir() if p.is_file()}
    assert after == before and sorted(p.name for p in ledger.jobs.state.dir.iterdir()) == names


def test_wait_without_a_cursor_is_a_usage_error(ledger: Ledger, capsys) -> None:
    assert main(["--root", str(ledger.root), "needs", "--wait"]) == 2
    assert "--from" in capsys.readouterr().err


def test_a_corrupt_snapshot_of_a_listed_job_exits_1(ledger: Ledger, capsys) -> None:
    ledger.add(stage="user_wait", wait_reason="review_exhausted")
    path = ledger.jobs.state.dir / "jobs.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["job"]["error"] = "tampered"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert main(["--root", str(ledger.root), "needs"]) == 1
    assert "corrupt job snapshot" in capsys.readouterr().err


def test_the_cli_prints_the_list(ledger: Ledger, capsys) -> None:
    job_id = ledger.add(stage="user_wait", wait_reason="delivery_unknown")
    assert main(["--root", str(ledger.root), "needs"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "needs: 1 need you, 0 gate, 0 blocked"
    assert f"job {job_id} research user_wait delivery_unknown" in out


def test_a_gate_after_a_design_with_picks_shows_the_reply_template(ledger: Ledger) -> None:
    work = ledger.tmp / "design-work"
    work.mkdir()
    pick = {"id": "a", "options": [{"key": "A"}, {"key": "B"}], "recommended": "A"}
    (work / "choices.json").write_text(json.dumps({"picks": [pick, pick]}), encoding="utf-8")
    design = ledger.add(stage="done", review="design", workspace=str(work))
    gate = ledger.add(
        stage="approval_wait", wait_reason="approval_required", submit={"predecessors": [design]}
    )
    _, text = ledger.list()
    root = ledger.root.resolve()
    approve = f"taskwatch --root {root} approve {gate} --reason <your words>"
    assert f"  decisions: 2 picks   reply: ok | <id>=<key> | text   then: {approve}" in text

"""`needs` against synthetic ledgers written by the real Jobs store."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.job_watch import needs
from taskwatch.job_workspace import settle
from taskwatch.jobs import Jobs, _digest
from tests.git_fixture import git, make_repo
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
## Card
Synthetic result; decisions are listed above.
- Complete the synthetic result.
"""

CLEAN_BRIEF = BRIEF.replace("1. Pick A or B\n2. Pick C or D", "none")
CLEAN_PASS = {"verdict": "PASS", "ok": True, "findings": [], "problems": []}


def accepted_done(ledger, **changes):
    ident = ledger.add(stage="done", reviews=(CLEAN_PASS,), **changes)
    folder = ledger.jobs.folder(ledger.get(ident))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.py").write_text("value = 1\n", encoding="utf-8")
    (folder / "brief.md").write_text(CLEAN_BRIEF, encoding="utf-8")
    return ident


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
        return cursor_of(needs(self.root)[1])

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
    ident = ledger.add(stage="user_wait", wait_reason="review_exhausted")
    path = ledger.jobs.folder(ledger.get(ident)) / "state.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["job"]["error"] = "tampered"
    path.write_text(json.dumps(row), encoding="utf-8")
    assert main(["--root", str(ledger.root), "needs"]) == 1


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


def test_a_known_rate_limit_wait_is_not_a_human_need_but_a_parked_deadline_is_shown(
    ledger: Ledger,
) -> None:
    cooling = ledger.add(stage="review", retry_not_before=5000.0, ended_at=1.0)
    exhausted = ledger.add(
        stage="user_wait", wait_reason="review_exhausted", retry_not_before=5000.0
    )
    code, text = ledger.list()
    assert code == 0
    assert text.splitlines()[0] == "needs: 1 need you, 0 gate, 0 blocked"
    assert cooling not in text
    assert f"job {exhausted} research user_wait review_exhausted" in text
    assert "  retry not before: 1970-01-01T01:23:20Z (resume does not skip it)" in text
    plain = ledger.add(stage="user_wait", wait_reason="rate_limit")
    assert "retry not before" not in "\n".join(
        line for line in ledger.list()[1].splitlines() if plain in line
    )


def test_a_finite_deadline_outside_the_calendar_range_is_still_shown(ledger: Ledger) -> None:
    identifier = ledger.add(stage="user_wait", wait_reason="approval", retry_not_before=1e20)
    code, text = ledger.list()
    assert code == 0 and identifier in text
    assert "retry not before: epoch 1e+20 seconds (resume does not skip it)" in text


def test_needs_reads_a_job_stored_before_review_reservations(ledger: Ledger) -> None:
    job_id = ledger.add(stage="user_wait", wait_reason="review_exhausted")
    path = ledger.jobs.folder(ledger.get(job_id)) / "state.json"
    obj = json.loads(path.read_text(encoding="utf-8"))
    old = {
        k: v
        for k, v in obj["job"].items()
        if k not in {"max_reviews", "reviews_used", "review_cycle_start", "review_inflight"}
        and k != "retry_not_before"
    }
    old["fix_rounds"] = 3
    obj["job"], obj["sha256"] = old, _digest({"revision": obj["revision"], "job": old})
    path.write_text(json.dumps(obj), encoding="utf-8")
    code, text = ledger.list()
    assert code == 0 and f"job {job_id} research user_wait review_exhausted" in text


@pytest.mark.parametrize("marker", ["ok", "final"])
def test_quiet_wait_passes_clean_accepted_completion_until_a_gate(ledger: Ledger, marker) -> None:
    cursor = ledger.size()
    done = accepted_done(ledger)
    if marker == "final":
        ledger.set(done, review_target={"files": ("result.txt",)})
    ledger.set(done, reviews=({"verdict": "PASS", marker: True, "findings": []},))
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        ledger.add(stage="approval_wait", wait_reason="approval_required")

    code, text = ledger.watch(cursor, sleep)
    assert code == 0 and len(calls) == 1
    assert "1 gate" in text and done in text
    assert "FINISHED SINCE THE CURSOR" in text


def test_include_done_wakes_for_clean_accepted_completion(ledger: Ledger) -> None:
    cursor = ledger.size()
    done = accepted_done(ledger)
    code, text = ledger.list(cursor=cursor, wait=True, include_done=True)
    assert code == 0 and done in text and "FINISHED SINCE THE CURSOR" in text


@pytest.mark.parametrize(
    "case",
    [
        "review_only_fail",
        "open_finding",
        "decision",
        "design_pick",
        "missing_verdict",
        "unreadable_brief",
        "missing_brief",
        "malformed_brief",
        "malformed_evidence",
        "unaccepted_pass",
        "rejected_pass",
    ],
)
def test_completion_uncertainty_wakes_quiet_wait(ledger: Ledger, case) -> None:
    cursor = ledger.size()
    ident = accepted_done(ledger)
    folder = ledger.jobs.folder(ledger.get(ident))
    review = dict(CLEAN_PASS)
    if case == "review_only_fail":
        ledger.set(ident, review_target={"files": ("result.txt",)})
        review = {"verdict": "FAIL", "final": True, "findings": []}
    elif case == "open_finding":
        review["findings"] = [{"severity": "P1", "rule": "S-1", "finding": "Open issue"}]
    elif case == "decision":
        (folder / "brief.md").write_text(BRIEF, encoding="utf-8")
    elif case == "design_pick":
        choices = {
            "picks": [
                {
                    "id": "storage",
                    "question": "Which store?",
                    "options": [
                        {"key": "A", "label": "First", "evidence": "https://a.invalid"},
                        {"key": "B", "label": "Second", "evidence": "https://b.invalid"},
                    ],
                    "recommended": "A",
                    "why": "Both satisfy the contract.",
                    "reversible": "cheap",
                    "affects": "storage",
                }
            ],
            "decided": [],
            "existing_tools": [
                {"need": "storage", "searched": ["one", "two", "three"], "found": []}
            ],
        }
        (folder / "choices.json").write_text(json.dumps(choices), encoding="utf-8")
        ledger.set(ident, review="design", workspace=str(folder), outputs=("choices.json",))
    elif case == "missing_verdict":
        review.pop("verdict")
    elif case == "unreadable_brief":
        (folder / "brief.md").write_bytes(b"\xff")
    elif case == "missing_brief":
        (folder / "brief.md").unlink()
    elif case == "malformed_brief":
        (folder / "brief.md").write_text("No required sections.\n", encoding="utf-8")
    elif case == "malformed_evidence":
        review["findings"] = "not a findings list"
    elif case == "unaccepted_pass":
        review.pop("ok")
    elif case == "rejected_pass":
        review["problems"] = ["Synthetic review contract violation"]
    ledger.set(ident, reviews=(review,))
    code, text = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and ident in text and "FINISHED SINCE THE CURSOR" in text


def test_latest_accepted_pass_resolves_historical_findings(ledger: Ledger) -> None:
    cursor = ledger.size()
    ident = accepted_done(ledger)
    historical = {
        "verdict": "FAIL",
        "findings": [{"severity": "P1", "rule": "S-1", "finding": "Resolved issue"}],
    }
    ledger.set(ident, reviews=(historical, CLEAN_PASS))
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        ledger.add(stage="user_wait", wait_reason="approval_required")

    code, text = ledger.watch(cursor, sleep)
    assert code == 0 and len(calls) == 1
    assert f"job {ident} research done PASS; decisions: 0; open none" in text


def test_quiet_timeout_does_not_consume_clean_completion_cursor(ledger: Ledger) -> None:
    cursor = ledger.size()
    done = accepted_done(ledger)
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, text = needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == 3 and "cursor:" not in text
    code, replay = ledger.list(cursor=cursor, wait=True, include_done=True)
    assert code == 0 and done in replay


@pytest.mark.parametrize("flood", ["clean", "old_waits", "both"])
def test_wait_render_cap_keeps_the_new_trigger_visible(ledger: Ledger, flood) -> None:
    if flood in {"old_waits", "both"}:
        for _ in range(22):
            ledger.add(stage="user_wait", wait_reason="old_stop")
    cursor = ledger.size()
    if flood in {"clean", "both"}:
        for _ in range(22):
            accepted_done(ledger)
    trigger = ledger.add(stage="user_wait", wait_reason="new_stop")
    code, text = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and f"job {trigger} research user_wait new_stop" in text
    first = next(line for line in text.splitlines() if line.startswith("job "))
    assert first.startswith(f"job {trigger} ")


@pytest.mark.parametrize("damage", [None, "missing", "malformed"])
def test_design_completion_requires_valid_choices_without_picks(ledger: Ledger, damage) -> None:
    cursor = ledger.size()
    ident = accepted_done(ledger)
    folder = ledger.jobs.folder(ledger.get(ident))
    ledger.set(ident, review="design", workspace=str(folder), outputs=("choices.json",))
    if damage != "missing":
        content = (
            "not json"
            if damage
            else json.dumps(
                {
                    "picks": [],
                    "decided": [],
                    "existing_tools": [
                        {"need": "storage", "searched": ["one", "two", "three"], "found": []}
                    ],
                }
            )
        )
        (folder / "choices.json").write_text(content, encoding="utf-8")
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        ledger.add(stage="approval_wait", wait_reason="approval_required")

    code, text = ledger.watch(cursor, sleep)
    assert code == 0 and ident in text
    assert len(calls) == (1 if damage is None else 0)


def committed_completion(
    ledger: Ledger, pointer: str, links: dict[str, str] | None = None
) -> tuple[str, Path]:
    repo = make_repo(ledger.tmp)
    work = ledger.tmp / "result-work"
    git(repo, "worktree", "add", "-b", "result", str(work))
    (work / "new_result.py").write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    (work / "out.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git(work, "add", "new_result.py", "out.txt")
    for name, target in (links or {}).items():
        link = work / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        git(work, "add", name)
    git(work, "commit", "-m", "Add synthetic result")
    sha = git(work, "rev-parse", "HEAD")
    ident = ledger.add(
        review="code",
        stage="done",
        target=str(repo),
        workspace=str(work),
        workspace_state="active",
        branch="result",
        branch_state="kept",
        fingerprint=sha,
        reviews=({**CLEAN_PASS, "fingerprint": sha},),
    )
    folder = ledger.jobs.folder(ledger.get(ident))
    (folder / "brief.md").write_text(CLEAN_BRIEF.replace("a.py:1", pointer), encoding="utf-8")
    (folder / "evidence.txt").write_text("Synthetic record\n", encoding="utf-8")
    return ident, repo


@pytest.mark.parametrize("pointer", ["new_result.py:2", "out.txt:3", "evidence.txt:1"])
@pytest.mark.parametrize("cleaned", [False, True])
def test_clean_code_completion_uses_accepted_commit_after_settlement(ledger, pointer, cleaned):
    cursor = ledger.size()
    ident, _ = committed_completion(ledger, pointer)
    job = ledger.get(ident)
    if cleaned:
        changes = settle(job, dict(os.environ), {job.target})
        assert changes["workspace_state"] == "removed" and not Path(job.workspace).exists()
        ledger.set(ident, **changes)
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, text = needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == 3 and "cursor:" not in text


@pytest.mark.parametrize("damage", ["missing_file", "line_range", "missing_commit", "malformed"])
def test_removed_code_completion_still_wakes_for_invalid_brief_evidence(ledger, damage):
    cursor = ledger.size()
    pointer = {
        "missing_file": "missing.py:1",
        "line_range": "out.txt:4",
        "missing_commit": "out.txt:3",
        "malformed": "out.txt:3",
    }[damage]
    ident, repo = committed_completion(ledger, pointer)
    job = ledger.get(ident)
    changes = settle(job, dict(os.environ), {job.target})
    assert changes["workspace_state"] == "removed"
    ledger.set(ident, **changes)
    # A newer target checkout cannot validate a pointer absent from the accepted result.
    (repo / "missing.py").write_text("synthetic = 1\n", encoding="utf-8")
    (repo / "out.txt").write_text("line\n" * 10, encoding="utf-8")
    git(repo, "add", "missing.py", "out.txt")
    git(repo, "commit", "-m", "Change synthetic target")
    if damage == "missing_commit":
        ledger.set(ident, fingerprint="0" * 40)
    elif damage == "malformed":
        (ledger.jobs.folder(job) / "brief.md").write_text("Invalid brief\n", encoding="utf-8")
    code, text = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and ident in text


def test_code_completion_without_pointers_still_requires_a_readable_result_commit(ledger):
    cursor = ledger.size()
    ident, _ = committed_completion(ledger, "new_result.py:2")
    job = ledger.get(ident)
    path = ledger.jobs.folder(job) / "brief.md"
    text = CLEAN_BRIEF.replace("- y. See a.py:1", "Complete synthetic result.")
    path.write_text(text.replace("a.py:1", "Result commit."), encoding="utf-8")
    ledger.set(ident, fingerprint="0" * 40)
    code, report = ledger.list(cursor=cursor, wait=True)
    assert code == 0 and ident in report


@pytest.mark.parametrize("cleaned", [False, True])
@pytest.mark.parametrize(
    ("target", "extra", "expected"),
    [
        ("new_result.py", {}, 3),
        ("nested/target.py", {"nested/target.py": "../new_result.py"}, 3),
        ("nested/new_result.py", {"nested": "."}, 3),
        ("../outside.py", {}, 0),
        ("/outside.py", {}, 0),
        ("missing.py", {}, 0),
        ("loop.py", {"loop.py": "linked.py"}, 0),
    ],
)
def test_completion_symlink_pointers(ledger, cleaned, target, extra, expected):
    from taskwatch.brief import problems

    cursor = ledger.size()
    ident, _ = committed_completion(ledger, "linked.py:2", {"linked.py": target, **extra})
    job = ledger.get(ident)
    folder = ledger.jobs.folder(job)
    if expected == 3:
        assert problems(folder / "brief.md", (Path(job.workspace), folder)) == []
    else:
        (folder / "linked.py").write_text("first\nsecond\n", encoding="utf-8")
    if cleaned:
        ledger.set(ident, **settle(job, dict(os.environ), {job.target}))
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, text = needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == expected
    assert ("cursor:" not in text) if expected == 3 else (ident in text)


def test_job_folder_evidence_under_a_result_file_name_stays_quiet(ledger):
    cursor = ledger.size()
    ident, _ = committed_completion(ledger, "out.txt/evidence.txt:1")
    job = ledger.get(ident)
    folder = ledger.jobs.folder(job)
    (folder / "out.txt").mkdir()
    (folder / "out.txt" / "evidence.txt").write_text("Synthetic record\n", encoding="utf-8")
    ledger.set(ident, **settle(job, dict(os.environ), {job.target}))
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, text = needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == 3 and "cursor:" not in text


def test_needs_exposes_landing_stop_and_retained_merge_install_facts(ledger: Ledger) -> None:
    identifier = ledger.add(
        stage="user_wait",
        resume_stage="land",
        wait_reason="land_install_failed",
        error="install acknowledgement lost",
        land_state={
            "phase": "install",
            "pr": {"number": 7, "url": "https://forge.invalid/acme/repo/pull/7"},
            "S": "a" * 40,
            "tree_verified": True,
            "install": {"status": "unknown"},
        },
    )
    code, text = ledger.list()
    assert code == 0
    assert f"job {identifier} research user_wait land_install_failed" in text
    assert "PR #7 https://forge.invalid/acme/repo/pull/7" in text
    assert f"merged {'a' * 40}" in text
    assert "tree verified" in text
    assert "install unknown" in text
    assert "install acknowledgement lost" in text
    assert f"resume {identifier}" in text

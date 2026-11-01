"""Block until a job needs attention, and list what waits for the owner.

Read only: no lock, no write to the state folder."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import choices
from .brief import count_findings, decision_count
from .jobs import JobState, _digest, _upgrade_record, _validate_record
from .kinds import kind_label
from .state import RECORD_VERSION, RecordError, state_dir

STOP_STAGES = frozenset({"approval_wait", "user_wait", "deliver_pending", "done", "cancelled"})
ATTENTION, TIMED_OUT, UNKNOWN = 0, 3, 2


class _Tail:
    """Follows jobs.jsonl by byte offset and keeps the newest snapshot of one job.

    Appends are whole lines and a torn tail has no newline, so a read without the writers'
    lock only ever sees committed lines. After the first pass a poll parses new lines only.
    """

    def __init__(self, path: Path, job_id: str) -> None:
        self.path, self.job_id = path, job_id
        self.offset = 0
        self.job: dict | None = None

    def poll(self) -> dict | None:
        try:
            with self.path.open("rb") as handle:
                size = handle.seek(0, 2)
                if size < self.offset:
                    self.offset, self.job = 0, None
                handle.seek(self.offset)
                data = handle.read(size - self.offset)
        except FileNotFoundError:
            return self.job
        except OSError as exc:
            raise RecordError(f"{self.path.name}: unreadable") from exc
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise RecordError(f"{self.path.name}: not json") from exc
            if isinstance(row, dict) and row.get("version") != RECORD_VERSION:
                raise RecordError(f"{self.path.name}: unknown version")
            job = row.get("job") if isinstance(row, dict) else None
            if not isinstance(job, dict):
                raise RecordError(f"{self.path.name}: corrupt job snapshot")
            if job.get("id") != self.job_id:
                continue
            if row.get("sha256") != _digest(job):
                raise RecordError(f"{self.path.name}: corrupt job snapshot")
            job = _upgrade_record(job)
            _validate_record(job)
            self.job = job
        self.offset += end
        return self.job


def watch(
    root: Path,
    job_id: str,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Return (exit code, report). 0 = needs attention, 3 = time limit, 2 = unknown job."""
    tail = _Tail(state_dir(root.resolve()) / "jobs.jsonl", job_id)
    deadline = clock() + timeout
    while True:
        data = tail.poll()
        if data is None:
            return UNKNOWN, f"taskwatch: unknown job {job_id}"
        if data["stage"] in STOP_STAGES:
            return ATTENTION, _report(JobState(**data), root.resolve())
        remaining = deadline - clock()
        if remaining <= 0:
            return TIMED_OUT, (
                f"job {job_id}: still {data['stage']} after the watcher's time limit; "
                f"nothing to report. Start a new watcher if the work is still wanted."
            )
        sleep(min(interval, remaining))


def _report(job: JobState, root: Path) -> str:
    folder = state_dir(root) / "jobs" / job.id
    lines = [f"job {job.id}: {job.stage}"]
    if job.wait_reason:
        lines.append(f"reason: {_line(job.wait_reason)}")
    if job.error:
        lines.append("error: recorded; it may hold command output, so read it with show")
    review = job.reviews[-1] if job.reviews else None
    if review and review.get("verdict"):
        lines.append(f"verdict: {_line(review['verdict'])}")
    if job.outputs:
        base = Path(job.workspace) if job.workspace else None
        for output in job.outputs[:5]:
            lines.append(f"output: {_line(base / output if base else output)}")
    lines.append(f"details: {_line(folder)}")
    lines.append(f"next: taskwatch --root {_line(root)} show {job.id}")
    if job.stage == "approval_wait":
        lines.append(f"then: taskwatch --root {_line(root)} approve {job.id} --reason <source>")
    elif job.stage == "user_wait":
        lines.append(f"then: taskwatch --root {_line(root)} resume {job.id} --reason <source>")
    lines.append("This report is a hint; re-read the state before acting.")
    return "\n".join(lines[:20])


def _line(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= 300 else text[:297] + "..."


# --- needs: what waits for the owner -------------------------------------------------------

NEEDS_CAP = 20
GOAL_CHARS = 100
_ENTRY_EVENTS = frozenset({"wait", "approve", "resume"})
_BLOCKING = frozenset({"cancelled", "user_wait"})
_SECTIONS = ("GATE", "NEEDS YOU", "BLOCKED", "FINISHED SINCE THE CURSOR")


@dataclass(frozen=True)
class _Item:
    """A job in a list. The key tells one stop from the next: entering the same wait again
    for the same reason after an answer has a new entry count, so it wakes a watcher."""

    key: tuple
    section: str
    job_id: str
    blocker: str = ""


class _Ledger:
    """Follows jobs.jsonl by byte offset. Keeps the newest raw snapshot of every job, and the
    snapshots as they stood at `cursor`. Digests are checked only for a job that is printed."""

    def __init__(self, path: Path, cursor: int | None) -> None:
        self.path, self.cursor = path, cursor
        self.offset = 0
        self.rows: dict[str, dict] = {}
        self.baseline: dict[str, dict] | None = {} if cursor is None else None

    def poll(self) -> None:
        try:
            with self.path.open("rb") as handle:
                size = handle.seek(0, 2)
                if size < self.offset:
                    self.offset, self.rows = 0, {}
                handle.seek(self.offset)
                data = handle.read(size - self.offset)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RecordError(f"{self.path.name}: unreadable") from exc
        end = data.rfind(b"\n") + 1
        start = self.offset
        for line in data[:end].splitlines(keepends=True):
            if self.baseline is None and start >= (self.cursor or 0):
                self.baseline = dict(self.rows)
            row = self._row(line)
            self.rows[row["job"]["id"]] = row
            start += len(line)
        self.offset += end
        if self.baseline is None and self.offset >= (self.cursor or 0):
            self.baseline = dict(self.rows)

    def _row(self, line: bytes) -> dict:
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RecordError(f"{self.path.name}: not json") from exc
        if isinstance(row, dict) and row.get("version") != RECORD_VERSION:
            raise RecordError(f"{self.path.name}: unknown version")
        job = row.get("job") if isinstance(row, dict) else None
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            raise RecordError(f"{self.path.name}: corrupt job snapshot")
        return row


def _entry(job: dict) -> int:
    return sum(
        isinstance(e, dict) and e.get("type") in _ENTRY_EVENTS for e in job.get("events", [])
    )


def _blocker(job: dict, rows: dict[str, dict]) -> str:
    """The predecessor that keeps a queued job from ever starting without the owner."""
    for pred in job.get("predecessors", []):
        stage = rows[pred]["job"]["stage"] if pred in rows else "missing"
        if stage in _BLOCKING or stage == "missing":
            return f"{stage} job {pred}"
    return ""


def _items(rows: dict[str, dict]) -> list[_Item]:
    items = []
    for ident, row in rows.items():
        job = row["job"]
        stage, entry = job.get("stage"), _entry(job)
        if stage == "approval_wait":
            items.append(_Item((ident, stage, job.get("wait_reason", ""), entry), "GATE", ident))
        elif stage == "user_wait":
            items.append(
                _Item((ident, stage, job.get("wait_reason", ""), entry), "NEEDS YOU", ident)
            )
        elif stage == "queued" and (blocker := _blocker(job, rows)):
            items.append(_Item((ident, stage, blocker, entry), "BLOCKED", ident, blocker))
        elif stage == "done":
            items.append(_Item((ident, stage, "", entry), _SECTIONS[3], ident))
    return sorted(items, key=lambda i: (_last_at(rows[i.job_id]["job"]), i.job_id))


def _last_at(job: dict) -> float:
    stamps = [e["at"] for e in job.get("events", []) if isinstance(e, dict) and "at" in e]
    return max(stamps, default=0.0)


def needs(
    root: Path,
    *,
    cursor: int | None = None,
    wait: bool = False,
    timeout: float = 82800,
    interval: float = 30,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Return (exit code, list). Read only, no lock: nothing is written.

    Without `cursor` the list is what needs the owner now. With `cursor` it also lists the jobs
    that finished since it, and with `wait` it blocks until the list holds an item the cursor did
    not. 0 listed, 3 time limit.
    """
    root = root.resolve()
    ledger = _Ledger(state_dir(root) / "jobs.jsonl", cursor)
    deadline = clock() + timeout
    while True:
        ledger.poll()
        if ledger.baseline is None:
            ledger.baseline = {}  # a cursor beyond the ledger: list everything again
        items = _items(ledger.rows)
        old = {i.key for i in _items(ledger.baseline)}
        if not wait or any(i.key not in old for i in items):
            return 0, _render(root, ledger, items, old if cursor is not None else None)
        remaining = deadline - clock()
        if remaining <= 0:
            return TIMED_OUT, "needs: nothing new before the watcher's time limit; start a new one"
        sleep(min(interval, remaining))


def _state(row: dict) -> JobState:
    job = row["job"]
    if row.get("sha256") != _digest(job):
        raise RecordError("jobs.jsonl: corrupt job snapshot")
    job = _upgrade_record(job)
    _validate_record(job)
    return JobState(**job)


def _render(root: Path, ledger: _Ledger, items: list[_Item], old: set | None) -> str:
    if old is not None:
        items = [i for i in items if i.section != _SECTIONS[3] or i.key not in old]
    else:
        items = [i for i in items if i.section != _SECTIONS[3]]
    counts = {name: sum(i.section == name for i in items) for name in _SECTIONS}
    head = (
        f"needs: {counts['NEEDS YOU']} need you, {counts['GATE']} gate, {counts['BLOCKED']} blocked"
    )
    if old is not None:
        head += f", {counts[_SECTIONS[3]]} finished since the cursor"
    lines = [head, f"cursor: {ledger.offset}"]
    shown = items[:NEEDS_CAP]
    for name in _SECTIONS:
        group = [i for i in shown if i.section == name]
        if group:
            lines.append(name)
            for item in group:
                lines += _lines(root, ledger.rows, item)
    if len(items) > NEEDS_CAP:
        lines.append(f"(+{len(items) - NEEDS_CAP} more; read them with show)")
    lines.append("This list is a hint; re-read the state with show before acting.")
    return "\n".join(lines)


def _goal(job: JobState) -> str:
    return _line(job.prompt.strip().splitlines()[0] if job.prompt.strip() else "")[:GOAL_CHARS]


def _brief_text(root: Path, job_id: str) -> str:
    try:
        return (state_dir(root) / "jobs" / job_id / "brief.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _lines(root: Path, rows: dict[str, dict], item: _Item) -> list[str]:
    job = _state(rows[item.job_id])
    cmd = f"taskwatch --root {_line(root)}"
    kind = kind_label(job)
    if item.section == "BLOCKED":
        return [f"job {job.id} queued behind {item.blocker}"]
    review = job.reviews[-1] if job.reviews and isinstance(job.reviews[-1], Mapping) else {}
    if item.section == "GATE":
        return _gate_lines(root, rows, job, kind, cmd)
    if item.section == "NEEDS YOU":
        return [
            f"job {job.id} {kind} user_wait {_line(job.wait_reason)}  {_stuck(job)}",
            f"  goal: {_goal(job)}",
            f"  then: {cmd} resume {job.id} --reason <source> | cancel {job.id} --reason <text>",
        ]
    decisions = decision_count(_brief_text(root, job.id))
    verdict = review.get("verdict") or "none"
    return [
        f"job {job.id} {kind} done {verdict}; decisions: {decisions}; "
        f"open {count_findings(review)}   brief: {cmd} brief {job.id}"
    ]


def _gate_lines(root: Path, rows: dict[str, dict], job: JobState, kind: str, cmd: str) -> list[str]:
    preds = [_state(rows[p]) for p in job.predecessors if p in rows]
    if preds:
        after = ", ".join(f"{p.id} ({kind_label(p)}) {p.stage}" for p in preds)
        lines = [f"job {job.id} {kind}  after job {after}", f"  goal: {_goal(job)}"]
        lines += [f"  brief: {cmd} brief {p.id}" for p in preds]
    else:
        lines = [f"job {job.id} {kind}  gate: {_line(job.wait_reason)}", f"  goal: {_goal(job)}"]
    picks = sum(choices.pick_count(Path(p.workspace)) for p in preds if choices.is_design(p))
    if picks:
        decisions = f"{picks} picks   reply: ok | <id>=<key> | text"
    else:
        decisions = str(sum(decision_count(_brief_text(root, p.id)) for p in preds))
    lines.append(f"  decisions: {decisions}   then: {cmd} approve {job.id} --reason <your words>")
    return lines


def _stuck(job: JobState) -> str:
    """Why the job stopped, in counts the runner already keeps: round, open findings, repeats."""
    reviews = [r for r in job.reviews if isinstance(r, Mapping)]
    last = reviews[-1] if reviews else {}
    earlier = {f["rule"] for r in reviews[:-1] for f in r.get("findings", [])}
    repeated = sorted({f["rule"] for f in last.get("findings", [])} & earlier - {"other"})
    text = f"round {len(reviews)}; open {count_findings(last)}"
    return text + (f"; repeated rules: {', '.join(repeated)}" if repeated else "")

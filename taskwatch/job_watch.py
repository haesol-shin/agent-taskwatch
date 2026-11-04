"""List what waits for the owner and block until something new appears.

Read only: no lock, no write to the state folder."""

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import brief, choices, job_findings
from .activity import land_facts
from .brief import count_findings, decision_count
from .job_cursor import decode, encode
from .job_store import Store, projection, read_index, read_snapshot, validate_event
from .job_types import job_type_label
from .jobs import JobState, decode_job
from .state import RecordError, state_dir


def _line(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= 300 else text[:297] + "..."


TIMED_OUT = 3
NEEDS_CAP = 20
GOAL_CHARS = 100
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
    """Follow committed events, retaining projections, never historical job bodies."""

    def __init__(self, path: Path, cursor: int | None) -> None:
        self.path = path
        self.directory = path.parent
        self.cursor = cursor
        self.offset = 0
        self.sequence = 0
        self.rows: dict[str, dict] = {}
        self.baseline: dict[str, dict] | None = None
        self.index: dict | None = None
        self.boundary: tuple | None = None
        self.replay = False

    def position(self) -> int:
        return encode(self.index["generation"], self.offset) if self.index else 0

    def _signature(self) -> tuple | None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecordError(f"{self.path.name}: unreadable") from exc
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def stable(self) -> bool:
        return read_index(self.directory) == self.index and self._signature() == self.boundary

    def _empty(self) -> None:
        try:
            artifacts = any(
                (self.directory / name).exists()
                for name in (
                    "jobs.jsonl",
                    "jobs.cursors.json",
                    "job-changes.jsonl",
                    "jobs-pending.json",
                )
            )
            jobs = self.directory / "jobs"
            artifacts = artifacts or (jobs.exists() and any(jobs.iterdir()))
        except OSError as exc:
            raise RecordError("jobs store: unreadable") from exc
        if artifacts:
            raise RecordError(
                "jobs store: run show or normal scan once to initialize or migrate storage"
            )
        self.rows, self.baseline = {}, {}
        self.offset, self.sequence = 0, 0
        self.index, self.boundary = None, None

    def poll(self) -> bool:
        index = read_index(self.directory)
        if index is None:
            self._empty()
            return read_index(self.directory) is None
        boundary = self._signature()
        if boundary is None:
            if read_index(self.directory) != index:
                return False
            raise RecordError("job-changes.jsonl: missing indexed event stream")
        if (
            self.index != index
            or self.boundary is None
            or boundary[:2] != self.boundary[:2]
            or boundary[2] < self.offset
        ):
            self.offset, self.sequence, self.rows = 0, 0, {}
            self.baseline = None
        self.index, self.boundary = index, boundary
        try:
            with self.path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino) != boundary[:2]:
                    return False
                handle.seek(self.offset)
                data = handle.read(boundary[2] - self.offset)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RecordError("job-changes.jsonl: unreadable") from exc
        try:
            self._consume(data)
        except RecordError:
            if not self.stable():
                return False
            raise
        if not self.stable():
            return False
        # An index entry can precede its creation event during prepared publication.
        if set(self.rows) != set(index["receipts"]):
            try:
                pending, _ = _prepared(self)
                if set(self.rows) | {pending["job_id"]} == set(index["receipts"]):
                    return False
            except RecordError:
                if not self.stable():
                    return False
                raise
            raise RecordError("job-changes.jsonl: indexed membership does not match events")
        return True

    def _consume(self, data: bytes) -> None:
        target = decode(self.cursor) if self.cursor is not None else None
        valid = target is not None and target[0] == self.index["generation"]
        if self.baseline is None and not valid:
            self.baseline = {}
            self.replay = self.cursor is not None
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines(keepends=True):
            if self.baseline is None and self.offset == target[1]:
                self.baseline = dict(self.rows)
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise RecordError("job-changes.jsonl: not json") from exc
            row = validate_event(row, self.index["generation"], self.sequence + 1)
            previous = self.rows.get(row["job_id"])
            if previous is not None and row["revision"] != previous["revision"] + 1:
                raise RecordError("job-changes.jsonl: inconsistent job revision")
            self.rows[row["job_id"]] = row
            self.offset += len(line)
            self.sequence += 1
        if self.baseline is None:
            if self.offset == target[1]:
                self.baseline = dict(self.rows)
            else:
                self.baseline = {}
                self.replay = True


def _prepared(ledger: _Ledger) -> tuple[dict, dict]:
    pending, event, _ = Store(ledger.directory)._pending(ledger.index)
    ident = pending["job_id"]
    if pending["sequence"] != ledger.sequence + 1:
        raise RecordError("jobs-pending.json: conflicting event sequence")
    if pending["receipt"] is not None:
        valid = (
            pending["revision"] == 1
            and ident not in ledger.rows
            and ledger.index["receipts"].get(ident) == pending["receipt"]
        )
    else:
        previous = ledger.rows.get(ident)
        valid = (
            ident in ledger.index["receipts"]
            and previous is not None
            and pending["revision"] == previous["revision"] + 1
        )
    if not valid:
        raise RecordError("jobs-pending.json: conflicting receipt or revision")
    return pending, event


def _blocker(job: dict, rows: dict[str, dict]) -> str:
    """The predecessor that keeps a queued job from ever starting without the owner."""
    for pred in job.get("predecessors", []):
        stage = rows[pred]["projection"]["stage"] if pred in rows else "missing"
        if stage in _BLOCKING or stage == "missing":
            return f"{stage} job {pred}"
    return ""


def _items(rows: dict[str, dict]) -> list[_Item]:
    items = []
    for ident, row in rows.items():
        job = row["projection"]
        stage, entry = job["stage"], job["entry"]
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
    return sorted(items, key=lambda i: (rows[i.job_id]["projection"]["last_at"], i.job_id))


def needs(
    root: Path,
    *,
    cursor: int | None = None,
    wait: bool = False,
    include_done: bool = False,
    timeout: float = 82800,
    interval: float = 30,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Return (exit code, list). Read only, no lock: nothing is written.

    Without `cursor` the list is what needs the owner now. With `cursor` it also lists jobs
    that finished since it. `wait` wakes only for new owner attention, unless `include_done`
    requests every completion. 0 listed, 3 time limit.
    """
    root = root.resolve()
    ledger = _Ledger(state_dir(root) / "job-changes.jsonl", cursor)
    deadline = clock() + timeout
    attempted = False
    classified: dict[tuple, tuple[dict, bool]] = {}
    while True:
        if attempted and clock() >= deadline:
            return TIMED_OUT, "needs: nothing new before the watcher's time limit; start a new one"
        attempted = True
        stable = ledger.poll()
        if not stable and not ledger.stable():
            continue
        items = _items(ledger.rows)
        old = {i.key for i in _items(ledger.baseline or {})}
        triggers, evidence = (
            _wake_items(root, ledger, items, old, classified, include_done)
            if stable and wait
            else (set(), {})
        )
        if evidence is None:
            stable = False
        if stable and (not wait or triggers):
            ordered = (
                sorted(items, key=lambda i: (i.key not in triggers, i.section == _SECTIONS[3]))
                if wait
                else items
            )
            rendered = _render(root, ledger, ordered, old if cursor is not None else None)
            if rendered is not None and _unchanged(ledger, evidence):
                return 0, rendered
            stable = False
        if not stable and not ledger.stable():
            continue
        if not stable and not wait:
            return 0, (
                "needs: publication pending; run show or normal scan to recover, then retry\n"
                f"cursor: {cursor if cursor is not None else 0}"
            )
        remaining = deadline - clock()
        if remaining <= 0:
            return TIMED_OUT, "needs: nothing new before the watcher's time limit; start a new one"
        sleep(min(interval, remaining))


def _attention(root: Path, row: dict) -> bool:
    """Only recognized, accepted clean evidence can silence a completion."""
    job = _state(row)
    review = job.reviews[-1] if job.reviews else {}
    try:
        if (
            not isinstance(review, Mapping)
            or not job_findings.accepted(review)
            or review.get("verdict") != "PASS"
            or review.get("problems")
            or review.get("stale")
            or review.get("blocking")
            or not (review.get("ok") is True or review.get("final") is True)
            or not isinstance(review.get("findings"), tuple)
            or count_findings(review) != "none"
        ):
            return True
        folder = state_dir(root) / "jobs" / job.id
        workspace = Path(job.workspace)
        path = folder / "brief.md"
        commit = None
        roots = (workspace, folder, Path(job.target))
        if job.review == "code" and job.review_target is None:
            commit = (Path(job.target), job.fingerprint)
            roots = (folder,)
        if brief.problems(path, roots, commit=commit):
            return True
        if decision_count(path.read_text(encoding="utf-8")):
            return True
        if choices.is_design(job):
            return bool(
                choices.problems(workspace, (workspace, folder, Path(job.target)), job.outputs)
                or choices.pick_count(workspace)
            )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return True
    return False


def _wake_items(
    root: Path,
    ledger: _Ledger,
    items: list[_Item],
    old: set,
    cache: dict[tuple, tuple[dict, bool]],
    include_done: bool,
) -> tuple[set, dict[str, dict] | None]:
    candidates = [i for i in items if i.key not in old]
    triggers = {i.key for i in candidates if include_done or i.section != _SECTIONS[3]}
    done = [i for i in candidates if not include_done and i.section == _SECTIONS[3]]

    def key(item: _Item) -> tuple:
        return ledger.index["generation"], item.job_id, ledger.rows[item.job_id]["revision"]

    fresh = [i for i in done if key(i) not in cache]
    # Cache judgment, not storage validity: even a quiet candidate can become corrupt.
    rows = _snapshots(ledger, done) if done else {}
    if rows is None:
        return set(), None
    results = {key(i): (rows[i.job_id], _attention(root, rows[i.job_id])) for i in fresh}
    if not _unchanged(ledger, rows):
        return set(), None
    cache.update(results)
    evidence = {}
    for item in done:
        row, attention = cache[key(item)]
        if rows[item.job_id] != row:
            raise RecordError(f"job {item.job_id}: snapshot changed without a new revision")
        evidence[item.job_id] = row
        if attention:
            triggers.add(item.key)
    return triggers, evidence


def _state(row: dict) -> JobState:
    return decode_job(row["job"])


def _snapshots(ledger: _Ledger, items: list[_Item]) -> dict[str, dict] | None:
    selected = {item.job_id for item in items}
    for item in items:
        if item.section == "GATE":
            selected.update(ledger.rows[item.job_id]["projection"]["predecessors"])
    rows = {}
    try:
        for ident in selected:
            if ident not in ledger.index["receipts"]:
                continue
            row = read_snapshot(ledger.directory, ident, ledger.index["receipts"][ident])
            event = ledger.rows[ident]
            if row["revision"] > event["revision"]:
                pending, prepared = _prepared(ledger)
                if (
                    pending["job_id"] == ident
                    and pending["revision"] == row["revision"]
                    and pending["sha256"] == row["sha256"]
                    and prepared["projection"] == projection(row["job"])
                ):
                    return None
                raise RecordError(f"job {ident}: snapshot has no matching prepared event")
            if (
                row["revision"] != event["revision"]
                or projection(row["job"]) != event["projection"]
            ):
                raise RecordError(f"job {ident}: snapshot and committed event disagree")
            rows[ident] = row
    except RecordError:
        if not ledger.stable():
            return None
        raise
    return rows if ledger.stable() else None


def _unchanged(ledger: _Ledger, rows: dict[str, dict]) -> bool:
    try:
        for ident, row in rows.items():
            current = read_snapshot(ledger.directory, ident, ledger.index["receipts"][ident])
            if current != row:
                return False
    except RecordError:
        if not ledger.stable():
            return False
        raise
    return ledger.stable()


def _render(root: Path, ledger: _Ledger, items: list[_Item], old: set | None) -> str | None:
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
    lines = [head, f"cursor: {ledger.position()}"]
    shown = items[:NEEDS_CAP]
    rows = _snapshots(ledger, shown) if ledger.index else {}
    if rows is None:
        return None
    if ledger.replay:
        lines.append(
            "Cursor belongs to an unavailable position or older stream; replaying current items."
        )
    for name in _SECTIONS:
        group = [i for i in shown if i.section == name]
        if group:
            lines.append(name)
            for item in group:
                lines += _lines(root, rows, item)
    if len(items) > NEEDS_CAP:
        lines.append(f"(+{len(items) - NEEDS_CAP} more; read them with show)")
    lines.append("This list is a hint; re-read the state with show before acting.")
    return "\n".join(lines) if _unchanged(ledger, rows) else None


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
    job_type = job_type_label(job)
    if item.section == "BLOCKED":
        return [f"job {job.id} queued behind {item.blocker}"]
    review = job.reviews[-1] if job.reviews and isinstance(job.reviews[-1], Mapping) else {}
    if item.section == "GATE":
        return _gate_lines(root, rows, job, job_type, cmd)
    if item.section == "NEEDS YOU":
        return [
            f"job {job.id} {job_type} user_wait {_line(job.wait_reason)}  {_stuck(job)}",
            f"  goal: {_goal(job)}",
            *[f"  land: {fact}" for fact in land_facts(job)],
            *([f"  error: {_line(job.error)}"] if job.error and job.land_state else []),
            *_deadline(job),
            f"  then: {cmd} resume {job.id} --reason <source> | cancel {job.id} --reason <text>",
        ]
    decisions = decision_count(_brief_text(root, job.id))
    verdict = review.get("verdict") or "none"
    return [
        f"job {job.id} {job_type} done {verdict}; decisions: {decisions}; "
        f"open {_finding_count(review)}   brief: {cmd} brief {job.id}"
    ]


def _finding_count(review: Mapping) -> str:
    try:
        return count_findings(review)
    except (KeyError, TypeError):
        return "unknown"


def _gate_lines(
    root: Path, rows: dict[str, dict], job: JobState, job_type: str, cmd: str
) -> list[str]:
    preds = [_state(rows[p]) for p in job.predecessors if p in rows]
    if preds:
        after = ", ".join(f"{p.id} ({job_type_label(p)}) {p.stage}" for p in preds)
        lines = [f"job {job.id} {job_type}  after job {after}", f"  goal: {_goal(job)}"]
        lines += [f"  brief: {cmd} brief {p.id}" for p in preds]
    else:
        lines = [
            f"job {job.id} {job_type}  gate: {_line(job.wait_reason)}",
            f"  goal: {_goal(job)}",
        ]
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


def _deadline(job: JobState) -> list[str]:
    """A rate-limit deadline a resume does not bypass. A job that only waits for it is not
    listed: nothing needs the owner."""
    if job.retry_not_before <= 0:
        return []
    try:
        when = datetime.fromtimestamp(job.retry_not_before, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        when = f"epoch {job.retry_not_before:g} seconds"
    return [f"  retry not before: {when} (resume does not skip it)"]

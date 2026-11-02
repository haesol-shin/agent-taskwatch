"""One derived activity line for `taskwatch show`."""

import time
from pathlib import Path

from .jobs import JobState

ACTIVE_STAGES = frozenset({"work", "fix", "check", "review"})
MAX_LINE_CHARS = 120


def _line(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 3] + "..."


def _minutes(now: float, stamp: float) -> int:
    if stamp <= 0:
        return 0
    return max(0, int((now - stamp) // 60))


def _last_progress_line(job: JobState, folder: Path) -> str:
    if not job.progress or Path(job.progress).is_absolute():
        return ""
    base = folder.resolve()
    path = (base / job.progress).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return ""
    for raw in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = _line(raw)
        if line:
            return line
    return ""


def activity(job: JobState, folder: Path, now: float | None = None) -> str | None:
    """Return the one-line activity summary for an active job, or None when not running."""
    if job.stage not in ACTIVE_STAGES:
        return None
    current = time.time() if now is None else now
    started = _minutes(current, job.started_at)
    ago = _minutes(current, job.last_progress_at)
    head = f"{job.stage} {started}m; last progress {ago}m ago"
    line = _last_progress_line(job, folder)
    return f"{head}: {line}" if line else head

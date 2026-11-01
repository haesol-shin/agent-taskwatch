"""The worker's part of a job's decision brief: `brief.md` in the job folder.

The contract is in docs/kinds/brief.md. `problems` checks only what code can check: the five
headings, the size limits, and that every `file:line` pointer names a file that has the line.
Whether the brief is complete and honest is the reviewer's rule C-19.

`compose` builds what the owner reads from the job record and that file, on every read: the
runner's facts, the worker's narrative, the reviewer's scope and the open findings. Nothing
composed is stored.
"""

import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from .job_env import job_environment
from .jobs import JobConfig, Jobs, JobState
from .kinds import kind_label

SECTIONS = (
    "Why",
    "What changed and why",
    "Choices made",
    "Decisions needed",
    "Where to look",
)
MAX_LINES = 60
MAX_WHY_LINES = 3
MAX_ITEMS = 8
# A path with an extension, then `:line` or `:first-last`. A URL or a clock time does not match:
# the path must start at a boundary that is not `/`, `:`, `.`, `-` or a word character.
POINTER = re.compile(r"(?<![\w.:/-])(/?(?:[\w.-]+/)*[\w-][\w.-]*\.\w+):(\d+)(?:-(\d+))?")
_HEADING = re.compile(r"^##\s+(.*?)\s*$")


def sections(text: str) -> dict[str, list[str]]:
    """The non-empty lines under each `## <name>` heading, by name."""
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            current = match.group(1)
            found.setdefault(current, [])
        elif current is not None and line.strip():
            found[current].append(line.strip())
    return found


def problems(path: Path, roots: tuple[Path, ...]) -> list[str]:
    """Why the brief at `path` fails the contract; empty when it passes."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"required file is missing: {path.name}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path.name} cannot be read: {exc}"]
    found = sections(text)
    issues = [f"{path.name}: missing section `## {name}`" for name in SECTIONS if name not in found]
    issues += [
        f"{path.name}: section `{name}` is empty"
        for name in SECTIONS
        if name in found and _empty(name, found[name])
    ]
    if len(text.splitlines()) > MAX_LINES:
        issues.append(f"{path.name}: more than {MAX_LINES} lines")
    issues += _size_problems(found)
    for rel, first, last in _pointers(text):
        issues += _pointer_problem(rel, first, last, roots)
    return issues


def _empty(name: str, lines: list[str]) -> bool:
    """`none` is an answer only for Decisions needed."""
    if not lines:
        return True
    return name != "Decisions needed" and len(lines) == 1 and lines[0].lower().strip(".`") == "none"


def _size_problems(found: dict[str, list[str]]) -> list[str]:
    issues = []
    if len(found.get("Why", [])) > MAX_WHY_LINES:
        issues.append(f"brief.md: Why is more than {MAX_WHY_LINES} lines")
    changed = [line for line in found.get("What changed and why", []) if line.startswith("-")]
    if len(changed) > MAX_ITEMS:
        issues.append(f"brief.md: What changed and why has more than {MAX_ITEMS} bullets")
    issues += [
        f"brief.md: bullet does not end with a file:line pointer: {line[:60]}"
        for line in changed
        if not _ends_with_pointer(line)
    ]
    if len(found.get("Where to look", [])) > MAX_ITEMS:
        issues.append(f"brief.md: Where to look has more than {MAX_ITEMS} lines")
    return issues


def _ends_with_pointer(line: str) -> bool:
    last = None
    for last in POINTER.finditer(line):
        pass
    return last is not None and not line[last.end() :].strip(" .,;:)`*_")


def _pointers(text: str) -> list[tuple[str, int, int]]:
    return [
        (m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) for m in POINTER.finditer(text)
    ]


def resolve(rel: str, roots: tuple[Path, ...]) -> Path | None:
    """The existing file `rel` names under one of the roots, else None."""
    for root in roots:
        try:
            base = root.resolve()
            candidate = (base / rel).resolve()
        except OSError:
            continue
        if candidate.is_file() and (candidate == base or base in candidate.parents):
            return candidate
    return None


def _pointer_problem(rel: str, first: int, last: int, roots: tuple[Path, ...]) -> list[str]:
    path = resolve(rel, roots)
    if path is None:
        return [f"brief.md: pointer names no file: {rel}"]
    try:
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return [f"brief.md: pointer file cannot be read: {rel}: {exc}"]
    if first < 1 or last < first or last > count:
        return [f"brief.md: pointer {rel}:{first}-{last} is outside the file ({count} lines)"]
    return []


# --- composition ---------------------------------------------------------------------------

CARD_LINES = 25
# Lines per section of the card, its heading included. Facts are five lines: 5 + 2 + 6 + 6 + 4 + 2.
_CARD_CAPS = {
    "Why": 2,
    "What changed and why": 6,
    "Decisions needed": 6,
    "Remaining findings": 4,
    "Where to look": 2,
}
_GOAL_CHARS = 100
_FILES_SHOWN = 4
_FIX_CHARS = 120
MAX_FULL_LINES = 80
_NUMBERED = re.compile(r"^\d+[.)]\s")


def decision_count(text: str) -> int:
    """How many decisions the worker's brief asks the owner for; `none` or no brief is 0."""
    lines = sections(text).get("Decisions needed", [])
    if not lines or (len(lines) == 1 and lines[0].lower().strip(".`") == "none"):
        return 0
    return sum(bool(_NUMBERED.match(line)) for line in lines) or 1


def read_worker_brief(jobs: Jobs, job: JobState) -> str:
    try:
        return (jobs.folder(job) / "brief.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def compose(jobs: Jobs, config: JobConfig, job: JobState, *, card: bool = False) -> list[str]:
    """The brief of a job. `card` limits each section so the whole is at most 25 lines."""
    review = job.reviews[-1] if job.reviews and isinstance(job.reviews[-1], Mapping) else {}
    worker = sections(read_worker_brief(jobs, job))
    lines = _facts(jobs, config, job, review)
    for name in ("Why", "What changed and why", "Decisions needed"):
        lines += _section(name, worker.get(name) or ["(none)"], card)
    lines += _section("Remaining findings", _finding_lines(review), card)
    if not card:
        lines += [
            "Reviewer checked: " + (review.get("checked") or "(not recorded)"),
            "Reviewer did not check: " + (review.get("not_checked") or "(not recorded)"),
        ]
    lines += _section("Where to look", worker.get("Where to look") or ["(none)"], card)
    if not card and len(lines) > MAX_FULL_LINES:
        hidden = len(lines) - MAX_FULL_LINES + 1
        lines = [*lines[: MAX_FULL_LINES - 1], f"... {hidden} more lines in the worker brief"]
    return lines


def _section(name: str, lines: list[str], card: bool) -> list[str]:
    heading = f"{name}:"
    if not card:
        return [heading, *lines]
    room = _CARD_CAPS[name] - 1
    if len(lines) <= room:
        return [heading, *lines]
    shown = room - 1
    return [heading, *lines[:shown], f"... {len(lines) - shown} more in the full brief"]


def _facts(jobs: Jobs, config: JobConfig, job: JobState, review: Mapping) -> list[str]:
    goal = job.prompt.strip().splitlines()[0][:_GOAL_CHARS] if job.prompt.strip() else ""
    checks = job.checks[-1] if job.checks and isinstance(job.checks[-1], Mapping) else None
    check = "no check" if checks is None else ("check ok" if checks.get("ok") else "check failed")
    verdict = review.get("verdict") or "none yet"
    rounds = f"review round {len(job.reviews)}" if job.reviews else "no review"
    return [
        f"Job {job.id} ({kind_label(job)}): {goal}",
        f"Verdict: {verdict}; {rounds}; {check}; {_elapsed(job)}",
        _result(job),
        _changed(job, config),
        _open_counts(review),
    ]


def _result(job: JobState) -> str:
    target = job.review_target
    if target is not None:
        if "commit" in target:
            return f"Reviewed: commit {target['commit']}"
        return f"Reviewed: {', '.join(str(Path(job.workspace) / f) for f in target['files'])}"
    if job.review == "code":
        branch = job.branch or "(not recorded)"
        return f"Branch: {branch} in {job.target}"
    names = (str(Path(job.workspace) / name) for name in job.outputs)
    return f"Outputs: {', '.join(names)}"


def _changed(job: JobState, config: JobConfig) -> str:
    if job.review_target is not None:
        return "Changed: nothing (review only)"
    names = _git_changed(job, config) if job.review == "code" else list(job.outputs)
    if names is None:
        return "Changed: unavailable (no work folder or branch)"
    shown = ", ".join(names[:_FILES_SHOWN])
    more = f" (+{len(names) - _FILES_SHOWN} more)" if len(names) > _FILES_SHOWN else ""
    return f"Changed: {len(names)} files: {shown}{more}" if names else "Changed: no files"


def _git_changed(job: JobState, config: JobConfig) -> list[str] | None:
    """Files changed since the base, from the work folder or else the job's branch."""
    folder = Path(job.workspace) if job.workspace else None
    if folder is not None and folder.is_dir():
        where, ref = folder, "HEAD"
    elif job.branch:
        where, ref = Path(job.target), job.branch
    else:
        return None
    if not job.base:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(where), "diff", "--name-only", f"{job.base}..{ref}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=job_environment(config.env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.split() if proc.returncode == 0 else None


def _open_counts(review: Mapping) -> str:
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for finding in review.get("findings", []):
        counts[finding["severity"]] += 1
    shown = [f"{name} {n}" for name, n in counts.items() if n]
    return "Open findings: " + (", ".join(shown) if shown else "none")


def _finding_lines(review: Mapping) -> list[str]:
    """One line per finding, worst first: id, severity, rule, category, location, and the
    reviewer's fix sentence when the text under its heading gives one (rule C-17)."""
    body = review.get("body") or ""
    ranked = sorted(review.get("findings", []), key=lambda f: (f["severity"], f["finding"]))
    lines = []
    for f in ranked:
        fix = _fix_text(body, f["finding"])
        tail = f" -- fix: {fix}" if fix else ""
        lines.append(
            f"- {f['finding']} {f['severity']} {f['rule']} ({f['category']}): {f['location']}{tail}"
        )
    return lines or ["none"]


_FIX = re.compile(r"(?:Fix|fix|\uc218\uc815)\s*:\s*(.+)")


def _fix_text(body: str, ident: str) -> str:
    """The sentence after `Fix:` in the text under `### <ident>`, cut to one short line."""
    match = re.search(rf"^###\s+{re.escape(ident)}\b(.*?)(?=^###\s|\Z)", body, re.M | re.S)
    found = _FIX.search(match.group(1)) if match else None
    return found.group(1).strip()[:_FIX_CHARS] if found else ""


def _elapsed(job: JobState) -> str:
    stamps = [e["at"] for e in job.events if isinstance(e.get("at"), (int, float))]
    start = min(stamps, default=job.started_at)
    if not start:
        return "time unknown"
    end = job.ended_at if job.ended_at > 0 else max(stamps, default=time.time())
    minutes = max(0, int((end - start) // 60))
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if hours else f"{rest}m"

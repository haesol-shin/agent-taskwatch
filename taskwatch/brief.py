"""The worker's part of a job's decision brief: `brief.md` in the job folder.

The contract is in docs/jobs/brief.md. `problems` checks only what code can check: the six
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

from . import choices
from .activity import land_facts
from .fingerprint import git_fingerprint, resolve_commit
from .job_env import job_environment
from .job_types import job_type_label
from .jobs import JobConfig, Jobs, JobState
from .pointers import POINTER, pointer_problem, pointers

SECTIONS = (
    "Why",
    "What changed and why",
    "Choices made",
    "Decisions needed",
    "Where to look",
    "Card",
)
MAX_LINES = 60
MAX_WHY_LINES = 3
MAX_ITEMS = 8
MAX_CARD_CONCLUSION = 2
MAX_CARD_BULLETS = 6
MAX_CARD_BULLET_CHARS = 160
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


def problems(
    path: Path,
    roots: tuple[Path, ...],
    awaiting: tuple[str, ...] = (),
    listed: tuple[str, ...] = (),
    *,
    commit: tuple[Path, str] | None = None,
) -> list[str]:
    """Why the brief at `path` fails the contract; empty when it passes.

    `awaiting` are pick ids that `Choices made` must mark as awaiting the owner; `listed` are
    ids it must list.
    `commit` checks result pointers against that immutable Git commit before the local roots.
    """
    if commit is not None:
        try:
            repo, sha = commit
            resolve_commit(repo, git_fingerprint(sha))
        except (OSError, ValueError) as exc:
            return [f"brief.md: result commit cannot be read: {exc}"]
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
    issues += _choice_problems(found.get("Choices made", []), awaiting, listed)
    for rel, first, last in pointers(text):
        issues += [
            f"brief.md: {p}" for p in pointer_problem(rel, first, last, roots, commit=commit)
        ]
    return issues


def _mentions(ident: str, text: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(ident)}(?![\w.])", text) is not None


def _choice_problems(
    lines: list[str], awaiting: tuple[str, ...], listed: tuple[str, ...]
) -> list[str]:
    issues = [
        f"brief.md: Choices made must mark pick {ident} as `{choices.AWAITING}`"
        for ident in awaiting
        if not any(_mentions(ident, line) and choices.AWAITING in line for line in lines)
    ]
    text = "\n".join(lines)
    return issues + [
        f"brief.md: Choices made does not list {ident}"
        for ident in listed
        if not _mentions(ident, text)
    ]


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
    return issues + _card_problems(found.get("Card", []))


def _card_problems(lines: list[str]) -> list[str]:
    """The card is a conclusion of one or two lines, then at most six short bullets."""
    if not lines:
        return []  # the empty heading is reported with the others
    bullets = [line for line in lines if line.startswith("-")]
    conclusion = len(lines) - len(bullets)
    issues = []
    if not 1 <= conclusion <= MAX_CARD_CONCLUSION:
        issues.append(
            f"brief.md: Card needs 1 to {MAX_CARD_CONCLUSION} conclusion lines "
            f"(not bullets), found {conclusion}"
        )
    if len(bullets) > MAX_CARD_BULLETS:
        issues.append(
            f"brief.md: Card has more than {MAX_CARD_BULLETS} bullets: the job was too big; "
            "split it (docs/review/design.md D-13)"
        )
    issues += [
        f"brief.md: Card bullet is over {MAX_CARD_BULLET_CHARS} characters: {line[:60]}"
        for line in bullets
        if len(line) > MAX_CARD_BULLET_CHARS
    ]
    issues += [
        f"brief.md: Card must not hold a file:line pointer: {line[:60]}"
        for line in lines
        if POINTER.search(line)
    ]
    return issues


def _ends_with_pointer(line: str) -> bool:
    last = None
    for last in POINTER.finditer(line):
        pass
    return last is not None and not line[last.end() :].strip(" .,;:)`*_")


# --- composition ---------------------------------------------------------------------------

# The card's three labels, by `language` in jobs-config.json. The order is the card's order.
LABELS = {
    "en": ("Changed", "Small problems left", "Not checked"),
    "ko": (
        "\ubc14\ub010 \uac83",
        "\ub0a8\uc740 \uc791\uc740 \ubb38\uc81c",
        "\ud655\uc778\ud558\uc9c0 \ubabb\ud55c \uac83",
    ),
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


def _base_review(job: JobState) -> Mapping:
    """The review whose evidence the card reports: the shared accepted selection."""
    if not job.reviews:
        return {}
    from .job_delivery import accepted_review

    review = accepted_review(job)
    return review if isinstance(review, Mapping) else {}


def compose(jobs: Jobs, config: JobConfig, job: JobState) -> list[str]:
    """The full brief of a job: the runner's facts, the worker's sections, the review lists."""
    review = _base_review(job)
    worker = sections(read_worker_brief(jobs, job))
    active = _active_bot_findings(job)
    latest_round = review.get("round", len(job.reviews))
    remaining = {
        **review,
        "findings": [
            finding
            for finding in review.get("findings", [])
            if f"r{latest_round}.{finding['finding']}" not in active
        ],
    }
    reported = {
        **review,
        "findings": [
            *remaining["findings"],
            *(finding for finding, _thread in active.values()),
        ],
    }
    lines = _facts(jobs, config, job, reported)
    for name in ("Why", "What changed and why"):
        lines += _section(name, worker.get(name) or ["(none)"])
    lines += _section(
        "Decisions needed", _picks(job) or worker.get("Decisions needed") or ["(none)"]
    )
    finding_lines = _finding_lines(remaining) if remaining["findings"] else []
    lines += _section("Remaining findings", finding_lines + _active_bot_lines(job) or ["none"])
    lines += _bot_not_checked(job, config.language or "en")
    lines += [
        "Reviewer checked: " + (review.get("checked") or "(not recorded)"),
        "Reviewer did not check: " + (review.get("not_checked") or "(not recorded)"),
    ]
    lines += _section("Where to look", worker.get("Where to look") or ["(none)"])
    return lines


def card(jobs: Jobs, config: JobConfig, job: JobState) -> list[str]:
    """The message the owner reads: the worker's conclusion, a blank line, then only the lists
    that have content. `Changed` is the worker's `## Card` bullets; the other two come from the
    accepted review: the `summary` of each P2 or P3 finding, and the items of `not_checked`
    split on `; `."""
    review = _base_review(job)
    own = sections(read_worker_brief(jobs, job)).get("Card", [])
    conclusion = [line for line in own if not line.startswith("-")]
    changed = [line for line in own if line.startswith("-")]
    labels = LABELS[config.language or "en"]
    lists = (
        [line[1:].strip() for line in changed],
        _small_problems(job, review, config.language or "en"),
        _items(review.get("not_checked")) + _bot_not_checked(job, config.language or "en"),
    )
    # A job without a worker brief (a review-only job) has no conclusion: the verdict stands in.
    verdict = review.get("verdict") or "none"
    lines = conclusion or [f"{job_type_label(job)} job {job.id}: verdict {verdict}."]
    for label, bullets in zip(labels, lists):
        if bullets:
            lines += ["", label, *_bounded(bullets)]
    return lines


def _bounded(items: list[str]) -> list[str]:
    """At most six bullets with at most 160 characters of text each; a seventh and later show
    as `+N`."""
    shown = [f"- {_cut(item)}" for item in items[:MAX_CARD_BULLETS]]
    return (
        [*shown, f"- +{len(items) - MAX_CARD_BULLETS}"] if len(items) > MAX_CARD_BULLETS else shown
    )


def _cut(text: str) -> str:
    return (
        text if len(text) <= MAX_CARD_BULLET_CHARS else text[: MAX_CARD_BULLET_CHARS - 1] + "\u2026"
    )


def _items(text: object) -> list[str]:
    """The items of a `not_checked` text: short plain items joined by `; `."""
    if not isinstance(text, str):
        return []
    return [item.strip() for item in text.split("; ") if item.strip()]


def _ranked(review: Mapping) -> list[Mapping]:
    return sorted(review.get("findings", []), key=lambda f: (f["severity"], f["finding"]))


def _small(finding: Mapping) -> bool:
    """A P2 or P3 finding with the reviewer's sentence. P0 and P1 never ride on a delivered
    card, and a finding without a `summary` (an older record, a failed review) stays in the
    full brief, where its rule and location are."""
    return finding["severity"] in {"P2", "P3"} and bool(finding.get("summary"))


def _active_bot_findings(job: JobState) -> dict[str, tuple[Mapping, Mapping]]:
    """Resolve open bot findings and durable deferrals against their original round."""
    bot = job.land_state.get("bot", {})
    refs = set(bot.get("followup", {}).get("findings", []))
    threads = {
        thread["finding"]: thread
        for thread in bot.get("threads", {}).values()
        if thread.get("status") == "open"
        or (thread.get("status") == "deferred" and thread.get("finding") in refs)
    }
    active = {}
    for index, review in enumerate(job.reviews, 1):
        for finding in review.get("findings", []):
            ident = f"r{review.get('round', index)}.{finding['finding']}"
            if ident in threads:
                active[ident] = (finding, threads[ident])
    return active


def _small_problems(job: JobState, review: Mapping, language: str) -> list[str]:
    active = _active_bot_findings(job)
    round_number = review.get("round", len(job.reviews))
    items = []
    for finding in _ranked(review):
        if _small(finding) and f"r{round_number}.{finding['finding']}" not in active:
            items.append((finding["severity"], finding["summary"]))
    followup = job.land_state.get("bot", {}).get("followup", {}).get("id", "")
    for finding, thread in active.values():
        if _small(finding):
            status = thread["status"]
            if language == "ko":
                status = "보류" if status == "deferred" else "미해결"
            reference = f" [{followup}]" if thread["status"] == "deferred" else ""
            prefix = f"{finding['severity']} {status}{reference}: "
            # Keep the identity visible even when the summary fills the entire bullet.
            summary = _cut_to(finding["summary"], MAX_CARD_BULLET_CHARS - len(prefix))
            items.append((finding["severity"], prefix + summary))
    return [text for _, text in sorted(items, key=lambda item: item[0])]


def _cut_to(text: str, length: int) -> str:
    return text if len(text) <= length else text[: max(0, length - 1)] + "…"


def _active_bot_lines(job: JobState) -> list[str]:
    followup = job.land_state.get("bot", {}).get("followup", {}).get("id", "")
    lines = []
    for ident, (finding, thread) in _active_bot_findings(job).items():
        location = thread["path"]
        if thread.get("line") is not None:
            location += f":{thread['line']}"
        reference = f" [{followup}]" if thread["status"] == "deferred" else ""
        lines.append(
            f"- {ident} {finding['severity']} {thread['status']}{reference}: "
            f"{finding.get('summary', '(not recorded)')} -- {location} {thread['url']}"
        )
    return lines


def _bot_not_checked(job: JobState, language: str) -> list[str]:
    bot = job.land_state.get("bot", {})
    outcomes = bot.get("outcome", {})
    lines = []
    for slug, outcome in outcomes.items():
        if outcome.startswith("fallback:"):
            if language == "ko":
                lines.append(f"리뷰 봇 {slug}: {outcome}; 승인된 자체 리뷰로 대체")
            else:
                lines.append(f"Review bot {slug}: {outcome}; using accepted own review")
    if bot.get("apps"):
        lines.append(
            "봇의 마지막 확인과 병합 사이에 새 의견이 나타날 수 있음"
            if language == "ko"
            else "New bot feedback can appear between the final read and merge"
        )
    return lines


def trailer(jobs: Jobs, job: JobState) -> list[str]:
    """The lines that left the card and end `taskwatch brief`: where the full text is."""
    review = job.reviews[-1] if job.reviews and isinstance(job.reviews[-1], Mapping) else {}
    folder = jobs.folder(job)
    lines = [f"Full brief: taskwatch --root {jobs.state.root} brief {job.id}"]
    if (folder / "delivery-review.txt").exists():
        lines.append(f"Review: {folder / 'delivery-review.txt'}")
    for unit in job.public_text:
        state = review.get("texts", {}).get(unit["id"], {})
        lines.append(f"Public text {unit['id']}: {folder / 'pr' / unit['id']}")
        lines.append(f"Text fingerprint {unit['id']}: {state.get('fingerprint', 'unavailable')}")
    return lines


def cap(lines: list[str], tail: tuple[str, ...] = ()) -> list[str]:
    """`lines` then `tail`, at most 80 lines in all. Lines are cut from `lines` only, so the
    tail (the worker-brief path, the reply template) always reaches the reader."""
    room = MAX_FULL_LINES - len(tail)
    if len(lines) <= room:
        return [*lines, *tail]
    # The cut line takes one slot, so one more line is hidden than the output is over by.
    hidden = len(lines) - room + 1
    return [*lines[: room - 1], f"... {hidden} more lines omitted from this output", *tail]


def _picks(job: JobState) -> list[str]:
    """The owner's picks of a design job, rendered from its validated `choices.json`."""
    if not choices.is_design(job) or not job.workspace:
        return []
    data, _ = choices.load(Path(job.workspace) / choices.FILE)
    try:
        return choices.render_picks(data) if data else []
    except (KeyError, TypeError):
        return []  # an invalid file is the check's problem; the worker's text stands in


def reply_template(job: JobState) -> str | None:
    """The one line that ends a message with picks in it: how the owner answers."""
    count = choices.pick_count(Path(job.workspace)) if choices.is_design(job) else 0
    return choices.REPLY if count and job.workspace else None


def _section(name: str, lines: list[str]) -> list[str]:
    return [f"{name}:", *lines]


def _facts(jobs: Jobs, config: JobConfig, job: JobState, review: Mapping) -> list[str]:
    goal = job.prompt.strip().splitlines()[0][:_GOAL_CHARS] if job.prompt.strip() else ""
    checks = job.checks[-1] if job.checks and isinstance(job.checks[-1], Mapping) else None
    check = "no check" if checks is None else ("check ok" if checks.get("ok") else "check failed")
    verdict = review.get("verdict") or "none yet"
    rounds = f"review round {len(job.reviews)}" if job.reviews else "no review"
    return [
        f"Job {job.id} ({job_type_label(job)}): {goal}",
        f"Verdict: {verdict}; {rounds}; {check}; {_elapsed(job)}",
        _result(job),
        _changed(job, config),
        f"Open findings: {count_findings(review)}{_snippets(checks)}",
    ] + [f"Land: {fact}" for fact in land_facts(job)]


def _snippets(check: Mapping | None) -> str:
    count = ((check or {}).get("sources") or {}).get("snippet_only", 0)
    return f"; {count} claims rest on snippets only" if count else ""


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
    if job.review != "code":
        return _changed_line(list(job.outputs), None)
    stat = _git_changed(job, config)
    if stat is None:
        return "Changed: unavailable (no work folder or branch)"
    names = [name for name, _, _ in stat]
    # A binary file has no line counts (`-`): it is a file in the count and adds no lines.
    return _changed_line(names, (sum(a for _, a, _ in stat), sum(d for _, _, d in stat)))


def _changed_line(names: list[str], lines: tuple[int, int] | None) -> str:
    if not names:
        return "Changed: no files"
    shown = ", ".join(names[:_FILES_SHOWN])
    more = f" (+{len(names) - _FILES_SHOWN} more)" if len(names) > _FILES_SHOWN else ""
    stat = f", +{lines[0]} -{lines[1]}" if lines else ""
    return f"Changed: {len(names)} files{stat}: {shown}{more}"


def _git_changed(job: JobState, config: JobConfig) -> list[tuple[str, int, int]] | None:
    """`(path, added, deleted)` per file changed since the base, from the work folder or else
    the job's branch. One `git diff --numstat`: a rename is one file with its own counts, and
    git's quoting keeps non-UTF-8 path bytes out of the decoded text."""
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
            ["git", "-C", str(where), "diff", "--numstat", f"{job.base}..{ref}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=job_environment(config.env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    stat = []
    for record in proc.stdout.splitlines():
        added, deleted, path = record.split("\t", 2)
        stat.append(
            (
                _renamed_to(path),
                int(added) if added.isdigit() else 0,
                int(deleted) if deleted.isdigit() else 0,
            )
        )
    return stat


_BRACES = re.compile(r"\{[^{}]* => ([^{}]*)\}")


def _renamed_to(path: str) -> str:
    """The new path of a numstat rename: `a => b` or `dir/{a => b}/f`; other paths as they are."""
    if " => " not in path:
        return path
    if _BRACES.search(path):
        return _BRACES.sub(r"\1", path, count=1).replace("//", "/")
    return path.split(" => ", 1)[1]


def count_findings(review: Mapping) -> str:
    """`P2 1, P3 2` for the findings of a review, or `none`."""
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for finding in review.get("findings", []):
        counts[finding["severity"]] += 1
    shown = [f"{name} {n}" for name, n in counts.items() if n]
    return ", ".join(shown) if shown else "none"


def _finding_lines(review: Mapping) -> list[str]:
    """One line per finding, worst first: id, severity, rule, category, location, and the
    reviewer's fix sentence when the text under its heading gives one (rule C-17)."""
    body = review.get("body") or ""
    lines = []
    for f in _ranked(review):
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

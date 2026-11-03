"""Finding records: the worker's status file, the index across jobs, and the repeat triggers.

A finding has a rule id, so two findings are of the same kind when `rule` and the job `target`
match; `other` is never a kind. Nothing here reads free text or calls a model.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .fingerprint import is_ancestor, resolve_commit
from .jobs import JobState
from .state import State

FINDINGS_FILE = "findings.json"
NOTE_FILE = "root-cause.md"
INDEX = "findings.jsonl"
WINDOW = 10
NOTE_LABELS = ("Cause:", "Missed by:", "Prevention:")
# The one problem of a review whose verdict was read and is a FAIL.
FAIL_PROBLEM = "the reviewer did not pass the result"
_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"r\d+\.F\d+")


def finding_id(review: Mapping, finding: Mapping) -> str:
    return f"r{review['round']}.{finding['finding']}"


# --- reviews -------------------------------------------------------------------------------


def accepted(review: Mapping) -> bool:
    """A verdict was read and nothing else is wrong with the answer."""
    return review.get("verdict") in {"PASS", "FAIL"} and all(
        problem == FAIL_PROBLEM for problem in review.get("problems", ())
    )


def substantive(review: Mapping) -> bool:
    """An accepted failed review that names findings. Malformed or infrastructure rounds are not."""
    return accepted(review) and not review.get("ok") and bool(review.get("findings"))


def last_failed(reviews: Sequence[Mapping]) -> Mapping | None:
    """The most recent failed review that names findings, across any resume. A later passing
    review closes the question."""
    for review in reversed(reviews):
        if not isinstance(review, Mapping) or review.get("ok"):
            return None
        if substantive(review):
            return review
    return None


def _rules(review: Mapping) -> set[str]:
    return {f["rule"] for f in review.get("findings", ()) if f["rule"] != "other"}


def consecutive_rules(cycle: Sequence[Mapping]) -> tuple[list[str], list[int]]:
    """Rules the newest review of the cycle shares with the substantive review before it, and
    the two rounds. Rule ids only: no prose, category or location is compared."""
    if not cycle or not substantive(cycle[-1]):
        return [], []
    for earlier in reversed(cycle[:-1]):
        if substantive(earlier):
            shared = sorted(_rules(cycle[-1]) & _rules(earlier))
            return (shared, [earlier["round"], cycle[-1]["round"]]) if shared else ([], [])
    return [], []


def note_digest(folder: Path) -> str:
    """The digest of the root-cause note, empty when there is none."""
    try:
        return hashlib.sha256((folder / NOTE_FILE).read_bytes()).hexdigest()
    except OSError:
        return ""


def stop_problems(folder: Path, stop: Mapping) -> list[str]:
    """What a resume after a root-cause stop still needs: a note changed since the stop and
    complete for every rule that triggered it."""
    problems = []
    if note_digest(folder) == stop.get("note_digest", ""):
        problems.append(
            f"{NOTE_FILE} has not changed since the stop: analyse why the rule repeated"
        )
    return problems + note_problems(folder, stop.get("rules", []))


# --- the worker's record -------------------------------------------------------------------


def _load(folder: Path) -> tuple[dict[str, Mapping] | None, list[str]]:
    """The entries by id, and why the file is not acceptable; None when it cannot be read as a
    list at all."""
    try:
        raw = json.loads((folder / FINDINGS_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None, [f"{FINDINGS_FILE} is missing: record each finding of the last review"]
    except ValueError as exc:
        return None, [f"{FINDINGS_FILE} is not valid JSON: {exc}"]
    if not isinstance(raw, list):
        return None, [f"{FINDINGS_FILE} must be a JSON list with one entry per finding"]
    entries: dict[str, Mapping] = {}
    problems = []
    for number, entry in enumerate(raw, 1):
        ident = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(ident, str) or _ID.fullmatch(ident) is None:
            problems.append(f"{FINDINGS_FILE} entry {number} needs an id like r1.F1")
        elif ident in entries:
            problems.append(f"{FINDINGS_FILE} has more than one entry for {ident}")
        else:
            entries[ident] = entry
            if entry.get("status") not in ("fixed", "not_fixed"):
                problems.append(f"{ident} needs status fixed or not_fixed")
    return entries, problems


def _entries(folder: Path) -> dict[str, Mapping]:
    return _load(folder)[0] or {}


def status_lines(job: JobState, folder: Path) -> list[str]:
    """The worker's recorded status of the findings of the last failed review, for the next
    reviewer. A fixed finding shows the commit its check verified."""
    review = last_failed(job.reviews) or {}
    entries = _entries(folder)
    lines = []
    for finding in review.get("findings", []):
        entry = entries.get(finding_id(review, finding))
        if entry is None:
            continue
        detail = ""
        if entry.get("status") == "not_fixed":
            detail = f": {entry['reason']}"
        elif isinstance(entry.get("commit"), str):
            detail = f" {entry['commit'][:12]}"
        lines.append(
            f"- {finding_id(review, finding)} ({finding['severity']}) {entry.get('status')}{detail}"
        )
    return lines


def reviewed_head(review: Mapping) -> str | None:
    """The commit a review judged: the recorded head, or the one head the review recorded for
    its units. None when neither proves it."""
    head = review.get("reviewed_head")
    if isinstance(head, str) and _SHA.fullmatch(head):
        return head
    heads = {
        state.get("head")
        for state in review.get("texts", {}).values()
        if isinstance(state, Mapping)
    }
    heads = {h for h in heads if isinstance(h, str) and _SHA.fullmatch(h)}
    return heads.pop() if len(heads) == 1 else None


def _commit_problem(
    key: str,
    entry: Mapping,
    review: Mapping,
    head: str,
    code_changed: bool,
    repo: Path,
    env: Mapping[str, str] | None,
) -> str | None:
    """Why the commit of a fixed entry is not evidence, or None."""
    commit = entry.get("commit")
    if not isinstance(commit, str) or _SHA.fullmatch(commit) is None:
        return f"{key} is fixed and needs `commit`: the full 40-character SHA of the fix"
    try:
        resolve_commit(repo, commit, env)
    except ValueError:
        return f"{key}: commit {commit[:12]} is not a commit of the work folder"
    if not code_changed:
        # Only public text changed, so there is no new commit: HEAD is the reviewed code.
        if commit != head:
            return f"{key}: a fix of public text only names the current HEAD {head[:12]}"
        return None
    anchor = reviewed_head(review)
    if anchor is None:
        return f"{key} cannot be verified: the review recorded no reviewed head"
    try:
        valid = commit != anchor and is_ancestor(repo, anchor, commit, env)
        valid = valid and is_ancestor(repo, commit, head, env)
    except ValueError:
        valid = False
    if not valid:
        return (
            f"{key}: commit {commit[:12]} is not a later commit than the reviewed "
            f"{anchor[:12]} that HEAD contains"
        )
    return None


def worker_problems(
    folder: Path,
    review: Mapping,
    fingerprint: str,
    texts: Mapping[str, Mapping],
    repo: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Check problems of the worker's record for the findings of a failed review.

    With `repo`, the result is Git commits and a fixed entry must name the commit that fixed it.
    Without, a changed result fingerprint is the only evidence."""
    findings = review.get("findings", [])
    if not findings:
        return []
    entries, problems = _load(folder)
    if entries is None:
        return problems
    reviewed = review.get("texts", {})
    code_changed = fingerprint != review.get("fingerprint")
    changed = code_changed or any(
        texts.get(unit, {}).get("fingerprint") != state.get("fingerprint")
        for unit, state in reviewed.items()
    )
    for finding in findings:
        key = finding_id(review, finding)
        entry = entries.get(key)
        status = entry.get("status") if entry else None
        if entry is None:
            problems.append(f"{FINDINGS_FILE} has no entry for {key}")
        elif status == "fixed" and not changed:
            problems.append(f"{key} is fixed, but neither the code nor the public text changed")
        elif status == "fixed" and repo is not None and fingerprint:
            problem = _commit_problem(key, entry, review, fingerprint, code_changed, repo, env)
            if problem:
                problems.append(problem)
        elif status == "not_fixed":
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                problems.append(f"{key} is not_fixed and needs a reason")
    return problems


def repeated_rules(
    job: JobState, earlier: Sequence[Mapping], latest: Mapping, rows: Sequence[Mapping]
) -> list[str]:
    """Rules of the latest review that an earlier round or one of the last jobs also broke."""
    seen = {f["rule"] for review in earlier for f in review.get("findings", [])}
    order: list[str] = []
    by_job: dict[str, set[str]] = {}
    for row in rows:
        if row.get("target") == job.target and row.get("job") != job.id:
            if row["job"] not in by_job:
                order.append(row["job"])
            by_job.setdefault(row["job"], set()).add(row["rule"])
    for other in order[-WINDOW:]:
        seen |= by_job[other]
    rules = {f["rule"] for f in latest.get("findings", []) if f["rule"] != "other"}
    return sorted(rules & seen)


def notes(folder: Path) -> dict[str, dict[str, str]]:
    """`## <rule>` sections of the root-cause note, each with its labeled lines."""
    try:
        text = (folder / NOTE_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        heading = re.fullmatch(r"##\s+(\S.*?)\s*", line)
        if heading:
            current = sections.setdefault(heading.group(1), {})
            continue
        for label in NOTE_LABELS:
            if current is not None and line.startswith(label):
                current[label] = line[len(label) :].strip()
    return sections


def note_problems(folder: Path, rules: Sequence[str]) -> list[str]:
    sections = notes(folder)
    problems = []
    for rule in rules:
        section = sections.get(rule)
        if section is None:
            problems.append(f"{NOTE_FILE} needs a section `## {rule}`: the rule repeated")
            continue
        for label in NOTE_LABELS:
            if not section.get(label):
                problems.append(f"{NOTE_FILE} section `## {rule}` needs a `{label}` line")
    return problems


def named_preventions(folder: Path, rules: Sequence[str]) -> dict[str, str]:
    """The prevention a note names for each rule; `accepted: <reason>` names none."""
    found = {}
    for rule in rules:
        text = notes(folder).get(rule, {}).get("Prevention:", "")
        if text and not text.lower().startswith("accepted:"):
            found[rule] = text
    return found


# --- index ---------------------------------------------------------------------------------


def sync_index(state: State, job: JobState, folder: Path, now: float) -> None:
    """Append one line per finding whose status is new; the last line of a finding wins."""
    last: dict[tuple[str, str], str] = {}
    for row in state.read_records(INDEX):
        last[(row["job"], row["finding"])] = row["status"]
    entries = _entries(folder)
    stamp = datetime.fromtimestamp(now, timezone.utc)
    for review in job.reviews:
        for finding in review.get("findings", []):
            key = finding_id(review, finding)
            entry = entries.get(key)
            status = entry["status"] if entry and entry.get("status") else "open"
            if last.get((job.id, key)) == status:
                continue
            state.append_record(
                INDEX,
                {
                    "job": job.id,
                    "target": job.target,
                    "round": review["round"],
                    "finding": key,
                    "rule": finding["rule"],
                    "status": status,
                },
                stamp,
            )
            last[(job.id, key)] = status

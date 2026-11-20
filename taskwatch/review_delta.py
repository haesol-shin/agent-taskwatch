"""Round 2 and later of a code review: the fix since the last review, and when it is not enough.

taskwatch, not the reviewer, decides whether a later round reads only the delta (the diff from
the last reviewed commit to the current one) or the whole result again. The conditions and their
starting thresholds are in docs/checks.md, "Re-review scope". Everything here is plain code:
git diff and arithmetic on the review records. Nothing calls a model.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .fingerprint import FingerprintError, _git_ok

# F2. Provisional: starting guesses, to be tuned on the per-round records (docs/checks.md).
MAX_DELTA_RATIO = 0.5
MAX_DELTA_LINES = 400
STAT_LINES = 60
# F6. Paths whose change is read in full whatever its size.
SECURITY = re.compile(
    r"(^|/)(\.github|\.ssh|secret[^/]*|credential[^/]*|auth[^/]*|security[^/]*|\.env[^/]*"
    r"|[^/]*\.(pem|key))(/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Scope:
    mode: str  # "delta" or "full"
    reasons: tuple[str, ...]  # one line per condition that holds, F-code first
    new_files: tuple[str, ...]  # F1: delta files nobody has reviewed; read in full
    files: tuple[str, ...]  # every file the delta touches
    stat: str  # `git diff --stat` of the delta, cut to STAT_LINES
    lines: int | None  # added plus deleted lines of the delta; None when unreadable
    first_lines: int | None  # the same for the diff of the first review
    ratio: float | None  # lines / first_lines; None when either is unknown or first is 0
    sweep: tuple[str, ...] = ()  # F7: ids of repeat findings to search for by cause

    def record(self) -> dict:
        """What the review record keeps of this round, so the thresholds can be tuned later."""
        return {
            "mode": self.mode,
            "reasons": list(self.reasons),
            "new_files": list(self.new_files),
            "files": list(self.files),
            "lines": self.lines,
            "first_lines": self.first_lines,
            "ratio": self.ratio,
            "sweep": list(self.sweep),
        }


def _numstat(repo: Path, env: Mapping[str, str], old: str, new: str) -> dict[str, int]:
    """path -> added plus deleted lines (0 for a binary file) of `git diff old new`."""
    out = _git_ok(repo, ["diff", "--numstat", "--no-renames", "-z", old, new, "--"], env=env)
    rows: dict[str, int] = {}
    for entry in out.decode("utf-8", "replace").split("\0"):
        if not entry:
            continue
        added, deleted, path = entry.split("\t", 2)
        rows[path] = int(added) + int(deleted) if added.isdigit() and deleted.isdigit() else 0
    return rows


def _stat(repo: Path, env: Mapping[str, str], old: str, new: str) -> str:
    out = _git_ok(repo, ["diff", "--stat=100", "--no-renames", old, new, "--"], env=env)
    lines = out.decode("utf-8", "replace").splitlines()
    if len(lines) > STAT_LINES:
        lines = [*lines[: STAT_LINES - 1], f"... ({len(lines) - STAT_LINES + 1} more lines)"]
    return "\n".join(lines)


def _named(path: str, scope_text: str, roots: Sequence[str] = ()) -> bool:
    """The approval scope names this file or one of its folders, relative to the repository
    or as an absolute path under one of `roots` (the work folder, the target)."""
    for token in re.findall(r"[\w./-]+", scope_text):
        token = token.rstrip("/")
        under_root = False
        for root in roots:
            root = root.rstrip("/")
            if root and token == root:
                return True  # the scope names the whole repository
            if root and token.startswith(root + "/"):
                token, under_root = token[len(root) + 1 :], True
                break
        # A bare word like "src" is a path only when it was written under a root.
        if (under_root or "/" in token or "." in token) and (
            path == token or path.startswith(token + "/")
        ):
            return True
    return False


def _repeated(last: Mapping) -> tuple[str, ...]:
    """Ids of the last review's findings that name an earlier finding as the same defect."""
    return tuple(
        sorted(
            f"r{last['round']}.{finding['finding']}"
            for finding in last.get("findings", ())
            if finding.get("repeat_of")
        )
    )


def effective_base(
    repo: Path, env: Mapping[str, str], fingerprint: str, branch: str, old: str
) -> str:
    """The commit the reviewed result stands on now: the parent of the oldest commit of the
    branch that no other branch or remote ref holds. A worker that rebased onto newer work
    changes it; `old` (the commit recorded when the work folder was made) is the answer when
    the branch has no commit of its own or git cannot tell."""
    args = ["rev-list", "--reverse", "--first-parent", "--parents", fingerprint, "--not"]
    if branch:
        args.append(f"--exclude={branch}")
    args += ["--branches", "--remotes"]
    try:
        out = _git_ok(repo, args, env=env).decode().splitlines()
    except FingerprintError:
        return old
    if not out:
        return old
    parts = out[0].split()
    return parts[1] if len(parts) > 1 else old


def decide(
    repo: Path,
    env: Mapping[str, str],
    *,
    base: str,
    fingerprint: str,
    approval_scope: str,
    prior: Sequence[Mapping],
    roots: Sequence[str] = (),
) -> Scope:
    """The scope of the round that follows the reviews in `prior` (at least one).

    See the conditions and their effects in docs/checks.md, "Re-review scope".
    """
    first, last = prior[0], prior[-1]
    sweep = _repeated(last)
    full: list[str] = []
    if last.get("base") != base:
        full.append(
            f"F3: the base commit differs from the last review's ({last.get('base') or '-'})"
        )
    try:
        delta = _numstat(repo, env, last["fingerprint"], fingerprint)
        stat = _stat(repo, env, last["fingerprint"], fingerprint)
    except (FingerprintError, ValueError, KeyError) as exc:
        reason = f"the latest delta cannot be read, so the round reads everything: {exc}"
        return Scope("full", (*full, reason), (), (), "", None, None, None, sweep=sweep)

    lines = sum(delta.values())
    first_lines = None
    new_files = ()
    try:
        first_rows = _numstat(repo, env, first.get("base") or base, first["fingerprint"])
        first_lines = sum(first_rows.values())
        seen = set(first_rows) | set(
            _numstat(repo, env, last.get("base") or base, last["fingerprint"])
        )
        new_files = tuple(sorted(set(delta) - seen))
    except (FingerprintError, ValueError, KeyError) as exc:
        full.append(
            "Earlier review history cannot be read. Do not infer which files earlier rounds "
            f"saw from the missing history: {exc}"
        )
    ratio = round(lines / first_lines, 2) if first_lines else None
    over_ratio = first_lines is not None and lines > first_lines * MAX_DELTA_RATIO
    if lines > MAX_DELTA_LINES or over_ratio:
        first_size = "unknown" if first_lines is None else f"{first_lines} lines"
        full.append(
            f"F2: the delta is {lines} lines (first review: {first_size}; "
            f"limits: {MAX_DELTA_RATIO:.0%} of it, {MAX_DELTA_LINES} lines)"
        )
    if any(f.get("severity") == "P0" for f in last.get("findings", ())):
        full.append("F5: the last review had a P0 finding")
    touched = sorted(p for p in delta if SECURITY.search(p) or _named(p, approval_scope, roots))
    if touched:
        full.append(
            f"F6: the delta touches a security or approval-scope path: {', '.join(touched)}"
        )
    reasons = list(full)
    if new_files:
        reasons.insert(
            0, f"F1: the delta touches files no earlier round saw: {', '.join(new_files)}"
        )
    mode = "full" if full else "delta"
    return Scope(
        mode,
        tuple(reasons),
        new_files,
        tuple(sorted(delta)),
        stat,
        lines,
        first_lines,
        ratio,
        sweep=sweep,
    )


def new_findings(prior: Sequence[Mapping], findings: Sequence[Mapping]) -> int:
    """Findings of this round whose rule and location no earlier round reported."""
    seen = {(f["rule"], f["location"]) for r in prior for f in r.get("findings", ())}
    return sum((f["rule"], f["location"]) not in seen for f in findings)


def check_line(checks: Sequence[Mapping]) -> str:
    """The last check, so the reviewer need not run again what it ran."""
    if not checks:
        return "Last check: none recorded."
    check = checks[-1]
    state = "passed" if check.get("ok") else "did not pass"
    return (
        f"Last check: {state}, exit code {check.get('exit_code')}, log {check.get('log')}. "
        "Do not run again what it ran; read its log when a claim depends on it."
    )


def message_lines(scope: Scope, round_number: int, checks: Sequence[Mapping]) -> list[str]:
    """The part of a round 2+ message that depends on the scope. Guidelines carry the steps."""
    if scope.mode == "full":
        head = [
            f"This is re-review round {round_number}, and a full review: taskwatch found "
            "a reason to read the whole result again. Read all of it and judge it as a later "
            "round (K-22), closing each finding below.",
            *(f"- {reason}" for reason in scope.reasons),
        ]
        if scope.lines is None:
            head.append("Do not treat the missing delta file list as proof a file was untouched.")
        else:
            head.append(
                "For a new P0 or P1 in a file the delta does not touch, give the reason in "
                "the finding: new evidence, or what the fix changed. Without that reason "
                "report P1 findings as P2."
            )
    else:
        head = [
            f"This is re-review round {round_number}. Review the fix, not the whole job again: "
            "close each finding below, read every changed line of the delta, read the users of "
            "what changed, and read nothing else of an untouched file outside any sweep "
            "listed below. Do not reopen settled ground."
        ]
    head.append(
        "A P0 stays blocking even without a reason it was not visible before. "
        "This overrides any copied guideline that would report it as P2."
    )
    delta: list[str] = []
    if scope.lines is not None:
        ratio = "" if scope.ratio is None else f" ({scope.ratio:.0%} of the first review's diff)"
        delta = [
            f"Delta since the last review: {scope.lines} lines in {len(scope.files)} files{ratio}.",
            f"Files the delta touches: {', '.join(scope.files) or 'none'}",
        ]
    if scope.new_files:
        delta.append(
            "No earlier round saw these files. Read each in full, as in a first review: "
            + ", ".join(scope.new_files)
        )
    sweep = []
    if scope.sweep:
        sweep = [
            "",
            f"Same-cause sweep for repeated findings: {', '.join(scope.sweep)}. Search the "
            "whole result for the same cause of each repeat and report every location in one "
            "finding per cause (K-20 and C-7).",
            "",
        ]
    return [*head, *delta, *sweep, check_line(checks)]

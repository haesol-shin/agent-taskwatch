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


def _severe(prior: Sequence[Mapping]) -> bool:
    return any(f.get("severity") == "P0" for r in prior for f in r.get("findings", ()))


def _recurred(prior: Sequence[Mapping], checks: Sequence[Mapping]) -> list[str]:
    """Rules of the last review's findings that an earlier round already named."""
    last = {f["rule"] for f in prior[-1].get("findings", ()) if f["rule"] != "other"}
    before = {f["rule"] for r in prior[:-1] for f in r.get("findings", ())}
    named = checks[-1].get("recurred", {}) if checks else {}
    return sorted((last & before) | set(named))


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
    checks: Sequence[Mapping],
    roots: Sequence[str] = (),
) -> Scope:
    """The scope of the round that follows the reviews in `prior` (at least one).

    F1 delta file outside what earlier rounds saw (read those files in full, nothing else
    changes); F2 delta over 50% of the first review's diff or over 400 lines; F3 the base
    commit differs from the last review's; F5 an earlier round had a P0; F6 the delta touches a
    security path or a path the approval scope names; F7 a rule recurred. Any of F2, F3, F5, F6
    and F7, or a diff that cannot be read, makes the round a full review.
    """
    first, last = prior[0], prior[-1]
    full: list[str] = []
    if last.get("base") != base:
        full.append(
            f"F3: the base commit differs from the last review's ({last.get('base') or '-'})"
        )
    try:
        delta = _numstat(repo, env, last["fingerprint"], fingerprint)
        stat = _stat(repo, env, last["fingerprint"], fingerprint)
        first_rows = _numstat(repo, env, first.get("base") or base, first["fingerprint"])
        seen = set(first_rows) | set(
            _numstat(repo, env, last.get("base") or base, last["fingerprint"])
        )
    except (FingerprintError, ValueError, KeyError) as exc:
        reason = f"the diff cannot be read, so the round reads everything: {exc}"
        return Scope("full", (*full, reason), (), (), "", None, None, None)
    lines, first_lines = sum(delta.values()), sum(first_rows.values())
    ratio = round(lines / first_lines, 2) if first_lines else None
    new_files = tuple(sorted(set(delta) - seen))
    if lines > MAX_DELTA_LINES or lines > first_lines * MAX_DELTA_RATIO:
        full.append(
            f"F2: the delta is {lines} lines against {first_lines} in the first review "
            f"(limits: {MAX_DELTA_RATIO:.0%} of it, {MAX_DELTA_LINES} lines)"
        )
    if _severe(prior):
        full.append("F5: an earlier round had a P0 finding")
    touched = sorted(p for p in delta if SECURITY.search(p) or _named(p, approval_scope, roots))
    if touched:
        full.append(
            f"F6: the delta touches a security or approval-scope path: {', '.join(touched)}"
        )
    if repeated := _recurred(prior, checks):
        full.append(f"F7: a rule recurred: {', '.join(repeated)}")
    reasons = list(full)
    if new_files:
        reasons.insert(
            0, f"F1: the delta touches files no earlier round saw: {', '.join(new_files)}"
        )
    mode = "full" if full else "delta"
    return Scope(
        mode, tuple(reasons), new_files, tuple(sorted(delta)), stat, lines, first_lines, ratio
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
            "a reason to read the whole result again. Review it as in a first review, and "
            "still close each finding below.",
            *(f"- {reason}" for reason in scope.reasons),
        ]
    else:
        head = [
            f"This is re-review round {round_number}. Review the fix, not the whole job again: "
            "close each finding below, read every changed line of the delta, read the users of "
            "what changed, and read nothing else of an untouched file. Do not reopen settled "
            "ground."
        ]
    delta: list[str] = []
    if scope.lines is not None:
        ratio = "" if scope.ratio is None else f" ({scope.ratio:.0%} of the first review's diff)"
        delta = [
            f"Delta since the last review: {scope.lines} lines in {len(scope.files)} files{ratio}.",
            f"Files the delta touches: {', '.join(scope.files) or 'none'}",
        ]
    if scope.new_files and scope.mode == "delta":
        delta.append(
            "No earlier round saw these files. Read each in full, as in a first review: "
            + ", ".join(scope.new_files)
        )
    return [*head, *delta, check_line(checks)]

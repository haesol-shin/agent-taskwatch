"""Run one step of a job: a work or fix session, the deterministic check, or a fresh review.

One service runs one step and exits. Stage changes are compare-and-set on the attempt and
the step's stage under the state lock, so output of an old attempt cannot change a job.
A step that fails without a routed outcome records the error and leaves the stage, so the
scan resumes it. Nothing here calls a model except the omp RPC sessions of work, fix and review.
"""

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import (
    brief,
    choices,
    job_findings,
    job_text,
    job_workspace,
    review_delta,
    review_print,
    source_check,
)
from .checks import checklist_problems, message_problems
from .evidence import basis_of
from .executor import _design_check, _finding_lines, _scope, _verdict_line
from .fingerprint import (
    FingerprintError,
    _git_ok,
    files_fingerprint,
    head_sha,
    range_messages,
    resolve_commit,
    uncommitted_changes,
)
from .job_env import job_environment
from .job_rpc import RpcAborted, RpcClient, RpcError
from .jobs import (
    LANGUAGE_NAMES,
    MAX_RECOVERIES,
    UNIT_FILES,
    JobConfig,
    Jobs,
    JobState,
    progress_fingerprint,
)
from .kinds import declares_choices, kind_label
from .rate_limit import RateLimit
from .review_docs import docs_dir, profile_names

STEPS = frozenset({"work", "fix", "check", "review", "land"})
SOURCE_TABLE = "source-table.md"
EVIDENCE_EVERY = 15.0
SETTLE_POLL = 2.0
WATCH_EVERY = 2.0
_EVENTS = frozenset(
    {"agent_start", "agent_end", "prompt_result", "session_settled", "subagent_lifecycle"}
)
_PROGRESS_FRAMES = frozenset({"message_end", "tool_execution_end", "turn_end", "subagent_progress"})
_EVENT_FIELDS = (
    "id",
    "sessionFile",
    "parentToolCallId",
    "subagentId",
    "status",
    "agentInvoked",
    "sessionSettled",
    "isTerminal",
    "yielded",
)


def _now() -> float:
    return time.time()


class StepError(Exception):
    """The step failed in a way the scan can resume. The message is recorded.

    `rate_limit` is set when the step ended on a terminal HTTP 429."""

    def __init__(self, message: str = "", rate_limit: RateLimit | None = None) -> None:
        super().__init__(message)
        self.rate_limit = rate_limit


class InputsChanged(StepError):
    """A copied input or review profile no longer matches its recorded hash."""


class StaleAttempt(Exception):
    """The job moved on. This process must stop without writing."""


@dataclass(frozen=True, slots=True)
class Ctx:
    jobs: Jobs
    config: JobConfig
    job: JobState
    folder: Path
    workspace: Path

    @property
    def stage(self) -> str:
        return self.job.stage

    @property
    def env(self) -> dict[str, str]:
        return job_environment(self.config.env)


def run_job(jobs: Jobs, config: JobConfig, job_id: str, attempt: int) -> int:
    """0: step done and outcome recorded. 1: failure recorded. 2: stale attempt, nothing written."""
    job = jobs.load().get(job_id)
    if job is None or job.attempt != attempt or not _attempt_open(job):
        return 2
    try:
        jobs.verify_inputs(job)
    except (ValueError, OSError) as exc:
        return _stop(jobs, job, "inputs_invalid", f"copied input is unusable: {exc}")
    try:
        if job.stage == "land":
            from .job_land import run_land

            return run_land(Ctx(jobs, config, job, jobs.folder(job), Path(job.workspace)))
        ctx = _context(jobs, config, job)
        step = {"check": _check_step, "review": _review_step}.get(job.stage, _agent_step)
        return step(ctx)
    except (StaleAttempt, RpcAborted):
        return 2
    except InputsChanged as exc:
        return _stop(jobs, job, "inputs_invalid", str(exc))
    except (RpcError, StepError, OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) or type(exc).__name__
        limit = getattr(exc, "rate_limit", None)
        if limit is not None:
            return _rate_limited(jobs, job, message, limit)
        return _fail(jobs, job, message)


def _attempt_open(job: JobState) -> bool:
    return (
        job.stage in STEPS
        and job.ended_at == 0
        and (job.stage != "review" or job.review_inflight == job.attempt)
    )


def _cas(jobs: Jobs, job: JobState, stages, build: Callable[[JobState], dict]) -> bool:
    with jobs.state.lock():
        current = jobs.load().get(job.id)
        if (
            current is None
            or current.attempt != job.attempt
            or current.stage not in stages
            or not _attempt_open(current)
        ):
            return False
        jobs.save(replace(current, **build(current)), _now())
        return True


def _apply(ctx: Ctx, build: Callable[[JobState], dict]) -> None:
    if not _cas(ctx.jobs, ctx.job, {ctx.stage}, build):
        raise StaleAttempt


def _event(job: JobState, **fields) -> list:
    return [*job.events, {"at": _now(), **fields}]


def _fail(jobs: Jobs, job: JobState, message: str) -> int:
    done = _cas(
        jobs,
        job,
        {job.stage},
        lambda j: {
            "error": message,
            "events": _event(j, type="step_failed", error=message),
            **({"review_inflight": 0} if j.stage == "review" else {}),
        },
    )
    return 1 if done else 2


def _rate_limited(jobs: Jobs, job: JobState, message: str, limit: RateLimit) -> int:
    """A terminal HTTP 429 ends the step without spending a recovery or a review.

    The review this attempt reserved and never completed is refunded, nobody else's. A known
    deadline keeps the saved stage for the scanner to retry once it has passed; none stops for
    the owner. The process ends here; nothing sleeps until the deadline."""

    def build(current: JobState) -> dict:
        now = _now()
        events = _event(
            current, type="rate_limited", stage=current.stage, retry_not_before=limit.retry_at
        )
        changes = {
            "error": message,
            "ended_at": now,
            "resume_stage": current.stage,
            "events": events,
            **_refund(current),
        }
        if limit.retry_at is not None:
            return {**changes, "retry_not_before": limit.retry_at}
        # No usable hint: nothing says when to retry, so the owner decides.
        events.append({"at": now, "type": "user_wait", "reason": "rate_limit"})
        return {**changes, "stage": "user_wait", "wait_reason": "rate_limit"}

    return 1 if _cas(jobs, job, {job.stage}, build) else 2


def _refund(job: JobState) -> dict:
    """Return the reservation of this attempt if its review never completed."""
    if job.stage == "review" and job.review_inflight == job.attempt:
        return {"reviews_used": job.reviews_used - 1, "review_inflight": 0}
    return {}


def _stop(jobs: Jobs, job: JobState, reason: str, message: str) -> int:
    done = _cas(
        jobs,
        job,
        {job.stage},
        lambda j: {
            "stage": "user_wait",
            "wait_reason": reason,
            "ended_at": _now(),
            "error": message,
            "events": _event(j, type="user_wait", reason=reason),
        },
    )
    return 1 if done else 2


# --- workspace and files -------------------------------------------------------------------


def _context(jobs: Jobs, config: JobConfig, job: JobState) -> Ctx:
    folder = jobs.folder(job)
    workspace, described = _workspace(job, folder, config, job_environment(config.env), jobs)
    ctx = Ctx(jobs, config, replace(job, workspace=str(workspace), **described), folder, workspace)
    hashes = _prepare(ctx)
    recorded = job.guideline_hashes
    if recorded is not None and recorded != hashes:
        raise InputsChanged("a copied review profile does not match its recorded hash")
    _apply(ctx, lambda j: {"workspace": str(workspace), "guideline_hashes": hashes, **described})
    return ctx


def _workspace(
    job: JobState, folder: Path, config: JobConfig, env: Mapping[str, str], jobs: Jobs | None = None
) -> tuple[Path, dict]:
    """The step's folder, and the record fields a newly created work folder adds."""
    if job.review_target is not None:
        # Nothing is built, so the reviewer reads the target itself.
        return Path(job.target), {}
    if job.review != "code":
        path = folder / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path, {}
    path = folder / "worktree"
    if (path / ".git").exists():
        # A resumed job keeps its folder and branch.
        return path, job_workspace.adopt(job, path, env)
    if path.exists() and any(path.iterdir()):
        raise StepError("the job worktree folder exists but is not a worktree")
    try:
        if jobs is not None:
            prepared = job_workspace.prepare(job, env, jobs)
            if not _cas(jobs, job, {job.stage}, lambda current: prepared):
                raise StaleAttempt
            job = replace(job, **prepared)
        return path, job_workspace.create(job, path, config, env, jobs=jobs)
    except (job_workspace.WorkspaceError, subprocess.TimeoutExpired) as exc:
        raise StepError(str(exc) or type(exc).__name__) from exc


def _docs_source(folder: str = "review") -> Path:
    source = docs_dir(folder)
    if source is None:
        raise StepError(f"{folder} documents were not found")
    return source


def _layer(name: str) -> str:
    """The launch-path text of this path, read when the message is built."""
    try:
        return (_docs_source("launch") / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepError(f"launch layer is unreadable: {name}: {exc}") from exc


def _guidelines(job: JobState, folder: Path) -> tuple[Path, ...]:
    """The job's own copy of the review profile, so a basis cannot drift while it runs."""
    names = profile_names(job.review, job.outputs)
    target = folder / "review-docs"
    target.mkdir(parents=True, exist_ok=True)
    source = None
    for name in names:
        if not (target / name).is_file():
            source = source or _docs_source()
            shutil.copyfile(source / name, target / name)
    return tuple(target / name for name in names)


def _write_durable(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _prepare(ctx: Ctx) -> dict[str, str]:
    """Write the step's files durably; return the hashes of the job's review profile."""
    job, folder = ctx.job, ctx.folder
    (folder / "logs").mkdir(exist_ok=True)
    _write_durable(folder / "prompt.md", job.prompt)
    if job.review_target is None:
        _write_durable(folder / "WORKFLOW.md", _workflow(ctx))
    return {str(p): _file_sha(p) for p in _guidelines(job, folder)}


def _cli_command(ctx: Ctx, *words: str) -> str:
    argv = [
        sys.executable,
        "-m",
        "taskwatch.job_cli",
        "--root",
        str(ctx.jobs.state.root),
        *words,
    ]
    return " ".join(shlex.quote(part) for part in argv)


def _wait_command(ctx: Ctx) -> str:
    return _cli_command(ctx, "wait", ctx.job.id, "--reason", "<what you need approved>")


_APPROVAL_RULES = """## Approval
Approval source: {source}
Approved scope (complete; nothing outside it is approved):
{scope}

Do not write outside the workspace{exception}, send a message, call an external service or change
any external state beyond that scope. If a step needs something outside it, run this
command first, which records the request before you act, then stop work immediately and
take no further action:
  {command}
The user can then approve; the job resumes from its recorded stage."""


def _worker_paths(ctx: Ctx) -> tuple[Path, ...]:
    if ctx.job.review_target is not None:
        return ()
    return tuple(
        ctx.folder / name
        for name in (
            ctx.job.progress,
            ctx.job.checklist,
            "brief.md",
            job_findings.FINDINGS_FILE,
            job_findings.NOTE_FILE,
        )
    ) + tuple(
        job_text.unit_file(ctx.folder, unit["id"], name)
        for unit in ctx.job.public_text
        for name in UNIT_FILES
    )


def _approval_text(ctx: Ctx) -> str:
    job = ctx.job
    paths = _worker_paths(ctx)
    exception = " (except the exact worker-owned files listed below)" if paths else ""
    text = _APPROVAL_RULES.format(
        source=job.approval_source,
        scope=job.approval_scope,
        command=_wait_command(ctx),
        exception=exception,
    )
    if paths:
        text += "\n\nWritable worker-owned files (outside the workspace):\n"
        text += "\n".join(f"- `{path}`" for path in paths)
        text += (
            "\n\nCreate only the parent directories needed for these named files."
            "\nNo directory deletion or writes to any other job-folder file are authorized."
            "\nKeep all listed files outside the workspace; never copy them into it."
            "\nWrite findings when repairing review findings, and root-cause evidence for"
            " repeated rules; neither is required for an untouched initial result."
            "\nDeclared public-text files are local preparation only, not publication authority."
        )
    return text


def _kind_text(job: JobState) -> str:
    """The brief contract and the kind's own rules, read from docs/kinds when the step starts."""
    names = ("brief.md", f"{kind_label(job)}.md")
    try:
        return "\n\n".join(
            (_docs_source("kinds") / name).read_text(encoding="utf-8").strip() for name in names
        )
    except OSError as exc:
        raise StepError(f"kind documents are unreadable: {exc}") from exc


def _language_name(config: JobConfig) -> str | None:
    return LANGUAGE_NAMES[config.language] if config.language else None


def _language_lines(config: JobConfig) -> list[str]:
    """What the reviewer writes that the owner reads in the card; none when no language is set."""
    name = _language_name(config)
    if name is None:
        return []
    return [f"Write the `summary` of each finding and the items of `not_checked` in {name}."]


def _workflow(ctx: Ctx) -> str:
    job = ctx.job
    progress = ctx.folder / job.progress
    checklist = ctx.folder / job.checklist
    work_inputs = "\n".join(f"- {p}" for p in job.inputs.get("work", [])) or "- (none)"
    name = _language_name(ctx.config)
    card_language = (
        f"- Write the lines of the brief's `Card` section in {name}: the owner reads them.\n"
        if name
        else ""
    )
    return f"""# Workflow for job {job.id} ({job.review})

Work only in the workspace: {ctx.workspace}
Only the exact worker-owned files listed under Approval may be written outside it.

## Start
1. Read the intent: {job.intent}
2. Read the spec: {job.spec}
3. Read the original request: {ctx.folder / "prompt.md"}
4. Read the progress file `{progress}` if it exists, and continue unfinished work.
5. Read the checklist `{checklist}` if it exists.

Work inputs:
{work_inputs}

## While working
- Required result files, relative to the workspace: {", ".join(job.outputs)}
- Keep `{progress}` current: what is done, what remains, evidence.
- Keep `{checklist}` as JSON: {{"items": [{{"id": "...", "passes": true or false}}]}}.
  Set `passes` to true only when the evidence for that item exists.
- Write the decision brief `{ctx.folder / "brief.md"}` for the owner, as the brief contract
  below says. The check fails without it. State every choice you made, even a small one.
- Keep every worker-owned file listed under Approval outside the workspace; never copy it in.
- Code jobs: commit everything in the workspace. The checks accept only a clean, committed
  result.
- When a review sends you back to fix findings, record each one in
  `{ctx.folder / job_findings.FINDINGS_FILE}` before you finish, as the fix message shows.
  A rule that repeats also needs a section in `{ctx.folder / job_findings.NOTE_FILE}`.
- While working, run only the tests that cover the files you changed. Run the whole check once,
  with the `--run-command` option of the command in the End section, just before you end.
{card_language}- If you delegate to subagents, wait until each one finished.

{_kind_text(job)}

{_approval_text(ctx)}

## End
Before you finish, run this command. It prints what the runner's checks would reject (missing
files, checklist, brief, public text, commit messages, finding records) and exits 0 when
nothing is left; fix what it names and run it again:
  {_cli_command(ctx, "check", job.id)}
Add `--run-command` to also run the check command. Finish only when the checklist is complete
and the required files exist. The check command runs outside your session:
{json.dumps(job.check_command)}. A passing claim by you is not evidence.
"""


# --- messages ------------------------------------------------------------------------------


def _predecessor_lines(ctx: Ctx) -> list[str]:
    """One read-only block per predecessor. Nothing is copied: the spec says what to use."""
    if not ctx.job.predecessors:
        return []
    records = ctx.jobs.load()
    lines = ["", "Predecessor results (read only; the spec says how to use them):"]
    for identifier in ctx.job.predecessors:
        pred = records.get(identifier)
        if pred is None:
            lines.append(f"- {identifier}: not found")
            continue
        lines.append(f"- {pred.id} ({kind_label(pred)}), stage {pred.stage}")
        lines += [f"  {line}" for line in _result_lines(ctx.jobs, ctx.config, pred)]
    return lines


def _result_lines(jobs: Jobs, config: JobConfig, pred: JobState) -> list[str]:
    if pred.review == "code" and pred.review_target is None:
        branch = pred.branch or f"{config.branch_prefix}{pred.id[:8]}"
        result = [f"branch: {branch} in {pred.target}"]
    elif pred.workspace:
        result = [f"output: {Path(pred.workspace) / name}" for name in pred.outputs]
    else:
        result = ["output: (no workspace recorded)"]
    return [
        *result,
        f"result fingerprint: {pred.fingerprint or '(none yet)'}",
        f"brief: {jobs.folder(pred) / 'brief.md'}",
    ]


def _agent_message(ctx: Ctx, resumed: bool) -> str:
    job = ctx.job
    head = job.prompt
    lines = [
        "",
        "---",
        f"Taskwatch job {job.id} ({job.review}). Read {ctx.folder / 'WORKFLOW.md'} first.",
        f"Workspace: {ctx.workspace}",
        f"Intent: {job.intent}",
        f"Spec: {job.spec}",
    ]
    if job.review_target is None:
        lines += [f"Progress file: {ctx.folder / job.progress}"]
        lines += [f"Checklist file: {ctx.folder / job.checklist}"]
        lines += [f"Brief file: {ctx.folder / 'brief.md'}"]
    for path in job.inputs.get("work", []):
        lines.append(f"Work input: {path}")
    lines += choices.defaults_lines(ctx.jobs.state.root, job)
    lines += _predecessor_lines(ctx)
    lines += ["", _approval_text(ctx)]
    if ctx.stage == "fix":
        lines += ["", "This is a fix round. Fix exactly what follows, then finish."]
        lines.append(_fix_reason(job))
        lines += _record_lines(ctx)
    if resumed:
        lines += [
            "",
            "You were interrupted. Read the progress file, finish only what is unfinished,",
            "and do not redo completed work.",
        ]
    return head + "\n".join(lines)


def _record_lines(ctx: Ctx) -> list[str]:
    """How to record the findings of the last failed review, so the next check can verify them."""
    job = ctx.job
    review = job_findings.last_failed(job.reviews)
    if review is None or job.review_target is not None:
        return []
    ids = ", ".join(job_findings.finding_id(review, f) for f in review["findings"])
    path = ctx.folder / job_findings.FINDINGS_FILE
    lines = [
        "",
        f"Record the findings of round {review['round']} ({ids}) in {path}, a JSON list with one",
        "entry per finding. The next check rejects a missing, malformed or unverified entry:",
        '[{"id": "r1.F1", "status": "fixed", "commit": "<full 40-character SHA>"},',
        ' {"id": "r1.F2", "status": "not_fixed", "reason": "<nonblank explanation>"}]',
        "not_fixed explains; it does not dismiss the finding, and the reviewer still decides.",
    ]
    if job.review == "code":
        anchor = job_findings.reviewed_head(review)
        lines.append(
            "For a code result, `commit` is the full SHA of the commit that fixes the finding: "
            f"made after the reviewed commit {anchor or '(not recorded)'} and contained in HEAD. "
            "A fix of public text only names the current HEAD."
        )
    return lines


def _latest(job: JobState) -> dict | None:
    records = [r for r in (job.checks[-1:] + job.reviews[-1:]) if isinstance(r, Mapping)]
    return max(records, key=lambda r: r.get("seq", 0), default=None)


def _fix_reason(job: JobState) -> str:
    record = _latest(job)
    if record is None:
        return "No failure is recorded. Re-verify the result against the spec."
    if "exit_code" in record:
        problems = "\n".join(f"- {p}" for p in record["problems"])
        return f"The deterministic check failed.\n{problems}\nFull output: {record['log']}"
    problems = "\n".join(f"- {p}" for p in record["problems"])
    return f"The independent review did not pass.\n{problems}\n\nReview:\n{record['body']}"


def _subject_lines(ctx: Ctx) -> list[str]:
    job, target = ctx.job, ctx.job.review_target
    if target is None:
        log = job.checks[-1]["log"] if job.checks else ""
        return [
            f"Reviewed result: {ctx.workspace} (files: {', '.join(job.outputs)})",
            f"Result fingerprint: {job.fingerprint}",
            f"Check output: {log}",
            *_source_lines(ctx),
            f"Brief: {ctx.folder / 'brief.md'} (compare it with the result, rule C-19)",
        ]
    if "commit" in target:
        what = f"commit {target['commit']} of the repository at {ctx.workspace}"
    else:
        what = f"files {', '.join(target['files'])} under {ctx.workspace}"
    return [f"Review target: {what}", f"Target fingerprint: {job.fingerprint}"]


def _source_lines(ctx: Ctx) -> list[str]:
    if source_check.CLAIMS not in ctx.job.outputs:
        return []
    return [
        f"Source table: {ctx.folder / SOURCE_TABLE} (claim, quote, source, quote found; "
        "judge support only for the pairs the check found present, rule S-9)"
    ]


def _source_record(ctx: Ctx) -> dict:
    """The counts the brief shows, and the table the reviewer reads, from the saved sources."""
    if source_check.CLAIMS not in ctx.job.outputs:
        return {}
    result = source_check.check(ctx.workspace, ctx.job.outputs)
    try:
        _write_durable(ctx.folder / SOURCE_TABLE, source_check.table(result.rows))
    except OSError:
        pass  # the next check writes it; the problems above already fail this one
    return {"sources": {"claims": result.claims, "snippet_only": result.snippet_only}}


def _text_lines(job: JobState, review_root: Path) -> list[str]:
    """Where the reviewed public text is, and which commits each unit covers."""
    if not job.public_text:
        return []
    lines = [f"Public text snapshot (read only): {review_root / job_text.SNAPSHOT}"]
    for unit in job.public_text:
        state = job.texts.get(unit["id"], {})
        base, head = state.get("base", unit["base"]), state.get("head", unit["head"])
        lines.append(
            f"Unit {unit['id']} (text_contract=2): review title.txt, body.md, commit.txt "
            f"(final squash body), and the separate step messages under commits/ in "
            f"{review_root / job_text.SNAPSHOT / unit['id']}; "
            f"judge each claim against `git diff {base} {head}` and `git log {base}..{head}`."
            " Validate the title plus one blank line plus commit.txt as the squash message; "
            "the squash body must never be inferred from the step messages."
        )
    return lines


def _text_since(ctx: Ctx, last: Mapping, review_root: Path) -> list[str]:
    """What changed in the public text since the last review, and whether only text did."""
    job = ctx.job
    if not job.public_text:
        return []
    old = last.get("texts", {})
    same_tree = all(job.texts.get(u, {}).get("tree") == s.get("tree") for u, s in old.items())
    lines = []
    snapshot = last.get("snapshot")
    diff = (
        job_text.snapshot_diff(Path(snapshot), review_root / job_text.SNAPSHOT) if snapshot else ""
    )
    if diff:
        if same_tree and old:
            lines.append("The tree is identical to the reviewed tree; only text changed.")
        lines += ["Text changed since the last review (reviewed to current):", diff]
    return lines


def _finding_notes(ctx: Ctx, last: Mapping) -> list[str]:
    """The worker's record of the last findings, and the root-cause notes of repeated rules."""
    job = ctx.job
    lines = []
    status = job_findings.status_lines(job, ctx.folder)
    if status:
        lines += ["Worker record of the last findings; accept or reject each not_fixed reason:"]
        lines += status
    sections = job_findings.notes(ctx.folder)
    for rule in sorted({f["rule"] for f in last.get("findings", [])} & sections.keys()):
        note = "; ".join(f"{label} {text}" for label, text in sections[rule].items())
        lines.append(f"Root-cause note for {rule}: {note}")
    recurred = job.checks[-1].get("recurred", {}) if job.checks else {}
    for rule, prevention in recurred.items():
        lines.append(
            f"Rule {rule} repeated after a note named this prevention: {prevention}. "
            "Answer whether it was applied."
        )
    return lines


def _prior_lines(
    ctx: Ctx, prior: list[Mapping], review_root: Path, scope: review_delta.Scope | None = None
) -> list[str]:
    job = ctx.job
    last = prior[-1]
    if job.review_target is not None or not job_findings.accepted(last):
        # Nothing was fixed between these rounds: the last answer could not be accepted.
        problems = "; ".join(last["problems"]) or "none recorded"
        lines = [
            "",
            f"This is round {len(prior) + 1}. The last answer was not accepted: {problems}.",
            "Review the whole target again and answer in full.",
        ]
        previous = job_findings.last_failed(prior)
        if previous is not None:
            lines += [
                *_finding_notes(ctx, previous),
                "Findings of the last substantive failed review:",
                previous["body"],
            ]
        return lines
    if scope is not None:
        lines = ["", *review_delta.message_lines(scope, len(prior) + 1, job.checks)]
    else:
        lines = [
            "",
            f"This is re-review round {len(prior) + 1}. Review the changes since the last review",
            "and whether each finding below is resolved. Do not reopen settled ground.",
        ]
    lines += [
        f"Previously reviewed fingerprint: {last['fingerprint']}",
        f"Current fingerprint: {job.fingerprint}",
    ]
    text = _text_since(ctx, last, review_root)
    if job.review == "code" and not any(line.startswith("The tree is identical") for line in text):
        lines.append(f"See: git diff {last['fingerprint']} {job.fingerprint}")
        if scope is not None and scope.stat:
            lines += ["git diff --stat of the delta:", scope.stat]
    lines += text
    return [*lines, *_finding_notes(ctx, last), "Findings of the last review:", last["body"]]


def _review_message(
    ctx: Ctx,
    guidelines: tuple[Path, ...],
    resumed: bool,
    review_root: Path,
    scope: review_delta.Scope | None = None,
) -> str:
    job = ctx.job
    prior = [r for r in job.reviews if isinstance(r, Mapping)]
    lines = [
        f"You are the independent reviewer for job {job.id} ({job.review}).",
        f"Do not edit the deliverable. Write review evidence only under {review_root}.",
        f"Read these guidelines first: {', '.join(str(p) for p in guidelines)}",
        f"Intent: {job.intent}",
        f"Spec: {job.spec}",
        f"Approval source: {job.approval_source}",
        f"Approval scope: {job.approval_scope}",
        *_subject_lines(ctx),
        *_text_lines(job, review_root),
        "If you delegate to subagents, wait until each one finished.",
        *_language_lines(ctx.config),
        "",
        "Original request, in full:",
        job.prompt,
    ]
    if prior:
        lines += _prior_lines(ctx, prior, review_root, scope)
    lines += [f"Review input: {p}" for p in job.inputs.get("review", [])]
    lines += choices.defaults_lines(ctx.jobs.state.root, job)
    lines += _predecessor_lines(ctx)
    if ctx.config.review_mode == "print":
        lines += [f"Verdict schema: {_schema_path(ctx)}", "", _layer("taskwatch-print.md")]
    else:
        lines += ["", _layer("taskwatch.md")]
    if resumed:
        lines += ["", "You were interrupted. Review the current state again and answer in full."]
    return "\n".join(lines)


# --- RPC session ---------------------------------------------------------------------------


class Recorder:
    """Writes evidence from RPC frames. A refused write means the attempt is stale."""

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.abort = threading.Event()
        self.done = threading.Event()
        self._last = time.monotonic()

    def watch(self) -> None:
        """Raise the abort flag when the job leaves this attempt, even if omp is silent."""
        ctx = self.ctx
        while not self.done.wait(WATCH_EVERY):
            try:
                current = ctx.jobs.load().get(ctx.job.id)
            except (ValueError, OSError):
                continue
            if current is None or current.attempt != ctx.job.attempt or current.stage != ctx.stage:
                self.abort.set()
                return

    def frame(self, frame: dict) -> None:
        kind = frame.get("type")
        now = time.monotonic()
        if kind in _EVENTS:
            source = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            summary = {k: source[k] for k in _EVENT_FIELDS if k in source}
            self._write({"type": kind, **summary})
        elif kind in _PROGRESS_FRAMES and now - self._last >= EVIDENCE_EVERY:
            self._write(None)
        else:
            return
        self._last = now

    def beat(self) -> None:
        """Refresh the evidence of a run that gives no frames."""
        self._write(None)

    def _write(self, event: dict | None) -> None:
        ctx = self.ctx

        def build(job: JobState) -> dict:
            now = _now()
            changes = {"evidence_at": now, **_progress_changes(job, ctx, now)}
            if event is not None:
                changes["events"] = _event(job, **event)
            return changes

        if not _cas(ctx.jobs, ctx.job, {ctx.stage}, build):
            self.abort.set()


def _schema_path(ctx: Ctx) -> Path:
    return ctx.folder / review_print.SCHEMA_NAME


def _command(ctx: Ctx, review_root: Path | None = None) -> tuple[str, ...]:
    """The review step may use its own command. {guidelines} and {review} name the job copy and
    the review kind, {schema} the verdict schema file, {output} the file of the last message
    (print mode)."""
    config = ctx.config
    if ctx.stage != "review" or config.review_command is None:
        return config.omp_command
    fields = {
        "{guidelines}": str(ctx.folder / "review-docs"),
        "{review}": ctx.job.review,
        "{schema}": str(_schema_path(ctx)),
    }
    if review_root is not None:
        fields["{output}"] = str(review_root / "reply.txt")
    return tuple(fields.get(part, part) for part in config.review_command)


def _write_schema(ctx: Ctx) -> dict:
    """Copy the verdict schema into the job folder, where the review command reads it."""
    try:
        text = (_docs_source("launch") / review_print.SCHEMA_NAME).read_text(encoding="utf-8")
        schema = json.loads(text)
        _write_durable(_schema_path(ctx), text)
    except (OSError, ValueError) as exc:
        raise StepError(f"the verdict schema is unusable: {exc}") from exc
    return schema


def _print_reply(ctx: Ctx, message: str, review_root: Path) -> tuple[str, str]:
    """Run the print-mode review command once. Returns the review text and, when the reply holds
    no acceptable verdict, the reason (the text is then the raw reply, for the fix session)."""
    schema = _write_schema(ctx)
    output = review_root / "reply.txt"
    output.unlink(missing_ok=True)
    recorder = Recorder(ctx)
    watcher = threading.Thread(target=recorder.watch, daemon=True)
    watcher.start()
    try:
        stdout = review_print.run(
            _command(ctx, review_root),
            ctx.workspace,
            ctx.env,
            message,
            timeout=float(ctx.config.rpc_timeout),
            every=EVIDENCE_EVERY,
            tick=recorder.beat,
            abort=recorder.abort,
            stderr_path=ctx.folder / "logs" / f"review-a{ctx.job.attempt}.stderr",
        )
    except review_print.RunError as exc:
        raise StepError(str(exc), exc.rate_limit) from exc
    finally:
        recorder.done.set()
    if recorder.abort.is_set():
        raise StaleAttempt
    replies = []
    if output.is_file():
        replies.append(("last-message file", output.read_text(encoding="utf-8", errors="replace")))
    replies.append(("standard output", stdout))
    try:
        return review_print.verdict_body(replies, schema), ""
    except review_print.ReplyError as exc:
        raw = next((text for _, text in replies if text.strip()), "")
        return raw[: review_print.KEEP_CHARS], str(exc)


def _converse(
    ctx: Ctx,
    session_dir: Path,
    message: Callable[[bool], str],
    text: bool,
    repair: Callable[[str], str | None] | None = None,
) -> str:
    """One session: the prompt, and with `repair` at most one follow-up in the same session
    when `repair(reply)` returns a prompt."""
    timeout = float(ctx.config.rpc_timeout)
    recorder = Recorder(ctx)
    argv = [*_command(ctx), "--mode", "rpc", "--no-ui"]
    stderr = ctx.folder / "logs" / f"omp-a{ctx.job.attempt}.stderr"
    session_dir.mkdir(parents=True, exist_ok=True)
    watcher = threading.Thread(target=recorder.watch, daemon=True)
    watcher.start()
    try:
        with RpcClient(
            argv,
            ctx.workspace,
            on_frame=recorder.frame,
            stderr_path=stderr,
            abort=recorder.abort,
            poll=SETTLE_POLL,
            env=ctx.env,
        ) as client:
            client.start(timeout)
            # This launch's selection decides the model; the saved one must not.
            provider, model_id = client.startup_model(timeout)
            opened = client.open_session(session_dir, timeout, provider=provider, model_id=model_id)
            resumed = opened.get("resumed") is True
            path = opened.get("sessionFile")
            _apply(
                ctx,
                lambda j: {
                    "session_path": path if isinstance(path, str) else "",
                    "session_dir": str(session_dir),
                    "events": _event(j, type="session_open", resumed=resumed),
                },
            )
            client.prompt_and_wait(message(resumed), timeout)
            reply = client.last_assistant_text(timeout) if text else ""
            follow_up = repair(reply) if repair else None
            if follow_up:
                client.prompt_and_wait(follow_up, timeout)
                reply = client.last_assistant_text(timeout)
            client.finish()
    finally:
        recorder.done.set()
    if recorder.abort.is_set():
        raise StaleAttempt
    return reply


def _agent_step(ctx: Ctx) -> int:
    _converse(ctx, ctx.folder / "sessions" / "work", lambda r: _agent_message(ctx, r), False)
    stage = ctx.stage

    def build(job: JobState) -> dict:
        now = _now()
        return {
            "stage": "check",
            "resume_stage": "check",
            "ended_at": now,
            "error": "",
            "events": _event(job, type="step_exit", stage=stage, code=0),
            **_progress_changes(job, ctx, now),
        }

    _apply(ctx, build)
    return 0


def _inside(workspace: Path, relative: str) -> Path | None:
    path = (workspace / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(workspace.resolve()):
        return None
    return path


# --- progress, fingerprint, check ----------------------------------------------------------


def _progress_changes(job: JobState, ctx: Ctx, now: float) -> dict:
    current = progress_fingerprint(job, ctx.workspace, ctx.folder)
    if current == job.progress_hash:
        return {}
    return {"progress_hash": current, "last_progress_at": now}


def _fingerprint(job: JobState, workspace: Path, env: Mapping[str, str]) -> str:
    target = job.review_target
    if target is not None and "commit" in target:
        _git_ok(workspace, ["cat-file", "-e", f"{target['commit']}^{{commit}}"], env=env)
        return target["commit"]
    if job.review != "code" or target is not None:
        return files_fingerprint(workspace, job.outputs)
    dirty = uncommitted_changes(workspace, env)
    if dirty:
        raise FingerprintError(f"uncommitted changes: {'; '.join(dirty[:5])}")
    return head_sha(workspace, env)


def _basis(job: JobState, folder: Path) -> str:
    return basis_of(Path(job.intent), Path(job.spec), _guidelines(job, folder))


def _text_problems(
    job: JobState, workspace: Path, env: Mapping[str, str], bullet_one_line: bool
) -> list[str]:
    """The common commit message rules for each unit's commits; refs resolve at every check."""
    problems = []
    for unit in job.public_text:
        name = f"public text {unit['id']}"
        try:
            for ref in (unit["base"], unit["head"]):
                try:
                    resolve_commit(workspace, ref, env)
                except ValueError as exc:
                    raise ValueError(f"{ref} does not resolve: {exc}") from exc
            commits = range_messages(workspace, unit["base"], unit["head"], env)
        except (ValueError, OSError) as exc:
            problems.append(f"{name}: {exc}")
            continue
        if not commits:
            problems.append(f"{name}: no commits in {unit['base']}..{unit['head']}")
        for sha, message in commits:
            problems += [
                f"{name}: commit {sha[:7]}: {p}"
                for p in message_problems(message, bullet_one_line=bullet_one_line)
            ]
    return problems


def _squash_problems(ctx: Ctx) -> list[str]:
    problems = []
    for unit in ctx.job.public_text:
        try:
            title, commit = (
                job_text.unit_file(ctx.folder, unit["id"], name).read_text(encoding="utf-8")
                for name in ("title.txt", "commit.txt")
            )
        except OSError:
            continue  # Missing files already have an exact path problem.
        except UnicodeError:
            problems.append(f"public text {unit['id']}: title.txt and commit.txt must be UTF-8")
            continue
        rules = message_problems(
            title.rstrip("\n") + "\n\n" + commit,
            bullet_one_line=ctx.config.bullet_one_line,
        )
        if len(title.rstrip("\n").splitlines()) != 1:
            rules.append("title.txt must contain exactly one subject line")
        if not commit.strip():
            rules.append("commit.txt must contain the final squash body")
        problems += [f"public text {unit['id']}: squash message: {p}" for p in rules]
    return problems


def _file_problems(jobs: Jobs, job: JobState, workspace: Path, folder: Path) -> list[str]:
    problems = []
    for base, rel in [*((workspace, item) for item in job.outputs), (folder, job.progress)]:
        path = _inside(base, rel)
        if path is None or not path.is_file():
            problems.append(f"required file is missing: {rel}")
    path = _inside(folder, job.checklist)
    failing = checklist_problems(str(path)) if path is not None else ("missing",)
    if failing is None:
        problems.append("the checklist is not a nonempty JSON list of items with boolean passes")
    elif failing == ("missing",):
        problems.append(f"checklist is missing: {job.checklist}")
    elif failing:
        problems.append(f"checklist items do not pass: {', '.join(failing)}")
    for unit in job.public_text:
        for name in UNIT_FILES:
            if not job_text.unit_file(folder, unit["id"], name).is_file():
                problems.append(f"required file is missing: pr/{unit['id']}/{name}")
    if source_check.CLAIMS in job.outputs:
        problems += source_check.check(workspace, job.outputs).problems
    return problems + _brief_problems(jobs, job, workspace, folder)


def _brief_problems(jobs: Jobs, job: JobState, workspace: Path, folder: Path) -> list[str]:
    """The brief, and for a design job its `choices.json`; for an implement job after a design
    job, that the owner's picks reached the brief."""
    roots = (workspace, folder, Path(job.target))
    problems = []
    if choices.is_design(job):
        if not declares_choices(job.outputs):
            problems.append(f"{choices.FILE} must be listed in outputs so the review covers it")
        problems += choices.problems(
            workspace,
            roots,
            job.outputs,
            defaults_path=choices.defaults_path(jobs.state.root),
        )
    awaiting, listed, unanswered = choices.brief_expectations(jobs, job, workspace)
    return (
        problems
        + unanswered
        + brief.problems(folder / "brief.md", roots, tuple(awaiting), tuple(listed))
    )


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_command(ctx: Ctx, log: Path) -> tuple[dict, list[str]]:
    """Run the check argv in its own process group, and kill the whole group afterwards."""
    code, problems = -1, []
    try:
        with log.open("wb") as out:
            proc = subprocess.Popen(
                ctx.job.check_command,
                cwd=ctx.workspace,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=ctx.env,
            )
            try:
                code = proc.wait(timeout=ctx.config.rpc_timeout)
                if code != 0:
                    problems.append(f"check command exited {code}")
            except subprocess.TimeoutExpired:
                problems.append("check command timed out")
            finally:
                _kill_group(proc.pid)
                proc.wait()
    except OSError as exc:
        log.write_text(f"{exc}\n", encoding="utf-8")
        problems.append(f"check command could not run: {exc}")
    record = {"exit_code": code, "log": str(log), "log_sha256": _file_sha(log)}
    return record, problems


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_texts(ctx: Ctx, problems: list[str]) -> dict[str, dict]:
    """The state of each unit's text; a failure is a problem if no earlier one says so."""
    if not ctx.job.public_text:
        return {}
    try:
        return job_text.states(job_text.collect(ctx.job, ctx.workspace, ctx.folder, ctx.env))
    except (ValueError, OSError) as exc:
        if not problems:
            problems.append(f"the public text cannot be read: {exc}")
        return {}


def _finding_problems(ctx: Ctx, fingerprint: str, texts: dict) -> tuple[list[str], dict]:
    """Before a re-review: the worker's record of each finding of the last failed review, which
    holds across a resume, and repeated rules."""
    job = ctx.job
    review = job_findings.last_failed(job.reviews)
    if job.review_target is not None or review is None:
        return [], {}
    repo = ctx.workspace if job.review == "code" else None
    problems = job_findings.worker_problems(ctx.folder, review, fingerprint, texts, repo, ctx.env)
    earlier = [r for r in job.reviews if r["seq"] < review["seq"]]
    rows = ctx.jobs.state.read_records(job_findings.INDEX)
    repeated = job_findings.repeated_rules(job, earlier, review, rows)
    problems += job_findings.note_problems(ctx.folder, repeated)
    # The note a rule had before this review was written; the rule broke it again.
    before = [c for c in job.checks if c["seq"] < review["seq"]]
    noted = before[-1].get("noted", {}) if before else {}
    return problems, {
        "noted": job_findings.named_preventions(ctx.folder, repeated),
        "recurred": {rule: noted[rule] for rule in repeated if rule in noted},
    }


def _shape(ctx: Ctx, problems: list[str]) -> tuple[str, str, dict, dict]:
    """The shape checks of a result: files, texts, fingerprint and finding records.

    The check step and `shape_problems` both call it, so the agent runs the rules the runner
    applies. Problems are appended to `problems`; the rest is what the check record keeps.
    """
    job = ctx.job
    problems += _file_problems(ctx.jobs, job, ctx.workspace, ctx.folder)
    problems += _text_problems(job, ctx.workspace, ctx.env, ctx.config.bullet_one_line)
    problems += _squash_problems(ctx)
    texts = _public_texts(ctx, problems)
    fingerprint = basis = ""
    try:
        fingerprint = _fingerprint(job, ctx.workspace, ctx.env)
        basis = _basis(job, ctx.folder)
    except (ValueError, OSError) as exc:
        problems.append(str(exc))
    found, notes = _finding_problems(ctx, fingerprint, texts)
    problems += found
    return fingerprint, basis, texts, notes


def shape_problems(
    jobs: Jobs, config: JobConfig, job: JobState, run_command: bool = False
) -> list[str]:
    """What the check step would report about the shape of the result now. The result and the
    records are only read; the check command runs only when asked, into its own log."""
    if job.review_target is not None:
        raise ValueError("a review-only job has no result to check")
    if not job.workspace or not Path(job.workspace).is_dir():
        raise ValueError("the job has no workspace yet")
    ctx = Ctx(jobs, config, job, jobs.folder(job), Path(job.workspace))
    problems: list[str] = []
    if run_command:
        problems += _run_command(ctx, ctx.folder / "logs" / "precheck.log")[1]
    _shape(ctx, problems)
    return problems


def _passing_run(ctx: Ctx) -> dict | None:
    """The latest earlier check whose command exited 0 on this very result and basis, if any.

    The fingerprint covers the result and the basis covers intent, spec and guidelines, so the
    command would see the same input; only the file, text and finding checks run again.
    """
    try:
        fingerprint = _fingerprint(ctx.job, ctx.workspace, ctx.env)
        basis = _basis(ctx.job, ctx.folder)
    except (ValueError, OSError):
        return None
    if not fingerprint:
        return None
    for check in reversed(ctx.job.checks):
        if (
            check.get("exit_code") == 0
            and check.get("fingerprint") == fingerprint
            and check.get("basis") == basis
            and check.get("log")
            and Path(check["log"]).is_file()
        ):
            return check
    return None


def _check_step(ctx: Ctx) -> int:
    job = ctx.job
    seq = len(job.checks) + len(job.reviews) + 1
    reused = _passing_run(ctx)
    if reused is None:
        run, problems = _run_command(ctx, ctx.folder / "logs" / f"check-{seq}.log")
    else:
        run = {
            "exit_code": 0,
            "log": reused["log"],
            "log_sha256": reused["log_sha256"],
            "reused_check": reused.get("reused_check", reused["seq"]),
        }
        problems = []
    # After the command: it may have changed or removed what the checks require.
    fingerprint, basis, texts, notes = _shape(ctx, problems)
    record = {
        "seq": seq,
        "at": _now(),
        "ok": not problems,
        **run,
        "problems": problems,
        "fingerprint": fingerprint,
        "basis": basis,
        "texts": texts,
        **notes,
        **_source_record(ctx),
    }

    def build(current: dict) -> dict:
        changes = {
            "checks": [*current.checks, record],
            "fingerprint": fingerprint,
            "basis": basis,
            "texts": texts,
            "events": _event(current, type="check", ok=not problems),
            **_progress_changes(current, ctx, _now()),
        }
        return {**changes, **_check_route(current, problems)}

    _apply(ctx, build)
    return 0


def _check_route(job: JobState, problems: list[str]) -> dict:
    ended = {"ended_at": _now()}
    if not problems:
        return {**ended, "stage": "review", "resume_stage": "review", "recoveries": 0, "error": ""}
    message = "; ".join(problems)
    if job.recoveries < MAX_RECOVERIES:
        return {
            **ended,
            "stage": "fix",
            "resume_stage": "fix",
            "recoveries": job.recoveries + 1,
            "error": message,
        }
    return {**ended, "stage": "user_wait", "wait_reason": "check_failed", "error": message}


# --- review --------------------------------------------------------------------------------


def _pin_subject(ctx: Ctx) -> Ctx:
    """A review-only job has no check to fix the result, so the review step does it."""
    try:
        fingerprint = _fingerprint(ctx.job, ctx.workspace, ctx.env)
        basis = _basis(ctx.job, ctx.folder)
    except (ValueError, OSError) as exc:
        raise StepError(f"the review target cannot be read: {exc}") from exc
    _apply(ctx, lambda j: {"fingerprint": fingerprint, "basis": basis})
    return replace(ctx, job=replace(ctx.job, fingerprint=fingerprint, basis=basis))


def _result_stale(ctx: Ctx) -> bool:
    job = ctx.job
    try:
        changed = _fingerprint(job, ctx.workspace, ctx.env) != job.fingerprint
        if changed or _basis(job, ctx.folder) != job.basis or not job.fingerprint:
            return True
        return job_text.texts_changed(job, ctx.workspace, ctx.folder, ctx.env, job.texts)
    except (ValueError, OSError):
        return True


def _snapshot(ctx: Ctx, review_root: Path) -> dict:
    """Freeze the text the reviewer reads, so the review and the published copy agree."""
    if not ctx.job.public_text:
        return {}
    units = job_text.collect(ctx.job, ctx.workspace, ctx.folder, ctx.env)
    root = review_root / job_text.SNAPSHOT
    return {
        "texts": job_text.states(units),
        "snapshot": str(root),
        "snapshot_sha256": job_text.write_snapshot(root, units),
    }


def _review_base(ctx: Ctx) -> str:
    """The commit the reviewed result stands on. For a code job that builds commits it follows
    a rebase of the branch; otherwise it is what the job recorded."""
    job = ctx.job
    if job.review != "code" or job.review_target is not None or not job.fingerprint:
        return job.base
    return review_delta.effective_base(
        ctx.workspace, ctx.env, job.fingerprint, job.branch, job.base
    )


def _rereview_scope(ctx: Ctx, base: str) -> review_delta.Scope | None:
    """Round 2 and later of a code review that builds commits: delta or full, by code."""
    job = ctx.job
    prior = [r for r in job.reviews if isinstance(r, Mapping)]
    if not prior or job.review != "code" or job.review_target is not None:
        return None
    if not job_findings.accepted(prior[-1]):
        return None  # the last answer was not accepted and nothing changed since
    return review_delta.decide(
        ctx.workspace,
        ctx.env,
        base=base,
        fingerprint=job.fingerprint,
        approval_scope=job.approval_scope,
        prior=prior,
        checks=job.checks,
        roots=(str(ctx.workspace), job.target),
    )


def _review_step(ctx: Ctx) -> int:
    started = time.monotonic()
    if ctx.job.review_target is not None:
        ctx = _pin_subject(ctx)
    elif _result_stale(ctx):
        _apply(ctx, lambda j: _requeue(j, ran=False))
        return 0
    job = ctx.job
    base = _review_base(ctx)
    scope = _rereview_scope(ctx, base)
    guidelines = _guidelines(job, ctx.folder)
    name = f"review-{len(job.reviews)}-{len(job.checks)}"
    review_root = ctx.folder / "reviews" / name
    review_root.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot(ctx, review_root)
    if ctx.config.review_mode == "print":
        message = _review_message(ctx, guidelines, False, review_root, scope)
        text, reply_problem = _print_reply(ctx, message, review_root)
    else:
        reply_problem = ""
        text = _converse(
            ctx,
            ctx.folder / "sessions" / name,
            lambda r: _review_message(ctx, guidelines, r, review_root, scope),
            True,
            lambda body: _repair_prompt(ctx, body),
        )
    try:
        ctx.jobs.verify_inputs(job)
    except (ValueError, OSError) as exc:
        raise InputsChanged(f"a copied input changed during the review: {exc}") from exc
    problems = _evidence_problems(ctx, snapshot)
    if problems:
        # The reviewer saw evidence that no longer holds, so its verdict is not accepted.
        _apply(ctx, lambda j: {**_requeue(j), "error": "; ".join(problems)})
        return 0
    minutes = round((time.monotonic() - started) / 60, 1)
    record = _judge(ctx, text, snapshot, reply_problem, scope, minutes, base)
    digest = job_findings.note_digest(ctx.folder)
    _apply(ctx, lambda j: _review_route(j, record, digest))
    job_findings.sync_index(ctx.jobs.state, ctx.jobs.load()[job.id], ctx.folder, _now())
    return 0


def _evidence_problems(ctx: Ctx, snapshot: Mapping) -> list[str]:
    """Recheck what the accepted check rested on; a PASS must not outlive it."""
    job = ctx.job
    if job.review_target is not None:
        return []
    check = job.checks[-1] if job.checks else None
    if (
        check is None
        or not check["ok"]
        or check["exit_code"] != 0
        or check["fingerprint"] != job.fingerprint
        or check["basis"] != job.basis
    ):
        return ["there is no accepted check for this result"]
    try:
        if _file_sha(Path(check["log"])) != check["log_sha256"]:
            return ["the check log does not match its recorded hash"]
    except OSError as exc:
        return [f"the check log is unreadable: {exc}"]
    try:
        if snapshot and (
            snapshot["texts"] != job.texts
            or job_text.texts_changed(job, ctx.workspace, ctx.folder, ctx.env, snapshot["texts"])
        ):
            return ["the public text changed since the check"]
    except (ValueError, OSError) as exc:
        return [f"the public text cannot be read: {exc}"]
    return _file_problems(ctx.jobs, job, ctx.workspace, ctx.folder)


def _requeue(job: JobState, events: list | None = None, ran: bool = True) -> dict:
    """Send a result whose evidence went stale back to the step that produces the evidence.

    `ran` is False when the review never started, so its reservation is returned."""
    first = "review" if job.review_target is not None else "check"
    changes = {
        "stage": first,
        "resume_stage": first,
        "fingerprint": "",
        "basis": "",
        "texts": {},
        "ended_at": _now(),
        "events": events if events is not None else _event(job, type="review_stale"),
        "review_inflight": 0,
    }
    if not ran:
        changes.update(_refund(job))
    if job.recoveries >= MAX_RECOVERIES:
        return {**changes, "stage": "user_wait", "wait_reason": "result_unstable"}
    return {**changes, "recoveries": job.recoveries + 1}


def _reviewer_name(ctx: Ctx) -> str:
    """Who reviewed, from the command the runner started: its program, or the module of
    `python -m <module>`. The reply does not need to say it."""
    argv = _command(ctx)
    name = argv[2] if len(argv) > 2 and argv[1] == "-m" else argv[0]
    return Path(name).name


def _read_reply(job: JobState, body: str, record: dict) -> None:
    """Parse the verdict, findings, scope and design check of a reply into `record`.

    Raises ValueError with the first shape error; what parsed before it stays in `record`.
    """
    verdict, blocking = _verdict_line(body)
    record.update(verdict=verdict, blocking=blocking)
    record["findings"] = _finding_lines(body, verdict)
    if verdict == "PASS":
        record["problems"] += [
            f"finding {f['finding']} needs a summary: a PASS shows its {f['severity']} "
            "findings to the owner"
            for f in record["findings"]
            if f["severity"] in {"P2", "P3"} and "summary" not in f
        ]
    record["checked"], record["not_checked"] = _scope(body)
    if job.review in {"code", "design"}:
        record["design_check"] = _design_check(body, record["findings"])


def _repair_prompt(ctx: Ctx, body: str) -> str | None:
    """The one follow-up for a reply whose shape is wrong: the exact errors and the contract.
    None when the reply parses. Content is never judged here."""
    scratch: dict = {"problems": [], "findings": []}
    try:
        _read_reply(ctx.job, body, scratch)
    except ValueError as exc:
        scratch["problems"].append(str(exc))
    if not scratch["problems"]:
        return None
    errors = "\n".join(f"- {problem}" for problem in scratch["problems"])
    return (
        f"Your reply was not accepted, because of its shape:\n{errors}\n\n"
        "Answer again in this session. Send the complete review, not only the corrected "
        "line, in the shape below. Do not change what you found.\n\n" + _layer("taskwatch.md")
    )


def _judge(
    ctx: Ctx,
    body: str,
    snapshot: Mapping,
    reply_problem: str = "",
    scope: review_delta.Scope | None = None,
    minutes: float = 0.0,
    base: str = "",
) -> dict:
    job = ctx.job
    seq = len(job.checks) + len(job.reviews) + 1
    record = {
        "seq": seq,
        "at": _now(),
        "stale": False,
        # The commit a code review judged: the anchor a later fix commit must follow.
        "reviewed_head": job.fingerprint if _builds_commits(job) else "",
        "round": len(job.reviews) + 1,
        "verdict": "",
        "blocking": True,
        "reviewer": _reviewer_name(ctx),
        "fingerprint": job.fingerprint,
        "outputs_fingerprint": "",
        "basis": job.basis,
        "base": base,
        "body": body,
        "session_path": ctx.jobs.load()[job.id].session_path,
        "problems": [],
        "findings": [],
        "checked": "",
        "not_checked": "",
        "minutes": minutes,
        "new_findings": 0,
        "delta_ratio": scope.ratio if scope else None,
        **({"rereview": scope.record()} if scope else {}),
        **snapshot,
        **({"accepted_check": job.checks[-1]} if job.checks else {}),
    }
    try:
        if reply_problem:
            record["problems"].append(reply_problem)
        else:
            _read_reply(job, body, record)
    except ValueError as exc:
        record["problems"].append(str(exc))
    if record["problems"]:
        record.update(verdict="", findings=[])
    prior = [r for r in job.reviews if isinstance(r, Mapping)]
    record["new_findings"] = review_delta.new_findings(prior, record["findings"])
    try:
        if _fingerprint(job, ctx.workspace, ctx.env) != job.fingerprint:
            record["problems"].append("the result changed during the review")
            record["stale"] = True
        record["outputs_fingerprint"] = files_fingerprint(ctx.workspace, job.outputs)
        if _basis(job, ctx.folder) != job.basis:
            record["problems"].append("the review basis changed during the review")
            record["stale"] = True
    except (ValueError, OSError) as exc:
        record["problems"].append(f"the result cannot be fingerprinted: {exc}")
        record["stale"] = True
    if record["verdict"] == "FAIL":
        record["problems"].append(job_findings.FAIL_PROBLEM)
    record["ok"] = not record["problems"] and record["verdict"] == "PASS" and not record["blocking"]
    # A review-only job has nothing to fix: a parsed verdict on an unchanged target is its result.
    record["final"] = (
        job.review_target is not None
        and record["verdict"] != ""
        and all(problem == job_findings.FAIL_PROBLEM for problem in record["problems"])
    )
    return record


def _builds_commits(job: JobState) -> bool:
    return job.review == "code" and job.review_target is None


def _review_route(job: JobState, record: dict, digest: str = "") -> dict:
    """The outcome of a completed review attempt.

    A review is rerun directly while the cycle has one left, and a fix follows only a read
    FAIL: a malformed or infrastructure answer never starts a model fix. The same rule in two
    consecutive substantive rounds stops for a root-cause note before anything else runs."""
    now = _now()
    events = _event(job, type="review", ok=record["ok"], verdict=record["verdict"])
    changes = {
        "reviews": [*job.reviews, record],
        "review_inflight": 0,
        "ended_at": now,
        "events": events,
    }
    if record["ok"] or record["final"]:
        stage = "land" if job.land and _builds_commits(job) else "deliver_pending"
        done = {"stage": stage, "resume_stage": stage, "error": ""}
        return {**changes, **done}
    message = "; ".join(record["problems"])
    exhausted = job.reviews_used >= job.max_reviews
    cycle = [*job.reviews, record][job.review_cycle_start :]
    rules, rounds = job_findings.consecutive_rules(cycle)
    if rules:
        events.append(
            {
                "at": now,
                "type": "user_wait",
                "reason": "root_cause_required",
                "rules": rules,
                "rounds": rounds,
                "note_digest": digest,
                "exhausted": exhausted,
            }
        )
        text = (
            f"root cause required: {', '.join(rules)} broke in review rounds {rounds[0]} and "
            f"{rounds[1]}. Write a section per rule in {job_findings.NOTE_FILE} (Cause:, "
            "Missed by:, Prevention:) that explains this recurrence, then resume."
        )
        if exhausted:
            text += " The review limit is also reached."
        return {
            **changes,
            "stage": "user_wait",
            "wait_reason": "root_cause_required",
            "resume_stage": "fix",
            "error": text,
        }
    if job_findings.accepted(record):
        retry = "fix"
    elif record["stale"] and job.review_target is None:
        retry = "check"
    else:
        retry = "review"
    if exhausted:
        return {
            **changes,
            "stage": "user_wait",
            "wait_reason": "review_exhausted",
            "resume_stage": retry,
            "error": message,
        }
    if retry == "check":
        events.append({"at": now, "type": "review_stale"})
        return {**changes, **_requeue(job, events), "error": message}
    return {**changes, "stage": retry, "resume_stage": retry, "error": message}

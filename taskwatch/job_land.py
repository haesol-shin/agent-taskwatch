"""Reconcile one accepted tree through publication, squash, pinned install and delivery."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import forge, job_findings, job_text, job_workspace
from .checks import message_problems
from .jobs import _json_value

MAX_BASE_UPDATES = 3
# premerge: the base moved, so publication, CI and the bot gate run again under one bound.
BASE_UPDATED = object()
# The only body a draft PR carries before the reviewed text exists.
DRAFT_BODY = "CI is running on this draft pull request.\n"
# The per-head observations a new publication head or a CI fix round clears.
CI_WINDOW = frozenset(
    {
        "ci_green",
        "ci_evidence",
        "ci_bots",
        "ci_started_at",
        "ci_deadline",
        "ci_entry",
        "rerun_round",
    }
)
# Land state that shows a branch, a pull request or a base update already left the machine.
PUBLICATION = ("push_intent", "pushed", "pr", "base_update")
# A body link to a commit that exists only locally, which never reaches GitHub. Quotes and
# angle brackets around the URL (HTML src, angle-bracketed Markdown) are not part of the path.
BODY_LINK = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/(?P<kind>blob|raw)/(?P<sha>[0-9a-f]{7,40})/"
    r"(?P<path>[^\s?#)\"'<>]+)(?P<suffix>[?#][^\s)\"'<>]*)?"
)


class LandStop(Exception):
    def __init__(self, reason, message, *, fixable=False):
        self.reason = reason
        # A failed CI conclusion the worker can fix on the same branch, not a timeout or an
        # unregistered check.
        self.fixable = fixable
        super().__init__(message)


class Routed(Exception):
    """The transaction routed the job to another stage and this step is done."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def resume_entry(job):
    return sum(e.get("type") in {"resume", "approve"} for e in job.events)


def is_published(state):
    return any(state.get(key) for key in PUBLICATION)


def new_return(state, reason, message, phase=""):
    """A return record keyed to its PR number and round, for the state it returns from."""
    returns = list(state.get("returns", ()))
    return {
        "reason": reason,
        "pr": (state.get("pr") or {}).get("number", 0),
        "round": sum(item.get("reason") == reason for item in returns) + 1,
        "head": state.get("H", ""),
        "message": message,
        **({"phase": phase} if phase else {}),
    }


def amend_return(state, message):
    """The land state and the record of an amendment that sends a published job back to fix.

    The CI observation restarts like any return to fix. The reason `amended` is not
    `ci_failed`, so no CI fix round is spent."""
    record = new_return(state, "amended", message)
    kept = {key: value for key, value in state.items() if key not in CI_WINDOW}
    return {**kept, "returns": [*state.get("returns", ()), record]}, record


def green_outcome(evidence):
    """What a saved green proves: the head and the stable identity of every passing check.

    A re-report, a new run or check id, a changed attempt or status, and the order of the
    evidence all move while the CI result stays green, so none of them belongs here.
    """
    return {
        "head": evidence.head_sha,
        "checks": sorted(
            json.dumps([item.kind, item.name, item.app_id, item.workflow])
            for item in evidence.evidence
        ),
    }


def green_changed(saved, current):
    """True when the observed success no longer covers the saved one.

    Every observation that reaches here is green, so a stop means a saved check disappeared or
    the head moved, not that a field changed. A record without the outcome shape is not
    comparable and is re-recorded instead of trusted.
    """
    if not isinstance(saved, dict) or "head" not in saved or "checks" not in saved:
        return False
    if saved["head"] != current["head"]:
        return True
    return bool(set(saved["checks"]) - set(current["checks"]))


# How long a merged commit may take to register its default-branch push workflows.
POST_MERGE_GRACE = 15


class Boundary(forge.GitHub):
    def __init__(self, transaction):
        super().__init__(
            transaction.ctx.job.target,
            transaction.ctx.env,
            timeout=transaction.ctx.config.land_command_timeout,
        )
        self.transaction = transaction

    def _command(self, argv, *, write=False):
        try:
            return self.transaction.command(argv, timeout=self._remaining())
        except subprocess.TimeoutExpired as exc:
            error = forge.Uncertain if write else forge.ForgeError
            raise error(forge.command_timeout(exc)) from None
        except (OSError, subprocess.SubprocessError):
            error = forge.Uncertain if write else forge.ForgeError
            raise error("command failed; reconcile saved operation identity") from None


class Transaction:
    def __init__(self, ctx, lock_fd):
        self.ctx = ctx
        self.lock_fd = lock_fd
        self.state = _json_value(ctx.job.land_state)
        self.api = Boundary(self)
        # False until an accepted review is loaded: a draft publication proves CI on the
        # checked result, which no review has judged yet.
        self.reviewed = False

    def current(self):
        from .job_runner import StaleAttempt, _attempt_open

        job = self.ctx.jobs.load().get(self.ctx.job.id)
        if (
            job is None
            or job.attempt != self.ctx.job.attempt
            or job.stage != "land"
            or not _attempt_open(job)
        ):
            raise StaleAttempt
        return job

    def save(self, **changes):
        from .job_runner import StaleAttempt, _cas

        state = {**self.state, **changes}
        if not _cas(
            self.ctx.jobs,
            self.ctx.job,
            {"land"},
            lambda j: {"land_state": state, "evidence_at": time.time()},
        ):
            raise StaleAttempt
        self.state = state

    def command(self, argv, *, timeout=None, cwd=None, stdin=None):
        self.current()
        if timeout is None:
            timeout = self.ctx.config.land_command_timeout
        deadline = time.monotonic() + timeout
        with subprocess.Popen(
            argv,
            cwd=cwd or self.ctx.job.target,
            env=self.ctx.env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=(self.lock_fd,),
        ) as proc:
            try:
                first = True
                while True:
                    self.current()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        out, err = proc.communicate(
                            input=stdin if first else None, timeout=min(15, remaining)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                        self.save()
                if proc.returncode:
                    raise subprocess.CalledProcessError(proc.returncode, argv[0], out, err)
                return out
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def git(self, *args, stdin=None):
        return self.command(["git", *args], stdin=stdin).strip()

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.current()
            # Reading the record can outlast a short deadline; the remaining time is clamped
            # to zero, so a passed deadline saves and returns instead of sleeping a negative.
            remaining = max(0, end - time.monotonic())
            if remaining:
                time.sleep(min(15, remaining))
            self.save()

    def method(self):
        """The frozen merge method of this job."""
        return self.current().merge_method

    def freshness(self):
        from .job_delivery import acceptance_current, accepted_review
        from .job_runner import _requeue

        job = self.current()
        self.dispositions_current(job, accepted_review(job))
        if acceptance_current(self.ctx.jobs, self.ctx.config, job):
            return
        if self.state.get("S"):
            raise LandStop("land_review_stale", "accepted evidence changed after merge")
        from .job_runner import _apply

        if is_published(self.state):
            # Keep branch ownership evidence across check/review, including a checked head
            # that may have reached the remote without an acknowledgement.
            record = self.return_record(
                "evidence_changed", "accepted evidence changed after publication"
            )
            from .job_runner import _event

            state = {**self.state, "returns": [*self.state.get("returns", ()), record]}
            self.state = state
            _apply(
                self.ctx,
                lambda j: {
                    **_requeue(
                        j,
                        events=_event(
                            j,
                            type="land_return",
                            reason=record["reason"],
                            pr=record["pr"],
                            round=record["round"],
                        ),
                    ),
                    "land_state": state,
                },
            )
            raise FreshnessRequeued
        _apply(self.ctx, lambda j: {**_requeue(j), "land_state": {}})
        raise FreshnessRequeued

    def authority(self):
        """The job may land, and it publishes exactly one complete-result unit."""
        job = self.current()
        if not job.land or job.review != "code" or job.review_target is not None:
            raise LandStop("land_review_stale", "landing authority is absent")
        if len(job.public_text) != 1:
            raise LandStop("land_review_stale", "landing needs one complete-result publication")
        self.unit = job.public_text[0]["id"]
        return job

    def begin(self):
        """The transaction identity both publication phases share."""
        job = self.authority()
        repo = self.api.inspect_repository()
        self.api._writable(repo, job.merge_method)
        saved = self.state.get("repo")
        if saved is not None:
            # A record written before a capability field existed cannot prove the new value,
            # so only the keys it holds are compared.
            if any(saved.get(key) != value for key, value in asdict(repo).items() if key in saved):
                raise LandStop("land_pr_mismatch", "repository identity or capabilities changed")
        else:
            hook = selected_install_command(self.ctx.config, repo)
            self.save(
                repo=asdict(repo),
                branch=job.branch + "-land",
                repository_key=job_workspace.repository_key(job.target, self.ctx.env),
                install={
                    "status": "not_started" if hook else "disabled",
                    "command": hook,
                    "command_hash": digest(hook),
                },
                bot={
                    "apps": list(
                        (self.ctx.config.review_bots or {}).get(repo.identity.lower(), ())
                    ),
                    "wait": self.ctx.config.review_bot_wait,
                    "threads": {},
                    "outcome": {},
                },
            )
        self.repo = repo
        if "base_update_enabled" not in self.state:
            self.save(base_update_enabled=bool(self.ctx.config.land_base_update))

    def initialize(self):
        from .job_delivery import accepted_review

        self.begin()
        self.reviewed = True
        self.freshness()
        job = self.current()
        review = accepted_review(job)
        self.dispositions_current(job, review)
        texts = review.get("texts", {})
        if len(texts) != 1 or self.unit not in texts:
            raise LandStop("land_review_stale", "landing needs one complete-result publication")
        text = texts[self.unit]
        if (
            text.get("text_contract") != 2
            or text.get("head") != review.get("fingerprint")
            or text.get("base") != job.base
            or self.git("rev-parse", f"{text['head']}^{{tree}}") != text.get("tree")
        ):
            raise LandStop("land_review_stale", "checked publication does not cover accepted tree")
        self.result_tree = text["tree"]
        identity = digest({"review": _json_value(review), "check": _json_value(job.checks[-1])})
        if self.state.get("identity") and self.state["identity"] != identity:
            if self.state.get("pushed") or self.state.get("pr"):
                self.adopt_review(job, review, text, identity)
            elif not self.state.get("base_update"):
                # A prepared base update may have reached the remote without a response.
                # Keep its ownership evidence even when the accepted inputs change.
                self.state = {}
                self.begin()
        if self.state.get("identity") != identity:
            self.save(
                identity=identity,
                phase="accepted",
                R=text["head"],
                T=self.state.get("T", self.result_tree),
                B=text["base"],
                review_seq=review.get("seq", 0),
                review_digest=digest(_json_value(review)),
                check_digest=digest(_json_value(job.checks[-1])),
                snapshot_digest=review["snapshot_sha256"],
                text_digest=digest(_json_value(text)),
            )
        copy = self.ctx.folder / "land-text"
        code, _ = job_text.reviewed_copy(job, self.ctx.env, self.ctx.folder, self.unit, copy)
        if code:
            raise LandStop("land_review_stale", "reviewed snapshot cannot be copied")
        self.title = (copy / "title.txt").read_bytes().decode().rstrip("\n")
        self.reviewed_body = (copy / "body.md").read_bytes().decode()
        self.body = self.reviewed_body
        self.commit_file = copy / "commit.txt"
        self.body_file = copy / "body.md"
        self.commit = self.commit_file.read_bytes().decode()
        if bool(self.commit.strip()) == job_text.blank_body(job.public_text[0]):
            raise LandStop("land_review_stale", "squash body does not match the unit declaration")
        self.rewrite_body_links()

    def draft_pending(self):
        """A landing job that opens the draft PR and waits for CI before the review."""
        from .job_delivery import acceptance_current

        job = self.current()
        if not (
            job.land and job.ci_before_review and job.review == "code" and job.review_target is None
        ):
            return False
        if self.state.get("S") or self.state.get("merge_intent"):
            return False
        return not acceptance_current(self.ctx.jobs, self.ctx.config, job)

    def draft_evidence_current(self, job, state):
        """The checked result, its texts and the check evidence still match the worktree."""
        from .job_runner import _basis, _fingerprint

        check = job.checks[-1] if job.checks else {}
        try:
            fingerprint = _fingerprint(job, self.ctx.workspace, self.ctx.env)
            basis = _basis(job, self.ctx.folder)
            changed = job_text.texts_changed(
                job, self.ctx.workspace, self.ctx.folder, self.ctx.env, job.texts
            )
            log = Path(check.get("log") or "")
            log_ok = hashlib.sha256(log.read_bytes()).hexdigest() == check.get("log_sha256")
        except (ValueError, OSError, KeyError, TypeError):
            return False
        return (
            state.get("head") == job.fingerprint
            and state.get("base") == job.base
            and fingerprint == job.fingerprint
            and basis == job.basis
            and bool(check.get("ok"))
            and check.get("exit_code") == 0
            and check.get("fingerprint") == job.fingerprint
            and check.get("basis") == job.basis
            and log_ok
            and not changed
        )

    def requeue_check(self):
        """Send a drifted checked result back to check before any publication write."""
        from .job_runner import _apply, _requeue

        _apply(self.ctx, lambda j: _requeue(j))
        raise FreshnessRequeued

    def initialize_draft(self):
        """The transaction identity of the result the check accepted, before any review."""
        self.authority()
        job = self.current()
        state = job.texts.get(self.unit) or {}
        if not self.draft_evidence_current(job, state):
            self.requeue_check()
        self.begin()
        if (
            state.get("text_contract") != 2
            or state.get("base") != job.base
            or state.get("head") != job.fingerprint
            or self.git("rev-parse", f"{state['head']}^{{tree}}") != state.get("tree")
        ):
            raise LandStop("land_review_stale", "checked publication does not cover the result")
        self.result_tree = state["tree"]
        try:
            title = job_text.unit_file(self.ctx.folder, self.unit, "title.txt").read_bytes()
            commit = job_text.unit_file(self.ctx.folder, self.unit, "commit.txt").read_bytes()
        except OSError:
            raise LandStop("land_review_stale", "the written publication text is missing") from None
        self.title = title.decode().rstrip("\n")
        self.commit = commit.decode()
        self.reviewed_body = None
        self.body = DRAFT_BODY
        self.body_file = self.ctx.folder / "land-draft-body.md"
        self.body_file.write_text(self.body, encoding="utf-8")
        self.save(
            R=state["head"],
            T=self.state.get("T", self.result_tree),
            B=state["base"],
            identity=digest({"check": _json_value(job.checks[-1]), "text": _json_value(state)}),
            phase="draft",
        )

    def republish_for_tree(self):
        """A changed tree after publication becomes a child of the current head."""
        head = self.state.get("H")
        if not head or self.state.get("S"):
            return
        current = self.state["R"]
        if self.method() == "merge":
            if current == head:
                return
            # A merge keeps the worker's commits, so the new head is the new accepted result.
            # The tree may be equal when the fix only adds an empty commit.
            metadata = self.git("show", "-s", "--format=raw", current)
            self.reset_per_head()
            self.save(
                H=current,
                commit_metadata=metadata,
                republish={"from": head, "head": current},
                phase="head",
            )
            return
        if self.git("rev-parse", f"{head}^{{tree}}") == self.state["T"]:
            intent = self.state.get("republish") or {}
            if intent and not intent.get("head"):
                self.save(republish={**intent, "head": head})
            return
        # An earlier republication may have left an intent for an older tree; the current head
        # is the only parent the next one may build on.
        self.save(republish={"from": head})
        self.prepare_republish()

    def draft(self):
        """Push the branch, open a draft PR, wait for its CI, then hand it to the review.

        The flow order is machine checks, CI, then the independent review, as GitHub's own
        workflow does. A failed CI conclusion returns to fix on this branch and pull request
        while the CI fix budget holds.
        """
        self.initialize_draft()
        self.base_guard()
        self.head()
        self.observe_base_update()
        self.draft_pull_request()
        try:
            self.ci(bots=False, cycle=self.state.get("identity", ""))
        except LandStop as exc:
            if not exc.fixable or not self.return_for_ci_fix("draft", str(exc)):
                raise
        self.to_review()

    def draft_pull_request(self):
        """Create the draft pull request or reconcile the one this transaction published."""
        if self.state.get("pr"):
            return self.draft_identity(self.wait_pr_head(self.state["pr"]["number"]))
        matches = self.api.find_pull_requests(
            self.repo, self.state["branch"], self.repo.default_branch, self.state["H"]
        )
        if len(matches) > 1:
            raise LandStop("land_pr_mismatch", "duplicate publication PRs")
        if matches:
            pr = self.draft_identity(matches[0])
            self.save(
                pr={"number": pr.number, "url": pr.url},
                phase="pr",
                published_text={"title": pr.title, "body": pr.body},
            )
            return pr
        entry = resume_entry(self.current())
        if self.state.get("create_intent") and entry <= self.state.get("create_entry", entry):
            raise LandStop("land_interrupted", "create outcome unknown; explicit resume required")
        self.save(create_intent=True, create_entry=entry, phase="create")
        pr = self.api.create_pull_request(
            self.repo,
            self.state["branch"],
            self.state["H"],
            self.title,
            self.body_file,
            draft=True,
        )
        pr = self.draft_identity(pr)
        self.save(
            pr={"number": pr.number, "url": pr.url},
            phase="pr",
            published_text={"title": self.title, "body": self.body},
        )
        return pr

    def draft_identity(self, pr):
        """A draft PR must be this open branch and base; its text is not published yet."""
        if (
            pr.state != "OPEN"
            or pr.head_sha != self.state["H"]
            or pr.head_branch != self.state["branch"]
            or pr.base_branch != self.repo.default_branch
        ):
            raise LandStop("land_pr_mismatch", "draft PR identity, state or branch differs")
        saved = self.state.get("pr")
        if saved and saved != {"number": pr.number, "url": pr.url}:
            raise LandStop("land_pr_mismatch", "saved PR identity differs")
        return pr

    def wait_pr_head(self, number):
        """Wait for the published PR to observe the current publication head."""
        deadline = time.monotonic() + 60
        while True:
            pr = self.api.read_pull_request(self.repo, number, None)
            if pr.head_sha == self.state["H"]:
                return pr
            if pr.head_sha not in self.owned_heads() or time.monotonic() >= deadline:
                raise LandStop("land_pr_mismatch", "publication head was not observed")
            self.wait(2)

    def return_record(self, reason, message, phase=""):
        """One return record, keyed to its PR number and round; not yet persisted."""
        return new_return(self.state, reason, message, phase)

    def return_for_ci_fix(self, phase, message):
        """Spend one CI fix round on the same branch and PR; False when the budget is spent.

        The return record, the reset CI window and the transition to fix are one compare-and-
        set, so an interruption cannot spend the budget without routing the job.
        """
        from .job_runner import StaleAttempt, _cas, _event

        job = self.current()
        used = sum(item.get("reason") == "ci_failed" for item in self.state.get("returns", ()))
        if (
            used >= job.ci_fix_max
            or self.state.get("S")
            or self.state.get("merge_intent")
            or not self.state.get("pr")
        ):
            return False
        self.reset_ci_window()
        record = self.return_record("ci_failed", message, phase)
        state = {**self.state, "returns": [*self.state.get("returns", ()), record]}
        if not _cas(
            self.ctx.jobs,
            self.ctx.job,
            {"land"},
            lambda j: {
                "land_state": state,
                "stage": "fix",
                "resume_stage": "fix",
                "ended_at": time.time(),
                "error": f"CI failed on the published head: {message}",
                "events": _event(
                    j,
                    type="ci_fix",
                    reason="ci_failed",
                    pr=record["pr"],
                    round=record["round"],
                    phase=phase,
                    message=message,
                ),
            },
        ):
            raise StaleAttempt
        self.state = state
        raise Routed

    def resume_fix(self):
        """Whether a stop may send the resumed job back to the worker on the same branch.

        Before a pull request exists nothing is published, so the worker can fix the same
        result. After publication the return path of `return_for_ci_fix` decides, with its
        same-branch, same-PR conditions and budget. A recorded squash or merge intent keeps
        the stop on the land stage, because the merge already decided the lineage.
        """
        job = self.current()
        if self.state.get("S") or self.state.get("merge_intent"):
            return False
        if not self.state.get("pr"):
            return True
        used = sum(item.get("reason") == "ci_failed" for item in self.state.get("returns", ()))
        return used < job.ci_fix_max

    def to_review(self):
        """The draft publication and its green CI hand the same result to the review step."""
        from .job_runner import _apply, _event

        self.save(phase="draft_ci")
        _apply(
            self.ctx,
            lambda j: {
                "stage": "review",
                "resume_stage": "review",
                "ended_at": time.time(),
                "error": "",
                "recoveries": 0,
                "events": _event(
                    j,
                    type="draft_ci",
                    pr=self.state.get("pr", {}).get("number", 0),
                    head=self.state.get("H", ""),
                ),
            },
        )

    def ready(self):
        """Mark the draft ready only after the reviewed text is in place."""
        saved = self.state.get("pr")
        if not saved:
            return
        current = self.api.read_pull_request(self.repo, saved["number"], self.state["H"])
        if not current.draft:
            return
        entry = resume_entry(self.current())
        intent = self.state.get("ready_intent", {})
        if intent.get("head") == self.state["H"] and entry <= intent.get("entry", entry):
            raise LandStop(
                "land_interrupted", "ready acknowledgement unknown; distinct resume needed"
            )
        self.freshness()
        self.save(ready_intent={"head": self.state["H"], "entry": entry}, phase="ready")
        try:
            self.api.ready_pull_request(self.repo, saved["number"], self.state["H"])
        except forge.Uncertain:
            pass
        observed = self.api.read_pull_request(self.repo, saved["number"], self.state["H"])
        if observed.draft:
            raise LandStop(
                "land_interrupted", "ready acknowledgement unknown; distinct resume needed"
            )
        self.save(phase="ready")

    def dispositions_current(self, job, review):
        """Each reviewed disposition must still describe its thread and the worker's record."""
        if review.get("seq", 0) <= self.state.get("bot", {}).get("round", {}).get("seq", 0):
            return
        changed = job_findings.dispositions_missing(job, self.ctx.folder, review)
        if not changed:
            return
        self.reopen_round(_json_value(self.state.get("bot", {})), changed)
        raise FreshnessRequeued

    def reopen_round(self, bot, findings):
        """A changed record needs a fresh review of exactly those bot findings."""
        from .job_runner import _apply, _requeue

        def build(job):
            state = {**_json_value(bot)}
            for thread in state.get("threads", {}).values():
                if thread.get("finding") in findings:
                    thread["status"] = "open"
                    thread.pop("disposition", None)
            state["round"] = {
                "seq": state.get("round", {}).get("seq", 0),
                "awaiting": True,
                "findings": sorted(findings),
            }
            return {**_requeue(job), "land_state": {**self.state, "bot": state}}

        _apply(self.ctx, build)

    def adopt_review(self, job, review, text, identity):
        """Adopt a new accepted review on the same branch and pull request.

        A routed bot fix and a fix or text change after publication take this path: a changed
        tree becomes a child of the current head, and a text-only change keeps the head. The
        reviewed text is written to the pull request after this by `edit_published_pr`.
        """
        from .job_runner import _apply, _requeue

        published = self.state.get("H")
        if (
            not published
            or self.state.get("S")
            or self.state.get("merge_intent")
            or review.get("seq", 0) <= self.state.get("review_seq", 0)
        ):
            raise LandStop("land_review_stale", "accepted transaction identity changed")
        bot = self.state.get("bot", {})
        round_ = bot.get("round", {})
        changes = {"bot": bot}
        if round_.get("awaiting"):
            if review.get("seq", 0) <= round_.get("seq", 0):
                raise LandStop("land_review_stale", "accepted transaction identity changed")
            candidates = job_findings.bot_dispositions(job, self.ctx.folder)
            if candidates != _json_value(review.get("bot_dispositions", [])):
                _apply(self.ctx, lambda j: _requeue(j))
                raise FreshnessRequeued
            refs = {c["finding"] for c in candidates}
            if refs != set(round_.get("findings", ())):
                raise LandStop("land_review_stale", "bot dispositions are incomplete")
            changes["bot"] = {
                **bot,
                "dispositions": candidates,
                "disposition_review": review["seq"],
            }
        # The current main, not the last published base, determines the next tree and head.
        # Keep the prior intent until the base guard can reconcile its remote ownership.
        republish = self.state.get("republish") or {"from": published}
        if text["head"] == self.state.get("R") and not self.pending_base_update():
            republish = {**republish, "head": published}
        changes.update(
            identity=identity,
            R=text["head"],
            B=text["base"],
            review_seq=review["seq"],
            review_digest=digest(_json_value(review)),
            check_digest=digest(_json_value(job.checks[-1])),
            snapshot_digest=review["snapshot_sha256"],
            text_digest=digest(_json_value(text)),
            republish=republish,
        )
        self.save(**changes)

    def owner_reasons(self):
        """A generic resume is never an owner's acceptance of a bot rejection."""
        answers = [
            c
            for c in self.state.get("bot", {}).get("dispositions", ())
            if c["status"] == "not_fixed"
        ]
        if not answers:
            return
        bot = self.state["bot"]
        token = digest(answers)
        pending = bot.get("owner_pending", {})
        if bot.get("owner_accepted") == token:
            return
        job = self.current()
        if pending.get("digest") == token and any(
            event.get("type") == "resume"
            and event.get("entry", 0) > pending["entry"]
            and event.get("reason") == f"BOT_ACCEPT {token}"
            for event in job.events
        ):
            self.save(bot={**bot, "owner_accepted": token})
            return
        self.save(bot={**bot, "owner_pending": {"digest": token, "entry": resume_entry(job)}})
        raise LandStop(
            "bot_reason_approval",
            "owner must accept these exact not_fixed reasons: "
            + json.dumps(answers, ensure_ascii=False)
            + f"; resume --reason 'BOT_ACCEPT {token}'",
        )

    def post_answers(self):
        intent = self.state.get("republish", {})
        if not intent or intent.get("head"):
            return
        candidates = [
            c for c in self.state["bot"].get("dispositions", ()) if c["status"] == "not_fixed"
        ]
        if not candidates:
            return
        visible = {
            t["id"]: t
            for t in self.api.review_threads(self.repo, self.state["pr"]["number"], self.state["H"])
        }
        for candidate in candidates:
            root = visible.get(candidate["thread_id"])
            if (
                root is not None
                and root["author_type"] == "Bot"
                and root["author"].removesuffix("[bot]") in self.state["bot"]["apps"]
                and root["path"] == candidate["path"]
                and root["root_id"] == candidate["root_id"]
                and job_findings.bot_text(root["body"])["digest"] == candidate["digest"]
            ):
                self.reply(candidate, "answer")

    def prepare_republish(self):
        intent = self.state.get("republish")
        if not intent or intent.get("head"):
            return
        head = self.git(
            "commit-tree",
            self.state["T"],
            "-p",
            intent["from"],
            stdin=job_text.squash_message(self.title, self.commit),
        )
        # Only per-head observations reset; thread identities and the followup survive.
        self.state = {
            k: v
            for k, v in self.state.items()
            if k not in CI_WINDOW | {"push_intent", "pushed", "base_update"}
        }
        bot = self.state["bot"]
        bot = {k: v for k, v in bot.items() if k not in {"head", "window_end", "outcome"}}
        self.save(
            H=head,
            commit_metadata=self.git("show", "-s", "--format=raw", head),
            head_parent=intent["from"],
            republish={**intent, "head": head},
            bot={**bot, "outcome": {}},
        )

    def reply(self, candidate, kind):
        """Persist intent before writing; reconcile viewer, marker and exact bytes."""
        bot = self.state["bot"]
        key = f"{kind}:{candidate['root_id']}:{digest(candidate['body'])}"
        replies = dict(bot.get("replies", {}))
        prior = replies.get(key, {})
        entry = resume_entry(self.current())
        args = (self.repo, self.state["pr"]["number"], candidate["root_id"], candidate["body"])
        previous = tuple(
            row["body"]
            for old_key, row in replies.items()
            if old_key != key
            and old_key.startswith(f"{kind}:{candidate['root_id']}:")
            and row["status"] == "posted"
        )
        if self.api.find_review_reply(*args, kind, previous):
            replies[key] = {"status": "posted", "entry": entry, "body": candidate["body"]}
            self.save(bot={**bot, "replies": replies})
            return
        if prior and (prior["status"] == "posted" or entry <= prior["entry"]):
            raise LandStop(
                "land_interrupted", "reply acknowledgement unknown; distinct resume needed"
            )
        replies[key] = {"status": "intent", "entry": entry, "body": candidate["body"]}
        self.save(bot={**bot, "replies": replies})
        self.freshness()
        try:
            self.api.post_review_reply(*args)
        except forge.Uncertain:
            pass
        if not self.api.find_review_reply(*args, kind, previous):
            raise LandStop(
                "land_interrupted", "reply acknowledgement unknown; distinct resume needed"
            )
        replies[key] = {**replies[key], "status": "posted"}
        self.save(bot={**self.state["bot"], "replies": replies})

    def reconcile_dispositions(self, threads):
        for candidate in self.state.get("bot", {}).get("dispositions", ()):
            thread = threads.get(candidate["thread_id"])
            if not thread or any(
                thread.get(k) != candidate.get(k) for k in ("path", "digest", "severity", "finding")
            ):
                continue
            if candidate["status"] == "not_fixed":
                self.owner_reasons()
                self.reply(candidate, "answer")
                thread["status"] = "answered"
            else:
                if not thread["resolved"]:
                    self.reply(candidate, "fixed")
                thread["status"] = "fixed"
            thread["disposition"] = candidate

    def fetch_main(self):
        ref = f"refs/remotes/origin/{self.repo.default_branch}"
        self.git("fetch", "--no-tags", "origin", f"+refs/heads/{self.repo.default_branch}:{ref}")
        return self.git("rev-parse", f"{ref}^{{commit}}")

    def base_update_enabled(self):
        return bool(self.state.get("base_update_enabled", self.ctx.config.land_base_update))

    def merge_tree(self, base, main, result):
        """The three-way tree of result onto main, or Git's conflict paths."""
        try:
            out = self.command(
                [
                    "git",
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "-z",
                    f"--merge-base={base}",
                    main,
                    result,
                ]
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
            return None, self.conflict_paths(exc.stdout or "")
        return out.split("\0", 1)[0], None

    def conflict_paths(self, output):
        # Git's NUL format separates the unmerged names from the logical conflict records.
        fields = output.split("\0")[1:]
        split = fields.index("") if "" in fields else len(fields)
        paths = set(fields[:split])
        index = split + 1
        while index < len(fields) and fields[index]:
            count = int(fields[index])
            end = index + count + 1
            if fields[end].startswith("CONFLICT"):
                paths.update(fields[index + 1 : end])
            index = end + 2
        return sorted(paths)

    def names(self, paths):
        shown = ", ".join(paths[:10])
        return shown + (f" (+{len(paths) - 10} more)" if len(paths) > 10 else "")

    def updated_merge(self, result, main):
        """Use Git's three-way merge, including when paths overlap."""
        tree, conflicts = self.merge_tree(self.state["B"], main, result)
        if conflicts is not None:
            detail = self.names(conflicts) if conflicts else "no merged tree"
            raise LandStop("land_base_moved", "automatic base update conflicts: " + detail)
        return tree

    def link_dependencies(self, path):
        """Give the detached tree the job worktree's ignored paths.

        A fresh worktree holds no built dependencies, so a check that needs `node_modules`, a
        virtual environment or another ignored folder fails there although it passed in the job
        worktree. A symlink shares them without copying, does not touch the result workspace and
        goes away with the tree, so running the check here keeps the smallest surface.
        """
        workspace = Path(self.ctx.job.workspace)
        listing = self.command(
            ["git", "-C", str(workspace), "status", "--porcelain", "--ignored=traditional", "-z"]
        )
        for entry in listing.split("\0"):
            if not entry.startswith("!! "):
                continue
            relative = Path(entry[3:].rstrip("/"))
            source, target = workspace / relative, path / relative
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=source.is_dir())

    def check_updated_tree(self, head, main, tree):
        """Run the accepted check command on the updated tree before publication.

        The run takes a host check slot like the check step, so `check_slots` bounds it too.
        """
        from .job_runner import _acquire_slot, _release_slot

        path = self.ctx.folder / "base-update-tree"
        log = self.ctx.folder / "base-update.log"
        self.remove_check_tree(path)
        record = {"exit_code": 0, "log": str(log)}
        try:
            self.git("worktree", "add", "--detach", str(path), head)
            self.link_dependencies(path)
            slot = None
            try:
                slot = _acquire_slot(self.ctx)
                try:
                    output = self.command(
                        list(self.ctx.job.check_command),
                        timeout=self.ctx.config.rpc_timeout,
                        cwd=str(path),
                    )
                except subprocess.CalledProcessError as exc:
                    output = (exc.stdout or "") + (exc.stderr or "")
                    record["exit_code"] = exc.returncode
                except subprocess.TimeoutExpired:
                    output = "check command timed out\n"
                    record["exit_code"] = "timeout"
                except OSError as exc:
                    output = f"check command could not run: {exc}\n"
                    record["exit_code"] = "error"
            finally:
                _release_slot(slot)
            log.write_text(output, encoding="utf-8")
        finally:
            self.remove_check_tree(path)
        if record["exit_code"] != 0:
            self.save(
                base_update_failure={
                    "B": self.state["B"],
                    "M": main,
                    "T": self.state["T"],
                    "tree": tree,
                    "at": time.time(),
                    **record,
                }
            )
            raise LandStop(
                "land_base_update_failed",
                f"check command exited {record['exit_code']} on the updated base; see {log}",
            )
        return record

    def remove_check_tree(self, path):
        if path.exists():
            with contextlib.suppress(subprocess.SubprocessError):
                self.git("worktree", "remove", "--force", str(path))
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        with contextlib.suppress(subprocess.SubprocessError):
            self.git("worktree", "prune")

    def publication_head(self, target=None):
        """The remote publication head, or None before the branch exists."""
        remote = self.git(
            "ls-remote",
            "--heads",
            target or self.push_target(),
            f"refs/heads/{self.state['branch']}",
        )
        return remote.split()[0] if remote else None

    def owned_heads(self):
        """Publication heads this job created, which a rewrite may replace."""
        republish = self.state.get("republish") or {}
        update = self.state.get("base_update") or {}
        heads = [
            self.state.get("H"),
            republish.get("from"),
            republish.get("head"),
            update.get("head"),
            update.get("from"),
            update.get("previous"),
            *(record.get("head") for record in self.state.get("base_updates", ())),
        ]
        return {head for head in heads if head}

    def pending_base_update(self):
        update = self.state.get("base_update") or {}
        if update.get("head") and (
            update["head"] != self.state.get("H") or not self.state.get("pushed")
        ):
            return update
        return {}

    def reset_per_head(self):
        """A new publication head restarts CI and bot observation; review evidence stays."""
        self.reset_ci_window()
        self.state = {k: v for k, v in self.state.items() if k not in {"push_intent", "pushed"}}
        if bot := self.state.get("bot"):
            self.state = {
                **self.state,
                "bot": {
                    **{k: v for k, v in bot.items() if k not in {"head", "window_end", "outcome"}},
                    "outcome": {},
                },
            }

    def reset_ci_window(self):
        """A return to fix observes CI again in a fresh bounded window and rerun round."""
        self.state = {k: v for k, v in self.state.items() if k not in CI_WINDOW}

    def update_base(self, main):
        """Check and stage the publication for main before changing its branch."""
        if not self.base_update_enabled() or self.state.get("merge_intent"):
            return False
        if self.method() == "merge":
            # A rewritten base would replace the reviewed lineage, so a merge-method transaction
            # keeps the accepted result as its head and parks a moved base for the owner.
            return False
        updates = list(self.state.get("base_updates", ()))
        advancing = not updates or main != self.state.get("M")
        if advancing and len(updates) >= MAX_BASE_UPDATES:
            raise LandStop(
                "land_base_moved",
                f"default branch moved more than {MAX_BASE_UPDATES} times during landing",
            )
        tree = self.updated_merge(self.state["R"], main)
        previous = self.state.get("H")
        published = self.publication_head()
        if published is not None and published not in self.owned_heads():
            raise LandStop("land_head_changed", "publication branch belongs to a different head")
        old = self.state.get("base_update") or {}
        if published is None and (
            self.state.get("pushed") or self.state.get("pr") or old.get("published")
        ):
            raise LandStop("land_head_changed", "publication branch disappeared")
        # On the same base, append to the observed PR head unless an unpublished intent must
        # be replaced. An unchanged draft promoted to ready does not need a new head.
        append = not advancing and not self.pending_base_update() and published == previous
        if append and published and tree == self.state["T"]:
            self.state.pop("base_update_failure", None)
            self.save(base_updates=[*updates[:-1], {**updates[-1], "R": self.state["R"]}])
            return False
        parent = published if append and published else main
        message = job_text.squash_message(self.title, self.commit)
        head = self.git("commit-tree", tree, "-p", parent, stdin=message)
        check = self.check_updated_tree(head, main, tree)
        record = {
            "B": self.state["B"],
            "M": main,
            "R": self.state["R"],
            "T": self.state["T"],
            "tree": tree,
            "from_head": published,
            "head": head,
        }
        if not advancing:
            updates.pop()
        if published is not None:
            self.reset_per_head()
        self.state = {
            k: v for k, v in self.state.items() if k not in {"base_update", "base_update_failure"}
        }
        self.save(
            M=main,
            T=tree,
            base_updates=[*updates, record],
            base_update={
                "from": published,
                "previous": previous,
                "head": head,
                "tree": tree,
                "parent": parent,
                "identity": self.state["identity"],
                "check": check,
                "published": published is not None,
            },
        )
        return True

    def publish_base_update(self, update):
        """Push the checked candidate, leasing a branch rewrite to the observed head."""
        head = update["head"]
        parent = update.get("parent", self.state["M"])
        if self.git("rev-parse", f"{head}^{{tree}}") != update.get("tree") or self.git(
            "rev-list", "--parents", "-n", "1", head
        ).split() != [head, parent]:
            raise LandStop("land_head_changed", "updated publication object differs")
        self.rewrite_body_links(head)
        target = self.push_target()
        branch = f"refs/heads/{self.state['branch']}"
        remote_head = self.publication_head(target)
        if remote_head == head:
            self.adopt_base_update(update)
            return
        if remote_head is None and not update.get("published", False):
            lease = []
        elif (
            remote_head is not None
            and remote_head == update.get("from")
            and remote_head in self.owned_heads()
        ):
            lease = [f"--force-with-lease={branch}:{remote_head}"]
        elif remote_head is None:
            raise LandStop("land_head_changed", "publication branch disappeared")
        else:
            raise LandStop("land_head_changed", "publication branch belongs to a different head")
        if self.reviewed:
            self.freshness()
        self.save(push_intent=True, phase="push")
        try:
            self.git("push", *lease, target, f"{head}:{branch}")
        except subprocess.CalledProcessError:
            raise LandStop("land_interrupted", "base update push was refused") from None
        observed = self.git("ls-remote", "--heads", target, branch)
        if (observed.split()[0] if observed else None) != head:
            raise LandStop("land_interrupted", "base update push acknowledgement unknown")
        self.adopt_base_update(update)

    def adopt_base_update(self, update):
        head = update["head"]
        changes = {
            "H": head,
            "head_parent": update.get("parent", self.state["M"]),
            "commit_metadata": self.git("show", "-s", "--format=raw", head),
            "pushed": True,
        }
        self.save(**changes, republish={})

    def observe_base_update(self):
        """The same pull request follows a rewritten branch before its guards run again."""
        update = self.state.get("base_update") or {}
        if (
            not update.get("head")
            or update["head"] != self.state.get("H")
            or update.get("from") is None
            or update.get("pr_observed")
            or not self.state.get("pr")
        ):
            return
        deadline = time.monotonic() + 60
        while True:
            pr = self.api.read_pull_request(self.repo, self.state["pr"]["number"], None)
            if pr.head_sha == update["head"]:
                self.save(base_update={**update, "pr_observed": True})
                return
            if pr.head_sha not in {
                update.get("from"),
                update.get("previous"),
                (self.state.get("republish") or {}).get("from"),
            }:
                raise LandStop("land_pr_mismatch", "updated publication head was not observed")
            if time.monotonic() >= deadline:
                raise LandStop("land_pr_mismatch", "updated publication head was not observed")
            self.wait(2)

    def base_guard(self):
        """False when the publication stands on the fetched base; True after an update."""
        main = self.fetch_main()
        expected = self.state.get("M", self.state["B"])
        if main != expected:
            if not self.state.get("M") and self.predecessor_base(main):
                pass
            elif self.update_base(main):
                return True
            else:
                self.replay(main)
                raise LandStop("land_base_moved", "fresh default branch differs from accepted base")
        elif updates := self.state.get("base_updates"):
            pending = self.pending_base_update()
            if updates[-1].get("R") != self.state["R"] or (
                pending and pending.get("identity") != self.state["identity"]
            ):
                return self.update_base(main)
            return False
        self.save(M=main, T=self.result_tree)
        return False

    def predecessor_base(self, main):
        job = self.current()
        for predecessor in job.predecessors:
            prior = self.ctx.jobs.load().get(predecessor)
            if prior is None or prior.stage not in {"deliver_pending", "done"}:
                continue
            state = prior.land_state
            if (
                state.get("S") == main
                and state.get("tree_verified")
                and state.get("repository_key") == self.state["repository_key"]
                and state.get("install", {}).get("status") in {"succeeded", "disabled"}
                and self.git("rev-parse", f"{self.state['B']}^{{tree}}") == state.get("T")
                and self.git("rev-parse", f"{main}^{{tree}}") == state.get("T")
            ):
                return True
        return False

    def replay(self, main):
        job = self.current()
        if self.method() == "merge":
            # A replay would squash the accepted result; a merge keeps its commits instead.
            return
        prior = self.ctx.jobs.load().get(job.base_predecessor)
        state = prior.land_state if prior else {}
        entry = resume_entry(job)
        count = sum(
            event.get("type") == "land_replay" and event.get("entry") == entry
            for event in job.events
        )
        if (
            prior is None
            or prior.stage not in {"deliver_pending", "done"}
            or state.get("tree_verified") is not True
            or state.get("repository_key") != self.state["repository_key"]
            or state.get("install", {}).get("status") not in {"succeeded", "disabled"}
            or not state.get("S")
            or any(self.state.get(key) for key in ("H", "push_intent", "S"))
            or count >= 2
        ):
            return
        try:
            self.git("merge-base", "--is-ancestor", state["S"], main)
            tree = self.git(
                "merge-tree",
                "--write-tree",
                "--no-messages",
                f"--merge-base={job.base}",
                main,
                self.state["R"],
            ).splitlines()[0]
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 1 and exc.stdout:
                raise LandStop("land_replay_conflict", "stack replay conflicts with main") from None
            return
        if tree == self.git("rev-parse", f"{main}^{{tree}}"):
            return
        commit = self.git(
            "commit-tree",
            tree,
            "-p",
            main,
            stdin=job_text.squash_message(self.title, self.commit),
        )
        self.save(
            replay={
                "base": job.base,
                "onto": main,
                "from": self.state["R"],
                "tree": tree,
                "commit": commit,
            }
        )
        self.finish_replay()

    def finish_replay(self):
        from .job_runner import _apply, _event, _requeue

        replay = self.state["replay"]
        job = self.current()
        head = self.git("-C", job.workspace, "rev-parse", "HEAD")
        if (
            self.git("-C", job.workspace, "status", "--porcelain")
            or head not in {replay["from"], replay["commit"]}
            or job.base != replay["base"]
        ):
            raise LandStop("land_review_stale", "workspace changed during stack replay")
        if head != replay["commit"]:
            self.git("-C", job.workspace, "reset", "--hard", replay["commit"])
        _apply(
            self.ctx,
            lambda j: {
                **_requeue(replace(j, recoveries=0)),
                "base": replay["onto"],
                "public_text": [{**j.public_text[0], "base": replay["onto"]}],
                "land_state": {},
                "recoveries": 0,
                "reviews_used": 0,
                "review_cycle_start": len(j.reviews),
                "check_cycle_start": len(j.checks),
                "events": _event(j, type="land_replay", entry=resume_entry(j), **replay),
            },
        )
        raise FreshnessRequeued

    def push_target(self):
        urls = self.git("remote", "get-url", "--push", "--all", "origin").splitlines()
        fetch_urls = self.git("remote", "get-url", "--all", "origin").splitlines()
        identity = (self.repo.host.lower(), self.repo.owner.lower(), self.repo.name.lower())
        try:
            valid = (
                urls
                and fetch_urls
                and all(
                    tuple(part.lower() for part in forge.parse_origin(url, (self.repo.host,)))
                    == identity
                    for url in [*urls, *fetch_urls]
                )
            )
        except forge.ForgeError:
            valid = False
        if not valid:
            raise LandStop("land_head_changed", "push URLs differ from approved repository")
        endpoints = {"push": urls, "fetch": fetch_urls}
        if self.state.get("publication_endpoints", endpoints) != endpoints:
            raise LandStop("land_head_changed", "frozen publication endpoints changed")
        self.save(publication_endpoints=endpoints)
        # Resolved URLs would undergo insteadOf expansion a second time as argv.
        return "origin"

    def head(self):
        pending = self.pending_base_update()
        if pending:
            self.publish_base_update(pending)
            return
        self.republish_for_tree()
        if not self.state.get("H"):
            if self.method() == "merge":
                # A merge keeps the worker's commits, so publish the reviewed lineage itself.
                head = self.state["R"]
            else:
                message = job_text.squash_message(self.title, self.commit)
                head = self.git(
                    "commit-tree", self.state["T"], "-p", self.state["M"], stdin=message
                )
            metadata = self.git("show", "-s", "--format=raw", head)
            self.save(H=head, commit_metadata=metadata, phase="head")
        head = self.state["H"]
        self.rewrite_body_links(head)
        tree = self.git("rev-parse", f"{head}^{{tree}}")
        if self.method() == "merge":
            if tree != self.state["T"] or head != self.state["R"]:
                raise LandStop("land_head_changed", "merge publication head is not the result")
        else:
            parents = self.git("rev-list", "--parents", "-n", "1", head).split()
            if tree != self.state["T"] or parents != [
                head,
                self.state.get("head_parent", self.state["M"]),
            ]:
                raise LandStop("land_head_changed", "publication object parent or tree differs")
        target = self.push_target()
        remote_head = self.publication_head(target)
        previous = self.state.get("republish", {}).get("from")
        if remote_head not in {None, head, previous} or (previous and remote_head is None):
            raise LandStop("land_head_changed", "publication branch belongs to a different head")
        if remote_head != head:
            if self.reviewed:
                self.freshness()
            self.save(push_intent=True, phase="push")
            self.git("push", target, f"{head}:refs/heads/{self.state['branch']}")
        self.save(pushed=True)

    def rewrite_body_links(self, candidate=None):
        """Move a reviewed link only to a head with the same blob."""

        # Before writing a branch or PR, refuse to publish a link that the base update made
        # unresolvable. Reading the old head during initialization is not a publication.
        head = candidate or self.state.get("H")
        if not head or self.reviewed_body is None:
            return
        reviewed = set(self.git("rev-list", f"{self.state['B']}..{self.state['R']}").split())
        identity = f"{self.repo.owner}/{self.repo.name}".lower()
        count = 0

        def blob(commit, path):
            try:
                return self.git("rev-parse", "--verify", "--quiet", f"{commit}:{path}")
            except subprocess.CalledProcessError:
                return ""

        def published(match):
            nonlocal count
            if match["repo"].lower() != identity:
                return match[0]
            try:
                commit = self.git("rev-parse", "--verify", "--quiet", f"{match['sha']}^{{commit}}")
            except subprocess.CalledProcessError:
                return match[0]
            if commit not in reviewed:
                return match[0]
            path = unquote(match["path"])
            same = blob(commit, path)
            at_head = blob(head, path)
            if not same or same != at_head:
                if (
                    same
                    and candidate
                    and self.state.get("base_updates")
                    and blob(self.state["R"], path) != at_head
                ):
                    raise LandStop(
                        "land_pr_mismatch",
                        f"base update changed reviewed PR link target {path}; "
                        "use a link to an already published revision",
                    )
                return match[0]
            count += 1
            url = (
                f"https://github.com/{self.repo.owner}/{self.repo.name}/"
                f"{match['kind']}/{head}/{match['path']}"
            )
            return url + (match["suffix"] or "")

        body = BODY_LINK.sub(published, self.reviewed_body)
        if body != self.body:
            self.body = body
            self.body_file.write_text(body, encoding="utf-8")
        if self.state.get("body_rewrites") != count:
            self.save(body_rewrites=count)

    def validate_pr(self, pr):
        if (
            pr.head_sha != self.state["H"]
            or pr.head_branch != self.state["branch"]
            or pr.base_branch != self.repo.default_branch
            or pr.title != self.title
            or pr.body != self.body
            or pr.state == "CLOSED"
        ):
            raise LandStop("land_pr_mismatch", "PR identity, state or reviewed publication differs")
        return pr

    def edit_published_pr(self):
        """Write the current title and body to a published PR when its bytes differ."""
        intent = self.state.get("republish", {})
        old = self.state.get("published_text", {})
        if not self.state.get("pr") or not old or (intent and not intent.get("head")):
            return
        # A body that did not change needs no edit, but a republish still waits for the new head.
        if old.get("title") == self.title and old.get("body") == self.body and not intent:
            return
        deadline = time.monotonic() + 60
        while True:
            pr = self.api.read_pull_request(self.repo, self.state["pr"]["number"], None)
            if pr.head_sha == self.state["H"]:
                break
            if pr.head_sha != intent.get("from") or time.monotonic() >= deadline:
                raise LandStop("land_pr_mismatch", "republished PR head was not observed")
            self.wait(2)
        if pr.title == self.title and pr.body == self.body:
            # An earlier attempt finished this edit; only its readback reply was lost. Record
            # those bytes and clear the intent, so the next text change is a new edit.
            self.save(edit_intent={}, published_text={"title": self.title, "body": self.body})
            return
        if pr.title != old.get("title") or pr.body != old.get("body"):
            raise LandStop("land_pr_mismatch", "PR text differs from previously published bytes")
        entry = resume_entry(self.current())
        edit = self.state.get("edit_intent", {})
        if edit.get("head") == self.state["H"] and entry <= edit["entry"]:
            raise LandStop("land_interrupted", "PR edit acknowledgement unknown; resume required")
        self.freshness()
        self.save(edit_intent={"head": self.state["H"], "entry": entry})
        try:
            self.api.edit_pull_request(
                self.repo, pr.number, self.state["H"], self.title, self.body_file
            )
        except forge.Uncertain:
            pass
        observed = self.api.read_pull_request(self.repo, pr.number, self.state["H"])
        if observed.title == old.get("title") and observed.body == old.get("body"):
            raise LandStop("land_interrupted", "PR edit acknowledgement unknown; resume required")
        self.validate_pr(observed)
        # The readback proves this edit; a later text change on the same head is a new edit,
        # not a replay of an unknown acknowledgement.
        self.save(edit_intent={})

    def pull_request(self, *, create=True):
        matches = self.api.find_pull_requests(
            self.repo, self.state["branch"], self.repo.default_branch, self.state["H"]
        )
        if len(matches) > 1:
            raise LandStop("land_pr_mismatch", "duplicate publication PRs")
        if matches:
            pr = self.validate_pr(matches[0])
            saved = self.state.get("pr")
            if saved and saved != {"number": pr.number, "url": pr.url}:
                raise LandStop("land_pr_mismatch", "saved PR identity differs")
        else:
            entry = resume_entry(self.current())
            if self.state.get("pr"):
                raise LandStop("land_pr_mismatch", "saved PR disappeared")
            if not create:
                return None
            if self.state.get("create_intent") and entry <= self.state.get("create_entry", entry):
                raise LandStop(
                    "land_interrupted", "create outcome unknown; explicit resume required"
                )
            self.freshness()
            self.save(create_intent=True, create_entry=entry, phase="create")
            pr = self.validate_pr(
                self.api.create_pull_request(
                    self.repo, self.state["branch"], self.state["H"], self.title, self.body_file
                )
            )
        self.save(
            pr={"number": pr.number, "url": pr.url},
            phase="pr",
            published_text={"title": self.title, "body": self.body},
        )
        if pr.state == "MERGED":
            self.save(S=pr.merge_sha, phase="merged")
        return pr

    def observe_ci(self):
        """Query head CI with the current review app selection and record both."""
        self.migrate_green()
        apps = self.review_bots()
        previous = tuple(self.state.get("ci_bots", self.state.get("bot", {}).get("apps", ())))
        self.sync_bots(apps)
        options = {"exclude_apps": apps} if apps else {}
        evidence = self.api.inspect_ci(
            self.repo, self.state["pr"]["number"], self.state["H"], **options
        )
        if evidence.head_sha != self.state["H"] or any(
            e.head_sha != self.state["H"] for e in evidence.evidence
        ):
            raise LandStop("land_ci_failed", "CI evidence head differs")
        self.save(ci_evidence=[asdict(e) for e in evidence.evidence], ci_bots=list(apps))
        return evidence, apps, previous != tuple(apps)

    def migrate_green(self):
        """A pre-selection record used the frozen bot list; store that list with its digest."""
        saved = self.state.get("ci_green")
        if isinstance(saved, str):
            bots = self.state.get("ci_bots")
            if bots is None:
                bots = self.state.get("bot", {}).get("apps", ())
            self.save(ci_green={"digest": saved, "bots": list(bots)})

    def review_bots(self):
        """Read the repository's current app selection; it is configuration, not job state."""
        from .jobs import load_job_config

        repo = self.state.get("repo", {})
        identity = "/".join(str(repo.get(key, "")) for key in ("host", "owner", "name")).lower()
        try:
            config = load_job_config(self.ctx.jobs.state.root)
        except (OSError, ValueError) as exc:
            raise LandStop(
                "land_ci_failed", "current review bot configuration is unreadable"
            ) from exc
        return tuple((config.review_bots or {}).get(identity, ()))

    def sync_bots(self, apps):
        """Adopt the current selection; a changed app set starts its completion outcomes over."""
        bot = self.state.get("bot")
        if bot is None or list(apps) == list(bot.get("apps", ())):
            return bot
        bot = {**bot, "apps": list(apps), "outcome": {}}
        self.save(bot=bot)
        return bot

    def green(self, evidence, apps, message):
        """A saved green identity covers only the app selection that observed it."""
        if evidence.state != "success":
            fixable = any(e.conclusion == "failure" for e in evidence.evidence)
            raise LandStop("land_ci_failed", message, fixable=fixable)
        current = green_outcome(evidence)
        saved, bots = self.saved_green()
        # A record from before selections were stored cannot prove which apps it excluded, and
        # a legacy digest of every field cannot tell an id or status change from a real one.
        if saved is not None and bots is not None and bots == tuple(apps):
            if green_changed(saved, current):
                raise LandStop("land_ci_failed", message)
            return
        self.save(ci_green={"outcome": current, "bots": list(apps)})

    def saved_green(self):
        saved = self.state.get("ci_green")
        if isinstance(saved, dict):
            return saved.get("outcome"), tuple(saved.get("bots", ()))
        return saved, None

    def ci(self, *, bots=True, cycle=""):
        """Observe CI for the publication head; `bots=False` leaves the bot window unstarted.

        The draft phase proves CI before the review, so the review bots wait for a window that
        only starts once the reviewed text is published and the pull request is ready. `cycle`
        names the observation window: a cycle the job has not opened a window for resets it, so
        a new draft check or a new accepted review never inherits a spent deadline, while a
        retry under the same cycle keeps the window it opened.
        """
        if cycle:
            gate = {"head": self.state["H"], "cycle": cycle}
            if self.state.get("ci_gate") != gate:
                self.reset_ci_window()
                self.save(ci_gate=gate)
        bot = self.state.get("bot", {})
        if self.state.get("ci_green") and bot.get("apps") and bot.get("head") == self.state["H"]:
            evidence, apps, _ = self.observe_ci()
            self.green(evidence, apps, "saved green CI identity changed")
            return
        entry = resume_entry(self.current())
        if not self.state.get("ci_started_at") or (
            self.state.get("last_error") == "land_ci_failed"
            and entry > self.state.get("ci_entry", entry)
        ):
            now = time.time()
            self.save(
                ci_started_at=now,
                ci_deadline=now + self.ctx.config.rpc_timeout,
                ci_entry=entry,
                last_error="",
                phase="ci",
            )
        while True:
            if time.time() >= self.state["ci_deadline"]:
                raise LandStop("land_ci_failed", "CI observation deadline expired")
            evidence, apps, _ = self.observe_ci()
            if time.time() >= self.state["ci_deadline"]:
                raise LandStop("land_ci_failed", "CI observation deadline expired during query")
            if evidence.state == "success":
                self.green(evidence, apps, "CI became green under a new selection")
                bot = self.state.get("bot", {})
                if bots and bot.get("apps") and bot.get("head") != self.state["H"]:
                    self.save(
                        bot={
                            **bot,
                            "head": self.state["H"],
                            "window_end": time.time() + bot["wait"],
                            "outcome": {},
                        }
                    )
                return
            if evidence.state == "missing":
                remaining = self.state["ci_started_at"] + 15 - time.time()
                if remaining <= 0:
                    raise LandStop("land_ci_failed", "CI did not register within grace")
                self.wait(min(remaining, self.state["ci_deadline"] - time.time()))
                continue
            if evidence.state == "failed":
                self.rerun(evidence)
            self.wait(min(20, max(0, self.state["ci_deadline"] - time.time())))

    def rerun(self, evidence):
        failed = [e for e in evidence.evidence if e.kind == "run" and e.conclusion == "failure"]
        # Only a conclusion the worker's code can fix returns to a fix round; a cancelled run,
        # a timeout, an action-required gate or a missing check stays an owner-visible stop.
        fixable = any(e.conclusion == "failure" for e in evidence.evidence)
        if "rerun_round" not in self.state:
            if not failed:
                raise LandStop("land_ci_failed", "registered CI is not successful", fixable=fixable)
            self.save(
                rerun_round={str(e.id): {"attempt": e.attempt, "issued": False} for e in failed}
            )
        frozen = self.state["rerun_round"]
        for failure in failed:
            saved = frozen.get(str(failure.id))
            if not saved or (saved["issued"] and failure.attempt > saved["attempt"]):
                raise LandStop("land_ci_failed", "CI failed after frozen rerun round", fixable=True)
        if not failed:
            raise LandStop("land_ci_failed", "non-rerunnable CI failure", fixable=fixable)
        for run_id, saved in frozen.items():
            if saved["issued"]:
                continue
            frozen = {**frozen, run_id: {**saved, "issued": True}}
            self.save(rerun_round=frozen, phase="rerun")
            self.api.rerun_failed_run(self.repo, int(run_id), self.state["H"])

    def bot(self):
        if self.state.get("bot") is None:
            return
        while True:
            bot = self.sync_bots(self.review_bots())
            if not bot.get("apps"):
                return
            if bot.get("head") != self.state["H"] or "window_end" not in bot:
                # CI can become green before any app is configured. That head still gets a
                # bounded window; a later selection change reuses the existing end.
                bot = {
                    **bot,
                    "head": self.state["H"],
                    "window_end": time.time() + bot["wait"],
                    "outcome": {},
                }
                self.save(bot=bot)
            runs = self.api.inspect_bot_runs(self.repo, self.state["H"], bot["apps"])
            outcomes = dict(bot.get("outcome", {}))
            for app in bot["apps"]:
                if app in outcomes:
                    continue
                rows = runs[app]
                failed = next(
                    (
                        r
                        for r in rows
                        if r["status"] == "completed" and r["conclusion"] != "success"
                    ),
                    None,
                )
                if failed:
                    outcomes[app] = f"fallback:not_success:{failed['conclusion']}"
                elif rows and all(r["status"] == "completed" for r in rows):
                    outcomes[app] = "complete"
                elif time.time() >= bot["window_end"]:
                    outcomes[app] = "fallback:timeout" if rows else "fallback:absent"
            bot = {**bot, "outcome": outcomes}
            self.save(bot=bot)
            if all(app in outcomes for app in bot["apps"]):
                break
            self.wait(min(20, max(0, bot["window_end"] - time.time())))
        self.bot_findings()

    def bot_findings(self):
        """Save observation, durable leftovers and any stop as one state transition."""
        from .job_delivery import accepted_review
        from .job_runner import StaleAttempt, _cas, _event

        bot = self.state.get("bot", {})
        if not bot.get("apps"):
            return
        visible = self.api.review_threads(self.repo, self.state["pr"]["number"], self.state["H"])
        job = self.current()
        review = accepted_review(job)
        if not review.get("ok") or review.get("verdict") != "PASS":
            raise LandStop("land_review_stale", "no bound accepted code review")
        try:
            records, threads = job_findings.bot_updates(
                job, review, bot, visible, self.state["H"], time.time()
            )
        except ValueError as exc:
            raise LandStop("land_interrupted", str(exc)) from exc
        exhausted = job.reviews_used >= job.max_reviews
        self.reconcile_dispositions(threads)
        bot = self.state.get("bot", {})
        retained = set(bot.get("followup", {}).get("findings", ()))
        low = []
        for thread in threads.values():
            # A resolution toggle is not a disposition for a tracked high finding.
            if thread.get("disposition") and thread["status"] in {"fixed", "answered"}:
                status = thread["status"]
            elif thread["severity"] in {"P0", "P1"}:
                status = "open"
            # A retained deferral outlives a resume that resets the review budget.
            elif thread["finding"] in retained:
                status = "deferred"
            elif thread["resolved"]:
                status = "closed"
            elif exhausted:
                status = "deferred"
            else:
                status = "open"
            thread["status"] = status
            if status == "deferred":
                low.append(thread["finding"])
        followup = {"id": f"bot-followup:{job.id}", "findings": low}
        bot = {**bot, "threads": threads, "followup": followup}
        if stop_refs := [t["finding"] for t in threads.values() if t["status"] == "open"]:
            bot["round"] = {
                "seq": max(r["seq"] for r in records),
                "awaiting": True,
                "findings": stop_refs,
            }
        else:
            bot["round"] = {**bot.get("round", {}), "awaiting": False}
        state = {**self.state, "bot": bot}
        stop = any(thread["status"] == "open" for thread in threads.values())
        reason = "review_exhausted" if exhausted else "land_bot_findings"
        changes = {"land_state": state, "reviews": records, "evidence_at": time.time()}
        if stop:
            changes.update(
                stage="user_wait" if exhausted else "fix",
                resume_stage="fix",
                wait_reason=reason if exhausted else "",
                ended_at=time.time(),
                error="review bot findings require disposition; owner judges not_fixed reasons",
                events=_event(job, type="user_wait" if exhausted else "bot_fix", reason=reason),
            )
        if not _cas(self.ctx.jobs, self.ctx.job, {"land"}, lambda j: changes):
            raise StaleAttempt
        self.state = state
        job_findings.sync_index(
            self.ctx.jobs.state, self.ctx.jobs.load()[job.id], self.ctx.folder, time.time()
        )
        if stop:
            raise BotParked

    def premerge(self):
        """Refresh every merge precondition; the bot gate can outlive any of them."""
        self.freshness()
        if self.base_guard():
            # The updated tree needs a new publication head, then CI and this gate again.
            return BASE_UPDATED
        repo = self.api.inspect_repository()
        if repo != self.repo:
            raise LandStop("land_pr_mismatch", "repository capabilities changed")
        self.api._writable(repo, self.current().merge_method)
        pr = self.validate_pr(
            self.api.read_pull_request(repo, self.state["pr"]["number"], self.state["H"])
        )
        if pr.state == "MERGED":
            self.save(S=pr.merge_sha, phase="merged")
            return None
        if pr.draft:
            raise LandStop("land_merge_failed", "the pull request is still a draft")
        if pr.mergeability not in {"clean", "has_hooks"} or pr.auto_merge is not False:
            raise LandStop("land_merge_failed", "direct mergeability is not proven")
        if not self.state.get("ci_green"):
            raise LandStop("land_ci_failed", "merge requires observed green CI")
        evidence, apps, changed = self.observe_ci()
        self.green(evidence, apps, "green CI identity changed before merge")
        return pr, repo, changed

    def merge(self):
        # A lost merge acknowledgement is never authorization for another merge request.
        if self.state.get("merge_intent"):
            pr = self.validate_pr(
                self.api.read_pull_request(self.repo, self.state["pr"]["number"], self.state["H"])
            )
            if pr.state != "MERGED":
                raise LandStop("land_merge_failed", "saved merge request has no observed merge")
            self.save(S=pr.merge_sha, phase="merged")
            return False
        first = self.premerge()
        if first is BASE_UPDATED:
            return True
        if first is None:
            return False
        pr, repo, changed = first
        if changed and self.state.get("bot", {}).get("apps"):
            # The gate can outlive the approved base, PR text, acceptance evidence or CI.
            self.bot()
            again = self.premerge()
            if again is BASE_UPDATED:
                return True
            if again is None:
                return False
            pr, repo, changed = again
            if changed:
                raise LandStop("land_interrupted", "review bot selection changed during the gate")
        self.bot_findings()
        method = self.current().merge_method
        subject = self.title + f" (#{pr.number})"
        # Only the written title is measured: the generated suffix that GitHub appends to the
        # squash or merge subject is not part of the text the worker wrote or the review saw.
        problems = message_problems(
            job_text.squash_message(subject, self.commit),
            bullet_one_line=self.ctx.config.bullet_one_line,
            generated_suffix=f" (#{pr.number})",
        )
        if problems:
            raise LandStop("land_merge_failed", "; ".join(problems))
        self.save(merge_intent=True, phase="merge")
        try:
            self.api.merge_pull_request(
                repo, pr.number, self.state["H"], subject, self.commit_file, method=method
            )
        except forge.Uncertain:
            pass
        try:
            merged = self.validate_pr(self.api.observe_merge(repo, pr.number, self.state["H"]))
        except forge.ForgeError as exc:
            raise LandStop("land_merge_failed", str(exc)) from exc
        self.save(S=merged.merge_sha, phase="merged")
        return False

    def verify_tree(self):
        main = self.fetch_main()
        self.save(observed_main=main, tree_verified=False)
        squash = self.state["S"]
        try:
            contains = self.git("merge-base", "--is-ancestor", squash, main) == ""
            # Another change may land after S, so the default branch tip is not T; S must be.
            same = self.git("rev-parse", f"{squash}^{{tree}}") == self.state["T"]
        except subprocess.SubprocessError:
            contains = False
            same = False
        if not contains or not same:
            raise LandStop(
                "land_tree_mismatch", "merged commit is absent from the default branch or differs"
            )
        self.save(tree_verified=True, phase="verified")

    def post_merge_ci(self):
        """Require the merged commit's own push CI before installation, bounded like PR CI.

        Only a default-branch push run for S proves the merged work was built. Absence means
        no push workflow only when the observation at the end of the registration grace still
        has no run, so a run that registers inside the grace is judged, never skipped.
        """
        saved = self.state.get("post_merge_ci", {})
        if saved.get("status") in {"success", "none"}:
            return
        sha, apps = self.state["S"], tuple(self.state.get("bot", {}).get("apps", ()))
        entry = resume_entry(self.current())
        if not saved.get("started_at") or (
            self.state.get("last_error") == "land_post_merge_failed"
            and entry > saved.get("entry", entry)
        ):
            now = time.time()
            saved = {
                "status": "observing",
                "sha": sha,
                "bots": list(apps),
                "started_at": now,
                "deadline": now + self.ctx.config.rpc_timeout,
                "entry": entry,
            }
            self.save(post_merge_ci=saved, phase="post_merge_ci")
        while True:
            evidence = self.api.inspect_merged_ci(self.repo, sha, exclude_apps=apps)
            saved = {**saved, "evidence": [asdict(e) for e in evidence.evidence]}
            if not any(e.kind == "run" for e in evidence.evidence):
                end = min(saved["started_at"] + POST_MERGE_GRACE, saved["deadline"])
                if time.time() < end:
                    # Re-observe at the grace end; a run registered inside it must be judged.
                    self.wait(end - time.time())
                    continue
                self.save(post_merge_ci={**saved, "status": "none"}, phase="post_merge_ci")
                return
            if evidence.state == "success":
                if time.time() >= saved["deadline"]:
                    raise LandStop(
                        "land_post_merge_failed",
                        f"post-merge CI for {sha} finished after the deadline",
                    )
                self.save(post_merge_ci={**saved, "status": "success"}, phase="post_merge_ci")
                return
            if evidence.state in {"failed", "missing"}:
                self.save(post_merge_ci={**saved, "status": "failed"})
                raise LandStop(
                    "land_post_merge_failed",
                    f"post-merge CI for {sha} is not green: "
                    + post_merge_problems(saved["evidence"]),
                )
            if time.time() >= saved["deadline"]:
                raise LandStop(
                    "land_post_merge_failed",
                    f"post-merge CI for {sha} did not finish before the deadline: "
                    + post_merge_problems(saved["evidence"]),
                )
            self.save(post_merge_ci=saved)
            self.wait(min(20, max(0, saved["deadline"] - time.time())))

    def install(self):
        hook = self.state["install"]
        from .jobs import load_job_config

        try:
            current_config = load_job_config(self.ctx.jobs.state.root)
        except (OSError, ValueError) as exc:
            raise LandStop(
                "land_hook_changed", "current install configuration is unreadable"
            ) from exc
        configured = selected_install_command(current_config, self.repo)
        if digest(configured) != hook["command_hash"]:
            raise LandStop("land_hook_changed", "restore frozen install configuration or cancel")
        if hook["status"] in {"succeeded", "disabled"}:
            return
        entry = resume_entry(self.current())
        if hook["status"] == "started":
            hook = {**hook, "status": "unknown", "error": "installation interrupted"}
            self.save(install=hook)
        if hook["status"] in {"failed", "unknown"} and entry <= hook.get("entry", entry):
            raise LandStop("land_install_failed", hook.get("error", hook["status"]))
        argv = install_argv(hook["command"], self.state["S"], self.ctx.job.target, self.repo)
        if hook.get("argv") and hook["argv"] != argv:
            raise LandStop("land_hook_changed", "frozen expanded installation differs")
        hook = {**hook, "status": "started", "entry": entry, "argv": argv}
        self.save(install=hook, phase="install")
        try:
            log = self.command(argv, timeout=300, cwd=self.ctx.job.target)
            outcome = {"status": "succeeded", "exit_code": 0, "log": log}
        except subprocess.CalledProcessError as exc:
            outcome = {
                "status": "failed",
                "exit_code": exc.returncode,
                "log": (exc.stdout or "") + (exc.stderr or ""),
                "error": "install exited nonzero",
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            outcome = {"status": "unknown", "error": type(exc).__name__}
        self.save(install={**hook, **outcome})
        if outcome["status"] != "succeeded":
            raise LandStop("land_install_failed", outcome["error"])

    def run(self):
        if self.state.get("replay"):
            self.finish_replay()
        if self.draft_pending():
            self.draft()
            return
        self.initialize()
        self.owner_reasons()
        self.post_answers()
        while True:
            published_body = (self.state.get("published_text") or {}).get("body")
            if (
                not self.state.get("S")
                and self.state.get("H")
                and not self.state.get("republish")
                and not self.pending_base_update()
                and published_body in (None, self.body)
            ):
                self.pull_request(create=False)
            if not self.state.get("S") and not self.state.get("merge_intent"):
                self.base_guard()
                self.head()
                self.observe_base_update()
                self.edit_published_pr()
                self.pull_request()
                self.ready()
            if not self.state.get("S"):
                if not self.state.get("merge_intent"):
                    try:
                        self.ci(cycle=self.state.get("identity", ""))
                    except LandStop as exc:
                        if not exc.fixable or not self.return_for_ci_fix("reviewed", str(exc)):
                            raise
                    self.bot()
                try:
                    updated = self.merge()
                except LandStop as exc:
                    if not exc.fixable or not self.return_for_ci_fix("merge", str(exc)):
                        raise
                    updated = False
                if updated:
                    continue
            break
        self.verify_tree()
        self.post_merge_ci()
        self.install()
        from .job_runner import _apply

        ci, install = self.state["post_merge_ci"], self.state["install"]
        self.save(
            verified={
                "sha": self.state["S"],
                "post_merge_ci": ci["status"],
                "ci_evidence": ci["evidence"],
                "install": install["status"],
            },
            phase="complete",
            last_error="",
        )
        _apply(
            self.ctx,
            lambda j: {
                "stage": "deliver_pending",
                "resume_stage": "deliver_pending",
                "ended_at": time.time(),
                "error": "",
                "wait_reason": "",
            },
        )


class BotParked(Exception):
    pass


class FreshnessRequeued(Exception):
    pass


def selected_install_command(config, repo):
    command = (
        config.install_command
        if config.install_commands is None
        else config.install_commands.get(repo.identity.lower())
    )
    return list(command) if command else None


def install_argv(command, sha, target, repo):
    if not command or not any("{sha}" in part for part in command):
        raise LandStop("land_hook_changed", "install command requires full {sha}")
    for part in command:
        if part.startswith(("git+https://", "git+ssh://")):
            if not part.endswith("@{sha}"):
                raise LandStop("land_hook_changed", "source URL must end in @{sha}")
            origin = part[4 : -len("@{sha}")]
            identity = forge.parse_origin(origin, (repo.host,))
        elif part.startswith("https://") and "/archive/" in part:
            url = urlsplit(part)
            pieces = url.path.strip("/").split("/")
            if (
                len(pieces) != 4
                or pieces[2] != "archive"
                or pieces[3] not in {"{sha}.tar.gz", "{sha}.zip", "{sha}"}
                or url.query
                or url.fragment
                or url.username
                or url.password
            ):
                raise LandStop("land_hook_changed", "archive revision path requires pinned SHA")
            identity = (url.hostname, pieces[0], pieces[1].removesuffix(".git"))
        else:
            continue
        if tuple(p.lower() for p in identity) != (
            repo.host.lower(),
            repo.owner.lower(),
            repo.name.lower(),
        ):
            raise LandStop("land_hook_changed", "install source differs from transaction origin")
    return [part.replace("{sha}", sha).replace("{target}", target) for part in command]


def post_merge_problems(evidence):
    """Name every piece of a merged commit's CI observation that is not verified."""
    problems = []
    for item in evidence:
        kind = item.get("kind")
        conclusion = item.get("conclusion") or "no conclusion"
        if kind == "missing":
            problems.append(f"required check {item.get('name')} did not register")
        elif kind == "run":
            problems.append(
                f"run {item.get('id')} {item.get('name')} {item.get('status')}/{conclusion}"
            )
        elif kind == "status":
            problems.append(f"status {item.get('name')} {item.get('conclusion')}")
        else:
            problems.append(f"check {item.get('name')} {item.get('status')}/{conclusion}")
    return "; ".join(problems) or "no successful push workflow run"


def run_land(ctx):
    from .job_runner import _stop

    key = job_workspace.repository_key(ctx.job.target, ctx.env)
    with (Path(key) / "taskwatch-land.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _stop(
                ctx.jobs, ctx.job, "land_repository_busy", "repository landing lock is held"
            )
        transaction = Transaction(ctx, lock.fileno())
        try:
            transaction.run()
            return 0
        except Routed:
            return 0
        except FreshnessRequeued:
            return 0
        except BotParked:
            return 1
        except (LandStop, forge.ForgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = exc.reason if isinstance(exc, LandStop) else "land_interrupted"
            if isinstance(exc, forge.Mismatch):
                reason = "land_pr_mismatch"
            message = (
                forge.command_timeout(exc)
                if isinstance(exc, subprocess.TimeoutExpired)
                else str(exc)
            )
            transaction.save(last_error=reason, error=message)
            # The owner still sees the stop once, but a resume repairs the check on the
            # worker's side while the same branch and pull request can take the fix.
            resume = (
                "fix" if reason == "land_base_update_failed" and transaction.resume_fix() else None
            )
            return _stop(ctx.jobs, ctx.job, reason, message, resume_stage=resume)

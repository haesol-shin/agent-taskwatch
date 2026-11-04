"""Reconcile one accepted tree through publication, squash, pinned install and delivery."""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from . import forge, job_text, job_workspace
from .checks import message_problems
from .jobs import _json_value


class LandStop(Exception):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def resume_entry(job):
    return sum(e.get("type") in {"resume", "approve"} for e in job.events)


class Boundary(forge.GitHub):
    def __init__(self, transaction):
        super().__init__(transaction.ctx.job.target, transaction.ctx.env)
        self.transaction = transaction

    def _command(self, argv, *, write=False):
        try:
            return self.transaction.command(argv, timeout=self._remaining())
        except (OSError, subprocess.SubprocessError):
            error = forge.Uncertain if write else forge.ForgeError
            raise error("command failed; reconcile saved operation identity") from None


class Transaction:
    def __init__(self, ctx, lock_fd):
        self.ctx = ctx
        self.lock_fd = lock_fd
        self.state = _json_value(ctx.job.land_state)
        self.api = Boundary(self)

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

    def command(self, argv, *, timeout=30, cwd=None, stdin=None):
        self.current()
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
                        raise subprocess.TimeoutExpired(argv[0], timeout)
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
            time.sleep(min(15, end - time.monotonic()))
            self.save()

    def freshness(self):
        from .job_delivery import acceptance_current
        from .job_runner import _requeue

        job = self.current()
        if acceptance_current(self.ctx.jobs, self.ctx.config, job):
            return
        if self.state.get("push_intent") or self.state.get("S"):
            raise LandStop("land_review_stale", "accepted evidence changed after publication")
        from .job_runner import _apply

        _apply(self.ctx, lambda j: {**_requeue(j), "land_state": {}})
        raise FreshnessRequeued

    def initialize(self):
        self.freshness()
        job = self.current()
        if not job.land or job.review != "code" or job.review_target is not None:
            raise LandStop("land_review_stale", "landing authority is absent")
        review = job.reviews[-1]
        texts = review.get("texts", {})
        if len(job.public_text) != 1 or len(texts) != 1:
            raise LandStop("land_review_stale", "landing needs one complete-result publication")
        unit = next(iter(texts))
        text = texts[unit]
        if (
            text.get("text_contract") != 2
            or text.get("head") != review.get("fingerprint")
            or text.get("base") != job.base
            or self.git("rev-parse", f"{text['head']}^{{tree}}") != text.get("tree")
        ):
            raise LandStop("land_review_stale", "checked publication does not cover accepted tree")
        identity = digest({"review": _json_value(review), "check": _json_value(job.checks[-1])})
        if self.state.get("identity", identity) != identity:
            raise LandStop("land_review_stale", "accepted transaction identity changed")
        copy = self.ctx.folder / "land-text"
        code, _ = job_text.reviewed_copy(job, self.ctx.env, self.ctx.folder, unit, copy)
        if code:
            raise LandStop("land_review_stale", "reviewed snapshot cannot be copied")
        self.title = (copy / "title.txt").read_bytes().decode().rstrip("\n")
        self.body = (copy / "body.md").read_bytes().decode()
        self.commit_file = copy / "commit.txt"
        self.body_file = copy / "body.md"
        self.commit = self.commit_file.read_bytes().decode()
        if bool(self.commit.strip()) == job_text.blank_body(job.public_text[0]):
            raise LandStop("land_review_stale", "squash body does not match the unit declaration")
        repo = self.api.inspect_repository()
        self.api._writable(repo)
        if self.state.get("identity"):
            if self.state["repo"] != asdict(repo):
                raise LandStop("land_pr_mismatch", "repository identity or capabilities changed")
        else:
            hook = (
                list(self.ctx.config.install_command) if self.ctx.config.install_command else None
            )
            self.save(
                identity=identity,
                phase="accepted",
                R=text["head"],
                T=text["tree"],
                B=text["base"],
                repo=asdict(repo),
                branch=job.branch + "-land",
                repository_key=job_workspace.repository_key(job.target, self.ctx.env),
                review_digest=digest(_json_value(review)),
                check_digest=digest(_json_value(job.checks[-1])),
                snapshot_digest=review["snapshot_sha256"],
                text_digest=digest(_json_value(text)),
                install={
                    "status": "not_started" if hook else "disabled",
                    "command": hook,
                    "command_hash": digest(hook),
                },
            )
        self.repo = repo

    def fetch_main(self):
        ref = f"refs/remotes/origin/{self.repo.default_branch}"
        self.git("fetch", "--no-tags", "origin", f"+refs/heads/{self.repo.default_branch}:{ref}")
        return self.git("rev-parse", f"{ref}^{{commit}}")

    def base_guard(self):
        main = self.fetch_main()
        if main != self.state["B"] and not self.predecessor_base(main):
            raise LandStop("land_base_moved", "fresh default branch differs from accepted base")
        if self.state.get("M") and self.state["M"] != main:
            raise LandStop("land_base_moved", "default branch changed during landing")
        self.save(M=main)

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
        if not self.state.get("H"):
            message = job_text.squash_message(self.title, self.commit)
            head = self.git("commit-tree", self.state["T"], "-p", self.state["M"], stdin=message)
            metadata = self.git("show", "-s", "--format=raw", head)
            self.save(H=head, commit_metadata=metadata, phase="head")
        head = self.state["H"]
        if self.git("rev-parse", f"{head}^{{tree}}") != self.state["T"] or self.git(
            "rev-list", "--parents", "-n", "1", head
        ).split() != [head, self.state["M"]]:
            raise LandStop("land_head_changed", "publication object parent or tree differs")
        target = self.push_target()
        remote = self.git("ls-remote", "--heads", target, f"refs/heads/{self.state['branch']}")
        if remote and remote.split()[0] != head:
            raise LandStop("land_head_changed", "publication branch belongs to a different head")
        if not remote:
            self.freshness()
            self.save(push_intent=True, phase="push")
            self.git("push", target, f"{head}:refs/heads/{self.state['branch']}")
        self.save(pushed=True)

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
        self.save(pr={"number": pr.number, "url": pr.url}, phase="pr")
        if pr.state == "MERGED":
            self.save(S=pr.merge_sha, phase="merged")
        return pr

    def observe_ci(self):
        evidence = self.api.inspect_ci(self.repo, self.state["pr"]["number"], self.state["H"])
        if evidence.head_sha != self.state["H"] or any(
            e.head_sha != self.state["H"] for e in evidence.evidence
        ):
            raise LandStop("land_ci_failed", "CI evidence head differs")
        self.save(ci_evidence=[asdict(e) for e in evidence.evidence])
        return evidence

    def ci(self):
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
            evidence = self.observe_ci()
            if time.time() >= self.state["ci_deadline"]:
                raise LandStop("land_ci_failed", "CI observation deadline expired during query")
            if evidence.state == "success":
                self.save(ci_green=digest([asdict(e) for e in evidence.evidence]))
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
        if "rerun_round" not in self.state:
            if not failed:
                raise LandStop("land_ci_failed", "registered CI is not successful")
            self.save(
                rerun_round={str(e.id): {"attempt": e.attempt, "issued": False} for e in failed}
            )
        frozen = self.state["rerun_round"]
        for failure in failed:
            saved = frozen.get(str(failure.id))
            if not saved or (saved["issued"] and failure.attempt > saved["attempt"]):
                raise LandStop("land_ci_failed", "CI failed after frozen rerun round")
        if not failed:
            raise LandStop("land_ci_failed", "non-rerunnable CI failure")
        for run_id, saved in frozen.items():
            if saved["issued"]:
                continue
            frozen = {**frozen, run_id: {**saved, "issued": True}}
            self.save(rerun_round=frozen, phase="rerun")
            self.api.rerun_failed_run(self.repo, int(run_id), self.state["H"])

    def merge(self):
        # A lost merge acknowledgement is never authorization for another merge request.
        if self.state.get("merge_intent"):
            pr = self.validate_pr(
                self.api.read_pull_request(self.repo, self.state["pr"]["number"], self.state["H"])
            )
            if pr.state != "MERGED":
                raise LandStop("land_merge_failed", "saved merge request has no observed merge")
            self.save(S=pr.merge_sha, phase="merged")
            return
        self.freshness()
        self.base_guard()
        repo = self.api.inspect_repository()
        if repo != self.repo:
            raise LandStop("land_pr_mismatch", "repository capabilities changed")
        self.api._writable(repo)
        pr = self.validate_pr(
            self.api.read_pull_request(repo, self.state["pr"]["number"], self.state["H"])
        )
        if pr.state == "MERGED":
            self.save(S=pr.merge_sha, phase="merged")
            return
        if pr.mergeability not in {"clean", "has_hooks"} or pr.auto_merge is not False:
            raise LandStop("land_merge_failed", "direct mergeability is not proven")
        evidence = self.observe_ci()
        if (
            evidence.state != "success"
            or digest([asdict(e) for e in evidence.evidence]) != self.state["ci_green"]
        ):
            raise LandStop("land_ci_failed", "green CI identity changed before merge")
        subject = self.title + f" (#{pr.number})"
        problems = message_problems(
            job_text.squash_message(subject, self.commit),
            bullet_one_line=self.ctx.config.bullet_one_line,
        )
        if problems:
            raise LandStop("land_merge_failed", "; ".join(problems))
        self.save(merge_intent=True, phase="merge")
        try:
            self.api.squash_merge(repo, pr.number, self.state["H"], subject, self.commit_file)
        except forge.Uncertain:
            pass
        try:
            merged = self.validate_pr(self.api.observe_merge(repo, pr.number, self.state["H"]))
        except forge.ForgeError as exc:
            raise LandStop("land_merge_failed", str(exc)) from exc
        self.save(S=merged.merge_sha, phase="merged")

    def verify_tree(self):
        main = self.fetch_main()
        self.save(observed_main=main, tree_verified=False)
        squash = self.state["S"]
        try:
            contains = self.git("merge-base", "--is-ancestor", squash, main) == ""
            same = self.git("rev-parse", f"{squash}^{{tree}}") == self.state["T"]
            same = same and self.git("rev-parse", f"{main}^{{tree}}") == self.state["T"]
        except subprocess.SubprocessError:
            contains = False
            same = False
        if not contains or not same:
            raise LandStop("land_tree_mismatch", "merged/default tree differs from accepted tree")
        self.save(tree_verified=True, phase="verified")

    def install(self):
        hook = self.state["install"]
        from .jobs import load_job_config

        try:
            current_config = load_job_config(self.ctx.jobs.state.root)
        except (OSError, ValueError) as exc:
            raise LandStop(
                "land_hook_changed", "current install configuration is unreadable"
            ) from exc
        configured = (
            list(current_config.install_command) if current_config.install_command else None
        )
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
        self.initialize()
        if (
            not self.state.get("S")
            and self.state.get("H")
            and (self.state.get("pr") or self.state.get("create_intent"))
        ):
            self.pull_request(create=False)
        if not self.state.get("S") and not self.state.get("merge_intent"):
            self.base_guard()
            self.head()
            self.pull_request()
        if not self.state.get("S"):
            if not self.state.get("merge_intent"):
                self.ci()
            self.merge()
        self.verify_tree()
        self.install()
        from .job_runner import _apply

        self.save(phase="complete", last_error="")
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


class FreshnessRequeued(Exception):
    pass


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
        except FreshnessRequeued:
            return 0
        except (LandStop, forge.ForgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = exc.reason if isinstance(exc, LandStop) else "land_interrupted"
            if isinstance(exc, forge.Mismatch):
                reason = "land_pr_mismatch"
            transaction.save(last_error=reason, error=str(exc))
            return _stop(ctx.jobs, ctx.job, reason, str(exc))

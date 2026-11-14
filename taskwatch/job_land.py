"""Reconcile one accepted tree through publication, squash, pinned install and delivery."""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import forge, job_findings, job_text, job_workspace
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
        from .job_delivery import acceptance_current, accepted_review
        from .job_runner import _requeue

        job = self.current()
        self.dispositions_current(job, accepted_review(job))
        if acceptance_current(self.ctx.jobs, self.ctx.config, job):
            return
        if self.state.get("push_intent") or self.state.get("S"):
            raise LandStop("land_review_stale", "accepted evidence changed after publication")
        from .job_runner import _apply

        _apply(self.ctx, lambda j: {**_requeue(j), "land_state": {}})
        raise FreshnessRequeued

    def initialize(self):
        from .job_delivery import accepted_review

        self.freshness()
        job = self.current()
        if not job.land or job.review != "code" or job.review_target is not None:
            raise LandStop("land_review_stale", "landing authority is absent")
        review = accepted_review(job)
        self.dispositions_current(job, review)
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
            self.adopt_review(job, review, text, identity)
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
            hook = selected_install_command(self.ctx.config, repo)
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
        """Only a routed bot fix may replace a published transaction's acceptance."""
        from .job_runner import _apply, _requeue

        bot = self.state.get("bot", {})
        round_ = bot.get("round", {})
        if (
            not round_.get("awaiting")
            or review.get("seq", 0) <= round_.get("seq", 0)
            or self.state.get("S")
        ):
            raise LandStop("land_review_stale", "accepted transaction identity changed")
        candidates = job_findings.bot_dispositions(job, self.ctx.folder)
        if candidates != _json_value(review.get("bot_dispositions", [])):
            _apply(self.ctx, lambda j: _requeue(j))
            raise FreshnessRequeued
        refs = {c["finding"] for c in candidates}
        if refs != set(round_.get("findings", ())):
            raise LandStop("land_review_stale", "bot dispositions are incomplete")
        self.save(
            identity=identity,
            R=text["head"],
            T=text["tree"],
            review_digest=digest(_json_value(review)),
            check_digest=digest(_json_value(job.checks[-1])),
            snapshot_digest=review["snapshot_sha256"],
            text_digest=digest(_json_value(text)),
            bot={**bot, "dispositions": candidates, "disposition_review": review["seq"]},
            republish={"from": self.state["H"], "review": review["seq"]},
        )

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
            if k
            not in {
                "ci_green",
                "ci_evidence",
                "ci_started_at",
                "ci_deadline",
                "ci_entry",
                "rerun_round",
                "push_intent",
                "pushed",
            }
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

    def base_guard(self):
        main = self.fetch_main()
        if main != self.state["B"] and not self.predecessor_base(main):
            self.replay(main)
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

    def replay(self, main):
        job = self.current()
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
        if not self.state.get("H"):
            message = job_text.squash_message(self.title, self.commit)
            head = self.git("commit-tree", self.state["T"], "-p", self.state["M"], stdin=message)
            metadata = self.git("show", "-s", "--format=raw", head)
            self.save(H=head, commit_metadata=metadata, phase="head")
        head = self.state["H"]
        if self.git("rev-parse", f"{head}^{{tree}}") != self.state["T"] or self.git(
            "rev-list", "--parents", "-n", "1", head
        ).split() != [head, self.state.get("head_parent", self.state["M"])]:
            raise LandStop("land_head_changed", "publication object parent or tree differs")
        target = self.push_target()
        remote = self.git("ls-remote", "--heads", target, f"refs/heads/{self.state['branch']}")
        previous = self.state.get("republish", {}).get("from")
        remote_head = remote.split()[0] if remote else None
        if remote_head not in {None, head, previous} or (previous and remote_head is None):
            raise LandStop("land_head_changed", "publication branch belongs to a different head")
        if remote_head != head:
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

    def edit_republished_pr(self):
        intent = self.state.get("republish", {})
        if not intent or not intent.get("head") or not self.state.get("pr"):
            return
        deadline = time.monotonic() + 60
        while True:
            pr = self.api.read_pull_request(self.repo, self.state["pr"]["number"], None)
            if pr.head_sha == self.state["H"]:
                break
            if pr.head_sha != intent["from"] or time.monotonic() >= deadline:
                raise LandStop("land_pr_mismatch", "republished PR head was not observed")
            self.wait(2)
        if pr.title == self.title and pr.body == self.body:
            return
        old = self.state.get("published_text", {})
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
        apps = self.state.get("bot", {}).get("apps", ())
        options = {"exclude_apps": apps} if apps else {}
        evidence = self.api.inspect_ci(
            self.repo, self.state["pr"]["number"], self.state["H"], **options
        )
        if evidence.head_sha != self.state["H"] or any(
            e.head_sha != self.state["H"] for e in evidence.evidence
        ):
            raise LandStop("land_ci_failed", "CI evidence head differs")
        self.save(ci_evidence=[asdict(e) for e in evidence.evidence])
        return evidence

    def ci(self):
        bot = self.state.get("bot", {})
        if self.state.get("ci_green") and bot.get("apps") and bot.get("head") == self.state["H"]:
            evidence = self.observe_ci()
            if (
                evidence.state != "success"
                or digest([asdict(e) for e in evidence.evidence]) != self.state["ci_green"]
            ):
                raise LandStop("land_ci_failed", "saved green CI identity changed")
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
            evidence = self.observe_ci()
            if time.time() >= self.state["ci_deadline"]:
                raise LandStop("land_ci_failed", "CI observation deadline expired during query")
            if evidence.state == "success":
                changes = {"ci_green": digest([asdict(e) for e in evidence.evidence])}
                bot = self.state.get("bot", {})
                if bot.get("apps") and bot.get("head") != self.state["H"]:
                    changes["bot"] = {
                        **bot,
                        "head": self.state["H"],
                        "window_end": time.time() + bot["wait"],
                        "outcome": {},
                    }
                self.save(**changes)
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

    def bot(self):
        bot = self.state.get("bot", {})
        if not bot.get("apps"):
            return
        while True:
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
        self.bot_findings()
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
        self.initialize()
        self.owner_reasons()
        self.post_answers()
        self.prepare_republish()
        if not self.state.get("S") and self.state.get("H") and not self.state.get("republish"):
            self.pull_request(create=False)
        if not self.state.get("S") and not self.state.get("merge_intent"):
            self.base_guard()
            self.head()
            self.edit_republished_pr()
            self.pull_request()
        if not self.state.get("S"):
            if not self.state.get("merge_intent"):
                self.ci()
                self.bot()
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
        except BotParked:
            return 1
        except (LandStop, forge.ForgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = exc.reason if isinstance(exc, LandStop) else "land_interrupted"
            if isinstance(exc, forge.Mismatch):
                reason = "land_pr_mismatch"
            transaction.save(last_error=reason, error=str(exc))
            return _stop(ctx.jobs, ctx.job, reason, str(exc))

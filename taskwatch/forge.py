"""Deterministic GitHub observations, not an authorization or transaction manager.

The caller owns approval, persistence, registration grace, an absolute monotonic deadline
and rerun budgets. Every operation is single-shot; uncertain writes must be reconciled by
repository/head/base identity before another write. Merge exit zero is not merge proof:
observe_merge returns the actual squash SHA. GitHub's head guard does not atomically pin
base. CI covers required contexts and every check, status and PR workflow registered for
the exact head, using latest attempts. Path-filter-suppressed workflows are undiscoverable.
No empty, missing, skipped, cancelled or unknown evidence is green. No runner integration
or gh dependency is imposed on jobs that do not import and use this module.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from .job_workspace import default_branch


class ForgeError(Exception):
    """A failed or malformed response; diagnostics never include raw command output."""


class Unsupported(ForgeError):
    """The host or repository cannot support direct squash landing."""


class Mismatch(ForgeError):
    """Observed identity differs from the requested repository, head or state."""


class Pending(ForgeError):
    """The requested evidence is not yet available."""


class Uncertain(ForgeError):
    """A write may have happened; reconcile stable identity before issuing it again."""


@dataclass(frozen=True)
class Repository:
    host: str
    owner: str
    name: str
    default_branch: str
    squash_supported: bool | None
    queue_required: bool | None

    @property
    def identity(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    head_sha: str
    base_branch: str
    base_sha: str
    title: str
    body: str
    merge_sha: str | None
    mergeability: str | None
    auto_merge: bool | None
    head_branch: str


@dataclass(frozen=True)
class CIEvidence:
    kind: str
    id: int | None
    head_sha: str
    name: str
    workflow: str | None
    status: str | None
    conclusion: str | None
    required: bool | None
    attempt: int | None = None
    app_id: int | None = None
    run_id: int | None = None
    suite_id: int | None = None


@dataclass(frozen=True)
class CI:
    head_sha: str
    evidence: tuple[CIEvidence, ...]

    @property
    def state(self) -> str:
        if not self.evidence or any(e.kind == "missing" for e in self.evidence):
            return "missing"
        if any(
            e.status in {"requested", "queued", "in_progress", "pending", "waiting"}
            for e in self.evidence
        ):
            return "pending"
        if any(e.status != "completed" or e.conclusion != "success" for e in self.evidence):
            return "failed"
        return "success" if any(e.kind == "run" for e in self.evidence) else "missing"


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ForgeError("missing or invalid text field")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ForgeError("missing or invalid string field")
    return value


def _int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ForgeError("missing or invalid positive id")
    return value


def _sha(value: object) -> str:
    value = _text(value)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ForgeError("invalid full commit SHA")
    return value


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ForgeError("expected JSON object")
    return value


def _list(value: object) -> list:
    if not isinstance(value, list):
        raise ForgeError("expected JSON array")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ForgeError("missing or invalid boolean")
    return value


def _apps(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(app, str) or not re.fullmatch(r"[a-z0-9-]+", app) for app in value
    ):
        raise ForgeError("app slugs must match [a-z0-9-]+")
    return tuple(value)


def _bot_run(row: dict, head_sha: str) -> tuple[str, dict]:
    if _sha(row.get("head_sha")) != head_sha:
        raise Mismatch("check head differs")
    app = _text(_object(row.get("app")).get("slug"))
    _apps((app,))
    status = _text(row.get("status"))
    conclusion = row.get("conclusion")
    if conclusion is not None:
        conclusion = _text(conclusion)
    if status not in {
        "queued",
        "in_progress",
        "completed",
        "waiting",
        "requested",
        "pending",
    } or conclusion not in {
        None,
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }:
        raise ForgeError("invalid check status or conclusion")
    if (status == "completed") != (conclusion is not None):
        raise ForgeError("inconsistent check status and conclusion")
    return app, {"status": status, "conclusion": conclusion}


def parse_origin(origin: str, authenticated_hosts: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """Accept credential-free HTTPS or git SSH identities, never local/alias origins.

    Enterprise hosts must be explicitly allowed by the caller and authenticated by gh
    during inspection. The allowlist alone does not prove GitHub capability.
    """
    if not isinstance(origin, str) or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in origin
    ):
        raise Unsupported("whitespace or control characters in origin")
    if re.fullmatch(r"git@[^/:]+:[^\s]+", origin):
        origin = "ssh://" + origin.replace(":", "/", 1)
    try:
        url = urlsplit(origin)
        host = url.hostname or ""
        port = url.port
    except ValueError:
        raise Unsupported("invalid origin identity") from None
    if (
        url.scheme not in {"ssh", "https"}
        or url.password is not None
        or (url.username is not None and (url.scheme != "ssh" or url.username != "git"))
        or port is not None
        or url.query
        or url.fragment
        or host not in ("github.com", *authenticated_hosts)
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
    ):
        raise Unsupported("unsupported or ambiguous origin identity")
    path = url.path.removesuffix(".git")
    if not re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", path):
        raise Unsupported("ambiguous owner/repository identity")
    owner, name = path[1:].split("/")
    if name in {".", ".."}:
        raise Unsupported("ambiguous repository identity")
    return host, owner, name


class GitHub:
    """Single-shot git/gh boundary with caller environment and remaining deadline.

    deadline is an absolute time.monotonic() value shared by every subprocess, including
    capability and pagination queries. timeout caps each subprocess independently.
    """

    def __init__(
        self,
        target: str | Path,
        env: Mapping[str, str],
        *,
        authenticated_hosts: tuple[str, ...] = (),
        timeout: float = 30,
        deadline: float | None = None,
    ):
        self.target = Path(target)
        self.env = {**env, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
        self.authenticated_hosts = authenticated_hosts
        self.timeout = timeout
        self.deadline = deadline

    def _remaining(self) -> float:
        remaining = self.timeout
        if self.deadline is not None:
            remaining = min(remaining, self.deadline - time.monotonic())
        if remaining <= 0:
            raise Pending("caller deadline expired before command")
        return remaining

    def _command(self, argv: list[str], *, write: bool = False) -> str:
        remaining = self._remaining()
        try:
            with subprocess.Popen(
                argv,
                cwd=self.target,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as proc:
                try:
                    out, _ = proc.communicate(timeout=remaining)
                except BaseException:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise
        except subprocess.TimeoutExpired:
            error = Uncertain if write else Pending
            raise error("command timed out; output withheld") from None
        except (OSError, UnicodeError):
            error = Uncertain if write else ForgeError
            raise error("command could not be executed or decoded; output withheld") from None
        if proc.returncode:
            error = Uncertain if write else ForgeError
            raise error(f"command failed (exit {proc.returncode}); output withheld")
        return out

    def _api(
        self,
        repo: Repository,
        endpoint: str,
        *,
        pages: bool = False,
        fields: Mapping[str, str] | None = None,
    ):
        argv = [
            "gh",
            "api",
            "--hostname",
            repo.host,
            "--method",
            "POST" if fields else "GET",
            endpoint,
        ]
        for key, value in (fields or {}).items():
            argv += ["-f", f"{key}={value}"]
        if pages:
            argv += ["--paginate"]
        raw = self._command(argv)
        try:
            if not pages:
                return json.loads(raw)
            decoder = json.JSONDecoder()
            value = []
            while raw.strip():
                page, end = decoder.raw_decode(raw.lstrip())
                value.append(page)
                raw = raw.lstrip()[end:]
        except (ValueError, TypeError):
            raise ForgeError("malformed JSON; output withheld") from None
        return value

    @staticmethod
    def _path(repo: Repository) -> str:
        return f"repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def _rows(self, repo: Repository, endpoint: str, key: str | None = None) -> list[dict]:
        pages = _list(self._api(repo, endpoint, pages=True))
        if not pages:
            raise ForgeError("missing pagination envelope")
        rows = []
        for page in pages:
            items = _object(page).get(key) if key else page
            rows.extend(_object(row) for row in _list(items))
        if key and any(
            type(page.get("total_count")) is int and page["total_count"] > len(rows)
            for page in pages
        ):
            raise ForgeError("paginated response omitted registered evidence")
        return rows

    def _rules(self, repo: Repository, branch: str) -> list[dict]:
        return self._rows(repo, f"{self._path(repo)}/rules/branches/{quote(branch, safe='')}")

    def inspect_repository(self) -> Repository:
        origin = self._command(["git", "remote", "get-url", "--all", "origin"]).removesuffix("\n")
        host, owner, name = parse_origin(origin, self.authenticated_hosts)
        self._command(["gh", "auth", "status", "--hostname", host])
        provisional = Repository(host, owner, name, "", None, None)
        data = _object(self._api(provisional, self._path(provisional)))
        full_name = _text(data.get("full_name"))
        if full_name.lower() != f"{owner}/{name}".lower():
            raise Mismatch("repository response differs from origin")
        _, owner, name = parse_origin(f"https://{host}/{full_name}", self.authenticated_hosts)
        provisional = Repository(host, owner, name, "", None, None)
        branch = _text(data.get("default_branch"))
        # The shared helper supplies local tracking evidence, never its main fallback as truth.
        try:
            local = default_branch(str(self.target), self.env, timeout=self._remaining())
        except subprocess.TimeoutExpired:
            raise Pending("local default branch lookup timed out") from None
        except OSError:
            raise ForgeError("local default branch lookup failed") from None
        tracked = self._command(
            [
                "git",
                "for-each-ref",
                "--format=%(symref)",
                "refs/remotes/origin/HEAD",
            ]
        )
        if tracked and local != branch:
            raise Mismatch("local origin HEAD differs from forge default branch")
        rules = self._rules(provisional, branch)
        return Repository(
            host,
            owner,
            name,
            branch,
            _bool(data.get("allow_squash_merge")),
            any(_text(rule.get("type")) == "merge_queue" for rule in rules),
        )

    @staticmethod
    def _writable(repo: Repository):
        if repo.squash_supported is not True or repo.queue_required is not False:
            raise Unsupported("direct squash requires known squash support and no merge queue")

    def _pr(self, repo: Repository, data: dict, head_sha: str | None = None) -> PullRequest:
        head, base = _object(data.get("head")), _object(data.get("base"))
        identity = f"{repo.owner}/{repo.name}".lower()
        for side in (head, base):
            if _text(_object(side.get("repo")).get("full_name")).lower() != identity:
                raise Mismatch("pull request repository differs")
        head_id = _sha(head.get("sha"))
        branch = _text(base.get("ref"))
        if (head_sha is not None and head_id != _sha(head_sha)) or branch != repo.default_branch:
            raise Mismatch("pull request head or base differs")
        state = _text(data.get("state")).upper()
        merged = _bool(data.get("merged"))
        if state not in {"OPEN", "CLOSED"} or (merged and state != "CLOSED"):
            raise ForgeError("invalid pull request state")
        merge = data.get("merge_commit_sha")
        merge_sha = _sha(merge) if merged else None
        mergeability = data.get("mergeable_state")
        if mergeability is not None:
            mergeability = _text(mergeability)
        auto = data.get("auto_merge")
        if "auto_merge" not in data or (auto is not None and not isinstance(auto, dict)):
            raise ForgeError("missing or invalid auto merge evidence")
        url = _text(data.get("html_url"))
        number = _int(data.get("number"))
        if url != f"https://{repo.host}/{repo.owner}/{repo.name}/pull/{number}":
            raise Mismatch("pull request URL differs")
        return PullRequest(
            number,
            url,
            "MERGED" if merged else state,
            head_id,
            branch,
            _sha(base.get("sha")),
            _string(data.get("title")),
            _string(data.get("body")),
            merge_sha,
            mergeability,
            auto is not None,
            _text(head.get("ref")),
        )

    def read_pull_request(self, repo: Repository, number: int, head_sha: str) -> PullRequest:
        number = _int(number)
        data = _object(self._api(repo, f"{self._path(repo)}/pulls/{number}"))
        pr = self._pr(repo, data, head_sha)
        if pr.number != number:
            raise Mismatch("pull request number differs")
        return pr

    def find_pull_requests(
        self, repo: Repository, head_branch: str, base_branch: str, head_sha: str
    ) -> tuple[PullRequest, ...]:
        """Return zero or one all-state match, rejecting conflicting historical identities."""
        if base_branch != repo.default_branch:
            raise Mismatch("requested base differs from inspected default")
        endpoint = (
            f"{self._path(repo)}/pulls?state=all&per_page=100"
            f"&head={quote(repo.owner + ':' + _text(head_branch), safe='')}"
            f"&base={quote(base_branch, safe='')}"
        )
        found = []
        for row in self._rows(repo, endpoint):
            head = _object(row.get("head"))
            if _text(head.get("ref")) != head_branch:
                raise Mismatch("pull request branch differs")
            found.append(self.read_pull_request(repo, _int(row.get("number")), head_sha))
        if len(found) > 1:
            raise Mismatch("multiple pull requests match stable identity")
        return tuple(found)

    def create_pull_request(
        self, repo: Repository, head_branch: str, head_sha: str, title: str, body_file: str | Path
    ) -> PullRequest:
        """Issue one create and reconcile its identity; any lost acknowledgement is uncertain."""
        self._writable(repo)
        _sha(head_sha)
        self._command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo.identity,
                "--head",
                _text(head_branch),
                "--base",
                repo.default_branch,
                "--title",
                _text(title),
                "--body-file",
                str(Path(body_file).resolve()),
            ],
            write=True,
        )
        try:
            matches = self.find_pull_requests(repo, head_branch, repo.default_branch, head_sha)
            if not matches or matches[0].state != "OPEN":
                raise Mismatch("created pull request is not uniquely open")
            return matches[0]
        except ForgeError:
            raise Uncertain("create acknowledgement requires identity reconciliation") from None

    def _has_classic_protection(self, repo: Repository) -> bool:
        # A protected branch can have rulesets without a classic protection resource.
        # Only an explicit null rule on this exact ref proves absence, never a REST 404.
        query = (
            "query($owner:String!,$name:String!,$ref:String!){"
            "repository(owner:$owner,name:$name){nameWithOwner "
            "ref(qualifiedName:$ref){name branchProtectionRule{id}}}}"
        )
        data = _object(
            self._api(
                repo,
                "graphql",
                fields={
                    "query": query,
                    "owner": repo.owner,
                    "name": repo.name,
                    "ref": f"refs/heads/{repo.default_branch}",
                },
            )
        )
        if "errors" in data:
            raise ForgeError("classic protection query failed; output withheld")
        repository = _object(_object(data.get("data")).get("repository"))
        if _text(repository.get("nameWithOwner")).lower() != f"{repo.owner}/{repo.name}".lower():
            raise Mismatch("classic protection repository differs")
        ref = _object(repository.get("ref"))
        if _text(ref.get("name")) != repo.default_branch:
            raise Mismatch("classic protection branch differs")
        if "branchProtectionRule" not in ref:
            raise ForgeError("missing classic protection evidence")
        rule = ref["branchProtectionRule"]
        if rule is None:
            return False
        _text(_object(rule).get("id"))
        return True

    def _required(self, repo: Repository) -> set[tuple[str, int | None]]:
        path = self._path(repo)
        branch = quote(repo.default_branch, safe="")
        metadata = _object(self._api(repo, f"{path}/branches/{branch}"))
        if _text(metadata.get("name")) != repo.default_branch:
            raise Mismatch("branch protection response differs")
        required = set()
        rules = self._rules(repo, repo.default_branch)
        if _bool(metadata.get("protected")) and self._has_classic_protection(repo):
            protection = _object(self._api(repo, f"{path}/branches/{branch}/protection"))
            checks = protection.get("required_status_checks")
            if checks is not None:
                checks = _object(checks)
                for check in _list(checks.get("checks")):
                    check = _object(check)
                    app = check.get("app_id")
                    required.add((_text(check.get("context")), _int(app) if app else None))
                for context in _list(checks.get("contexts")):
                    if not any(name == context for name, _ in required):
                        required.add((_text(context), None))
        for rule in rules:
            kind = _text(rule.get("type"))
            if kind == "merge_queue":
                raise Unsupported("merge queue is required")
            if kind == "required_workflows" or kind == "workflows":
                raise Unsupported("required workflow rules cannot be resolved as contexts")
            if kind == "required_status_checks":
                params = _object(rule.get("parameters"))
                for check in _list(params.get("required_status_checks")):
                    check = _object(check)
                    app = check.get("integration_id")
                    required.add((_text(check.get("context")), _int(app) if app else None))
        return required

    def _workflow_runs(self, repo: Repository, pr: PullRequest) -> list[dict]:
        rows = self._rows(
            repo,
            f"{self._path(repo)}/actions/runs?head_sha={pr.head_sha}&per_page=100",
            "workflow_runs",
        )
        latest = {}
        for row in rows:
            self._validate_run(repo, row, pr.head_sha)
            event = _text(row.get("event"))
            prs = {_int(_object(item).get("number")) for item in _list(row.get("pull_requests"))}
            if prs and pr.number not in prs:
                raise Mismatch("workflow belongs to another pull request")
            if event in {"pull_request", "pull_request_target"}:
                if pr.number not in prs:
                    raise Mismatch("PR workflow has no matching PR association")
            elif event == "push":
                if _text(row.get("head_branch")) != pr.head_branch:
                    continue
            else:
                continue
            run_id, attempt = _int(row.get("id")), _int(row.get("run_attempt"))
            if run_id not in latest or attempt > _int(latest[run_id].get("run_attempt")):
                latest[run_id] = row
        return list(latest.values())

    def _job_attempts(
        self, repo: Repository, runs: list[dict], head_sha: str
    ) -> dict[int, tuple[int, int]]:
        attempts = {}
        for run in runs:
            if _int(run.get("run_attempt")) == 1:
                continue
            run_id = _int(run.get("id"))
            jobs = self._rows(
                repo,
                f"{self._path(repo)}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
                "jobs",
            )
            for job in jobs:
                attempt = _int(job.get("run_attempt"))
                if (
                    _sha(job.get("head_sha")) != head_sha
                    or _int(job.get("run_id")) != run_id
                    or attempt > run["run_attempt"]
                ):
                    raise Mismatch("job attempt identity differs")
                url = _text(job.get("check_run_url"))
                prefix = (
                    f"https://api.github.com/{self._path(repo)}/check-runs/"
                    if repo.host == "github.com"
                    else f"https://{repo.host}/api/v3/{self._path(repo)}/check-runs/"
                )
                if not url.startswith(prefix) or not url[len(prefix) :].isdigit():
                    raise Mismatch("job check URL differs")
                check_id = _int(int(url[len(prefix) :]))
                if check_id in attempts and attempts[check_id] != (run_id, attempt):
                    raise Mismatch("check has conflicting run attempts")
                attempts[check_id] = (run_id, attempt)
        return attempts

    def _checks(
        self,
        repo: Repository,
        head_sha: str,
        required: set,
        runs: list[dict],
        exclude_apps: tuple[str, ...] = (),
    ) -> list[CIEvidence]:
        rows = self._rows(
            repo,
            f"{self._path(repo)}/commits/{head_sha}/check-runs?filter=all&per_page=100",
            "check_runs",
        )
        attempts = self._job_attempts(repo, runs, head_sha)
        evidence = []
        for row in rows:
            if _sha(row.get("head_sha")) != head_sha:
                raise Mismatch("check head differs")
            name, app = _text(row.get("name")), _int(_object(row.get("app")).get("id"))
            is_required = (name, None) in required or (name, app) in required
            if exclude_apps and not is_required:
                if _text(_object(row.get("app")).get("slug")) in exclude_apps:
                    continue
            check_id = _int(row.get("id"))
            run_id, attempt = attempts.get(check_id, (None, None))
            evidence.append(
                CIEvidence(
                    "check",
                    check_id,
                    head_sha,
                    name,
                    None,
                    _text(row.get("status")),
                    row.get("conclusion"),
                    is_required,
                    attempt,
                    app,
                    run_id,
                    _int(_object(row.get("check_suite")).get("id")),
                )
            )
        # A higher check id is not proof of supersession: require the same run and a
        # strictly later attempt for the same app/name. Ambiguous checks remain visible.
        return [
            old
            for old in evidence
            if old.run_id is None
            or not any(
                new.run_id == old.run_id
                and new.name == old.name
                and new.app_id == old.app_id
                and new.attempt > old.attempt
                for new in evidence
                if new.attempt is not None
            )
        ]

    def _statuses(self, repo: Repository, head_sha: str, required: set) -> list[CIEvidence]:
        rows = self._rows(repo, f"{self._path(repo)}/commits/{head_sha}/statuses?per_page=100")
        contexts = {}
        for row in rows:
            name = _text(row.get("context"))
            if name not in contexts or _int(row.get("id")) > _int(contexts[name].get("id")):
                contexts[name] = row
        evidence = []
        for name, row in contexts.items():
            state = _text(row.get("state"))
            evidence.append(
                CIEvidence(
                    "status",
                    _int(row.get("id")),
                    head_sha,
                    name,
                    None,
                    "pending" if state == "pending" else "completed",
                    state,
                    (name, None) in required,
                )
            )
        return evidence

    def inspect_ci(self, repo: Repository, number: int, head_sha: str, *, exclude_apps=()) -> CI:
        """Gate on CI.state after caller-owned registration grace, never on evidence count.

        All optional registered checks/statuses and qualifying workflows also block green.
        Zero required contexts still needs a successful PR-associated or exact-branch push run.
        Workflow requiredness is derived from its suite's required check contexts.
        """
        head_sha = _sha(head_sha)
        exclude_apps = _apps(exclude_apps)
        pr = self.read_pull_request(repo, number, head_sha)
        required = self._required(repo)
        runs = self._workflow_runs(repo, pr)
        evidence = self._checks(repo, head_sha, required, runs, exclude_apps)
        evidence.extend(self._statuses(repo, head_sha, required))
        for row in runs:
            suite_id = _int(row.get("check_suite_id"))
            is_required = any(e.required and e.suite_id == suite_id for e in evidence)
            evidence.append(
                CIEvidence(
                    "run",
                    _int(row.get("id")),
                    head_sha,
                    _text(row.get("name")),
                    _text(row.get("path")),
                    _text(row.get("status")),
                    row.get("conclusion"),
                    is_required,
                    _int(row.get("run_attempt")),
                    suite_id=suite_id,
                )
            )
        for name, app in sorted(required, key=lambda item: (item[0], item[1] or 0)):
            if not any(
                e.name == name
                and (app is None or e.app_id == app)
                and e.kind in {"check", "status"}
                for e in evidence
            ):
                evidence.append(
                    CIEvidence(
                        "missing",
                        None,
                        head_sha,
                        name,
                        None,
                        None,
                        None,
                        True,
                        app_id=app,
                    )
                )
        return CI(head_sha, tuple(evidence))

    def inspect_bot_runs(self, repo: Repository, head_sha: str, apps) -> dict[str, list[dict]]:
        """Return every exact-head check for the requested app slugs, without supersession."""
        head_sha = _sha(head_sha)
        apps = _apps(apps)
        result = {app: [] for app in apps}
        rows = self._rows(
            repo,
            f"{self._path(repo)}/commits/{head_sha}/check-runs?filter=all&per_page=100",
            "check_runs",
        )
        for row in rows:
            app, run = _bot_run(row, head_sha)
            if app in result:
                result[app].append(run)
        return result

    def review_threads(self, repo: Repository, number: int, head_sha: str) -> list[dict]:
        """Observe root comments in thread order; never silently truncate comments."""
        number, head_sha = _int(number), _sha(head_sha)
        query = (
            "query($owner:String!,$name:String!,$cursor:String){"
            "repository(owner:$owner,name:$name){nameWithOwner "
            f"pullRequest(number:{number}){{number headRefOid "
            "reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor} "
            "nodes{id isResolved isOutdated comments(first:100){"
            "pageInfo{hasNextPage} nodes{databaseId url path line body "
            "author{__typename login}}}}}}}}"
        )
        result, seen = [], set()
        cursor = None
        while True:
            fields = {
                "query": query,
                "owner": repo.owner,
                "name": repo.name,
            }
            if cursor is not None:
                fields["cursor"] = cursor
            data = _object(self._api(repo, "graphql", fields=fields))
            if "errors" in data:
                raise ForgeError("review threads query failed; output withheld")
            repository = _object(_object(data.get("data")).get("repository"))
            identity = _text(repository.get("nameWithOwner"))
            if identity.lower() != f"{repo.owner}/{repo.name}".lower():
                raise Mismatch("review threads repository differs")
            pr = _object(repository.get("pullRequest"))
            if _int(pr.get("number")) != number or _sha(pr.get("headRefOid")) != head_sha:
                raise Mismatch("review threads pull request or head differs")
            threads = _object(pr.get("reviewThreads"))
            for node in _list(threads.get("nodes")):
                result.append(self._review_thread(_object(node)))
            page = _object(threads.get("pageInfo"))
            if not _bool(page.get("hasNextPage")):
                return result
            cursor = _text(page.get("endCursor"))
            if cursor in seen:
                raise ForgeError("review thread pagination repeated a cursor")
            seen.add(cursor)

    @staticmethod
    def _review_thread(node: dict) -> dict:
        comments = _object(node.get("comments"))
        if _bool(_object(comments.get("pageInfo")).get("hasNextPage")):
            raise ForgeError("review thread has more than 100 comments")
        roots = _list(comments.get("nodes"))
        if not roots:
            raise ForgeError("review thread has no root comment")
        root = _object(roots[0])
        author = _object(root.get("author"))
        line = root.get("line")
        if line is not None:
            line = _int(line)
        return {
            "id": _text(node.get("id")),
            "root_id": _int(root.get("databaseId")),
            "url": _text(root.get("url")),
            "path": _text(root.get("path")),
            "line": line,
            "author_type": _text(author.get("__typename")),
            "author": _text(author.get("login")),
            "body": _string(root.get("body")),
            "resolved": _bool(node.get("isResolved")),
            "outdated": _bool(node.get("isOutdated")),
        }

    @staticmethod
    def _validate_run(repo: Repository, row: dict, head_sha: str):
        if _sha(row.get("head_sha")) != _sha(head_sha):
            raise Mismatch("workflow head differs")
        identity = _text(_object(row.get("repository")).get("full_name"))
        if identity.lower() != f"{repo.owner}/{repo.name}".lower():
            raise Mismatch("workflow repository differs")

    def rerun_failed_run(self, repo: Repository, run_id: int, head_sha: str) -> None:
        """Issue one failed-job rerun; the caller observes the saved run/attempt afterward."""
        run_id = _int(run_id)
        row = _object(self._api(repo, f"{self._path(repo)}/actions/runs/{run_id}"))
        self._validate_run(repo, row, head_sha)
        if _int(row.get("id")) != run_id:
            raise Mismatch("workflow run id differs")
        if row.get("status") != "completed" or row.get("conclusion") != "failure":
            raise Mismatch("only an observed failed Actions run can be rerun")
        self._command(
            [
                "gh",
                "run",
                "rerun",
                str(run_id),
                "--repo",
                repo.identity,
                "--failed",
            ],
            write=True,
        )

    def squash_merge(
        self, repo: Repository, number: int, head_sha: str, subject: str, body_file: str | Path
    ) -> None:
        """Request squash only; always call observe_merge to establish actual MERGED and S."""
        self._writable(repo)
        pr = self.read_pull_request(repo, number, head_sha)
        if pr.state != "OPEN" or pr.auto_merge is not False:
            raise Mismatch("merge requires open PR without auto merge")
        if not subject.endswith(f" (#{number})"):
            raise Mismatch("merge subject must include the pull request suffix")
        self._command(
            [
                "gh",
                "pr",
                "merge",
                str(number),
                "--repo",
                repo.identity,
                "--squash",
                "--match-head-commit",
                head_sha,
                "--subject",
                subject,
                "--body-file",
                str(Path(body_file).resolve()),
            ],
            write=True,
        )

    def observe_merge(self, repo: Repository, number: int, head_sha: str) -> PullRequest:
        pr = self.read_pull_request(repo, number, head_sha)
        if pr.state == "OPEN":
            raise Pending("pull request has not been observed merged")
        if pr.state != "MERGED" or pr.merge_sha is None:
            raise Mismatch("pull request closed without a proven merge SHA")
        return pr

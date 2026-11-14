"""Exercise the GitHub boundary against an executable, offline forge model."""

import json
import os
import signal
import subprocess
import sys

import pytest

from taskwatch.forge import ForgeError, GitHub, Mismatch, Pending, Uncertain, Unsupported
from tests.git_fixture import git, make_repo

HOST = "forge.invalid"
IDENTITY = f"{HOST}/sample/project"
API = "repos/sample/project"
OTHER_SHA = "e" * 40
SQUASH_SHA = "f" * 40

FAKE_GH = r"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

state_file = Path(os.environ["FORGE_FIXTURE"])
state = json.loads(state_file.read_text())
args = sys.argv[1:]


def fail():
    sys.exit(73)


def option(name):
    if args.count(name) != 1:
        fail()
    return args[args.index(name) + 1]


def save():
    state_file.write_text(json.dumps(state))


def emit(value):
    print(json.dumps(value))


if args[:2] == ["auth", "status"]:
    if option("--hostname") != "forge.invalid" or not state.get("authenticated", True):
        fail()
    sys.exit(0)

if args[:1] == ["api"]:
    endpoint = args[args.index("--method") + 2]
    if (option("--hostname") != "forge.invalid"
            or option("--method") != ("POST" if endpoint == "graphql" else "GET")):
        fail()
    if endpoint in state.get("raw", {}):
        print(state["raw"][endpoint])
        sys.exit(0)
    if endpoint in state.get("errors", []):
        fail()
    if endpoint == "graphql":
        fields = dict(args[index + 1].split("=", 1)
                      for index, arg in enumerate(args) if arg == "-f")
        if "reviewThreads(" in fields.get("query", ""):
            if fields.get("owner") != "sample" or fields.get("name") != "project":
                fail()
            if "pullRequest(number:17)" not in fields["query"]:
                fail()
            if "comments(first:100)" not in fields["query"]:
                fail()
            emit(state["thread_pages"][int(fields.get("cursor", "0"))])
            sys.exit(0)
        if (fields.get("owner") != "sample" or fields.get("name") != "project"
                or fields.get("ref") != "refs/heads/main"):
            fail()
        value = state.get("graphql", {"data": {"repository": {
            "nameWithOwner": "sample/project", "ref": {
                "name": "main",
                "branchProtectionRule": state.get("classic_rule", {"id": "RULE_example"}),
            },
        }}})
        emit(value)
        sys.exit(0)
    parsed = urlsplit(endpoint)
    path = parsed.path
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        path = "/".join([parts[0], parts[1].lower(), parts[2].lower(), *parts[3:]])
    query = parse_qs(parsed.query)
    paged = False
    if path == "repos/sample/project":
        value = state["repository"]
    elif path == "repos/sample/project/rules/branches/main":
        value, paged = state["rules"], True
    elif path == "repos/sample/project/branches/main":
        value = state["branch"]
    elif path == "repos/sample/project/branches/main/protection":
        value = state["protection"]
    elif path == "repos/sample/project/pulls":
        if query != {"state": ["all"], "per_page": ["100"],
                     "head": ["sample:topic"], "base": ["main"]}:
            fail()
        value, paged = state["pull_pages"], True
    elif path.startswith("repos/sample/project/pulls/"):
        value = state["pulls"][path.rsplit("/", 1)[1]]
    elif path == "repos/sample/project/commits/" + state["head"] + "/check-runs":
        if query != {"filter": ["all"], "per_page": ["100"]}:
            fail()
        value = [{"check_runs": page} for page in state["checks"]]
        paged = True
    elif path == "repos/sample/project/commits/" + state["head"] + "/statuses":
        value, paged = state["statuses"], True
    elif path == "repos/sample/project/actions/runs":
        if query != {"head_sha": [state["head"]], "per_page": ["100"]}:
            fail()
        value = [{"workflow_runs": page} for page in state["runs"]]
        paged = True
    elif path.endswith("/jobs"):
        value = [{"jobs": page} for page in state.get("jobs", [[]])]
        paged = True
    elif path.startswith("repos/sample/project/actions/runs/"):
        value = state["run_details"][path.rsplit("/", 1)[1]]
    else:
        fail()
    if paged:
        for page in (value if "--paginate" in args else value[:1]):
            emit(page)
    else:
        emit(value)
    sys.exit(0)

if option("--repo") != "forge.invalid/sample/project":
    fail()
if args[:2] == ["pr", "create"]:
    if option("--head") != "topic" or option("--base") != "main":
        fail()
    body = Path(option("--body-file")).read_bytes()
    state["created_body"] = body.hex()
    pr = state["new_pull"]
    pr["title"] = option("--title")
    pr["body"] = body.decode("utf-8")
    state["pulls"][str(pr["number"])] = pr
    state["pull_pages"] = [[pr]]
    save()
    if state.get("create_timeout"):
        time.sleep(10)
    if state.get("create_lost_ack"):
        fail()
    print(pr["html_url"])
elif args[:2] == ["pr", "merge"]:
    if "--squash" not in args or option("--match-head-commit") != state["head"]:
        fail()
    pr = state["pulls"][args[2]]
    state["merged_body"] = Path(option("--body-file")).read_bytes().hex()
    state["merged_subject"] = option("--subject")
    if state.get("merge_observed", True):
        pr.update(state="closed", merged=True, merge_commit_sha=state["squash"])
    save()
    if state.get("merge_timeout"):
        time.sleep(10)
    if state.get("merge_lost_ack"):
        fail()
elif args[:2] == ["run", "rerun"]:
    if "--failed" not in args:
        fail()
    state.setdefault("reruns", []).append(int(args[2]))
    save()
else:
    fail()
"""


def pull(head, number=17, **updates):
    value = {
        "number": number,
        "html_url": f"https://{HOST}/sample/project/pull/{number}",
        "state": "open",
        "merged": False,
        "head": {"sha": head, "ref": "topic", "repo": {"full_name": "sample/project"}},
        "base": {"sha": OTHER_SHA, "ref": "main", "repo": {"full_name": "sample/project"}},
        "title": "fix: deterministic landing",
        "body": "Evidence.\n",
        "merge_commit_sha": None,
        "mergeable_state": "clean",
        "auto_merge": None,
    }
    value.update(updates)
    return value


def check(head, *, id=31, name="unit", conclusion="success", status="completed", app=7):
    return {
        "id": id,
        "name": name,
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app, "slug": "ci-app"},
        "check_suite": {"id": 41},
    }


def run(head, *, id=51, attempt=1, conclusion="success", status="completed"):
    return {
        "id": id,
        "head_sha": head,
        "run_attempt": attempt,
        "check_suite_id": id - 10,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "status": status,
        "conclusion": conclusion,
        "repository": {"full_name": "sample/project"},
        "pull_requests": [{"number": 17}],
        "event": "pull_request",
        "head_branch": "topic",
    }


class ForgeFixture:
    def __init__(self, root):
        self.target = make_repo(root)
        self.head = git(self.target, "rev-parse", "HEAD")
        git(self.target, "branch", "topic")
        git(self.target, "remote", "add", "origin", f"https://{HOST}/sample/project.git")
        git(self.target, "update-ref", "refs/remotes/origin/main", self.head)
        git(self.target, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.state_file = root / "forge-state.json"
        self.data = {
            "head": self.head,
            "squash": SQUASH_SHA,
            "repository": {
                "full_name": "sample/project",
                "default_branch": "main",
                "allow_squash_merge": True,
            },
            "branch": {"name": "main", "protected": False},
            "rules": [[]],
            "protection": {"required_status_checks": {"checks": [], "contexts": []}},
            "pulls": {"17": pull(self.head)},
            "pull_pages": [[pull(self.head)]],
            "new_pull": pull(self.head),
            "checks": [[check(self.head)]],
            "statuses": [[]],
            "runs": [[run(self.head)]],
            "run_details": {"51": run(self.head, conclusion="failure")},
        }
        bin_dir = root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "gh"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_GH, encoding="utf-8")
        executable.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FORGE_FIXTURE": str(self.state_file),
            "GH_HOST": HOST,
        }
        self.client = GitHub(self.target, self.env, authenticated_hosts=(HOST,))
        self.save()
        self.repo = self.client.inspect_repository()

    def save(self):
        self.state_file.write_text(json.dumps(self.data), encoding="utf-8")

    def observed(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def ci(self):
        self.save()
        return self.client.inspect_ci(self.repo, 17, self.head)

    def body_file(self):
        path = self.target.parent / "body.txt"
        path.write_bytes("Why this change.\n\nUnicode: \u2603\n".encode())
        return path


@pytest.fixture
def forge(tmp_path):
    return ForgeFixture(tmp_path)


def test_observed_success_preserves_exact_repository_pr_head_and_run(forge):
    assert forge.repo.identity == IDENTITY
    assert forge.repo.default_branch == "main"
    assert forge.repo.squash_supported is True
    assert forge.repo.queue_required is False
    (pr,) = forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)
    assert (pr.number, pr.head_sha, pr.base_sha, pr.state) == (17, forge.head, OTHER_SHA, "OPEN")
    ci = forge.ci()
    assert ci.state == "success"
    assert ci.head_sha == forge.head
    assert [(e.kind, e.id, e.head_sha) for e in ci.evidence] == [
        ("check", 31, forge.head),
        ("run", 51, forge.head),
    ]
    workflow = ci.evidence[1]
    assert (workflow.workflow, workflow.attempt) == (".github/workflows/ci.yml", 1)


@pytest.mark.parametrize("merged", [False, True])
def test_lookup_includes_closed_and_merged_pull_requests(forge, merged):
    pr = pull(
        forge.head, state="closed", merged=merged, merge_commit_sha=SQUASH_SHA if merged else None
    )
    forge.data["pulls"]["17"] = pr
    forge.data["pull_pages"] = [[], [pr]]
    forge.save()
    (found,) = forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)
    assert found.state == ("MERGED" if merged else "CLOSED")


def test_lookup_rejects_duplicates_across_pages(forge):
    second = pull(forge.head, number=18)
    forge.data["pulls"]["18"] = second
    forge.data["pull_pages"].append([second])
    forge.save()
    with pytest.raises(Mismatch):
        forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)


def test_lookup_returns_empty_without_inventing_a_pull_request(forge):
    forge.data["pull_pages"] = [[]]
    forge.save()
    assert forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head) == ()


def test_create_consumes_body_bytes_and_observes_created_identity(forge):
    forge.data["pulls"] = {}
    forge.data["pull_pages"] = [[]]
    forge.save()
    body = forge.body_file()
    pr = forge.client.create_pull_request(forge.repo, "topic", forge.head, "fix: exact body", body)
    assert (pr.number, pr.head_sha, pr.title) == (17, forge.head, "fix: exact body")
    assert pr.body.encode() == body.read_bytes()
    assert bytes.fromhex(forge.observed()["created_body"]) == body.read_bytes()


def test_merge_consumes_body_and_requires_observed_squash_sha(forge):
    body = forge.body_file()
    assert (
        forge.client.squash_merge(
            forge.repo,
            17,
            forge.head,
            "fix: deterministic landing (#17)",
            body,
        )
        is None
    )
    state = forge.observed()
    assert bytes.fromhex(state["merged_body"]) == body.read_bytes()
    assert state["merged_subject"] == "fix: deterministic landing (#17)"
    merged = forge.client.observe_merge(forge.repo, 17, forge.head)
    assert (merged.number, merged.head_sha, merged.merge_sha) == (17, forge.head, SQUASH_SHA)
    assert merged.merge_sha != merged.head_sha


def test_successful_merge_command_is_not_observed_merge_proof(forge):
    forge.data["merge_observed"] = False
    forge.save()
    forge.client.squash_merge(forge.repo, 17, forge.head, "fix: guarded (#17)", forge.body_file())
    with pytest.raises(Pending):
        forge.client.observe_merge(forge.repo, 17, forge.head)


def test_closed_without_merge_is_not_merge_proof(forge):
    forge.data["pulls"]["17"]["state"] = "closed"
    forge.save()
    with pytest.raises(Mismatch):
        forge.client.observe_merge(forge.repo, 17, forge.head)


@pytest.mark.parametrize("operation", ["create", "merge"])
def test_lost_write_acknowledgement_requires_reconciliation(forge, operation):
    forge.data[f"{operation}_lost_ack"] = True
    forge.save()
    body = forge.body_file()
    with pytest.raises(Uncertain):
        if operation == "create":
            forge.client.create_pull_request(forge.repo, "topic", forge.head, "fix: body", body)
        else:
            forge.client.squash_merge(forge.repo, 17, forge.head, "fix: body (#17)", body)
    if operation == "create":
        (pr,) = forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)
        assert (pr.number, pr.head_sha, pr.state) == (17, forge.head, "OPEN")
    else:
        assert forge.client.observe_merge(forge.repo, 17, forge.head).merge_sha == SQUASH_SHA


@pytest.mark.parametrize("field", ["head", "head_repo", "base_repo", "base", "number", "url"])
def test_read_rejects_pull_request_identity_mismatch(forge, field):
    pr = forge.data["pulls"]["17"]
    if field == "head":
        pr["head"]["sha"] = OTHER_SHA
    elif field in {"head_repo", "base_repo"}:
        pr[field.split("_")[0]]["repo"]["full_name"] = "other/project"
    elif field == "base":
        pr["base"]["ref"] = "other"
    elif field == "number":
        pr["number"] = 18
    else:
        pr["html_url"] = f"https://{HOST}/other/project/pull/17"
    forge.save()
    with pytest.raises(Mismatch):
        forge.client.read_pull_request(forge.repo, 17, forge.head)


@pytest.mark.parametrize("kind", ["empty", "checks_only", "statuses_only"])
def test_ci_requires_at_least_one_successful_workflow(forge, kind):
    forge.data["runs"] = [[]]
    if kind != "checks_only":
        forge.data["checks"] = [[]]
    if kind == "statuses_only":
        forge.data["statuses"] = [[{"id": 61, "context": "external", "state": "success"}]]
    assert forge.ci().state == "missing"


@pytest.mark.parametrize("kind", ["check", "run"])
@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "cancelled", "failed"),
        ("completed", "skipped", "failed"),
        ("completed", None, "failed"),
        ("completed", "neutral", "failed"),
        ("in_progress", None, "pending"),
        ("queued", None, "pending"),
        ("requested", None, "pending"),
    ],
)
def test_non_successful_registered_evidence_cannot_be_green(
    forge,
    kind,
    status,
    conclusion,
    expected,
):
    row = forge.data["checks" if kind == "check" else "runs"][0][0]
    row.update(status=status, conclusion=conclusion)
    assert forge.ci().state == expected


def test_optional_workflow_failure_blocks_even_with_required_check_success(forge):
    forge.data["branch"]["protected"] = True
    forge.data["protection"]["required_status_checks"]["contexts"] = ["unit"]
    optional = run(forge.head, id=52, conclusion="failure")
    optional.update(name="Optional audit", path=".github/workflows/audit.yml")
    forge.data["runs"].append([optional])
    ci = forge.ci()
    assert ci.state == "failed"
    assert next(e for e in ci.evidence if e.id == 31).required is True
    assert next(e for e in ci.evidence if e.id == 52).conclusion == "failure"
    assert next(e for e in ci.evidence if e.id == 52).required is False


@pytest.mark.parametrize("source", ["protection", "ruleset"])
def test_required_contexts_cannot_disappear(forge, source):
    if source == "protection":
        forge.data["branch"]["protected"] = True
        forge.data["protection"]["required_status_checks"]["contexts"] = ["security"]
    else:
        forge.data["rules"] = [
            [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "security", "integration_id": 7}],
                    },
                }
            ]
        ]
    ci = forge.ci()
    assert ci.state == "missing"
    (missing,) = (e for e in ci.evidence if e.kind == "missing")
    assert (missing.name, missing.required, missing.head_sha) == ("security", True, forge.head)


def test_required_app_context_is_not_satisfied_by_another_app(forge):
    forge.data["branch"]["protected"] = True
    forge.data["protection"]["required_status_checks"]["checks"] = [
        {"context": "unit", "app_id": 8},
    ]
    ci = forge.ci()
    assert ci.state == "missing"
    assert next(e for e in ci.evidence if e.kind == "missing").app_id == 8


@pytest.mark.parametrize("kind", ["merge_queue", "required_workflows", "workflows"])
def test_unresolvable_required_rules_are_not_treated_as_green(forge, kind):
    forge.data["rules"] = [[{"type": kind}]]
    with pytest.raises(Unsupported):
        forge.ci()


def test_latest_check_status_and_run_attempts_win_across_pages(forge):
    forge.data["checks"] = [
        [check(forge.head, id=30, conclusion="failure")],
        [check(forge.head)],
    ]
    forge.data["statuses"] = [
        [{"id": 60, "context": "external", "state": "failure"}],
        [{"id": 61, "context": "external", "state": "success"}],
    ]
    forge.data["runs"] = [
        [run(forge.head, attempt=1, conclusion="failure")],
        [run(forge.head, attempt=2)],
    ]
    forge.data["jobs"] = [
        [
            {
                "run_id": 51,
                "run_attempt": attempt,
                "head_sha": forge.head,
                "check_run_url": f"https://{HOST}/api/v3/{API}/check-runs/{check_id}",
            }
            for check_id, attempt in [(30, 1), (31, 2)]
        ]
    ]
    ci = forge.ci()
    assert ci.state == "success"
    assert [(e.kind, e.id, e.attempt) for e in ci.evidence] == [
        ("check", 31, 2),
        ("status", 61, None),
        ("run", 51, 2),
    ]


@pytest.mark.parametrize("field", ["check_head", "run_head", "run_repo", "run_pr"])
def test_ci_rejects_foreign_head_repository_or_pr(forge, field):
    if field == "check_head":
        forge.data["checks"][0][0]["head_sha"] = OTHER_SHA
    elif field == "run_head":
        forge.data["runs"][0][0]["head_sha"] = OTHER_SHA
    elif field == "run_repo":
        forge.data["runs"][0][0]["repository"]["full_name"] = "other/project"
    else:
        forge.data["runs"][0][0]["pull_requests"] = [{"number": 18}]
    with pytest.raises(Mismatch):
        forge.ci()


def test_rerun_uses_observed_failed_run(forge):
    forge.client.rerun_failed_run(forge.repo, 51, forge.head)
    assert forge.observed()["reruns"] == [51]


@pytest.mark.parametrize("field", ["id", "head", "repo", "success", "pending", "cancelled"])
def test_rerun_refuses_wrong_or_nonfailed_run_without_writing(forge, field):
    row = forge.data["run_details"]["51"]
    if field == "id":
        row["id"] = 52
    elif field == "head":
        row["head_sha"] = OTHER_SHA
    elif field == "repo":
        row["repository"]["full_name"] = "other/project"
    elif field == "pending":
        row.update(status="in_progress", conclusion=None)
    else:
        row["conclusion"] = field
    forge.save()
    with pytest.raises(Mismatch):
        forge.client.rerun_failed_run(forge.repo, 51, forge.head)
    assert "reruns" not in forge.observed()


@pytest.mark.parametrize("raw", ["not-json", "null", "[]", '{"number": null}'])
def test_malformed_or_null_pull_response_is_a_typed_error(forge, raw):
    forge.data["raw"] = {f"{API}/pulls/17": raw}
    forge.save()
    with pytest.raises(ForgeError):
        forge.client.read_pull_request(forge.repo, 17, forge.head)


@pytest.mark.parametrize("raw", ["null", "[]", "[null]", '[{"check_runs": null}]'])
def test_malformed_or_missing_paginated_evidence_is_not_green(forge, raw):
    endpoint = f"{API}/commits/{forge.head}/check-runs?filter=all&per_page=100"
    forge.data["raw"] = {endpoint: raw}
    with pytest.raises(ForgeError):
        forge.ci()


@pytest.mark.parametrize(
    "origin",
    [
        f"https://{HOST}/sample/project.git",
        f"git@{HOST}:sample/project.git",
        f"ssh://git@{HOST}/sample/project.git",
    ],
)
def test_supported_origin_schemes_require_explicit_enterprise_allowlist(forge, origin):
    git(forge.target, "remote", "set-url", "origin", origin)
    assert forge.client.inspect_repository().identity == IDENTITY
    without_allowlist = GitHub(forge.target, forge.env)
    with pytest.raises(Unsupported):
        without_allowlist.inspect_repository()


@pytest.mark.parametrize(
    "origin",
    [
        "file:///sample/project.git",
        "../project.git",
        "git://forge.invalid/sample/project.git",
        "https://token@forge.invalid/sample/project.git",
        "ssh://user@forge.invalid/sample/project.git",
        "https://forge.invalid:8443/sample/project",
        "https://forge.invalid/sample/project?token=synthetic",
        "alias:sample/project.git",
    ],
)
def test_ambiguous_or_credential_bearing_origins_are_unsupported(forge, origin):
    git(forge.target, "remote", "set-url", "origin", origin)
    with pytest.raises(Unsupported):
        forge.client.inspect_repository()


def test_enterprise_allowlist_does_not_replace_authentication(forge):
    forge.data["authenticated"] = False
    forge.save()
    with pytest.raises(ForgeError):
        forge.client.inspect_repository()


def test_repository_response_must_match_origin(forge):
    forge.data["repository"]["full_name"] = "other/project"
    forge.save()
    with pytest.raises(Mismatch):
        forge.client.inspect_repository()


@pytest.mark.parametrize("source", ["protection", "ruleset"])
def test_successful_required_context_is_matched_by_name_and_app(forge, source):
    if source == "protection":
        forge.data["branch"]["protected"] = True
        forge.data["protection"]["required_status_checks"]["checks"] = [
            {"context": "unit", "app_id": 7},
        ]
    else:
        forge.data["rules"] = [
            [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "unit", "integration_id": 7}],
                    },
                }
            ]
        ]
    ci = forge.ci()
    assert ci.state == "success"
    check_evidence = next(e for e in ci.evidence if e.kind == "check" and e.id == 31)
    workflow = next(e for e in ci.evidence if e.kind == "run" and e.id == 51)
    assert (check_evidence.name, check_evidence.app_id, check_evidence.required) == (
        "unit",
        7,
        True,
    )
    assert workflow.required is True


def test_required_legacy_status_can_satisfy_protection(forge):
    forge.data["branch"]["protected"] = True
    forge.data["protection"]["required_status_checks"]["contexts"] = ["external"]
    forge.data["statuses"] = [[{"id": 61, "context": "external", "state": "success"}]]
    ci = forge.ci()
    assert ci.state == "success"
    status = next(e for e in ci.evidence if e.kind == "status" and e.id == 61)
    assert (status.name, status.required) == ("external", True)


@pytest.mark.parametrize(
    "endpoint",
    [
        f"{API}/branches/main/protection",
        f"{API}/rules/branches/main",
    ],
)
def test_unavailable_protection_evidence_is_not_treated_as_unprotected(forge, endpoint):
    forge.data["branch"]["protected"] = True
    forge.data["errors"] = [endpoint]
    with pytest.raises(ForgeError):
        forge.ci()


def test_latest_failed_attempt_is_not_hidden_by_earlier_success(forge):
    forge.data["runs"] = [
        [run(forge.head, attempt=2, conclusion="failure")],
        [run(forge.head, attempt=1)],
    ]
    ci = forge.ci()
    assert ci.state == "failed"
    (workflow,) = (e for e in ci.evidence if e.kind == "run")
    assert (workflow.id, workflow.attempt, workflow.conclusion) == (51, 2, "failure")


@pytest.mark.parametrize("capability", ["squash", "queue"])
def test_unsupported_repository_cannot_create_or_merge(forge, capability):
    if capability == "squash":
        forge.data["repository"]["allow_squash_merge"] = False
    else:
        forge.data["rules"] = [[{"type": "merge_queue"}]]
    forge.save()
    repo = forge.client.inspect_repository()
    body = forge.body_file()
    with pytest.raises(Unsupported):
        forge.client.create_pull_request(repo, "topic", forge.head, "fix: body", body)
    with pytest.raises(Unsupported):
        forge.client.squash_merge(repo, 17, forge.head, "fix: body (#17)", body)
    state = forge.observed()
    assert "created_body" not in state
    assert "merged_body" not in state


def test_ambiguous_duplicate_checks_do_not_discard_failure(forge):
    forge.data["checks"] = [
        [
            check(forge.head, id=30, conclusion="failure"),
            check(forge.head),
        ]
    ]
    assert forge.ci().state == "failed"


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
def test_unassociated_non_ci_run_cannot_supply_success(forge, event):
    row = forge.data["runs"][0][0]
    row.update(pull_requests=[], event=event, head_branch="topic")
    assert forge.ci().state == "missing"


@pytest.mark.parametrize(
    "origin",
    [
        "https://for\ng e.invalid/sample/project",
        "https://for\nge.invalid/sample/project",
        " https://forge.invalid/sample/project",
        "https://forge.invalid/sample/project\t",
    ],
)
def test_raw_origin_normalization_is_rejected(origin):
    from taskwatch.forge import parse_origin

    with pytest.raises(Unsupported):
        parse_origin(origin, (HOST,))


@pytest.mark.parametrize("branch, expected", [("topic", "success"), ("other", "missing")])
def test_push_ci_requires_publication_branch(forge, branch, expected):
    forge.data["runs"][0][0].update(pull_requests=[], event="push", head_branch=branch)
    assert forge.ci().state == expected


def test_pr_event_requires_explicit_pr_association(forge):
    forge.data["runs"][0][0]["pull_requests"] = []
    with pytest.raises(Mismatch):
        forge.ci()


def test_same_attempt_duplicates_cannot_hide_failure(forge):
    forge.data["checks"] = [[check(forge.head, id=30, conclusion="failure"), check(forge.head)]]
    forge.data["runs"][0][0]["run_attempt"] = 2
    forge.data["jobs"] = [
        [
            {
                "run_id": 51,
                "run_attempt": 2,
                "head_sha": forge.head,
                "check_run_url": f"https://{HOST}/api/v3/{API}/check-runs/{check_id}",
            }
            for check_id in (30, 31)
        ]
    ]
    assert forge.ci().state == "failed"


@pytest.mark.parametrize("operation", ["create", "merge"])
def test_timeout_after_write_is_reconciled_without_repeating_write(forge, operation):
    forge.data[f"{operation}_timeout"] = True
    forge.save()
    forge.client.timeout = 0.5
    body = forge.body_file()
    with pytest.raises(Uncertain):
        if operation == "create":
            forge.client.create_pull_request(forge.repo, "topic", forge.head, "fix: body", body)
        else:
            forge.client.squash_merge(forge.repo, 17, forge.head, "fix: body (#17)", body)
    if operation == "create":
        (found,) = forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)
        assert found.body.encode() == body.read_bytes()
    else:
        assert forge.client.observe_merge(forge.repo, 17, forge.head).merge_sha == SQUASH_SHA


def test_cancelled_default_branch_lookup_terminates_process_group(forge):
    executable = forge.target.parent / "bin" / "git"
    marker = forge.target.parent / "pid"
    executable.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(10)\n"
    )
    executable.chmod(0o700)
    program = """
import signal
from taskwatch.job_workspace import default_branch
class Cancelled(Exception):
    pass
def cancel(signum, frame):
    raise Cancelled()
signal.signal(signal.SIGALRM, cancel)
signal.setitimer(signal.ITIMER_REAL, 0.5)
try:
    default_branch('.', __import__('os').environ)
except Cancelled:
    print('cancelled and reaped')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            env=forge.env,
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "cancelled and reaped"
        pid = int(marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_ruleset_only_protection_does_not_require_classic_resource(forge):
    forge.data["branch"]["protected"] = True
    forge.data["classic_rule"] = None
    forge.data["errors"] = [f"{API}/branches/main/protection"]
    forge.data["rules"] = [
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "unit", "integration_id": 7}]
                },
            }
        ]
    ]
    ci = forge.ci()
    assert ci.state == "success"
    assert next(e for e in ci.evidence if e.kind == "check").required is True
    forge.data["checks"] = [[]]
    assert forge.ci().state == "missing"


def test_classic_and_ruleset_required_contexts_are_both_enforced(forge):
    forge.data["branch"]["protected"] = True
    forge.data["protection"]["required_status_checks"]["contexts"] = ["unit"]
    forge.data["rules"] = [
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "security", "integration_id": 7}]
                },
            }
        ]
    ]
    ci = forge.ci()
    assert ci.state == "missing"
    assert next(e for e in ci.evidence if e.kind == "check").required is True
    assert next(e for e in ci.evidence if e.kind == "missing").name == "security"
    forge.data["checks"][0].append(check(forge.head, id=32, name="security"))
    assert forge.ci().state == "success"


@pytest.mark.parametrize(
    "response",
    [
        {
            "errors": [{"message": "Permission denied"}],
            "data": {
                "repository": {
                    "nameWithOwner": "sample/project",
                    "ref": {
                        "name": "main",
                        "branchProtectionRule": None,
                    },
                },
            },
        },
        {"data": {"repository": None}},
        {"data": {"repository": {"nameWithOwner": "sample/project", "ref": None}}},
        {"data": {"repository": {"nameWithOwner": "sample/project", "ref": {"name": "main"}}}},
        {
            "data": {
                "repository": {
                    "nameWithOwner": "sample/project",
                    "ref": {
                        "name": "main",
                        "branchProtectionRule": {},
                    },
                }
            }
        },
    ],
)
def test_missing_or_unauthorized_classic_observation_is_not_absence(forge, response):
    forge.data["branch"]["protected"] = True
    forge.data["graphql"] = response
    with pytest.raises(ForgeError):
        forge.ci()


@pytest.mark.parametrize("field", ["repository", "branch"])
def test_classic_observation_must_match_repository_and_branch(forge, field):
    repository = {
        "nameWithOwner": "sample/project",
        "ref": {
            "name": "main",
            "branchProtectionRule": None,
        },
    }
    if field == "repository":
        repository["nameWithOwner"] = "other/project"
    else:
        repository["ref"]["name"] = "other"
    forge.data["branch"]["protected"] = True
    forge.data["graphql"] = {"data": {"repository": repository}}
    with pytest.raises(Mismatch):
        forge.ci()


def test_classic_query_transport_failure_is_not_absence(forge):
    forge.data["branch"]["protected"] = True
    forge.data["errors"] = ["graphql"]
    with pytest.raises(ForgeError):
        forge.ci()


@pytest.mark.parametrize(
    "origin",
    [
        f"https://{HOST}/Sample/Project.git",
        f"git@{HOST}:Sample/Project.git",
    ],
)
def test_canonical_identity_survives_origin_case_and_attempt_check_urls(forge, origin):
    git(forge.target, "remote", "set-url", "origin", origin)
    forge.repo = forge.client.inspect_repository()
    assert forge.repo.identity == IDENTITY
    (pr,) = forge.client.find_pull_requests(forge.repo, "topic", "main", forge.head)
    assert pr.url == f"https://{HOST}/sample/project/pull/17"
    forge.data["checks"] = [[check(forge.head, id=30, conclusion="failure"), check(forge.head)]]
    forge.data["runs"] = [[run(forge.head, attempt=2)]]
    forge.data["jobs"] = [
        [
            {
                "run_id": 51,
                "run_attempt": attempt,
                "head_sha": forge.head,
                "check_run_url": f"https://{HOST}/api/v3/{API}/check-runs/{check_id}",
            }
            for check_id, attempt in [(30, 1), (31, 2)]
        ]
    ]
    assert forge.ci().state == "success"


def test_ci_excludes_only_optional_app_slug_not_check_name(forge):
    bot = check(forge.head, name="lint", conclusion="failure")
    bot["app"]["slug"] = "review-app"
    forge.data["checks"] = [[bot, check(forge.head, id=32, name="review-app")]]
    forge.save()
    observed = forge.client.inspect_ci(forge.repo, 17, forge.head, exclude_apps=("review-app",))
    assert observed.state == "success"
    assert [e.name for e in observed.evidence if e.kind == "check"] == ["review-app"]
    forge.data["rules"] = [
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "lint", "integration_id": 7}]
                },
            }
        ]
    ]
    forge.save()
    observed = forge.client.inspect_ci(forge.repo, 17, forge.head, exclude_apps=("review-app",))
    assert observed.state == "failed"
    assert any(e.name == "lint" and e.required for e in observed.evidence)


def test_bot_runs_include_all_pages_and_attempts_with_absent_apps(forge):
    forge.data["checks"] = [
        [check(forge.head, status="in_progress", conclusion=None)],
        [check(forge.head, id=32, conclusion="failure")],
    ]
    forge.save()
    assert forge.client.inspect_bot_runs(forge.repo, forge.head, ("ci-app", "absent")) == {
        "ci-app": [
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "failure"},
        ],
        "absent": [],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", OTHER_SHA),
        ("status", "unknown"),
        ("conclusion", "unknown"),
        ("app", {}),
    ],
)
def test_bot_runs_reject_malformed_or_wrong_head_checks(forge, field, value):
    forge.data["checks"][0][0][field] = value
    forge.save()
    with pytest.raises(ForgeError):
        forge.client.inspect_bot_runs(forge.repo, forge.head, ("ci-app",))


def thread(head, index=1, *, more=False):
    return {
        "data": {
            "repository": {
                "nameWithOwner": "sample/project",
                "pullRequest": {
                    "number": 17,
                    "headRefOid": head,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": more, "endCursor": str(index)},
                        "nodes": [
                            {
                                "id": f"THREAD_{index}",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [
                                        {
                                            "databaseId": index,
                                            "url": f"https://{HOST}/discussion/{index}",
                                            "path": "src.py",
                                            "line": 3,
                                            "body": "Review finding",
                                            "author": {
                                                "__typename": "Bot",
                                                "login": "review-app[bot]",
                                            },
                                        },
                                        {"databaseId": 999, "body": "Reply is not a root"},
                                    ],
                                },
                            }
                        ],
                    },
                },
            }
        }
    }


def test_review_threads_paginate_and_preserve_root_bot_identity(forge):
    forge.data["thread_pages"] = [thread(forge.head, more=True), thread(forge.head, 2)]
    forge.save()
    assert forge.client.review_threads(forge.repo, 17, forge.head) == [
        {
            "id": f"THREAD_{index}",
            "root_id": index,
            "url": f"https://{HOST}/discussion/{index}",
            "path": "src.py",
            "line": 3,
            "author_type": "Bot",
            "author": "review-app[bot]",
            "body": "Review finding",
            "resolved": False,
            "outdated": False,
        }
        for index in (1, 2)
    ]


@pytest.mark.parametrize("failure", ["head", "repository", "comments", "cursor", "errors"])
def test_review_threads_reject_incomplete_or_mismatched_observations(forge, failure):
    page = thread(forge.head)
    repository = page["data"]["repository"]
    threads = repository["pullRequest"]["reviewThreads"]
    if failure == "head":
        repository["pullRequest"]["headRefOid"] = OTHER_SHA
    elif failure == "repository":
        repository["nameWithOwner"] = "other/project"
    elif failure == "comments":
        threads["nodes"][0]["comments"]["pageInfo"]["hasNextPage"] = True
    elif failure == "cursor":
        threads["pageInfo"] = {"hasNextPage": True, "endCursor": "0"}
    else:
        page["errors"] = [{"message": "unavailable"}]
    forge.data["thread_pages"] = [page]
    forge.save()
    with pytest.raises(ForgeError):
        forge.client.review_threads(forge.repo, 17, forge.head)

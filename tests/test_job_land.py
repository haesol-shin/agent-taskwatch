"""Landing an accepted tree through the real runner and scanner, with an offline forge.

`gh` is an executable that speaks the GitHub REST subset the adapter uses and applies its writes
to a throwaway bare repository. `git` is the real binary behind a shim that routes the synthetic
GitHub URL to that bare repository, so nothing leaves the temporary folder. Each land step runs
as a child process of the scanner; a crash is a real SIGKILL of that process.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main as cli_main
from taskwatch.job_runner import run_job
from taskwatch.job_scan import scan_jobs
from taskwatch.jobs import Jobs, JobState, load_job_config
from tests.git_fixture import git, make_repo
from tests.test_job_delivery import Sender
from tests.test_job_runner import FAKE, GOOD_FILES, RECORDS, Env
from tests.test_public_text import code_payload
from tests.verdicts import CODE_PASS

ORIGIN = "https://github.com/sample/project.git"
FAKE_GH = r"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = "sample/project"
state_file = Path(os.environ["FORGE_STATE"])
state = json.loads(state_file.read_text(encoding="utf-8"))
args = sys.argv[1:]
with Path(os.environ["FORGE_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")


def save():
    state_file.write_text(json.dumps(state), encoding="utf-8")


def fail():
    sys.exit(73)


def kill_caller():
    os.kill(os.getppid(), 9)
    os._exit(1)


def take(name):
    left = state.get(name, 0)
    if left:
        state[name] = left - 1
        save()
    return bool(left)


def option(name):
    if args.count(name) != 1:
        fail()
    return args[args.index(name) + 1]


def emit(value):
    print(json.dumps(value))


def pages(value):
    for page in value if "--paginate" in args else value[:1]:
        emit(page)


def git(*argv, data=None):
    command = [state["real_git"], "--git-dir", state["bare"], "-c", "user.name=Forge",
               "-c", "user.email=forge@example.invalid", *argv]
    done = subprocess.run(command, input=data, capture_output=True, text=True)
    if done.returncode:
        fail()
    return done.stdout.strip()


def ref(name):
    command = [state["real_git"], "--git-dir", state["bare"], "rev-parse", "--verify", "--quiet",
               name]
    done = subprocess.run(command, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def external_commit():
    parent = ref("refs/heads/main")
    new = git("commit-tree", parent + "^{tree}", "-p", parent, data="external change\n")
    git("update-ref", "refs/heads/main", new, parent)


def tree_with_extra(tree):
    blob = git("hash-object", "-w", "--stdin", data="external\n")
    lines = git("ls-tree", tree).splitlines()
    lines.append(f"100644 blob {blob}\texternal.txt")
    lines.sort(key=lambda line: line.split("\t", 1)[1])
    return git("mktree", data="\n".join(lines) + "\n")


def row(pr):
    number = pr["number"]
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": pr["state"],
        "merged": pr["merged"],
        "head": {"sha": pr["head_sha"], "ref": pr["head_ref"], "repo": {"full_name": REPO}},
        "base": {"sha": pr["base_sha"], "ref": "main", "repo": {"full_name": REPO}},
        "title": pr["title"],
        "body": pr["body"],
        "merge_commit_sha": pr["merge_sha"],
        "mergeable_state": state.get("mergeable", "clean"),
        "auto_merge": None,
    }


def snapshot():
    snap = json.loads(json.dumps(state["ci"][state.get("ci_index", 0)]))
    for run in snap["runs"]:
        if str(run["id"]) in state.get("rerun_done", []):
            run.update(state.get("rerun_to", {}).get(str(run["id"]), {}))
    return snap


def fill(value, sha):
    number = next((p["number"] for p in state["pulls"].values() if p["head_sha"] == sha), 1)
    text = json.dumps(value).replace('"@H@"', json.dumps(sha)).replace('"@N@"', str(number))
    return json.loads(text)


def listing_of_runs(sha):
    calls = state.get("ci_calls", 0)
    state["ci_index"] = min(calls, len(state["ci"]) - 1)
    state["ci_calls"] = calls + 1
    save()
    if take("advance_main_on_ci"):
        external_commit()
    if take("edit_title_on_ci"):
        for pr in state["pulls"].values():
            pr["title"] += "!"
        save()
    pages([{"workflow_runs": fill(snapshot()["runs"], sha)}])


def api():
    endpoint = args[args.index("--method") + 2]
    if option("--hostname") != "github.com" or any(p in endpoint for p in state.get("errors", [])):
        fail()
    if endpoint == "graphql":
        ref_data = {"name": "main", "branchProtectionRule": None}
        emit({"data": {"repository": {"nameWithOwner": REPO, "ref": ref_data}}})
        return
    parsed = urlsplit(endpoint)
    query, path, base = parse_qs(parsed.query), parsed.path, "repos/sample/project"
    if path == base:
        emit({"full_name": REPO, "default_branch": "main", "allow_squash_merge": state["squash"]})
    elif path == base + "/rules/branches/main":
        pages(state["rules"])
    elif path == base + "/branches/main":
        emit({"name": "main", "protected": False})
    elif path == base + "/pulls":
        if query.get("state") != ["all"] or query.get("base") != ["main"]:
            fail()
        head = query["head"][0]
        pages([[row(p) for p in state["pulls"].values() if "sample:" + p["head_ref"] == head]])
    elif path.startswith(base + "/pulls/"):
        emit(row(state["pulls"][path.rsplit("/", 1)[1]]))
    elif path.startswith(base + "/commits/") and path.endswith("/check-runs"):
        pages([{"check_runs": fill(snapshot()["checks"], path.split("/")[4])}])
    elif path.startswith(base + "/commits/") and path.endswith("/statuses"):
        pages([fill(snapshot()["statuses"], path.split("/")[4])])
    elif path == base + "/actions/runs":
        listing_of_runs(query["head_sha"][0])
    elif path.endswith("/jobs"):
        pages([{"jobs": []}])
    elif path.startswith(base + "/actions/runs/"):
        sha = next(iter(state["pulls"].values()))["head_sha"]
        run_id = path.rsplit("/", 1)[1]
        emit(next(r for r in fill(snapshot()["runs"], sha) if str(r["id"]) == run_id))
    else:
        fail()


def create():
    if option("--base") != "main":
        fail()
    body = Path(option("--body-file")).read_bytes().decode("utf-8")
    state.setdefault("created", []).append({"head": option("--head"), "title": option("--title")})
    save()
    if take("create_refused"):
        fail()
    if take("create_kill_before"):
        kill_caller()
    sha = ref("refs/heads/" + option("--head"))
    if not sha:
        fail()
    copies = 2 if take("create_duplicate") else 1
    for _ in range(copies):
        number = state["next"]
        state["next"] = number + 1
        edited = body + "edited\n" if take("create_edits_body") else body
        state["pulls"][str(number)] = {
            "number": number, "head_ref": option("--head"), "head_sha": sha,
            "base_sha": ref("refs/heads/main"), "title": option("--title"), "body": edited,
            "state": "open", "merged": False, "merge_sha": None,
        }
    save()
    if take("create_kill_after"):
        kill_caller()
    if take("create_lost_ack"):
        fail()
    print(f"https://github.com/{REPO}/pull/{number}")


def merge():
    pr = state["pulls"][args[2]]
    banned = {"--admin", "--auto", "--merge-queue", "--delete-branch"}
    if "--squash" not in args or option("--match-head-commit") != pr["head_sha"]:
        fail()
    if banned & set(args):
        fail()
    body = Path(option("--body-file")).read_bytes().decode("utf-8")
    call = {"number": int(args[2]), "subject": option("--subject"), "body": body}
    state.setdefault("merge_calls", []).append(call)
    save()
    if take("merge_refused") or pr["state"] != "open":
        fail()
    if take("merge_queued"):
        return
    if take("advance_main_at_merge"):
        external_commit()
    tree = git("rev-parse", pr["head_sha"] + "^{tree}")
    if take("merge_extra_file"):
        tree = tree_with_extra(tree)
    parent = ref("refs/heads/main")
    squash = git("commit-tree", tree, "-p", parent, data=call["subject"] + "\n\n" + body)
    git("update-ref", "refs/heads/main", squash, parent)
    pr.update(state="closed", merged=True, merge_sha=squash)
    save()
    if take("merge_kill_after"):
        kill_caller()
    if take("merge_lost_ack"):
        fail()


def rerun():
    if "--failed" not in args:
        fail()
    state.setdefault("reruns", []).append(int(args[2]))
    save()
    if take("rerun_refused"):
        fail()
    state.setdefault("rerun_done", []).append(args[2])
    save()
    if take("rerun_lost_ack"):
        fail()


if args[:2] == ["auth", "status"]:
    sys.exit(0 if option("--hostname") == "github.com" else 73)
if args[:1] == ["api"]:
    api()
    sys.exit(0)
if option("--repo") != "github.com/sample/project":
    fail()
if args[:2] == ["pr", "create"]:
    create()
elif args[:2] == ["pr", "merge"]:
    merge()
elif args[:2] == ["run", "rerun"]:
    rerun()
else:
    fail()
"""
SHIM = r"""
import os
import subprocess
import sys
from pathlib import Path

real, bare = os.environ["REAL_GIT"], os.environ["REMOTE_BARE"]
args = sys.argv[1:]
index = 0
while index < len(args) and args[index].startswith("-"):
    index += 2 if args[index] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
command = args[index] if index < len(args) else ""
prefix = []
if command in {"fetch", "push", "ls-remote"}:
    prefix = ["-c", f"url.{bare}.insteadOf=https://github.com/sample/project.git"]
trap = Path(__file__).with_name("kill-once")
when = trap.read_text(encoding="utf-8").strip() if trap.exists() else ""
if when in (f"before:{command}", f"after:{command}"):
    trap.unlink()
    if when.startswith("before:"):
        os.kill(os.getppid(), 9)
        os._exit(1)
    code = subprocess.run([real, *prefix, *args]).returncode
    os.kill(os.getppid(), 9)
    os._exit(code)
os.execv(real, [real, *prefix, *args])
"""
HOOK = r"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
calls = here / "calls.jsonl"
index = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "tty": os.isatty(0)}
with calls.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")
modes = json.loads((here / "modes.json").read_text(encoding="utf-8"))
mode = modes[min(index, len(modes) - 1)]
if mode == "fail":
    sys.exit(3)
if mode == "crash":
    os.kill(os.getppid(), signal.SIGKILL)
    os._exit(1)
if mode == "orphan":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=False,
        start_new_session=True,
    )
    (here / "orphan.pid").write_text(str(child.pid), encoding="utf-8")
if mode == "mutate":
    path = Path((here / "mutate.txt").read_text(encoding="utf-8"))
    path.write_text(path.read_text(encoding="utf-8") + "changed after install\n", encoding="utf-8")
"""
RUN_STEP = (
    "import sys; from taskwatch import job_scan; job_scan.Systemd.wake = lambda self: None; "
    "from taskwatch.job_cli import main; raise SystemExit(main(sys.argv[1:]))"
)
WRITES = (["pr", "create"], ["pr", "merge"], ["run", "rerun"])
STOPPED = {"user_wait", "done", "cancelled", "approval_wait"}


def run_row(id=51, attempt=1, status="completed", conclusion="success", head="@H@"):
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
        "pull_requests": [{"number": "@N@"}],
        "event": "pull_request",
        "head_branch": "topic",
    }


def check_row(name="unit", conclusion="success", id=31):
    return {
        "id": id,
        "name": name,
        "head_sha": "@H@",
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": 7},
        "check_suite": {"id": 41},
    }


def ci(runs=(), checks=(), statuses=()):
    return {"runs": list(runs), "checks": list(checks), "statuses": list(statuses)}


GREEN = [ci([run_row()])]
PENDING = [ci([run_row(status="in_progress", conclusion=None)])]
PARAMETERS = {"required_status_checks": [{"context": "unit"}]}
REQUIRED_UNIT = [[{"type": "required_status_checks", "parameters": PARAMETERS}]]


def wait_for(check, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition did not become true")


class StepService:
    """The scanner's service seam: each launched step is a real child process."""

    def __init__(self, root: Path, logs: Path) -> None:
        self.root = root
        self.logs = logs
        logs.mkdir(exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.started: list[str] = []

    def status(self, unit: str) -> str:
        proc = self.procs.get(unit)
        if proc is None:
            return "missing"
        return "active" if proc.poll() is None else "inactive"

    def start(self, job: JobState) -> None:
        argv = [sys.executable, "-c", RUN_STEP, "--root", str(self.root)]
        argv += ["run", job.id, "--attempt", str(job.attempt)]
        with (self.logs / f"{job.unit}.log").open("wb") as log:
            self.procs[job.unit] = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.started.append(job.id)

    def stop(self, unit: str) -> bool:
        self.kill(unit)
        return True

    def kill(self, unit: str | None = None) -> None:
        for name, proc in self.procs.items():
            if (unit is None or name == unit) and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def wait(self, seconds: float = 180) -> None:
        for proc in self.procs.values():
            proc.wait(timeout=seconds)

    def text(self) -> str:
        return "\n".join(path.read_text(errors="replace") for path in sorted(self.logs.iterdir()))


class Lab(Env):
    """A code job with a reviewed publication unit, a bare remote, a fake forge and a hook."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        share: "Lab | None" = None,
        receipt: str = "k1",
        alias: bool = False,
        land: bool = True,
        window: int = 2,
        install: tuple[str, ...] | None = ("install", "{sha}", "{target}"),
        public_base: str = "main",
        sender_modes: tuple[str, ...] | None = None,
        hook_modes: tuple[str, ...] | None = None,
        **extra,
    ) -> None:
        tmp_path.mkdir(exist_ok=True)
        self.base = tmp_path
        self.root = tmp_path / "root"
        self.window = window
        self.receipt = receipt
        if share is None:
            self.world(tmp_path, monkeypatch)
        else:
            for name in ("repo", "bare", "bin", "hook_dir", "sender", "forge_file", "forge_log"):
                setattr(self, name, getattr(share, name))
        self.install_argv = (
            [sys.executable, str(self.hook_dir / "hook.py"), *install] if install else None
        )
        if sender_modes:
            self.sender.modes(*sender_modes)
        if hook_modes or share is None:
            self.set_hook_modes(*(hook_modes or ("ok",)))
        self.configure()
        self.service = StepService(self.root, tmp_path / "unit-logs")
        target = self.repo
        if alias:
            target = tmp_path / f"alias-{receipt}"
            git(self.repo, "worktree", "add", "-b", f"alias-{receipt}", str(target))
        super().__init__(
            tmp_path,
            monkeypatch,
            review="code",
            target=target,
            public_text=[{"id": "pr", "base": public_base}],
            receipt_key=receipt,
            **({"land": True} if land else {}),
            **extra,
        )
        self.tree = self.folder() / "worktree"

    def world(self, tmp_path: Path, monkeypatch) -> None:
        real = shutil.which("git")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in (("gh", FAKE_GH), ("git", SHIM)):
            (self.bin / name).write_text(f"#!{sys.executable}\n{text}", encoding="utf-8")
            (self.bin / name).chmod(0o700)
        self.bare = tmp_path / "remote.git"
        self.forge_file = tmp_path / "forge.json"
        self.forge_log = tmp_path / "forge.log"
        for key, value in {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "REAL_GIT": real,
            "REMOTE_BARE": str(self.bare),
            "FORGE_STATE": str(self.forge_file),
            "FORGE_LOG": str(self.forge_log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.invalid",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.invalid",
        }.items():
            monkeypatch.setenv(key, value)
        self.repo = make_repo(tmp_path, "repo")
        subprocess.run(
            [real, "clone", "--quiet", "--bare", str(self.repo), str(self.bare)], check=True
        )
        git(self.repo, "remote", "add", "origin", ORIGIN)
        git(self.repo, "fetch", "--quiet", "origin")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.hook_dir = tmp_path / "hook"
        self.hook_dir.mkdir()
        (self.hook_dir / "hook.py").write_text(HOOK, encoding="utf-8")
        self.sender = Sender(tmp_path / "sender")
        self.sender.modes("success")
        self.forge_file.write_text(
            json.dumps(
                {
                    "real_git": real,
                    "bare": str(self.bare),
                    "squash": True,
                    "rules": [[]],
                    "next": 17,
                    "pulls": {},
                    "ci": GREEN,
                }
            ),
            encoding="utf-8",
        )

    # --- configuration and fixtures ---------------------------------------------------------

    def configure(self, **changes) -> None:
        data = {
            "version": 1,
            "slots": 2,
            "stall_seconds": 120,
            "omp_command": [sys.executable, str(FAKE)],
            "rpc_timeout": self.window,
            "deliver_command": list(self.sender.command),
            "install_command": self.install_argv,
            **changes,
        }
        path = self.root / "taskwatch" / "jobs-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({key: value for key, value in data.items() if value is not None}),
            encoding="utf-8",
        )

    def set_hook_modes(self, *modes: str) -> None:
        (self.hook_dir / "modes.json").write_text(json.dumps(list(modes)), encoding="utf-8")

    def forge(self) -> dict:
        return json.loads(self.forge_file.read_text(encoding="utf-8"))

    def edit(self, **changes) -> None:
        self.forge_file.write_text(json.dumps({**self.forge(), **changes}), encoding="utf-8")

    def forge_calls(self) -> list[list[str]]:
        if not self.forge_log.exists():
            return []
        return [json.loads(x) for x in self.forge_log.read_text(encoding="utf-8").splitlines()]

    def writes(self) -> list[str]:
        return [" ".join(call[:2]) for call in self.forge_calls() if call[:2] in WRITES]

    def hook_calls(self) -> list[dict]:
        path = self.hook_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

    def cards(self) -> list[dict]:
        return [c for c in self.sender.calls() if not c["text"].startswith("Job ")]

    def trap(self, when: str) -> None:
        (self.bin / "kill-once").write_text(when, encoding="utf-8")

    def advance_remote(self) -> str:
        parent = git(self.bare, "rev-parse", "refs/heads/main")
        new = git(self.bare, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "external")
        git(self.bare, "update-ref", "refs/heads/main", new)
        return new

    def land(self) -> dict:
        return self.get().to_json()["land_state"]

    def remote_land_branch(self) -> str:
        branch = f"refs/heads/{self.get().branch}-land"
        return git(self.bare, "rev-parse", "--verify", "--quiet", branch)

    # --- accepted result --------------------------------------------------------------------

    def write_text(self, name: str, text: str) -> None:
        (self.folder() / "pr" / "pr" / name).write_text(text, encoding="utf-8")

    def accept(self) -> None:
        """Build one commit, pass the check and the review; a landing job is then in `land`."""
        assert self.run("check") == 0  # creates the work folder; nothing is built yet
        for name, text in GOOD_FILES.items():
            if name in RECORDS:
                (self.folder() / name).write_text(text, encoding="utf-8")
        (self.folder() / "pr" / "pr").mkdir(parents=True, exist_ok=True)
        self.write_text("title.txt", "feat(x): add\n")
        self.write_text("body.md", "body\n")
        self.write_text("commit.txt", "- Preserve the complete result\n")
        (self.tree / "out.txt").write_text("ok\n", encoding="utf-8")
        (self.tree / f"{self.receipt}.txt").write_text("one\n", encoding="utf-8")
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-m", "feat(x): add\n\nWhy it is needed.")
        self.scenario([{"reply": f"{CODE_PASS}\nNo findings."}])
        assert self.run("check") == 0
        assert list(self.get().checks[-1]["problems"]) == []
        assert self.run("review") == 0
        assert self.get().stage == ("land" if self.get().land else "deliver_pending")

    def reviewed_text(self) -> dict[str, str]:
        out = self.base / f"reviewed-{self.receipt}"
        assert cli_main(["--root", str(self.root), "text", self.job.id, "--out", str(out)]) == 0
        names = ("body.md", "commit.txt")
        return {name: (out / name).read_text(encoding="utf-8") for name in names}

    # --- scanner ----------------------------------------------------------------------------

    def scan_once(self) -> list[str]:
        config = load_job_config(self.root)
        return scan_jobs(self.jobs, config, now=time.time(), boot_id="boot-1", service=self.service)

    def drive(self, done, rounds: int = 12) -> JobState:
        job = self.get()
        for _ in range(rounds):
            self.scan_once()
            self.service.wait()
            job = self.get()
            if done(job):
                return job
        raise AssertionError(
            f"not settled: {job.stage} {job.wait_reason} {job.error}\n{self.service.text()}"
        )

    def finish(self) -> JobState:
        job = self.drive(lambda j: j.stage in STOPPED)
        assert job.stage == "done", f"{job.wait_reason}: {job.error}\n{self.service.text()}"
        return job

    def park(self, reason: str) -> JobState:
        job = self.drive(lambda j: j.stage in STOPPED)
        assert (job.stage, job.wait_reason) == ("user_wait", reason), job.error
        return job

    def resume(self, reason: str = "owner") -> None:
        args = ["--root", str(self.root), "resume", self.job.id, "--reason", reason]
        assert cli_main(args) == 0

    def close(self) -> None:
        self.service.kill()
        pid_file = self.hook_dir / "orphan.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def make(tmp_path, monkeypatch):
    labs: list[Lab] = []

    def build(**kwargs) -> Lab:
        where = kwargs.pop("where", tmp_path)
        labs.append(Lab(where, monkeypatch, **kwargs))
        return labs[-1]

    yield build
    for lab in labs:
        lab.close()


def cleaned_forge(lab: Lab) -> None:
    """Nothing was published: no forge write, no landing branch on the remote."""
    assert lab.writes() == []
    assert git(lab.bare, "branch", "--list", "*-land") == ""


# --- the full path ---------------------------------------------------------------------------


def test_accepted_result_becomes_one_head_pr_squash_install_and_card(make):
    lab = make()
    lab.accept()
    reviewed = lab.reviewed_text()
    accepted = lab.get()
    assert accepted.stage == "land" and accepted.land is True
    result = git(lab.tree, "rev-parse", "HEAD")
    tree = git(lab.tree, "rev-parse", "HEAD^{tree}")
    base = git(lab.repo, "rev-parse", "main")

    lab.finish()
    job, state = lab.get(), lab.land()
    squash = git(lab.bare, "rev-parse", "refs/heads/main")
    head = state["H"]
    assert (state["R"], state["T"], state["B"], state["S"]) == (result, tree, base, squash)
    assert head not in (result, squash) and state["M"] == base
    assert git(lab.bare, "rev-list", "--parents", "-n", "1", head).split() == [head, base]
    assert git(lab.bare, "rev-parse", f"{head}^{{tree}}") == tree
    assert git(lab.bare, "log", "-1", "--format=%B", head) == (
        "feat(x): add\n\n- Preserve the complete result"
    )
    assert lab.remote_land_branch() == head and state["branch"] == f"{job.branch}-land"
    assert git(lab.bare, "rev-list", "--parents", "-n", "1", squash).split() == [squash, base]
    assert git(lab.bare, "rev-parse", f"{squash}^{{tree}}") == tree
    assert git(lab.bare, "log", "-1", "--format=%B", squash) == (
        "feat(x): add (#17)\n\n- Preserve the complete result"
    )
    assert state["pr"] == {"number": 17, "url": "https://github.com/sample/project/pull/17"}
    assert state["tree_verified"] is True and state["install"]["status"] == "succeeded"

    forge = lab.forge()
    assert list(forge["pulls"]) == ["17"] and len(forge["created"]) == 1
    assert forge["pulls"]["17"]["body"] == reviewed["body.md"]
    assert forge["pulls"]["17"]["title"] == "feat(x): add"
    assert forge["merge_calls"] == [
        {"number": 17, "subject": "feat(x): add (#17)", "body": reviewed["commit.txt"]}
    ]
    assert lab.writes() == ["pr create", "pr merge"]
    assert "--admin" not in json.dumps(lab.forge_calls())
    hook = lab.hook_calls()
    target = str(lab.repo.resolve())
    assert [(c["argv"], c["tty"]) for c in hook] == [(["install", squash, target], False)]
    assert hook[0]["cwd"] == target
    assert len(lab.cards()) == 1 and [d["status"] for d in job.deliveries] == ["success"]


def test_a_landed_job_is_not_landed_or_sent_again(make):
    lab = make()
    lab.accept()
    lab.finish()
    for _ in range(3):
        lab.scan_once()
    assert lab.writes() == ["pr create", "pr merge"]
    assert len(lab.hook_calls()) == 1 and len(lab.cards()) == 1


def test_a_nonlanding_job_needs_neither_forge_nor_hook(make):
    lab = make(land=False)
    lab.accept()
    job = lab.get()
    assert job.land is False and dict(job.land_state) == {} and job.stage == "deliver_pending"
    before = git(lab.bare, "rev-parse", "main")
    lab.finish()
    assert lab.forge_calls() == [] and lab.hook_calls() == []
    assert git(lab.bare, "rev-parse", "main") == before and len(lab.cards()) == 1


def test_no_install_command_lands_and_delivers_without_a_hook(make):
    lab = make(install=None)
    lab.accept()
    lab.finish()
    assert lab.hook_calls() == [] and lab.land()["install"]["status"] == "disabled"
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.cards()) == 1


def test_pinned_source_url_expands_the_observed_squash(make):
    lab = make(install=("git+https://github.com/sample/project.git@{sha}",))
    lab.accept()
    lab.finish()
    squash = git(lab.bare, "rev-parse", "main")
    assert [c["argv"] for c in lab.hook_calls()] == [
        [f"git+https://github.com/sample/project.git@{squash}"]
    ]


def test_a_pinned_source_of_another_repository_never_runs(make):
    lab = make(install=("git+https://github.com/other/project.git@{sha}",))
    lab.accept()
    job = lab.drive(lambda j: j.stage in STOPPED)
    assert job.stage == "user_wait" and job.wait_reason.startswith("land_")
    assert lab.hook_calls() == [] and lab.cards() == []


# --- submission boundaries -------------------------------------------------------------------


def test_land_is_false_unless_the_submission_says_so(tmp_path):
    job = Jobs(tmp_path / "root").submit(code_payload(tmp_path, [{"id": "pr", "base": "main"}]), 10)
    assert job.land is False and dict(job.land_state) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"review": "source"},
        {"review": "design"},
        {"public_text": None},
        {"approval_source": ""},
        {"approval_scope": "  "},
        {"land": "yes"},
    ],
)
def test_land_needs_a_code_job_one_unit_and_approval_records(tmp_path, change):
    data = {**code_payload(tmp_path, [{"id": "pr", "base": "main"}]), "land": True, **change}
    data = {key: value for key, value in data.items() if value is not None}
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError):
        jobs.submit(data, 10)
    assert jobs.load() == {}


def test_land_refuses_a_review_only_job(tmp_path):
    data = code_payload(tmp_path, [{"id": "pr", "base": "main"}])
    keys = ("approval_source", "approval_scope", "prompt", "target", "intent", "spec")
    data = {key: data[key] for key in keys}
    data |= {"review": "source", "review_target": {"commit": "HEAD"}}
    jobs = Jobs(tmp_path / "root")
    jobs.submit({**data, "receipt_key": "control"}, 10)  # the same shape is valid without land
    with pytest.raises(ValueError):
        jobs.submit({**data, "receipt_key": "landing", "land": True}, 10)
    assert [job.receipt_key for job in jobs.load().values()] == ["control"]


def test_land_refuses_two_publication_units(tmp_path):
    units = [{"id": "a", "base": "main"}, {"id": "b", "base": "main"}]
    data = {**code_payload(tmp_path, units), "land": True}
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError):
        jobs.submit(data, 10)
    assert jobs.load() == {}


# --- L1: accepted identity -------------------------------------------------------------------


@pytest.mark.parametrize("what", ["text", "code"])
def test_changed_result_before_publication_returns_to_the_check(make, what):
    lab = make()
    lab.accept()
    if what == "text":
        lab.write_text("body.md", "a different claim\n")
    else:
        (lab.tree / "extra.txt").write_text("more\n", encoding="utf-8")
        git(lab.tree, "add", "extra.txt")
        git(lab.tree, "commit", "-m", "fix(x): more\n\nA reason.")
    job = lab.drive(lambda j: j.stage != "land")
    assert job.stage == "check" and lab.cards() == []
    cleaned_forge(lab)


def test_review_without_a_checked_publication_marker_is_not_landable(make):
    lab = make()
    lab.accept()
    with lab.jobs.state.lock():
        job = lab.get()
        review = {**job.reviews[-1], "texts": {}}
        lab.jobs.save(replace(job, reviews=(*job.reviews[:-1], review)), time.time())
    job = lab.drive(lambda j: j.stage != "land")
    assert job.stage in {"check", "user_wait"} and lab.cards() == []
    cleaned_forge(lab)


def test_changed_text_after_publication_parks_instead_of_requeueing(make):
    lab = make()
    lab.accept()
    lab.edit(ci=PENDING)
    lab.park("land_ci_failed")
    assert lab.writes() == ["pr create"]
    lab.write_text("commit.txt", "- Reject unreviewed publication\n")
    lab.edit(ci=GREEN)
    lab.resume()
    job = lab.park("land_review_stale")
    assert job.stage == "user_wait" and lab.writes() == ["pr create"]
    assert lab.cards() == [] and lab.hook_calls() == []


# --- L2: base --------------------------------------------------------------------------------


def test_moved_default_branch_before_publication_parks(make):
    lab = make()
    lab.accept()
    lab.advance_remote()
    lab.park("land_base_moved")
    cleaned_forge(lab)


def test_default_branch_moving_during_ci_parks_before_merge(make):
    lab = make()
    lab.accept()
    lab.edit(advance_main_on_ci=1)
    lab.park("land_base_moved")
    assert lab.writes() == ["pr create"] and lab.hook_calls() == []


def test_foreign_remote_head_is_never_overwritten(make):
    lab = make()
    lab.accept()
    main = git(lab.bare, "rev-parse", "main")
    foreign = git(lab.bare, "commit-tree", f"{main}^{{tree}}", "-p", main, "-m", "foreign")
    git(lab.bare, "update-ref", f"refs/heads/{lab.get().branch}-land", foreign)
    lab.park("land_head_changed")
    assert lab.remote_land_branch() == foreign and lab.writes() == []


# --- L3/L4: head and PR ----------------------------------------------------------------------


@pytest.mark.parametrize("when", ["before:push", "after:push"])
def test_crash_around_the_push_reuses_the_same_head(make, when):
    lab = make()
    lab.accept()
    lab.trap(when)
    lab.finish()
    state = lab.land()
    assert lab.remote_land_branch() == state["H"]
    assert git(lab.bare, "rev-list", "--parents", "-n", "1", state["H"]).split() == [
        state["H"],
        state["B"],
    ]
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.forge()["created"]) == 1


def test_lost_create_acknowledgement_reuses_the_created_pr_after_resume(make):
    lab = make()
    lab.accept()
    lab.edit(create_lost_ack=1)
    lab.park("land_interrupted")
    head = lab.land()["H"]
    assert list(lab.forge()["pulls"]) == ["17"] and lab.cards() == []
    lab.scan_once()
    assert len(lab.forge()["created"]) == 1
    lab.resume()
    lab.finish()
    assert lab.land()["H"] == head and lab.land()["pr"]["number"] == 17
    assert len(lab.forge()["created"]) == 1 and lab.writes() == ["pr create", "pr merge"]


def test_a_missing_pr_after_a_saved_create_needs_one_resume_per_create(make):
    lab = make()
    lab.accept()
    lab.edit(create_refused=2)
    lab.park("land_interrupted")
    for _ in range(2):
        lab.scan_once()
        assert len(lab.forge()["created"]) == 1
    lab.resume("first")
    lab.park("land_interrupted")
    assert len(lab.forge()["created"]) == 2
    lab.scan_once()
    assert len(lab.forge()["created"]) == 2
    lab.resume("second")
    lab.finish()
    forge = lab.forge()
    assert len(forge["created"]) == 3 and list(forge["pulls"]) == ["17"]
    assert lab.writes().count("pr merge") == 1


def test_crash_after_creating_the_pr_reuses_it(make):
    lab = make()
    lab.accept()
    lab.edit(create_kill_after=1)
    lab.finish()
    assert len(lab.forge()["created"]) == 1 and list(lab.forge()["pulls"]) == ["17"]


def test_crash_before_the_pr_exists_needs_an_owner_resume_before_creating(make):
    lab = make()
    lab.accept()
    lab.edit(create_kill_before=1)
    lab.park("land_interrupted")
    assert lab.forge()["pulls"] == {} and len(lab.forge()["created"]) == 1
    lab.resume()
    lab.finish()
    assert len(lab.forge()["created"]) == 2 and list(lab.forge()["pulls"]) == ["17"]


@pytest.mark.parametrize("damage", ["duplicate", "closed", "body", "title"])
def test_a_reconciled_pr_must_be_the_one_reviewed_pr(make, damage):
    lab = make()
    lab.accept()
    lab.edit(ci=PENDING)
    lab.park("land_ci_failed")
    forge = lab.forge()
    pr = forge["pulls"]["17"]
    if damage == "duplicate":
        forge["pulls"]["18"] = {**pr, "number": 18}
    elif damage == "closed":
        pr["state"] = "closed"
    else:
        pr[damage] = pr[damage] + "edited\n"
    lab.edit(pulls=forge["pulls"], ci=GREEN)
    lab.resume()
    lab.park("land_pr_mismatch")
    assert lab.writes() == ["pr create"] and lab.hook_calls() == []


def test_a_pr_created_with_other_text_is_refused(make):
    lab = make()
    lab.accept()
    lab.edit(create_edits_body=1)
    lab.park("land_pr_mismatch")
    assert lab.writes() == ["pr create"]


def test_pr_text_edited_during_ci_blocks_the_merge(make):
    lab = make()
    lab.accept()
    lab.edit(edit_title_on_ci=1)
    lab.park("land_pr_mismatch")
    assert lab.writes() == ["pr create"]


# --- L5: CI ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        [ci()],
        [ci([run_row(conclusion="skipped")])],
        [ci([run_row(conclusion="cancelled")])],
        [ci([run_row()], checks=[check_row("optional", "failure")])],
        [ci([run_row()], statuses=[{"id": 5, "context": "lint", "state": "failure"}])],
        [ci(checks=[check_row()])],
        PENDING,
    ],
    ids=["empty", "skipped", "cancelled", "optional-check", "optional-status", "no-run", "pending"],
)
def test_ci_that_is_not_fully_green_never_merges(make, script):
    lab = make()
    lab.accept()
    lab.edit(ci=script)
    lab.park("land_ci_failed")
    assert lab.writes() == ["pr create"] and lab.hook_calls() == [] and lab.cards() == []


def test_ci_for_another_head_is_never_green(make):
    lab = make()
    lab.accept()
    lab.edit(ci=[ci([run_row(head="e" * 40)])])
    job = lab.drive(lambda j: j.stage in STOPPED)
    assert job.stage == "user_wait" and job.wait_reason.startswith("land_")
    assert lab.writes() == ["pr create"]


def test_required_check_and_a_successful_run_land(make):
    lab = make()
    lab.accept()
    lab.edit(rules=REQUIRED_UNIT, ci=[ci([run_row()], checks=[check_row()])])
    lab.finish()
    assert lab.writes() == ["pr create", "pr merge"]


def test_a_required_check_that_never_registered_parks(make):
    lab = make()
    lab.accept()
    lab.edit(rules=REQUIRED_UNIT, ci=GREEN)
    lab.park("land_ci_failed")
    assert lab.writes() == ["pr create"]


def test_deadline_survives_a_crash_and_a_distinct_resume_opens_a_new_window(make):
    lab = make(window=6)
    lab.accept()
    lab.edit(ci=PENDING)
    lab.scan_once()
    deadline = wait_for(lambda: lab.land().get("ci_deadline"))
    lab.service.kill()
    lab.park("land_ci_failed")
    assert lab.land()["ci_deadline"] == deadline and lab.writes() == ["pr create"]
    lab.edit(ci=GREEN)
    lab.resume()
    lab.finish()
    assert lab.land()["ci_deadline"] > deadline
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.forge()["created"]) == 1


FAILED_PAIR = [ci([run_row(51, conclusion="failure"), run_row(52, conclusion="failure")])]
FIXED = {"run_attempt": 2, "conclusion": "success"}


def test_rerun_round_is_frozen_and_a_lost_acknowledgement_spends_its_budget(make):
    lab = make(window=60)
    lab.accept()
    lab.edit(ci=FAILED_PAIR, rerun_to={"51": FIXED, "52": FIXED}, rerun_lost_ack=1)
    lab.park("land_interrupted")
    assert lab.forge()["reruns"] == [51]
    lab.resume()
    lab.finish()
    assert lab.forge()["reruns"] == [51, 52]
    assert lab.writes() == ["pr create", "run rerun", "run rerun", "pr merge"]


def test_a_frozen_run_that_fails_again_parks_and_keeps_its_spent_budget(make):
    lab = make(window=60)
    lab.accept()
    again = {"run_attempt": 2, "conclusion": "failure"}
    lab.edit(ci=[ci([run_row(51, conclusion="failure")])], rerun_to={"51": again})
    lab.park("land_ci_failed")
    assert lab.forge()["reruns"] == [51]
    lab.edit(ci=GREEN, rerun_to={})
    lab.resume()
    lab.finish()
    assert lab.forge()["reruns"] == [51] and lab.writes().count("pr merge") == 1


# --- L6: merge -------------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["merge_lost_ack", "merge_kill_after"])
def test_a_lost_merge_acknowledgement_of_a_merged_pr_resumes_verification(make, flag):
    lab = make()
    lab.accept()
    lab.edit(**{flag: 1})
    lab.finish()
    assert len(lab.forge()["merge_calls"]) == 1 and len(lab.hook_calls()) == 1
    assert lab.land()["tree_verified"] is True and len(lab.cards()) == 1


def test_a_lost_merge_acknowledgement_of_an_open_pr_is_not_blindly_replayed(make):
    lab = make()
    lab.accept()
    lab.edit(merge_refused=1)
    lab.park("land_merge_failed")
    assert len(lab.forge()["merge_calls"]) == 1 and lab.hook_calls() == []
    lab.scan_once()
    assert len(lab.forge()["merge_calls"]) == 1
    before = int(git(lab.bare, "rev-list", "--count", "refs/heads/main"))
    lab.resume()
    lab.drive(lambda j: j.stage in STOPPED)
    assert int(git(lab.bare, "rev-list", "--count", "refs/heads/main")) <= before + 1
    assert list(lab.forge()["pulls"]) == ["17"] and lab.writes().count("pr create") == 1


def test_a_merge_that_exits_zero_without_merging_is_not_success(make):
    lab = make()
    lab.accept()
    lab.edit(merge_queued=1)
    lab.park("land_merge_failed")
    assert lab.hook_calls() == [] and lab.cards() == []


# --- L7: final tree --------------------------------------------------------------------------


def test_tree_mismatch_after_merge_keeps_the_squash_and_installs_nothing(make):
    lab = make()
    lab.accept()
    lab.edit(merge_extra_file=1)
    job = lab.park("land_tree_mismatch")
    squash = git(lab.bare, "rev-parse", "main")
    assert lab.land()["S"] == squash and lab.land()["tree_verified"] is False
    assert lab.hook_calls() == [] and lab.cards() == [] and job.stage == "user_wait"
    lab.resume()
    lab.park("land_tree_mismatch")
    assert lab.writes() == ["pr create", "pr merge"]
    assert git(lab.bare, "rev-parse", "main") == squash
    assert list(lab.forge()["pulls"]) == ["17"]


def test_default_branch_moving_while_merging_is_detected_after_the_merge(make):
    lab = make()
    lab.accept()
    lab.edit(advance_main_at_merge=1, merge_extra_file=1)
    lab.park("land_tree_mismatch")
    assert lab.hook_calls() == [] and len(lab.forge()["merge_calls"]) == 1


# --- L8: install -----------------------------------------------------------------------------


def test_failed_install_keeps_the_squash_and_never_replays_without_a_resume(make):
    lab = make(hook_modes=("fail", "ok"))
    lab.accept()
    lab.park("land_install_failed")
    squash = git(lab.bare, "rev-parse", "main")
    assert lab.land()["S"] == squash and lab.land()["install"]["status"] == "failed"
    for _ in range(2):
        lab.scan_once()
    assert len(lab.hook_calls()) == 1 and lab.cards() == []
    lab.resume()
    lab.finish()
    assert len(lab.hook_calls()) == 2 and len(lab.cards()) == 1
    assert lab.writes() == ["pr create", "pr merge"]


def test_an_interrupted_install_is_unknown_and_each_retry_needs_its_own_resume(make):
    lab = make(hook_modes=("fail", "crash", "ok"))
    lab.accept()
    lab.park("land_install_failed")
    lab.resume("first")
    lab.park("land_install_failed")
    assert lab.land()["install"]["status"] == "unknown" and len(lab.hook_calls()) == 2
    for _ in range(2):
        lab.scan_once()
    assert len(lab.hook_calls()) == 2 and lab.cards() == []
    lab.resume("second")
    lab.finish()
    assert len(lab.hook_calls()) == 3 and len(lab.cards()) == 1
    assert lab.writes() == ["pr create", "pr merge"]


def test_a_crash_during_the_first_install_is_unknown_not_replayed(make):
    lab = make(hook_modes=("crash", "ok"))
    lab.accept()
    lab.park("land_install_failed")
    assert lab.land()["install"]["status"] == "unknown" and len(lab.hook_calls()) == 1
    lab.scan_once()
    assert len(lab.hook_calls()) == 1 and lab.cards() == []


@pytest.mark.parametrize("change", ["other", "disabled"])
def test_a_changed_hook_never_runs_and_resume_does_not_adopt_it(make, change):
    lab = make(hook_modes=("fail", "ok"))
    lab.accept()
    lab.park("land_install_failed")
    other = lab.hook_dir / "other.py"
    other.write_text("import sys; open(sys.argv[0] + '.ran', 'a').write('x')\n", encoding="utf-8")
    replacement = [sys.executable, str(other), "{sha}"] if change == "other" else None
    lab.configure(install_command=replacement)
    lab.resume()
    lab.park("land_hook_changed")
    assert len(lab.hook_calls()) == 1 and not Path(f"{other}.ran").exists()
    lab.configure()
    lab.resume()
    lab.finish()
    assert len(lab.hook_calls()) == 2 and lab.writes() == ["pr create", "pr merge"]


# --- delivery --------------------------------------------------------------------------------


def test_delivery_failure_after_install_does_not_land_or_install_again(make):
    lab = make(sender_modes=("nonjson_failure", "success"))
    lab.accept()
    lab.park("delivery_unknown")
    for _ in range(3):
        lab.scan_once()
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.hook_calls()) == 1
    lab.resume()
    job = lab.finish()
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.hook_calls()) == 1
    assert job.land_state["install"]["status"] == "succeeded"


def test_evidence_changed_after_the_merge_parks_with_the_squash_and_never_requeues(make):
    lab = make(hook_modes=("mutate",))
    lab.accept()
    (lab.hook_dir / "mutate.txt").write_text(
        str(lab.folder() / "pr" / "pr" / "body.md"), encoding="utf-8"
    )
    job = lab.park("land_review_stale")
    squash = git(lab.bare, "rev-parse", "main")
    assert job.land_state["S"] == squash and lab.cards() == []
    lab.resume()
    assert lab.park("land_review_stale").land_state["S"] == squash
    assert lab.writes() == ["pr create", "pr merge"] and len(lab.hook_calls()) == 1


# --- stale attempts, cancellation, serialization ---------------------------------------------


def test_a_stale_land_attempt_changes_nothing(make):
    lab = make()
    lab.accept()
    lab.put("land")
    assert run_job(lab.jobs, lab.config, lab.job.id, lab.attempt - 1) == 2
    assert lab.forge_calls() == [] and lab.hook_calls() == []
    assert dict(lab.get().land_state) == {}


def test_cancel_stops_the_land_step_without_merging(make):
    lab = make(window=120)
    lab.accept()
    lab.edit(ci=PENDING)
    lab.scan_once()
    wait_for(lambda: lab.forge().get("pulls"))
    assert cli_main(["--root", str(lab.root), "cancel", lab.job.id, "--reason", "owner"]) == 0
    lab.service.wait(60)
    assert lab.get().stage == "cancelled" and lab.writes() == ["pr create"]
    assert lab.hook_calls() == []


def test_worktree_alias_of_one_repository_lands_one_job_at_a_time(make):
    first = make()
    first.accept()
    second = make(share=first, receipt="k2", alias=True)
    second.accept()
    first.scan_once()
    assert len(first.service.started) == 1
    first.service.wait()
    for _ in range(6):
        first.scan_once()
        first.service.wait()
    jobs = first.jobs.load()
    assert sorted(job.stage for job in jobs.values()) == ["done", "user_wait"]
    assert {job.wait_reason for job in jobs.values()} <= {"", "land_base_moved"}
    assert first.writes() == ["pr create", "pr merge"]


def test_an_orphan_of_a_finished_install_keeps_other_roots_out_of_the_repository(make, tmp_path):
    first = make(hook_modes=("orphan",))
    first.accept()
    second = make(share=first, receipt="k2", alias=True, where=tmp_path / "two")
    second.accept()
    first.finish()
    assert (first.hook_dir / "orphan.pid").exists()
    for _ in range(3):
        second.scan_once()
        second.service.wait()
    assert second.get().stage in {"land", "queued", "user_wait"}
    assert first.writes() == ["pr create", "pr merge"] and "H" not in second.land()
    os.kill(int((first.hook_dir / "orphan.pid").read_text(encoding="utf-8")), signal.SIGKILL)
    time.sleep(0.5)
    if second.get().stage == "user_wait":
        second.resume()
    second.park("land_base_moved")
    assert first.writes() == ["pr create", "pr merge"]


# --- predecessors ----------------------------------------------------------------------------


def test_successor_starts_from_the_landed_squash_after_the_branch_is_cleaned(make):
    first = make()
    first.accept()
    first.finish()
    squash = first.land()["S"]
    first.scan_once()
    branch = first.get().branch
    assert git(first.repo, "branch", "--list", branch) == ""
    second = make(
        share=first, receipt="k2", base="@k1", predecessors=["k1"], public_base="origin/main"
    )
    second.accept()
    assert second.get().base == squash
    second.finish()
    assert second.land()["B"] == squash and second.land()["pr"]["number"] == 18
    assert first.writes().count("pr merge") == 2

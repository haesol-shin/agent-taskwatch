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
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskwatch import job_land
from taskwatch.job_cli import main as cli_main
from taskwatch.job_runner import Ctx, run_job
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
    if state.get("follow_branch") and pr["state"] == "open":
        pr["head_sha"] = ref("refs/heads/" + pr["head_ref"])
        save()
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
elif args[:2] == ["pr", "edit"]:
    pr = state["pulls"][args[2]]
    pr["title"] = option("--title")
    pr["body"] = Path(option("--body-file")).read_bytes().decode("utf-8")
    save()
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
    prefix += ["-c", f"url.{bare}.insteadOf=review-publish:sample/project.git"]
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

    def start(self, job: JobState, private_tmp: Path | None = None) -> None:
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
        blank_body: bool = False,
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
        self.blank_body = blank_body
        target = self.repo
        if alias:
            target = tmp_path / f"alias-{receipt}"
            git(self.repo, "worktree", "add", "-b", f"alias-{receipt}", str(target))
        super().__init__(
            tmp_path,
            monkeypatch,
            review="code",
            target=target,
            public_text=[
                {
                    "id": "pr",
                    "base": public_base,
                    **({"blank_squash_body": True} if blank_body else {}),
                }
            ],
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
        self.write_text(
            "commit.txt", "\n" if self.blank_body else "- Preserve the complete result\n"
        )
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


def test_a_declared_blank_squash_body_lands_with_the_title_alone(make):
    lab = make(blank_body=True)
    lab.accept()
    assert lab.reviewed_text()["commit.txt"] == ""
    lab.finish()
    state = lab.land()
    assert git(lab.bare, "log", "-1", "--format=%B", state["H"]) == "feat(x): add"
    assert lab.forge()["merge_calls"] == [
        {"number": 17, "subject": "feat(x): add (#17)", "body": ""}
    ]
    assert git(lab.bare, "log", "-1", "--format=%B", state["S"]) == "feat(x): add (#17)"


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


# --- guards against changes while an external observation is in flight -----------------------


def test_install_reloads_host_config_instead_of_using_the_runner_startup_copy(make):
    lab = make()
    startup = load_job_config(lab.root)
    command = list(startup.install_command)
    frozen = {
        "status": "not_started",
        "command": command,
        "command_hash": job_land.digest(command),
    }
    job = lab.put("land", land_state={"S": "a" * 40, "install": frozen})
    ctx = Ctx(lab.jobs, startup, job, lab.folder(), lab.tree)
    transaction = job_land.Transaction(ctx, -1)
    transaction.repo = job_land.forge.Repository(
        "forge.invalid", "sample", "project", "main", True, False
    )
    # Model a host edit during CI: the runner's startup config still contains the old hook.
    lab.configure(install_command=None)

    with pytest.raises(job_land.LandStop) as stopped:
        transaction.install()

    assert stopped.value.reason == "land_hook_changed"
    assert lab.hook_calls() == []
    assert lab.land()["install"]["status"] == "not_started"


@pytest.mark.parametrize("selection", ["selected", "missing", "empty", "legacy", "migrated"])
def test_repository_hook_freezes_and_installs_only_the_selected_entry(make, monkeypatch, selection):
    lab = make()
    key = "forge.invalid/Sample/Project"
    hooks = {key: lab.install_argv} if selection == "selected" else {}
    if selection == "missing":
        hooks = {"forge.invalid/other/project": lab.install_argv}
    if selection not in {"legacy", "migrated"}:
        lab.configure(install_commands=hooks)
    lab.accept()
    job = lab.put("land")
    startup = load_job_config(lab.root)
    ctx = Ctx(lab.jobs, startup, job, lab.folder(), lab.tree)
    repo = job_land.forge.Repository("forge.invalid", "Sample", "Project", "main", True, False)
    with (lab.repo / ".git" / "HEAD").open() as lock:
        transaction = job_land.Transaction(ctx, lock.fileno())
        monkeypatch.setattr(transaction.api, "inspect_repository", lambda: repo)
        transaction.initialize()
        enabled = selection in {"selected", "legacy", "migrated"}
        frozen = transaction.state["install"]
        assert frozen["status"] == ("not_started" if enabled else "disabled")
        assert frozen["command"] == (lab.install_argv if enabled else None)
        if selection == "migrated":
            hooks = {key: lab.install_argv}
        if selection != "legacy":
            # An unrelated repository edit cannot invalidate this transaction.
            lab.configure(install_commands={**hooks, "forge.invalid/other/repo": ["x", "{sha}"]})
        transaction.save(S="a" * 40)
        transaction.install()
    assert lab.land()["install"]["status"] == ("succeeded" if enabled else "disabled")
    assert [call["argv"] for call in lab.hook_calls()] == (
        [["install", "a" * 40, str(lab.repo)]] if enabled else []
    )


@pytest.mark.parametrize("replacement", ["changed", "removed", "added"])
def test_selected_repository_hook_change_stops_before_install(make, monkeypatch, replacement):
    lab = make()
    key = "forge.invalid/sample/project"
    hooks = {} if replacement == "added" else {key: lab.install_argv}
    lab.configure(install_commands=hooks)
    lab.accept()
    job = lab.put("land")
    ctx = Ctx(lab.jobs, load_job_config(lab.root), job, lab.folder(), lab.tree)
    repo = job_land.forge.Repository("forge.invalid", "sample", "project", "main", True, False)
    with (lab.repo / ".git" / "HEAD").open() as lock:
        transaction = job_land.Transaction(ctx, lock.fileno())
        monkeypatch.setattr(transaction.api, "inspect_repository", lambda: repo)
        transaction.initialize()
        updated = {} if replacement == "removed" else {key: ["replacement", "{sha}"]}
        lab.configure(install_commands=updated)
        transaction.save(S="a" * 40)
        with pytest.raises(job_land.LandStop) as stopped:
            transaction.install()
    assert stopped.value.reason == "land_hook_changed"
    assert lab.hook_calls() == []


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/sample/project/archive/main.zip?pin={sha}",
        "https://github.com/sample/project/archive/main.tar.gz#{sha}",
        "https://github.com/sample/project/archive/main/{sha}.zip",
        "https://github.com/sample/project/archive/{sha}.zip?download=1",
    ],
)
def test_archive_source_must_pin_the_revision_path_not_incidental_url_text(source):
    repo = SimpleNamespace(host="github.com", owner="sample", name="project")
    valid = "https://github.com/sample/project/archive/{sha}.zip"
    assert job_land.install_argv(("installer", valid), "a" * 40, "/target", repo) == [
        "installer",
        f"https://github.com/sample/project/archive/{'a' * 40}.zip",
    ]
    with pytest.raises(job_land.LandStop) as stopped:
        job_land.install_argv(("installer", source), "a" * 40, "/target", repo)
    assert stopped.value.reason == "land_hook_changed"


def test_stale_acceptance_before_push_clears_the_prepared_transaction_with_the_requeue(make):
    lab = make()
    lab.accept()
    old = {"phase": "head", "R": "a" * 40, "T": "b" * 40, "H": "c" * 40}
    job = lab.put("land", land_state=old)
    ctx = Ctx(lab.jobs, lab.config, job, lab.folder(), lab.tree)
    transaction = job_land.Transaction(ctx, -1)
    lab.write_text("body.md", "changed before the push\n")

    with pytest.raises(job_land.FreshnessRequeued):
        transaction.freshness()

    current = lab.get()
    assert current.stage == "check"
    assert dict(current.land_state) == {}
    assert current.recoveries == job.recoveries + 1
    cleaned_forge(lab)


@pytest.mark.parametrize("observed", ["success", "failed"])
def test_ci_query_finishing_after_deadline_cannot_accept_green_or_spend_a_rerun(
    monkeypatch, observed
):
    clock = [105.0]
    transaction = object.__new__(job_land.Transaction)
    transaction.state = {"ci_started_at": 100.0, "ci_deadline": 110.0}
    transaction.current = lambda: SimpleNamespace(events=())
    saved = []
    reruns = []
    transaction.save = lambda **changes: saved.append(changes)
    transaction.rerun = lambda evidence: reruns.append(evidence)

    def observation():
        clock[0] = 111.0
        return SimpleNamespace(state=observed, evidence=())

    transaction.observe_ci = observation
    monkeypatch.setattr(job_land.time, "time", lambda: clock[0])

    with pytest.raises(job_land.LandStop) as stopped:
        transaction.ci()

    assert stopped.value.reason == "land_ci_failed"
    assert saved == [] and reruns == []
    assert transaction.state["ci_deadline"] == 110.0


@pytest.mark.parametrize("multiple", [False, True])
def test_foreign_push_urls_stop_before_any_remote_write(make, tmp_path, multiple):
    lab = make()
    lab.accept()
    foreign = tmp_path / "foreign.git"
    subprocess.run([os.environ["REAL_GIT"], "init", "--bare", str(foreign)], check=True)
    if multiple:
        git(lab.repo, "config", "--add", "remote.origin.pushurl", ORIGIN)
    git(lab.repo, "config", "--add", "remote.origin.pushurl", str(foreign))
    lab.park("land_head_changed")
    cleaned_forge(lab)
    assert git(foreign, "for-each-ref", "--format=%(refname)") == ""
    assert lab.hook_calls() == []


def test_main_fetch_ignores_restricted_remote_tracking_mapping(make):
    lab = make()
    lab.accept()
    base = lab.get().base
    git(
        lab.repo,
        "config",
        "--replace-all",
        "remote.origin.fetch",
        "+refs/heads/other:refs/remotes/origin/other",
    )
    moved = lab.advance_remote()
    assert git(lab.repo, "rev-parse", "origin/main") == base
    lab.park("land_base_moved")
    assert git(lab.repo, "rev-parse", "origin/main") == moved
    cleaned_forge(lab)
    assert lab.hook_calls() == []


def test_saved_pr_merged_during_ci_resumes_verification_without_remerge(make):
    lab = make()
    lab.accept()
    lab.edit(ci=PENDING)
    lab.park("land_ci_failed")
    state = lab.land()
    assert "merge_intent" not in state and state["pr"]["number"] == 17
    merged = git(
        lab.bare,
        "commit-tree",
        state["T"],
        "-p",
        state["M"],
        "-m",
        "feat(x): add (#17)\n\nWhy it is needed.",
    )
    git(lab.bare, "update-ref", "refs/heads/main", merged, state["M"])
    pulls = lab.forge()["pulls"]
    pulls["17"].update(state="closed", merged=True, merge_sha=merged)
    lab.edit(pulls=pulls)
    lab.resume()
    lab.finish()
    assert lab.land()["S"] == merged and lab.land()["tree_verified"]
    assert lab.writes() == ["pr create"]
    assert len(lab.hook_calls()) == 1 and len(lab.cards()) == 1


def test_resolved_push_alias_is_not_rewritten_a_second_time(make, tmp_path):
    lab = make()
    lab.accept()
    foreign = tmp_path / "foreign.git"
    subprocess.run([os.environ["REAL_GIT"], "init", "--bare", str(foreign)], check=True)
    git(lab.repo, "config", "remote.origin.pushurl", "review-publish:sample/project.git")
    git(lab.repo, "config", "url.ssh://git@github.com/.insteadOf", "review-publish:")
    git(lab.repo, "config", f"url.{foreign}.insteadOf", "ssh://git@github.com/sample/project.git")
    resolved = git(lab.repo, "remote", "get-url", "--push", "origin")
    assert resolved == "ssh://git@github.com/sample/project.git"
    assert git(lab.repo, "ls-remote", "--get-url", resolved) == str(foreign)
    lab.finish()
    assert lab.remote_land_branch() == lab.land()["H"]
    assert git(foreign, "for-each-ref", "--format=%(refname)") == ""
    assert len(lab.hook_calls()) == 1 and len(lab.cards()) == 1


def test_missing_pr_reconciliation_never_creates_before_rechecking_moved_base(make):
    lab = make()
    lab.accept()
    lab.edit(create_refused=1)
    lab.park("land_interrupted")
    assert len(lab.forge()["created"]) == 1 and not lab.forge()["pulls"]
    lab.advance_remote()
    lab.resume()
    lab.park("land_base_moved")
    assert len(lab.forge()["created"]) == 1 and not lab.forge()["pulls"]
    assert lab.writes() == ["pr create"] and lab.hook_calls() == []


def _accepted_stack(make):
    first = make()
    first.accept()
    second = make(share=first, receipt="k2", base="@k1", public_base="@k1")
    second.accept()
    assert second.get().base == first.get().reviews[-1]["fingerprint"]
    cleaned_forge(second)
    return first, second


def _land_direct(lab, expected=0):
    lab.config = load_job_config(lab.root)
    assert lab.run("land") == expected
    return lab.get()


def _move_main(lab, name="external.txt", text="external\n"):
    git(lab.repo, "fetch", "origin")
    git(lab.repo, "reset", "--hard", "origin/main")
    (lab.repo / name).write_text(text, encoding="utf-8")
    git(lab.repo, "add", name)
    git(lab.repo, "commit", "-m", "fix(x): change main\n\n- Keep independent work")
    git(lab.repo, "push", "origin", "main")
    return git(lab.repo, "rev-parse", "HEAD")


def test_stacked_tree_preserved_publishes_only_after_parent_lands(make):
    first, second = _accepted_stack(make)
    tree = git(second.tree, "rev-parse", "HEAD^{tree}")
    # Model the predecessor's live land service without starting another land process.
    first.jobs.save(
        replace(first.get(), unit="prior", ended_at=0, last_progress_at=time.time()), time.time()
    )
    service = SimpleNamespace(
        status=lambda unit: "active" if unit == "prior" else "inactive",
        stop=lambda unit: False,
    )
    scan_jobs(second.jobs, load_job_config(second.root), now=time.time(), service=service)
    assert second.get().unit == ""
    assert second.get().wait_reason == f"predecessor:{first.job.id}"
    cleaned_forge(second)
    assert _land_direct(first).stage == "deliver_pending"
    squash = first.land()["S"]
    assert _land_direct(second).stage == "deliver_pending"
    state = second.land()
    assert git(second.bare, "rev-parse", f"{state['H']}^") == squash
    assert git(second.bare, "rev-parse", f"{state['H']}^{{tree}}") == tree
    assert state["T"] == tree and state["pr"]["number"] == 18
    assert second.writes() == ["pr create", "pr merge"] * 2


def test_stacked_replay_rechecks_only_child_diff_on_moved_main(make):
    first, second = _accepted_stack(make)
    _land_direct(first)
    main = _move_main(first)
    before = second.writes()
    old = second.get()
    job = _land_direct(second)
    assert job.stage == job.resume_stage == "check"
    assert job.base == job.public_text[0]["base"] == main
    assert job.reviews_used == job.recoveries == job.review_inflight == 0
    assert job.review_cycle_start == len(old.reviews)
    assert not job.land_state and second.writes() == before
    assert git(second.tree, "rev-parse", "HEAD^") == main
    assert (second.tree / "external.txt").read_text() == "external\n"
    assert (second.tree / "k1.txt").read_text() == "one\n"
    assert (second.tree / "k2.txt").read_text() == "one\n"
    assert git(second.tree, "diff", "--name-only", main, "HEAD") == "k2.txt"
    replay = next(e for e in job.events if e.get("type") == "land_replay")
    assert git(second.tree, "rev-parse", "HEAD") == replay["commit"]
    assert second.run("check") == 0
    second.scenario([{"reply": f"{CODE_PASS}\nNo findings."}])
    assert second.run("review") == 0
    _land_direct(second)
    assert second.get().stage == "deliver_pending"
    assert second.land()["M"] == main and second.land()["R"] == replay["commit"]
    assert second.writes() == ["pr create", "pr merge"] * 2


def test_stacked_replay_conflict_keeps_workspace_and_publication_untouched(make):
    first, second = _accepted_stack(make)
    _land_direct(first)
    _move_main(first, "k2.txt", "different\n")
    head = git(second.tree, "rev-parse", "HEAD")
    before = second.writes()
    job = _land_direct(second, expected=1)
    assert (job.stage, job.wait_reason) == ("user_wait", "land_replay_conflict")
    assert git(second.tree, "rev-parse", "HEAD") == head
    assert "replay" not in job.land_state and "H" not in job.land_state
    assert job.base == second.get().reviews[-1]["texts"]["pr"]["base"]
    assert second.writes() == before


@pytest.mark.parametrize("when", ["before:reset", "after:reset"])
def test_stacked_replay_restart_reuses_saved_commit(make, when):
    first, second = _accepted_stack(make)
    _land_direct(first)
    main = _move_main(first)
    second.trap(when)
    second.scan_once()
    second.service.wait()
    intent = second.land()["replay"]
    assert second.get().stage == "land"
    assert git(second.tree, "rev-parse", "HEAD") == (
        intent["from"] if when == "before:reset" else intent["commit"]
    )
    job = second.drive(lambda j: j.stage == "check", rounds=1)
    assert job.base == main and not job.land_state
    assert git(second.tree, "rev-parse", "HEAD") == intent["commit"]
    events = [e for e in job.events if e.get("type") == "land_replay"]
    assert len(events) == 1 and events[0]["commit"] == intent["commit"]
    assert second.writes() == ["pr create", "pr merge"]


@pytest.mark.parametrize("owner_input", [False, True])
def test_stacked_replay_restart_budget_requires_a_new_owner_entry(make, owner_input):
    first, second = _accepted_stack(make)
    _land_direct(first)
    _move_main(first)
    job = second.get()
    events = (*job.events, *({"type": "land_replay", "entry": 0} for _ in range(2)))
    if owner_input:
        events = (*events, {"type": "resume"})
    second.jobs.save(replace(job, events=events), time.time())
    job = _land_direct(second, expected=0 if owner_input else 1)
    assert job.stage == ("check" if owner_input else "user_wait")
    if not owner_input:
        assert job.wait_reason == "land_base_moved"
    assert second.writes() == ["pr create", "pr merge"]


@pytest.mark.parametrize("tree_changed", [False, True])
def test_stacked_predecessor_fixed_uses_final_tree_at_landing(make, tree_changed):
    first, second = _accepted_stack(make)
    if tree_changed:
        (first.tree / "k1.txt").write_text("revised\n", encoding="utf-8")
        git(first.tree, "add", "k1.txt")
        git(first.tree, "commit", "-m", "fix(x): revise\n\n- Preserve corrected input")
    else:
        git(first.tree, "commit", "--allow-empty", "-m", "fix(x): explain\n\n- Explain input")
    assert first.run("check") == 0
    first.scenario([{"reply": f"{CODE_PASS}\nNo findings."}])
    assert first.run("review") == 0
    _land_direct(first)
    job = _land_direct(second)
    assert job.stage == ("check" if tree_changed else "deliver_pending")
    if tree_changed:
        assert (second.tree / "k1.txt").read_text() == "revised\n"
        assert (second.tree / "k2.txt").read_text() == "one\n"
    else:
        assert second.land()["M"] == first.land()["S"]


# --- optional review-bot gate ----------------------------------------------------------------


APP = "review-bot-example"


def bot_thread(n=1, severity="P2", **changes):
    return {
        "id": f"thread-{n}",
        "root_id": n,
        "url": f"https://forge.invalid/review/{n}",
        "path": "example.py",
        "line": n,
        "author_type": "Bot",
        "author": APP,
        "body": f"{severity}: Problem {n}",
        "resolved": False,
        "outdated": False,
        **changes,
    }


@contextmanager
def bot_lab(make, monkeypatch, *, exhausted=True):
    monkeypatch.setattr(sys.modules[__name__], "ORIGIN", "https://forge.invalid/sample/project.git")
    monkeypatch.setattr(
        sys.modules[__name__], "FAKE_GH", FAKE_GH.replace("github.com", "forge.invalid")
    )
    monkeypatch.setattr(sys.modules[__name__], "SHIM", SHIM.replace("github.com", "forge.invalid"))
    lab = make(window=60, install=None)
    lab.configure(review_bots={"forge.invalid/sample/project": [APP]})
    lab.accept()
    job = lab.put("land")
    lab.jobs.save(replace(job, reviews_used=job.max_reviews if exhausted else 1), time.time())
    job = lab.get()
    ctx = Ctx(lab.jobs, load_job_config(lab.root), job, lab.folder(), lab.tree)
    with (lab.repo / ".git" / "HEAD").open() as lock:
        tx = job_land.Transaction(ctx, lock.fileno())
        tx.api.authenticated_hosts = ("forge.invalid",)
        tx.api.inspect_bot_runs = lambda *args: {
            APP: [{"status": "completed", "conclusion": "success"}]
        }
        tx.api.review_threads = lambda *args: []
        yield lab, tx


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("severity", ["P0", "P1", "P2", "P3"])
def test_bot_priority_routes_without_spending_review_budget(make, monkeypatch, exhausted, severity):
    with bot_lab(make, monkeypatch, exhausted=exhausted) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread(severity=severity)]
        if not exhausted or severity in {"P0", "P1"}:
            with pytest.raises(job_land.BotParked):
                tx.run()
            job = lab.get()
            assert (job.stage, job.wait_reason, job.resume_stage) == (
                ("user_wait", "review_exhausted", "fix") if exhausted else ("fix", "", "fix")
            )
            assert lab.writes() == ["pr create"]
        else:
            tx.run()
            assert lab.get().stage == "deliver_pending"
            assert lab.land()["bot"]["followup"]["findings"] == ["r2.F1"]
        assert lab.get().reviews_used == (lab.get().max_reviews if exhausted else 1)


def test_exhausted_low_observation_survives_restart_merge_and_one_delivery(
    make, monkeypatch, capsys
):
    from taskwatch import brief, job_delivery

    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread(n) for n in range(1, 17)]
        tx.run()
        job = lab.get()
        assert job.stage == "deliver_pending" and job.land_state["tree_verified"]
        assert job_delivery.acceptance_current(lab.jobs, tx.ctx.config, job)
        assert job_delivery.accepted_review(job)["verdict"] == "PASS"
        assert len(job.land_state["bot"]["followup"]["findings"]) == 16
        assert lab.reviewed_text()["body.md"].startswith("body\n")
        # A fresh runner instance selects the same PASS after merge, not the last FAIL.
        ctx = Ctx(lab.jobs, tx.ctx.config, lab.put("land"), lab.folder(), lab.tree)
        restarted = job_land.Transaction(ctx, tx.lock_fd)
        restarted.api.authenticated_hosts = ("forge.invalid",)
        restarted.run()
        full = "\n".join(brief.compose(lab.jobs, tx.ctx.config, lab.get()))
        for n in range(1, 17):
            assert f"https://forge.invalid/review/{n}" in full
        capsys.readouterr()
        assert cli_main(["--root", str(lab.root), "brief", job.id]) == 0
        rendered = capsys.readouterr().out
        for n in range(1, 17):
            assert f"https://forge.invalid/review/{n}" in rendered
        done = lab.finish()
        assert done.stage == "done" and len(lab.cards()) == 1
        assert f"bot-followup:{job.id}" in lab.cards()[0]["text"]
        assert lab.writes() == ["pr create", "pr merge"]


def test_mixed_exhaustion_retains_one_group_and_resolved_high_still_blocks(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        visible = [bot_thread(1, "P1"), *[bot_thread(n) for n in range(2, 18)]]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        original = lab.land()["bot"]["followup"]
        assert len(original["findings"]) == 16
        assert len(lab.get().reviews) == 2
        lab.resume()
        # U1 exhausted resume normally goes to fix; explicitly exercise a land restart.
        job = lab.put("land")
        tx.ctx = Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree)
        visible[0]["resolved"] = True
        with pytest.raises(job_land.BotParked):
            tx.bot_findings()
        assert lab.land()["bot"]["followup"] == original
        assert len(lab.get().reviews) == 2
        assert lab.writes() == ["pr create"]


def test_late_finding_and_priority_promotion_replace_refs_without_duplicate_records(
    make, monkeypatch
):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.initialize()
        tx.save(H="a" * 40, pr={"number": 17, "url": "https://forge.invalid/pr/17"})
        visible = [bot_thread()]
        tx.api.review_threads = lambda *args: visible
        tx.bot_findings()
        tx.bot_findings()
        assert len(lab.get().reviews) == 2
        visible.append(bot_thread(2))
        tx.bot_findings()
        assert lab.land()["bot"]["followup"]["findings"] == ["r2.F1", "r3.F1"]
        visible[0]["body"] = "P1: Promoted"
        with pytest.raises(job_land.BotParked):
            tx.bot_findings()
        assert lab.land()["bot"]["followup"]["findings"] == ["r3.F1"]
        assert lab.land()["bot"]["threads"]["thread-1"]["severity"] == "P1"


@pytest.mark.parametrize("damage", ["missing_review", "missing_thread"])
def test_missing_followup_evidence_prevents_landing(make, monkeypatch, damage):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.initialize()
        tx.save(H="a" * 40, pr={"number": 17})
        tx.api.review_threads = lambda *args: [bot_thread()]
        tx.bot_findings()
        bot = tx.state["bot"]
        if damage == "missing_thread":
            tx.save(bot={**bot, "threads": {}})
        else:
            job = lab.get()
            lab.jobs.save(replace(job, reviews=job.reviews[:1]), time.time())
        with pytest.raises(job_land.LandStop, match="evidence is missing"):
            tx.bot_findings()
        assert "pr merge" not in lab.writes()


def test_untracked_resolved_humans_and_lookalikes_are_not_findings(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.api.review_threads = lambda *args: [
            bot_thread(1, "P1", resolved=True),
            bot_thread(2, "P1", author_type="User"),
            bot_thread(3, "P1", author=APP + "-lookalike"),
        ]
        tx.run()
        assert lab.get().stage == "deliver_pending"
        assert len(lab.get().reviews) == 1


@pytest.mark.parametrize("apps", [[APP], [APP, APP]])
@pytest.mark.parametrize("registration,completion", [(120, 120), (0, 580)])
def test_bot_window_waits_for_late_registration_and_completes_early(
    monkeypatch, registration, completion, apps
):
    clock = [0]
    tx = object.__new__(job_land.Transaction)
    tx.state = {
        "H": "a" * 40,
        "bot": {"apps": apps, "head": "a" * 40, "window_end": 600, "wait": 600, "outcome": {}},
    }
    tx.repo = None
    observed = []
    tx.save = lambda **changes: tx.state.update(changes)

    def wait(seconds):
        assert 0 < seconds <= 20
        clock[0] += seconds

    tx.wait = wait
    tx.bot_findings = lambda: observed.append(clock[0])
    tx.api = SimpleNamespace(
        inspect_bot_runs=lambda *args: {
            APP: (
                []
                if clock[0] < registration
                else [
                    {
                        "status": "completed" if clock[0] >= completion else "in_progress",
                        "conclusion": "success" if clock[0] >= completion else None,
                    }
                ]
            )
        }
    )
    monkeypatch.setattr(job_land.time, "time", lambda: clock[0])
    tx.bot()
    assert observed == [completion]
    assert tx.state["bot"]["outcome"] == {APP: "complete"}


@pytest.mark.parametrize("apps", [[APP], [APP, APP]])
@pytest.mark.parametrize(
    "signal,expected,elapsed",
    [
        ([], "fallback:absent", 600),
        ([{"status": "in_progress", "conclusion": None}], "fallback:timeout", 600),
        ([{"status": "completed", "conclusion": "failure"}], "fallback:not_success:failure", 0),
    ],
)
def test_bot_fallback_never_extends_deadline_or_skips_thread_gate(
    monkeypatch, signal, expected, elapsed, apps
):
    clock = [0]
    tx = object.__new__(job_land.Transaction)
    tx.repo = None
    tx.state = {
        "H": "a" * 40,
        "bot": {"apps": apps, "head": "a" * 40, "window_end": 600, "wait": 600, "outcome": {}},
    }
    tx.save = lambda **changes: tx.state.update(changes)

    def wait(seconds):
        assert 0 < seconds <= 20
        clock[0] += seconds

    tx.wait = wait
    tx.api = SimpleNamespace(inspect_bot_runs=lambda *args: {APP: signal})
    seen = []
    tx.bot_findings = lambda: seen.append(clock[0])
    monkeypatch.setattr(job_land.time, "time", lambda: clock[0])
    tx.bot()
    tx.bot()
    assert seen == [elapsed, elapsed]
    assert tx.state["bot"]["outcome"] == {APP: expected}
    assert tx.state["bot"]["window_end"] == 600


@pytest.mark.parametrize("late", [False, True])
def test_visible_high_blocks_even_on_fallback_or_final_reread(make, monkeypatch, late):
    with bot_lab(make, monkeypatch) as (lab, tx):
        calls = []

        def threads(*args):
            calls.append(1)
            return [bot_thread(severity="P1")] if not late or len(calls) > 1 else []

        tx.api.review_threads = threads
        tx.api.inspect_bot_runs = lambda *args: {
            APP: [{"status": "completed", "conclusion": "failure"}]
        }
        with pytest.raises(job_land.BotParked):
            tx.run()
        assert lab.writes() == ["pr create"]
        assert lab.land()["bot"]["outcome"][APP] == "fallback:not_success:failure"


@pytest.mark.parametrize(
    "change",
    [
        "code",
        "output",
        "text",
        "basis",
        "check",
        "log",
        "snapshot",
        "binding",
        "newer_fail",
    ],
)
def test_deferred_observation_preserves_every_acceptance_freshness_guard(make, monkeypatch, change):
    from taskwatch import job_delivery

    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread()]
        tx.run()
        job = lab.get()
        if change == "code":
            (lab.tree / "k1.txt").write_text("changed\n")
        elif change == "output":
            (lab.tree / "out.txt").write_text("changed\n")
        elif change == "text":
            lab.write_text("body.md", "changed\n")
        elif change == "basis":
            Path(job.intent).write_text("changed\n")
        elif change == "check":
            lab.jobs.save(
                replace(job, checks=(*job.checks[:-1], {**job.checks[-1], "ok": False})),
                time.time(),
            )
        elif change == "log":
            Path(job.checks[-1]["log"]).write_text("changed\n")
        elif change == "snapshot":
            snapshot = Path(job.reviews[0]["snapshot"]) / "pr" / "body.md"
            snapshot.chmod(0o600)
            snapshot.write_text("changed\n")
        elif change == "binding":
            lab.jobs.save(
                replace(
                    job,
                    reviews=(*job.reviews[:-1], {**job.reviews[-1], "accepted_review_seq": 999}),
                ),
                time.time(),
            )
        elif change == "newer_fail":
            lab.jobs.save(
                replace(
                    job,
                    reviews=(
                        *job.reviews,
                        {**job.reviews[0], "seq": 99, "round": 3, "ok": False, "verdict": "FAIL"},
                    ),
                ),
                time.time(),
            )
        job = lab.get()
        assert not job_delivery.acceptance_current(lab.jobs, tx.ctx.config, job)
        stopped = job_delivery.deliver_job(lab.jobs, tx.ctx.config, job, time.time())
        assert stopped.wait_reason == "land_review_stale" and lab.cards() == []


def test_bot_followup_save_failure_cannot_merge(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread()]
        save = lab.jobs.save

        def fail_observation(job, now):
            if len(job.reviews) > 1:
                raise OSError("synthetic storage failure")
            save(job, now)

        monkeypatch.setattr(lab.jobs, "save", fail_observation)
        with pytest.raises(OSError, match="storage failure"):
            tx.run()
        assert lab.writes() == ["pr create"]
        assert len(lab.get().reviews) == 1


def test_existing_transaction_without_bot_state_keeps_legacy_behavior(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.initialize()
        tx.state.pop("bot")
        tx.save()
        tx.api.inspect_bot_runs = lambda *args: pytest.fail("legacy bot query")
        tx.api.review_threads = lambda *args: pytest.fail("legacy thread query")
        tx.run()
        assert "bot" not in lab.land() and lab.get().stage == "deliver_pending"


def test_bot_selection_and_deadline_stay_frozen_across_restart_and_config_edit(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.initialize()
        tx.base_guard()
        tx.head()
        tx.pull_request()
        tx.ci()
        frozen = tx.state["bot"]
        lab.configure(
            review_bots={"forge.invalid/sample/project": ["other-app"]}, review_bot_wait=42
        )
        ctx = Ctx(lab.jobs, load_job_config(lab.root), lab.get(), lab.folder(), lab.tree)
        restarted = job_land.Transaction(ctx, tx.lock_fd)
        restarted.api.authenticated_hosts = ("forge.invalid",)
        restarted.initialize()
        # Bot wait is independent of the earlier CI observation deadline.
        restarted.save(ci_deadline=time.time() - 1)
        restarted.ci()
        assert restarted.state["bot"] == frozen


def test_resumed_budget_does_not_repark_retained_deferrals(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread()]
        tx.run()
        assert lab.get().stage == "deliver_pending"
        # A resume resets the budget; the retained low finding stays deferred, not blocking.
        job = lab.put("land", reviews_used=0)
        tx.ctx = Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree)
        tx.state = job.land_state
        tx.bot_findings()
        assert lab.land()["bot"]["followup"]["findings"] == ["r2.F1"]
        assert lab.land()["bot"]["threads"]["thread-1"]["status"] == "deferred"
        assert len(lab.get().reviews) == 2


def test_manual_resolution_closes_a_low_thread_and_landing_continues(make, monkeypatch):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        visible = [bot_thread()]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        assert lab.get().stage == "fix"
        visible[0]["resolved"] = True
        job = lab.put("land")
        tx.ctx = Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree)
        tx.state = job.land_state
        tx.bot_findings()
        threads = lab.land()["bot"]["threads"]
        assert threads["thread-1"]["status"] == "closed"
        assert threads["thread-1"]["resolved"] is True
        assert lab.land()["bot"]["followup"]["findings"] == []


def test_resolution_never_closes_a_high_finding_or_a_retained_deferral(make, monkeypatch):
    with bot_lab(make, monkeypatch) as (lab, tx):
        visible = [bot_thread(1, "P1"), bot_thread(2)]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        assert lab.land()["bot"]["followup"]["findings"] == ["r2.F2"]
        visible[0]["resolved"] = True
        visible[1]["resolved"] = True
        visible.append(bot_thread(3, "P3"))
        job = lab.put("land", reviews_used=0)
        tx.ctx = Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree)
        tx.state = job.land_state
        with pytest.raises(job_land.BotParked):
            tx.bot_findings()
        assert lab.land()["bot"]["threads"]["thread-3"]["status"] == "open"
        visible[2]["resolved"] = True
        job = lab.put("land", reviews_used=0)
        tx.ctx = Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree)
        tx.state = job.land_state
        with pytest.raises(job_land.BotParked):
            tx.bot_findings()
        threads = lab.land()["bot"]["threads"]
        assert threads["thread-1"]["status"] == "open"
        assert threads["thread-2"]["status"] == "deferred"
        assert threads["thread-3"]["status"] == "closed"
        assert lab.land()["bot"]["followup"]["findings"] == ["r2.F2"]


def restart_bot_tx(lab, tx):
    job = lab.put("land")
    newer = job_land.Transaction(
        Ctx(lab.jobs, tx.ctx.config, job, lab.folder(), lab.tree), tx.lock_fd
    )
    newer.api.authenticated_hosts = ("forge.invalid",)
    newer.api.inspect_bot_runs = tx.api.inspect_bot_runs
    newer.api.review_threads = tx.api.review_threads
    return newer


def accept_bot_fix(lab, *, status="fixed", reason="Not applicable", change_text=False):
    job = lab.get()
    refs = job.land_state["bot"]["round"]["findings"]
    if status == "fixed":
        (lab.tree / "k1.txt").write_text("corrected\n", encoding="utf-8")
        git(lab.tree, "add", "k1.txt")
        git(lab.tree, "commit", "-m", "fix(x): correct input\n\n- Preserve valid input")
        detail = {"commit": git(lab.tree, "rev-parse", "HEAD")}
    else:
        detail = {"reason": reason}
    (lab.folder() / "findings.json").write_text(
        json.dumps([{"id": ref, "status": status, **detail} for ref in refs]), encoding="utf-8"
    )
    if change_text:
        lab.write_text("title.txt", "fix(x): correct input\n")
        lab.write_text("body.md", "Corrected input.\n")
        lab.write_text("commit.txt", "- Keep invalid input from being accepted\n")
    assert lab.run("check") == 0
    assert lab.get().checks[-1]["problems"] == ()
    lab.scenario(
        [
            {
                "reply": CODE_PASS
                + "\n"
                + "\n".join(f"BOT_DISPOSITION {ref}: accepted" for ref in refs)
            }
        ]
    )
    assert lab.run("review") == 0
    assert lab.get().stage == "land", lab.get().error


@pytest.mark.parametrize("resolved", [False, True])
def test_bot_fixed_republishes_same_pr_and_delivers_once(make, monkeypatch, resolved):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        visible = [bot_thread(severity="P1")]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        old = lab.land()["H"]
        lab.jobs.save(replace(lab.get(), max_reviews=lab.get().reviews_used + 1), time.time())
        accept_bot_fix(lab, change_text=True)
        visible[0]["resolved"] = resolved
        visible.append(bot_thread(2, "P2"))
        tx = restart_bot_tx(lab, tx)
        replies = []
        tx.api.find_review_reply = lambda *args: args[3] in replies
        tx.api.post_review_reply = lambda *args: replies.append(args[3])
        lab.edit(follow_branch=True)
        tx.run()
        state = lab.land()
        assert lab.get().stage == "deliver_pending"
        assert state["H"] != old
        assert git(lab.bare, "rev-parse", f"{state['H']}^") == old
        assert state["tree_verified"]
        assert git(lab.bare, "rev-parse", f"{state['S']}^{{tree}}") == state["T"]
        assert lab.forge()["pulls"]["17"]["title"] == "fix(x): correct input"
        assert len(replies) == (0 if resolved else 1)
        assert state["bot"]["threads"]["thread-1"]["status"] == "fixed"
        assert state["bot"]["followup"]["findings"] == ["r4.F1"]
        tx = restart_bot_tx(lab, tx)
        tx.run()
        assert len(replies) == (0 if resolved else 1)
        assert lab.finish().stage == "done"
        assert len(lab.cards()) == 1
        assert f"bot-followup:{lab.get().id}" in lab.cards()[0]["text"]


def test_bot_not_fixed_needs_exact_owner_acceptance_before_publication(make, monkeypatch):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread(severity="P1")]
        with pytest.raises(job_land.BotParked):
            tx.run()
        old = lab.land()["H"]
        accept_bot_fix(lab, status="not_fixed", reason="Already guarded for @someone")
        tx = restart_bot_tx(lab, tx)
        with pytest.raises(job_land.LandStop, match="BOT_ACCEPT") as stopped:
            tx.run()
        assert stopped.value.reason == "bot_reason_approval"
        token = lab.land()["bot"]["owner_pending"]["digest"]
        assert lab.land()["H"] == old
        # A new owner entry without the bound token cannot approve a reason.
        lab.jobs.save(
            replace(
                lab.get(),
                events=(
                    *lab.get().events,
                    {
                        "type": "resume",
                        "entry": 1,
                        "reason": "continue",
                    },
                ),
            ),
            time.time(),
        )
        tx = restart_bot_tx(lab, tx)
        with pytest.raises(job_land.LandStop, match="BOT_ACCEPT"):
            tx.run()
        lab.jobs.save(
            replace(
                lab.get(),
                events=(
                    *lab.get().events,
                    {
                        "type": "resume",
                        "entry": 2,
                        "reason": f"BOT_ACCEPT {token}",
                    },
                ),
            ),
            time.time(),
        )
        tx = restart_bot_tx(lab, tx)
        replies = []
        tx.api.find_review_reply = lambda *args: args[3] in replies
        tx.api.post_review_reply = lambda *args: replies.append(args[3])
        lab.edit(follow_branch=True)
        tx.run()
        assert lab.get().stage == "deliver_pending"
        assert len(replies) == 1
        assert "@someone" not in replies[0]
        assert "@\u200bsomeone" in replies[0]
        assert lab.land()["bot"]["threads"]["thread-1"]["status"] == "answered"


@pytest.mark.parametrize(
    "mutation", ["foreign_head", "worker_status", "source", "path", "priority"]
)
def test_bot_fix_never_adopts_foreign_or_changed_evidence(make, monkeypatch, mutation):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        visible = [bot_thread(severity="P2")]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        old = lab.land()["H"]
        accept_bot_fix(lab)
        if mutation == "foreign_head":
            foreign = git(lab.bare, "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "foreign")
            git(lab.bare, "update-ref", f"refs/heads/{lab.land()['branch']}", foreign)
        elif mutation == "worker_status":
            (lab.folder() / "findings.json").write_text(
                '[{"id":"r2.F1","status":"not_fixed","reason":"different"}]', encoding="utf-8"
            )
        elif mutation == "source":
            visible[0]["body"] += "\nEdited"
        elif mutation == "path":
            visible[0]["path"] = "other.py"
        else:
            visible[0]["body"] = "P1: Problem 1"
        tx = restart_bot_tx(lab, tx)
        lab.edit(follow_branch=True)
        if mutation == "foreign_head":
            with pytest.raises(job_land.LandStop) as stopped:
                tx.run()
            assert stopped.value.reason == "land_head_changed"
        elif mutation == "worker_status":
            with pytest.raises(job_land.FreshnessRequeued):
                tx.run()
            assert lab.get().stage == "check"
        else:
            with pytest.raises(job_land.BotParked):
                tx.run()
            assert lab.get().stage == "fix"
        assert "pr merge" not in lab.writes()


def test_changed_worker_record_after_disposition_returns_to_a_new_round(make, monkeypatch):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        tx.api.review_threads = lambda *args: [bot_thread(severity="P1")]
        with pytest.raises(job_land.BotParked):
            tx.run()
        accept_bot_fix(lab)
        # The worker amends the accepted fix; its record no longer matches the reviewed bytes.
        git(lab.tree, "commit", "--allow-empty", "-m", "fix(x): reword\n\n- Keep the fix")
        (lab.folder() / "findings.json").write_text(
            json.dumps(
                [
                    {
                        "id": lab.land()["bot"]["round"]["findings"][0],
                        "status": "fixed",
                        "commit": git(lab.tree, "rev-parse", "HEAD"),
                    }
                ]
            ),
            encoding="utf-8",
        )
        tx = restart_bot_tx(lab, tx)
        lab.edit(follow_branch=True)
        with pytest.raises(job_land.FreshnessRequeued):
            tx.run()
        job = lab.get()
        assert job.stage == "check"
        assert job.land_state["bot"]["round"]["awaiting"] is True
        assert job.land_state["bot"]["threads"]["thread-1"]["status"] == "open"
        assert lab.run("check") == 0
        refs = lab.get().land_state["bot"]["round"]["findings"]
        lab.scenario(
            [
                {
                    "reply": CODE_PASS
                    + "\n"
                    + "\n".join(f"BOT_DISPOSITION {r}: accepted" for r in refs)
                }
            ]
        )
        assert lab.run("review") == 0
        tx = restart_bot_tx(lab, tx)
        replies = []
        tx.api.find_review_reply = lambda *args: args[3] in replies
        tx.api.post_review_reply = lambda *args: replies.append(args[3])
        tx.run()
        assert lab.get().stage == "deliver_pending"


def test_late_low_observation_at_exhaustion_still_lands(make, monkeypatch):
    with bot_lab(make, monkeypatch, exhausted=False) as (lab, tx):
        visible = [bot_thread(severity="P2")]
        tx.api.review_threads = lambda *args: visible
        with pytest.raises(job_land.BotParked):
            tx.run()
        accept_bot_fix(lab)
        # The same thread gains new low content after its accepted fix, at the review limit.
        visible[0]["body"] = "P2: Problem 1, expanded"
        job = lab.get()
        lab.jobs.save(replace(job, reviews_used=job.max_reviews), time.time())
        tx = restart_bot_tx(lab, tx)
        lab.edit(follow_branch=True)
        tx.api.find_review_reply = lambda *args: False
        tx.api.post_review_reply = lambda *args: None
        tx.run()
        job = lab.get()
        assert job.stage == "deliver_pending"
        threads = job.land_state["bot"]["threads"]
        assert job.land_state["bot"]["followup"]["findings"] == (threads["thread-1"]["finding"],)
        assert "pr merge" in lab.writes()


@pytest.mark.parametrize("acknowledged", [False, True])
def test_reply_unknown_acknowledgement_requires_distinct_resume(acknowledged):
    from taskwatch import forge

    tx = job_land.Transaction.__new__(job_land.Transaction)
    tx.state = {"bot": {}, "pr": {"number": 1}}
    tx.repo = object()
    events = []
    posted = []
    visible = []
    tx.current = lambda: SimpleNamespace(events=events)
    tx.freshness = lambda: None
    tx.save = lambda **changes: tx.state.update(changes)

    def post(*args):
        posted.append(args[3])
        if acknowledged:
            visible.append(args[3])
        raise forge.Uncertain("lost acknowledgement")

    tx.api = SimpleNamespace(
        find_review_reply=lambda *args: args[3] in visible,
        post_review_reply=post,
    )
    candidate = {"root_id": 1, "body": "A reviewed reason\n\n<!-- taskwatch:answer thread=1 -->"}
    if acknowledged:
        tx.reply(candidate, "answer")
        tx.reply(candidate, "answer")
        assert len(posted) == 1
    else:
        for _ in range(2):
            with pytest.raises(job_land.LandStop) as stopped:
                tx.reply(candidate, "answer")
            assert stopped.value.reason == "land_interrupted"
        assert len(posted) == 1
        events.append({"type": "resume"})
        with pytest.raises(job_land.LandStop):
            tx.reply(candidate, "answer")
        assert len(posted) == 2

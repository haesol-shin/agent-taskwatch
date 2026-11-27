"""The work folder of a code job: a named branch on a fresh base, and its cleanup.

Every git command runs in the job environment, never prompts, and never uses `--force`.
Worktrees with uncommitted or untracked files or an output that cannot be saved are kept;
other ignored files do not block removal. Unintegrated branches are kept. Nothing here
deletes the job folder, records, reviews or logs.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .jobs import JobConfig, Jobs, JobState, follow_links

FETCH_TIMEOUT = 5.0
LOCAL_TIMEOUT = 30.0
FALLBACK_BRANCH = "main"


class WorkspaceError(Exception):
    """The work folder could not be created. The message is recorded."""


def _git(repo: str | Path, env: Mapping[str, str], *args: str, timeout: float = LOCAL_TIMEOUT):
    argv = ["git", "-C", os.fspath(repo), *args]
    # A new session lets a timeout kill helpers such as ssh, which would hold the pipes open.
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**env, "GIT_TERMINAL_PROMPT": "0"},
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out.strip(), err.strip())


def _reason(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else "git refused without a message"


def _resolve(repo: str | Path, env: Mapping[str, str], rev: str) -> str | None:
    proc = _git(repo, env, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def repository_key(target: str, env: Mapping[str, str]) -> str:
    """The real common Git directory shared by aliases and linked worktrees."""
    proc = _git(target, env, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout:
        raise WorkspaceError(f"cannot identify repository: {_reason(proc.stderr)}")
    return str(Path(proc.stdout).resolve(strict=True))


def default_branch(target: str, env: Mapping[str, str], *, timeout: float = LOCAL_TIMEOUT) -> str:
    proc = _git(target, env, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", timeout=timeout)
    if proc.returncode == 0 and proc.stdout.startswith("origin/"):
        return proc.stdout[len("origin/") :]
    return FALLBACK_BRANCH


def fetch(target: str, env: Mapping[str, str], branch: str) -> str | None:
    """None when origin's branch is now current; otherwise why it is not."""
    try:
        if _git(target, env, "remote", "get-url", "origin").returncode != 0:
            return "no origin remote"
        proc = _git(target, env, "fetch", "--quiet", "origin", branch, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"fetch timed out after {FETCH_TIMEOUT:g}s"
    except OSError as exc:
        return f"git is unavailable: {exc}"
    return None if proc.returncode == 0 else f"fetch failed: {_reason(proc.stderr)}"


def _base(job: JobState, env: Mapping[str, str]) -> tuple[str, str]:
    """The commit a new work folder starts from, and a note when it may be stale."""
    if job.base:
        sha = _resolve(job.target, env, job.base)
        if sha is None:
            raise WorkspaceError(f"base is not a commit of the target: {job.base}")
        return sha, ""
    branch = default_branch(job.target, env)
    problem = fetch(job.target, env, branch)
    sha = _resolve(job.target, env, f"origin/{branch}")
    if sha is None:
        # No remote ref at all: the local branch, then the checked-out commit, as before.
        sha = _resolve(job.target, env, branch) or _resolve(job.target, env, "HEAD")
        problem = problem or f"origin/{branch} does not exist"
    if sha is None:
        raise WorkspaceError("the target has no commit to start from")
    return sha, f"base may be stale: {problem}" if problem else ""


def verified_predecessor_base(
    target: str, predecessor: JobState, env: Mapping[str, str]
) -> str | None:
    """Resolve an accepted tip, or a landed base after tree and install verification."""
    if not predecessor.land:
        return None
    if predecessor.stage == "land" and not predecessor.land_state.get("S"):
        if repository_key(target, env) != repository_key(predecessor.target, env):
            raise WorkspaceError("base predecessor belongs to a different repository")
        tip = _resolve(target, env, f"refs/heads/{predecessor.branch}")
        reviewed = predecessor.reviews[-1].get("fingerprint") if predecessor.reviews else None
        if tip is None or tip != reviewed:
            raise WorkspaceError("base predecessor tip differs from its accepted review")
        return tip
    state = predecessor.land_state
    sha, tree = state.get("S", ""), state.get("T", "")
    if (
        predecessor.stage not in {"deliver_pending", "done"}
        or state.get("tree_verified") is not True
        or state.get("install", {}).get("status") not in {"succeeded", "disabled"}
        or not isinstance(sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        or not isinstance(tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", tree) is None
    ):
        raise WorkspaceError("base predecessor has not completed verified landing and install")
    if repository_key(target, env) != repository_key(predecessor.target, env):
        raise WorkspaceError("base predecessor belongs to a different repository")
    observed = _git(target, env, "rev-parse", "--verify", f"{sha}^{{tree}}")
    if _resolve(target, env, sha) != sha or observed.returncode or observed.stdout != tree:
        raise WorkspaceError("base predecessor landed commit or tree is unavailable")
    return sha


def landed_commit(target: str, revision: str, env: Mapping[str, str]) -> str:
    """The full sha of a commit that origin's default branch contains, after a fetch."""
    branch = default_branch(target, env)
    if problem := fetch(target, env, branch):
        raise WorkspaceError(f"cannot confirm {revision} on origin/{branch}: {problem}")
    sha = _resolve(target, env, revision)
    if sha is None:
        raise WorkspaceError(f"not a commit of the target: {revision}")
    if _git(target, env, "merge-base", "--is-ancestor", sha, f"origin/{branch}").returncode:
        raise WorkspaceError(f"{revision} is not in origin/{branch}")
    return sha


def _landed_base(target: str, predecessor: JobState, sha: str, env: Mapping[str, str]) -> str:
    """The commit a hand-landed predecessor recorded, when this repository has it."""
    if repository_key(target, env) != repository_key(predecessor.target, env):
        raise WorkspaceError("base predecessor belongs to a different repository")
    if _resolve(target, env, sha) != sha:
        raise WorkspaceError("base predecessor landed commit is unavailable")
    return sha


def prepare(job: JobState, env: Mapping[str, str], jobs: Jobs | None = None) -> dict:
    """Resolve the immutable base before the runner records its worktree creation intent."""
    if job.workspace_state == "prepared":
        return {"base": job.base, "base_note": job.base_note, "workspace_state": "prepared"}
    predecessor = None
    if job.base_predecessor:
        if jobs is None:
            raise WorkspaceError("predecessor base requires the job store")
        records = jobs.load()
        lineage = follow_links(job.base_predecessor, records.get)
        if lineage.job is None:
            raise WorkspaceError("base predecessor no longer exists")
        if lineage.landed:
            predecessor = _landed_base(job.target, lineage.job, lineage.landed, env)
        else:
            predecessor = verified_predecessor_base(job.target, lineage.job, env)
    sha, note = (predecessor, "") if predecessor else _base(job, env)
    prepared = {"base": sha, "base_note": note, "workspace_state": "prepared"}
    # A unit base given as `@<receipt key>` is the job base: both resolve to this one commit.
    if any(unit["base"].startswith("@") for unit in job.public_text):
        prepared["public_text"] = [
            {**unit, "base": sha} if unit["base"].startswith("@") else dict(unit)
            for unit in job.public_text
        ]
    return prepared


def create(
    job: JobState,
    path: Path,
    config: JobConfig,
    env: Mapping[str, str],
    jobs: Jobs | None = None,
) -> dict:
    """Add the worktree on a new branch; return the record fields that describe it."""
    prepared = prepare(job, env, jobs)
    sha, note = prepared["base"], prepared["base_note"]
    branch = f"{config.branch_prefix}{job.id[:8]}"
    proc = _git(job.target, env, "worktree", "add", "-b", branch, os.fspath(path), sha)
    if proc.returncode != 0:
        raise WorkspaceError(f"git worktree add failed: {proc.stderr}")
    return {
        "base": sha,
        "base_note": note,
        "branch": branch,
        "workspace_state": "active",
        "branch_state": "kept",
    }


def adopt(job: JobState, path: Path, env: Mapping[str, str]) -> dict:
    """Record a worktree that exists but was never recorded: setup was interrupted before it.

    A detached worktree is from a record that predates this lifecycle and stays unmanaged.
    """
    if job.workspace_state and job.workspace_state != "prepared":
        return {}
    proc = _git(path, env, "branch", "--show-current")
    if proc.returncode != 0 or not proc.stdout:
        return {}
    return {"branch": proc.stdout, "workspace_state": "active", "branch_state": "kept"}


# --- cleanup -------------------------------------------------------------------------------


def settle(job: JobState, env: Mapping[str, str], fetched: set[str]) -> dict:
    """Record changes for a finished job: remove a clean worktree, delete an integrated branch.

    `fetched` holds the targets already fetched in this scan, so a target is fetched once.
    """
    if job.review != "code" or job.review_target is not None:
        return {}
    changes = {}
    state = job.workspace_state
    if state == "active" or state.startswith("kept"):
        state = _remove(job, env)
        if state != job.workspace_state:
            changes["workspace_state"] = state
    if state == "removed" and job.branch and job.branch_state in {"kept", "integrated"}:
        branch_state = _delete_if_integrated(job, env, fetched)
        if branch_state != job.branch_state:
            changes["branch_state"] = branch_state
    return changes


def _remove(job: JobState, env: Mapping[str, str]) -> str:
    path = Path(job.workspace)
    if not path.exists():
        _git(job.target, env, "worktree", "prune")
        return "removed"
    listing = _git(job.target, env, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return f"kept: {_reason(listing.stderr)}"
    if not _listed(listing.stdout, path):
        return "kept: orphan"
    status = _git(path, env, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return f"kept: {_reason(status.stderr)}"
    if status.stdout:
        return "kept: worktree has modified or untracked files"
    problem = _copy_outputs(job, path, env)
    if problem:
        return f"kept: {problem}"
    proc = _git(job.target, env, "worktree", "remove", os.fspath(path))
    if proc.returncode != 0:
        reason = _reason(proc.stderr)
        return "kept: orphan" if "not a working tree" in reason else f"kept: {reason}"
    _git(job.target, env, "worktree", "prune")
    return "removed"


def _listed(listing: str, path: Path) -> bool:
    """Whether git's worktree list holds this path; an orphan folder is not in it."""
    wanted = path.resolve()
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            if Path(line[len("worktree ") :]).resolve() == wanted:
                return True
        except OSError:
            continue
    return False


def _copy_outputs(job: JobState, path: Path, env: Mapping[str, str]) -> str:
    """Save declared output data that cannot be restored from HEAD."""
    if not job.outputs:
        return ""
    root = path.resolve()
    folder = path.parent / "outputs"
    for relative in job.outputs:
        source = root / relative
        try:
            resolved = source.resolve()
            if not resolved.is_relative_to(root):
                return f"declared output {relative} resolves outside the worktree"
            try:
                source.lstat()
            except FileNotFoundError:
                continue  # A declared output never written leaves nothing to lose.
            if not source.is_file():
                return f"could not copy the uncommitted output {relative}: not a regular file"
            entry = source.relative_to(root)
            saved = (
                _head_type(root, entry, env) == "blob"
                if source == resolved
                else _output_in_head(root, entry, env)
            )
            if saved:
                continue
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, target)
        except (OSError, RuntimeError) as exc:
            return f"could not copy the uncommitted output {relative}: {exc}"
    return ""


def _output_in_head(root: Path, relative: Path, env: Mapping[str, str]) -> bool:
    """A fresh checkout needs every relative link on the way to the final file."""
    pending = list(reversed(relative.parts))
    entry = Path()
    while pending:
        part = pending.pop()
        if part == "..":
            if entry == Path() or _head_type(root, entry, env) != "tree":
                return False  # The checkout must have the directory being traversed.
            entry = entry.parent
            continue
        entry /= part
        if (root / entry).is_symlink():
            link = (root / entry).readlink()
            if link.is_absolute() or _head_type(root, entry, env) != "blob":
                return False
            entry = entry.parent
            pending.extend(reversed(link.parts))
    return _head_type(root, entry, env) == "blob"


def _head_type(root: Path, entry: Path, env: Mapping[str, str]) -> str:
    result = _git(root, env, "cat-file", "-t", f"HEAD:{entry.as_posix()}")
    return result.stdout if result.returncode == 0 else ""


def _delete_if_integrated(job: JobState, env: Mapping[str, str], fetched: set[str]) -> str:
    tip = _resolve(job.target, env, f"refs/heads/{job.branch}")
    if tip is None:
        return "deleted"
    branch = default_branch(job.target, env)
    if job.target not in fetched:
        fetched.add(job.target)
        # A failed fetch leaves the old remote ref: a merge seen there is still a merge.
        fetch(job.target, env, branch)
    upstream = _resolve(job.target, env, f"origin/{branch}")
    if upstream is None or not _integrated(job.target, env, tip, upstream):
        return "kept"
    proc = _git(job.target, env, "branch", "-D", job.branch)
    return "deleted" if proc.returncode == 0 else "integrated"


def _integrated(target: str, env: Mapping[str, str], tip: str, upstream: str) -> bool:
    """The branch is in upstream's history, or upstream already holds exactly its changes."""
    if _git(target, env, "merge-base", "--is-ancestor", tip, upstream).returncode == 0:
        return True
    # A squash merge leaves the commits out of history but the content in. Merging the branch
    # onto upstream then changes nothing; any difference or conflict means it is not integrated.
    merged = _git(target, env, "merge-tree", "--write-tree", "--no-messages", upstream, tip)
    if merged.returncode != 0 or not merged.stdout:
        return False
    tree = _git(target, env, "rev-parse", "--verify", "--quiet", f"{upstream}^{{tree}}")
    return tree.returncode == 0 and merged.stdout.splitlines()[0] == tree.stdout

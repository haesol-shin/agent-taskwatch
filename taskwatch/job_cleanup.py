"""Remove rebuildable files of a finished job: a step's private /tmp and review tool caches.

The review text, snapshots, `review.md` and the job records stay; only the named tool folders
under `reviews/*/` go. Worktree cleanup stays in `job_workspace`.
"""

import os
import shutil
from pathlib import Path

# The folder a step sees as /tmp (see `job_scan.Systemd.start`).
PRIVATE_TMP = "private-tmp"
# Tool folders a reviewer may create in its review root. Everything else there is kept.
REVIEW_TOOL_CACHES = frozenset(
    {"uv-cache", "ruff-cache", "venv", ".venv", "pytest-tmp", "node_modules"}
)


def remove_private_tmp(folder: Path) -> None:
    """Delete the step's private /tmp, read-only tool folders included."""
    _remove_tree(folder / PRIVATE_TMP)


def remove_review_tool_caches(folder: Path) -> None:
    """Delete tool caches of every review round; a failure is retried by the next scan."""
    try:
        roots = list((folder / "reviews").iterdir())
    except OSError:
        return
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        for name in REVIEW_TOOL_CACHES:
            _remove_tree(root / name)


def _remove_tree(path: Path) -> None:
    """Delete a cache folder. A tool can leave read-only folders, so each is made writable
    first. What cannot be deleted stays and is retried at the next scan. Symbolic links are
    never followed."""
    if path.is_symlink() or not path.is_dir():
        return
    try:
        os.chmod(path, 0o700)
        for base, dirs, _ in os.walk(path):
            for name in dirs:
                child = Path(base) / name
                if not child.is_symlink():
                    os.chmod(child, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)

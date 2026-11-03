"""The public text of a job: unit state, the snapshot a review read, and the copy handed out.

The text lives in the job folder, never in the work folder, so it is never in a diff. A review
reads a read-only snapshot of it, and `reviewed_copy` hands out that snapshot, not the working
files, so the text that was published is the text that was reviewed.
"""

import difflib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .fingerprint import (
    EVIDENCE_PREFIX,
    basis_fingerprint,
    head_sha,
    range_messages,
    resolve_commit,
    text_fingerprint,
    tree_sha,
    without_evidence,
)
from .jobs import UNIT_FILES, JobState

SNAPSHOT = "text"
EXIT_CHANGED = 2
EXIT_NOT_REVIEWED = 3


def unit_file(folder: Path, unit_id: str, name: str) -> Path:
    return folder / "pr" / unit_id / name


@dataclass(frozen=True, slots=True)
class UnitText:
    id: str
    base: str
    head: str
    tree: str
    title: str
    body: str
    commit: str
    messages: tuple[str, ...]

    def state(self) -> dict:
        fingerprint = text_fingerprint(self.title, self.body, self.messages, self.commit)
        return {
            "base": self.base,
            "head": self.head,
            "tree": self.tree,
            "fingerprint": fingerprint,
            "text_contract": 2,
        }


def collect(
    job: JobState, workspace: Path, folder: Path, env: Mapping[str, str]
) -> dict[str, UnitText]:
    """Read every unit once: refs become shas, so one read gives one consistent state."""
    evidence = [job.texts, *(r.get("texts", {}) for r in (*job.checks, *job.reviews))]
    if any(s.get("text_contract") != 2 for record in evidence for s in record.values()):
        raise ValueError("historical public text cannot acquire text_contract=2; submit a new job")
    if len(job.public_text) > 1:
        raise ValueError("split independent publication units into jobs")
    units = {}
    for unit in job.public_text:
        base = resolve_commit(workspace, unit["base"], env)
        head = resolve_commit(workspace, unit["head"], env)
        if base != resolve_commit(workspace, job.base, env) or head != head_sha(workspace, env):
            raise ValueError("public text must cover the complete job base and result HEAD")
        commits = range_messages(workspace, base, head, env)
        title, body, commit = (
            unit_file(folder, unit["id"], name).read_bytes().decode("utf-8") for name in UNIT_FILES
        )
        if not all(text.strip() for text in (title, body, commit)):
            raise ValueError("title.txt, body.md and commit.txt must be nonblank")
        units[unit["id"]] = UnitText(
            unit["id"],
            base,
            head,
            tree_sha(workspace, head, env),
            title,
            body,
            commit,
            tuple(message for _sha, message in commits),
        )
    return units


def states(units: Mapping[str, UnitText]) -> dict[str, dict]:
    return {unit.id: unit.state() for unit in units.values()}


def texts_changed(
    job: JobState, workspace: Path, folder: Path, env: Mapping[str, str], expected: Mapping
) -> bool:
    """The public text no longer matches the recorded states."""
    if not job.public_text:
        return False
    return states(collect(job, workspace, folder, env)) != expected


# --- snapshot ------------------------------------------------------------------------------


def _files(units: Mapping[str, UnitText]) -> dict[str, str]:
    files = {}
    for unit in units.values():
        files[f"{unit.id}/title.txt"] = unit.title
        files[f"{unit.id}/body.md"] = unit.body
        files[f"{unit.id}/commit.txt"] = unit.commit
        for number, message in enumerate(unit.messages, start=1):
            files[f"{unit.id}/commits/{number:02d}.txt"] = message.rstrip("\n") + "\n"
    return files


def _walk(root: Path, file_mode: int, dir_mode: int) -> None:
    for base, _dirs, names in os.walk(root, topdown=False):
        for name in names:
            os.chmod(Path(base) / name, file_mode)
        os.chmod(base, dir_mode)


def write_snapshot(root: Path, units: Mapping[str, UnitText]) -> str:
    """Write the read-only snapshot and return its digest."""
    if root.exists():
        _walk(root, 0o644, 0o755)
        _remove(root)
    for relative, text in _files(units).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _walk(root, 0o444, 0o555)
    return snapshot_digest(root)


def _remove(root: Path) -> None:
    for base, dirs, names in os.walk(root, topdown=False):
        for name in names:
            (Path(base) / name).unlink()
        for name in dirs:
            (Path(base) / name).rmdir()
    root.rmdir()


def snapshot_digest(root: Path) -> str:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    return basis_fingerprint([(p.relative_to(root).as_posix(), p) for p in files])


def snapshot_diff(old: Path, new: Path) -> str:
    """Unified diff of two snapshots, empty when they are the same."""
    names = sorted(
        {
            path.relative_to(root).as_posix()
            for root in (old, new)
            if root.is_dir()
            for path in root.rglob("*")
            if path.is_file()
        }
    )
    lines: list[str] = []
    for name in names:
        before = _lines(old / name)
        after = _lines(new / name)
        lines += difflib.unified_diff(before, after, f"reviewed/{name}", f"current/{name}", "")
    return "\n".join(lines)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


# --- the copy to publish -------------------------------------------------------------------


def _accepted(job: JobState) -> Mapping | None:
    accepted = [r for r in job.reviews if isinstance(r, Mapping) and r.get("ok")]
    return accepted[-1] if accepted else None


def _unit_id(job: JobState, wanted: str | None) -> str:
    ids = [unit["id"] for unit in job.public_text]
    if not ids:
        raise ValueError("the job has no public_text")
    if wanted is None and len(ids) == 1:
        return ids[0]
    if wanted not in ids:
        raise ValueError(f"name one unit with --pr: {', '.join(ids)}")
    return wanted


def snapshot_messages(unit_dir: Path) -> list[str]:
    """Commit messages of one snapshot unit in commit order.

    The files are numbered 01, 02, ... 100; a string sort would put 100 before 11.
    """
    files = sorted((unit_dir / "commits").glob("*.txt"), key=lambda p: int(p.stem))
    return [p.read_text(encoding="utf-8") for p in files]


def reviewed_copy(
    job: JobState, env: Mapping[str, str], folder: Path, wanted: str | None, out: Path
) -> tuple[int, list[str]]:
    """(exit code, lines to print). 0 wrote the copy, 2 the text changed, 3 no review passed."""
    unit_id = _unit_id(job, wanted)
    record = _accepted(job)
    texts = record.get("texts") if record else None
    if not record or not isinstance(texts, Mapping) or unit_id not in texts:
        return EXIT_NOT_REVIEWED, ["no review of this text has passed yet"]
    snapshot = Path(record.get("snapshot", ""))
    if not snapshot.is_dir() or snapshot_digest(snapshot) != record.get("snapshot_sha256"):
        return EXIT_CHANGED, ["snapshot changed since the review"]
    # The workspace may be gone; step messages and publication bytes come from the snapshot.
    state = texts[unit_id]
    contract = state.get("text_contract")
    if contract not in (None, 2):
        return EXIT_NOT_REVIEWED, ["unsupported public text contract"]
    names = UNIT_FILES if contract == 2 else UNIT_FILES[:2]
    try:
        live = [
            (
                unit_file(folder, unit_id, name).read_bytes().decode("utf-8")
                if contract == 2
                else unit_file(folder, unit_id, name).read_text(encoding="utf-8")
            )
            for name in names
        ]
        messages = snapshot_messages(snapshot / unit_id)
        commit = live[2] if contract == 2 else None
        fingerprint = text_fingerprint(live[0], live[1], messages, commit)
    except (OSError, UnicodeError):
        return EXIT_CHANGED, ["text missing since review"]
    if fingerprint != state["fingerprint"]:
        return EXIT_CHANGED, ["text changed since review"]
    if contract is None and (out / "commit.txt").exists():
        raise ValueError("historical copy needs an output folder without commit.txt")
    evidence = (
        f"{EVIDENCE_PREFIX} round {record['round']} PASS, "
        f"code {state['head'][:7]}, text {state['fingerprint'][:8]}"
    )
    out.mkdir(parents=True, exist_ok=True)
    title = out / "title.txt"
    body = out / "body.md"
    title.write_bytes((snapshot / unit_id / "title.txt").read_bytes())
    reviewed = without_evidence((snapshot / unit_id / "body.md").read_bytes().decode("utf-8"))
    body.write_text(f"{reviewed}\n\n{evidence}\n", encoding="utf-8")
    lines = [str(title), str(body)]
    if contract == 2:
        commit_path = out / "commit.txt"
        commit_path.write_bytes((snapshot / unit_id / "commit.txt").read_bytes())
        lines.append(str(commit_path))
    else:
        lines.append(
            "Historical text: title/body only; no reviewed squash body or landing coverage"
        )
    return 0, lines

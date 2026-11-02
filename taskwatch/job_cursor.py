"""Generation-qualified integer cursors without changing job snapshot records."""

import json
import os
import tempfile
import uuid
from pathlib import Path

from .state import RecordError, _fsync


def generation(path: Path, info: os.stat_result) -> int:
    try:
        rows = json.loads(path.with_suffix(".cursors.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        rows = []
    except (OSError, ValueError) as exc:
        raise RecordError("jobs.jsonl: unreadable cursor generations") from exc
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or set(row) != {"device", "inode", "generation"}
        or any(type(value) is not int or value < 0 for value in row.values())
        for row in rows
    ):
        raise RecordError("jobs.jsonl: invalid cursor generations")
    for row in rows:
        if (row["device"], row["inode"]) == (info.st_dev, info.st_ino):
            return row["generation"]
    # Even identifiers describe ledgers predating the first compaction.
    return (info.st_dev << 65) | (info.st_ino << 1)


def prepare_generation(path: Path, replacement: os.stat_result) -> None:
    """Publish both generations before replacing the ledger, including across a crash.

    An unlocked reader can open either inode during the swap. Keeping both mappings
    prevents it from issuing an ambiguous cursor; the metadata stays bounded.
    """
    current = path.stat()
    rows = [
        {
            "device": current.st_dev,
            "inode": current.st_ino,
            "generation": generation(path, current),
        },
        {
            "device": replacement.st_dev,
            "inode": replacement.st_ino,
            "generation": (uuid.uuid4().int << 1) | 1,
        },
    ]
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix="jobs-cursors-", delete=False
        ) as handle:
            name = handle.name
            json.dump(rows, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path.with_suffix(".cursors.json"))
        _fsync(path.parent)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)

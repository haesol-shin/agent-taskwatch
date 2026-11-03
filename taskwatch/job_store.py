"""Current job snapshots and a recoverable, compact notification stream.

Callers hold State.lock for every Store operation. Read helpers never repair files.
"""

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .state import RecordError, _create_new, _fsync, _make_dirs, _repair_torn, _write_all

_HEX = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TEMP = re.compile(r"state-[0-9a-f]{32}\.tmp")


def _digest(value: dict) -> str:
    from .jobs import _digest as digest

    try:
        return digest(value)
    except (ValueError, TypeError) as exc:
        raise RecordError("invalid non-JSON checksum payload") from exc


def _bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _hex(value) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _positive(value) -> bool:
    return type(value) is int and value > 0


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecordError(f"{path.name}: unreadable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise RecordError(f"{path.name}: expected object")
    return value


def read_index(directory: Path) -> dict | None:
    path = directory / "jobs-store.json"
    if not path.exists():
        return None
    obj = _object(path)
    if (
        set(obj) != {"version", "generation", "receipts"}
        or type(obj["version"]) is not int
        or obj["version"] != 2
        or not _hex(obj["generation"])
        or not isinstance(obj["receipts"], dict)
    ):
        raise RecordError("jobs-store.json: invalid index or unknown version")
    receipts = obj["receipts"]
    if any(not _hex(k) or not isinstance(v, str) or not v for k, v in receipts.items()):
        raise RecordError("jobs-store.json: invalid membership")
    if len(set(receipts.values())) != len(receipts):
        raise RecordError("jobs-store.json: conflicting receipt key")
    return obj


def _job(data: dict):
    from .jobs import decode_job

    try:
        return decode_job(data)
    except (ValueError, TypeError, KeyError) as exc:
        raise RecordError(f"invalid job snapshot: {exc}") from exc


def _snapshot(obj: dict, ident: str) -> dict:
    if (
        set(obj) != {"version", "revision", "job", "sha256"}
        or type(obj["version"]) is not int
        or obj["version"] != 2
        or not _positive(obj["revision"])
        or not isinstance(obj["job"], dict)
    ):
        raise RecordError("state.json: invalid envelope or unknown version")
    if obj["sha256"] != _digest({"revision": obj["revision"], "job": obj["job"]}):
        raise RecordError("state.json: corrupt checksum")
    _job(obj["job"])
    if not _hex(ident) or obj["job"]["id"] != ident:
        raise RecordError("state.json: filename/job id mismatch")
    return obj


def read_snapshot(directory: Path, ident: str, receipt: str) -> dict:
    if not _hex(ident):
        raise RecordError("state.json: invalid job id")
    obj = _snapshot(_object(directory / "jobs" / ident / "state.json"), ident)
    if obj["job"]["receipt_key"] != receipt:
        raise RecordError("state.json: receipt key changed")
    return obj


def projection(job: dict) -> dict:
    events = job.get("events", [])
    return {
        "stage": job["stage"],
        "wait_reason": job["wait_reason"],
        "entry": sum(
            isinstance(e, dict) and e.get("type") in {"wait", "approve", "resume"} for e in events
        ),
        "predecessors": job["predecessors"],
        "last_at": max((e["at"] for e in events if isinstance(e, dict) and "at" in e), default=0.0),
    }


def _projection(value) -> None:
    from .jobs import STAGES

    if not isinstance(value, dict) or set(value) != {
        "stage",
        "wait_reason",
        "entry",
        "predecessors",
        "last_at",
    }:
        raise RecordError("job-changes.jsonl: invalid projection")
    if (
        not isinstance(value["stage"], str)
        or value["stage"] not in STAGES
        or not isinstance(value["wait_reason"], str)
        or type(value["entry"]) is not int
        or value["entry"] < 0
        or not isinstance(value["predecessors"], list)
        or any(not isinstance(p, str) or not p for p in value["predecessors"])
        or type(value["last_at"]) not in {int, float}
        or not math.isfinite(value["last_at"])
    ):
        raise RecordError("job-changes.jsonl: invalid projection fields")


def validate_event(row: dict, generation: str, sequence: int) -> dict:
    if not isinstance(row, dict) or set(row) != {
        "version",
        "generation",
        "sequence",
        "job_id",
        "revision",
        "projection",
        "sha256",
    }:
        raise RecordError("job-changes.jsonl: invalid event")
    if (
        type(row["version"]) is not int
        or row["version"] != 2
        or not _hex(generation)
        or row["generation"] != generation
        or not _positive(row["sequence"])
        or row["sequence"] != sequence
        or not _hex(row["job_id"])
        or not _positive(row["revision"])
        or row["sha256"] != _digest({k: v for k, v in row.items() if k != "sha256"})
    ):
        raise RecordError("job-changes.jsonl: corrupt event or unknown version")
    _projection(row["projection"])
    return row


def _event(generation: str, sequence: int, obj: dict) -> dict:
    row = {
        "version": 2,
        "generation": generation,
        "sequence": sequence,
        "job_id": obj["job"]["id"],
        "revision": obj["revision"],
        "projection": projection(obj["job"]),
    }
    row["sha256"] = _digest(row)
    return validate_event(row, generation, sequence)


def _envelope(data: dict, revision: int) -> dict:
    return {
        "version": 2,
        "revision": revision,
        "job": data,
        "sha256": _digest({"revision": revision, "job": data}),
    }


def _atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    _create_new(temporary, data, 0o600)
    os.replace(temporary, path)
    _fsync(path.parent)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self._signature = None
        self._offset = 0
        self._sequence = 0
        self._last = None
        self._anchor = b""
        self._generation = None

    def prepare(self, now: float) -> dict:
        index = read_index(self.directory)
        if index is None:
            index = self._migrate(now)
        self._recover(index, now)
        self._tail(index["generation"], now)
        return read_index(self.directory)

    def _tail(self, generation: str, now: float) -> tuple[int, dict | None]:
        path = self.directory / "job-changes.jsonl"
        if not path.exists():
            raise RecordError("job-changes.jsonl: missing committed stream")
        _repair_torn(path, datetime.fromtimestamp(now, timezone.utc))
        info = path.stat()
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if signature == self._signature and generation == self._generation:
            return self._sequence, self._last
        reset = (
            self._signature is None
            or generation != self._generation
            or signature[:2] != self._signature[:2]
            or info.st_size <= self._offset
        )
        with path.open("rb") as handle:
            if not reset and self._offset:
                handle.seek(self._offset - len(self._anchor))
                reset = handle.read(len(self._anchor)) != self._anchor
            if reset:
                self._offset, self._sequence, self._last = 0, 0, None
            handle.seek(self._offset)
            try:
                for line in handle:
                    row = validate_event(json.loads(line), generation, self._sequence + 1)
                    self._sequence += 1
                    self._offset += len(line)
                    self._last = row
            except (ValueError, TypeError) as exc:
                self._signature = None
                raise RecordError(f"job-changes.jsonl: invalid committed event: {exc}") from exc
            handle.seek(max(0, self._offset - 128))
            self._anchor = handle.read(min(self._offset, 128))
        self._signature, self._generation = signature, generation
        return self._sequence, self._last

    def save(self, data: dict, now: float) -> None:
        _job(data)
        index = self.prepare(now)
        receipts = index["receipts"]
        ident, receipt = data["id"], data["receipt_key"]
        if ident in receipts and receipts[ident] != receipt:
            raise RecordError("state.json: receipt key changed")
        if any(k != ident and v == receipt for k, v in receipts.items()):
            raise RecordError("jobs-store.json: conflicting receipt key")
        revision = 1
        if ident in receipts:
            revision = read_snapshot(self.directory, ident, receipt)["revision"] + 1
        elif (self.directory / "jobs" / ident / "state.json").exists():
            raise RecordError("state.json: unindexed snapshot")
        sequence, _ = self._tail(index["generation"], now)
        obj = _envelope(data, revision)
        event = _event(index["generation"], sequence + 1, obj)
        folder = self.directory / "jobs" / ident
        _make_dirs(folder)
        name = f"state-{uuid.uuid4().hex}.tmp"
        _create_new(folder / name, _bytes(obj), 0o600)
        pending = {
            "version": 2,
            "generation": index["generation"],
            "job_id": ident,
            "revision": revision,
            "sequence": sequence + 1,
            "sha256": obj["sha256"],
            "temporary": name,
            "event": _bytes(event).decode(),
            "receipt": receipt if ident not in receipts else None,
        }
        _atomic(self.directory / "jobs-pending.json", _bytes(pending))
        self._recover(index, now)

    def _pending(self, index: dict) -> tuple[dict, dict, Path]:
        pending = _object(self.directory / "jobs-pending.json")
        if set(pending) != {
            "version",
            "generation",
            "job_id",
            "revision",
            "sequence",
            "sha256",
            "temporary",
            "event",
            "receipt",
        } or (
            type(pending["version"]) is not int
            or pending["version"] != 2
            or pending["generation"] != index["generation"]
            or not _hex(pending["job_id"])
            or not _positive(pending["revision"])
            or not _positive(pending["sequence"])
            or not isinstance(pending["sha256"], str)
            or not _DIGEST.fullmatch(pending["sha256"])
            or not isinstance(pending["temporary"], str)
            or not _TEMP.fullmatch(pending["temporary"])
            or not isinstance(pending["event"], str)
            or (
                pending["receipt"] is not None
                and (not isinstance(pending["receipt"], str) or not pending["receipt"])
            )
        ):
            raise RecordError("jobs-pending.json: invalid transaction")
        try:
            event = validate_event(
                json.loads(pending["event"]), index["generation"], pending["sequence"]
            )
        except (ValueError, TypeError) as exc:
            raise RecordError("jobs-pending.json: invalid event") from exc
        if (
            _bytes(event).decode() != pending["event"]
            or event["job_id"] != pending["job_id"]
            or event["revision"] != pending["revision"]
        ):
            raise RecordError("jobs-pending.json: inconsistent event")
        folder = self.directory / "jobs" / pending["job_id"]
        return pending, event, folder

    def _recover(self, index: dict, now: float) -> None:
        marker = self.directory / "jobs-pending.json"
        if not marker.exists():
            return
        pending, event, folder = self._pending(index)
        ident = pending["job_id"]
        temporary = folder / pending["temporary"]
        if temporary.is_symlink():
            raise RecordError("jobs-pending.json: temporary snapshot is a symlink")
        target = folder / "state.json"
        receipt = index["receipts"].get(ident)
        creating = pending["receipt"] is not None
        if creating:
            if receipt is not None and receipt != pending["receipt"]:
                raise RecordError("jobs-pending.json: conflicting membership")
            receipt = pending["receipt"]
            if pending["revision"] != 1 or any(
                k != ident and v == receipt for k, v in index["receipts"].items()
            ):
                raise RecordError("jobs-pending.json: conflicting receipt")
        elif receipt is None:
            raise RecordError("jobs-pending.json: missing membership")
        obj = _snapshot(_object(temporary if temporary.exists() else target), ident)
        if (
            obj["revision"] != pending["revision"]
            or obj["sha256"] != pending["sha256"]
            or obj["job"]["receipt_key"] != receipt
            or projection(obj["job"]) != event["projection"]
        ):
            raise RecordError("jobs-pending.json: snapshot mismatch")
        sequence, last = self._tail(index["generation"], now)
        if sequence != pending["sequence"] - 1 and not (
            sequence == pending["sequence"] and last == event
        ):
            raise RecordError("jobs-pending.json: conflicting event sequence")
        if temporary.exists():
            if target.exists():
                old = _snapshot(_object(target), ident)
                if old != obj and (
                    creating
                    or old["revision"] + 1 != obj["revision"]
                    or old["job"]["receipt_key"] != receipt
                ):
                    raise RecordError("jobs-pending.json: conflicting current snapshot")
            elif not creating:
                raise RecordError("jobs-pending.json: missing current snapshot")
            _fsync(temporary)
            os.replace(temporary, target)
        _fsync(target)
        _fsync(folder)
        if creating:
            index = {**index, "receipts": {**index["receipts"], ident: receipt}}
            _atomic(self.directory / "jobs-store.json", _bytes(index))
        path = self.directory / "job-changes.jsonl"
        if sequence < pending["sequence"]:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                _write_all(fd, pending["event"].encode())
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            _fsync(path)
        marker.unlink()
        _fsync(marker.parent)

    def _legacy(self, path: Path, now: float) -> dict:
        from .jobs import _upgrade_record

        latest, receipts = {}, {}
        if not path.exists():
            return latest
        _repair_torn(path, datetime.fromtimestamp(now, timezone.utc))
        try:
            with path.open("rb") as handle:
                for line in handle:
                    row = json.loads(line)
                    if (
                        not isinstance(row, dict)
                        or type(row.get("version")) is not int
                        or row["version"] != 1
                        or not isinstance(row.get("job"), dict)
                        or row.get("sha256") != _digest(row["job"])
                    ):
                        raise RecordError("jobs.jsonl: invalid snapshot or unknown version")
                    data = _upgrade_record(row["job"])
                    job = _job(data)
                    old = latest.get(job.id)
                    if receipts.get(job.receipt_key, job.id) != job.id or (
                        old is not None and old["job"]["receipt_key"] != job.receipt_key
                    ):
                        raise RecordError("jobs.jsonl: conflicting or changed receipt")
                    receipts[job.receipt_key] = job.id
                    latest[job.id] = _envelope(data, 1 if old is None else old["revision"] + 1)
        except (ValueError, TypeError, KeyError) as exc:
            raise RecordError(f"jobs.jsonl: invalid committed snapshot: {exc}") from exc
        return latest

    def _migrate(self, now: float) -> dict:
        legacy = self.directory / "jobs.jsonl"
        latest = self._legacy(legacy, now)
        states = list((self.directory / "jobs").glob("*/state.json"))
        changes = self.directory / "job-changes.jsonl"
        if (
            states or changes.exists() or (self.directory / "jobs-pending.json").exists()
        ) and not legacy.exists():
            raise RecordError("orphaned v2 files without legacy migration source")
        if (self.directory / "jobs-pending.json").exists():
            raise RecordError("orphaned pending transaction before format marker")
        for path in states:
            expected = latest.get(path.parent.name)
            if expected is None or path.read_bytes() != _bytes(expected):
                raise RecordError("conflicting pre-marker migration snapshot")
        generation = uuid.uuid4().hex
        if changes.exists() and changes.stat().st_size:
            try:
                first = json.loads(changes.open("rb").readline())
                generation = first["generation"]
            except (ValueError, KeyError, TypeError) as exc:
                raise RecordError("conflicting pre-marker event stream") from exc
        rows = [_event(generation, n, obj) for n, obj in enumerate(latest.values(), 1)]
        expected_events = b"".join(_bytes(row) for row in rows)
        if changes.exists() and changes.read_bytes() != expected_events:
            raise RecordError("conflicting pre-marker event stream")
        for ident, obj in latest.items():
            folder = self.directory / "jobs" / ident
            _make_dirs(folder)
            path = folder / "state.json"
            if not path.exists():
                _atomic(path, _bytes(obj))
            else:
                _fsync(path)
                _fsync(folder)
        if not changes.exists():
            _atomic(changes, expected_events)
        else:
            _fsync(changes)
            _fsync(changes.parent)
        index = {
            "version": 2,
            "generation": generation,
            "receipts": {ident: obj["job"]["receipt_key"] for ident, obj in latest.items()},
        }
        _atomic(self.directory / "jobs-store.json", _bytes(index))
        return index
